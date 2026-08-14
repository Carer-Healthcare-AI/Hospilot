"""Ask Hospilot — text-to-SQL grounded Q&A pipeline."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from init_db import DEFAULT_DB, init_db
from llm import chat

ROOT = Path(__file__).resolve().parent

FORBIDDEN_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|"
    r"PRAGMA|VACUUM|TRUNCATE|GRANT|REVOKE|INTO)\b",
    re.IGNORECASE,
)

SQL_BLOCK = re.compile(r"```(?:sql)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


@dataclass
class AskResult:
    question: str
    answer: str
    can_answer: bool
    sql: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB
    if not path.exists():
        init_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def schema_text(conn: sqlite3.Connection) -> str:
    """Compact schema + sample enums for the LLM."""
    lines: list[str] = []
    tables = conn.execute(
        "SELECT name, type FROM sqlite_master "
        "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
        "ORDER BY type DESC, name"
    ).fetchall()

    for t in tables:
        name, typ = t["name"], t["type"]
        cols = conn.execute(f"PRAGMA table_info('{name}')").fetchall()
        # Views may have empty PRAGMA table_info on some SQLite builds; fall back
        if not cols and typ == "view":
            try:
                sample = conn.execute(f"SELECT * FROM '{name}' LIMIT 0")
                col_names = [d[0] for d in sample.description or []]
                col_sql = ", ".join(col_names)
            except sqlite3.Error:
                col_sql = "(unknown)"
        else:
            col_sql = ", ".join(
                f"{c['name']} {c['type'] or 'ANY'}" + (" PK" if c["pk"] else "")
                for c in cols
            )
        lines.append(f"{typ.upper()} {name}({col_sql})")

    lines.append("")
    lines.append("Notes:")
    lines.append("- Free bed = beds.status = 'free' AND beds.is_active = 1")
    lines.append("- beds.status ∈ {free, occupied, cleaning, maintenance}")
    lines.append("- beds.ward_type ∈ {ICU, General, Maternity, Pediatric, Emergency}")
    lines.append("- staff_roster.shift ∈ {morning, evening, night}")
    lines.append("- 'tonight' / 'night shift tonight' → shift = 'night'")
    lines.append("- Seed roster 'today' date is 2026-08-13 (use this when user says today/tonight)")
    lines.append("- Short-staffed = actual_headcount < required_headcount (see v_staffing_gaps)")
    lines.append("- Prefer views v_bed_availability, v_staffing_gaps, v_current_inpatients when useful")
    return "\n".join(lines)


SQL_SYSTEM = """You are a hospital data analyst that writes SQLite queries.

Rules:
1. Output ONLY a single SQLite SELECT (or WITH ... SELECT). No markdown unless wrapping SQL in ```sql.
2. If the schema cannot answer the question, output exactly: CANNOT_ANSWER: <short reason>
3. Never invent tables/columns. Use only the schema provided.
4. Read-only: SELECT / WITH only. No writes, DDL, or PRAGMA.
5. For "free" / "available" beds: status = 'free' AND is_active = 1. Do NOT count cleaning or maintenance as free.
6. For "tonight" / "this night": shift = 'night' and roster_date = '2026-08-13' unless the user names another date.
7. Prefer exact aggregates and joins; keep queries simple and correct.
8. Limit large row listings to 50 rows unless the user asks for everything.
"""

ANSWER_SYSTEM = """You answer hospital staff questions using ONLY the SQL result rows provided.

Rules:
1. Be concise and factual. Lead with the direct answer, then a brief explanation.
2. Cite the numbers from the rows. Do not invent facts not present in the rows/SQL.
3. If rows are empty, say so clearly (e.g. none found) — do not guess.
4. Mention how you interpreted the question when relevant (e.g. free = status free, tonight = night shift on 2026-08-13).
5. Do not mention being an AI. Do not suggest querying other systems.
"""


def extract_sql(text: str) -> str | None:
    text = text.strip()
    if text.upper().startswith("CANNOT_ANSWER"):
        return None
    m = SQL_BLOCK.search(text)
    if m:
        return m.group(1).strip().rstrip(";")
    # Raw SQL
    if re.match(r"(?is)^\s*(with|select)\b", text):
        return text.strip().rstrip(";")
    # Sometimes model prefixes with "SQL:"
    m2 = re.search(r"(?is)\b(with|select)\b.*", text)
    if m2:
        return m2.group(0).strip().rstrip(";")
    return None


def is_cannot_answer(text: str) -> tuple[bool, str]:
    t = text.strip()
    if t.upper().startswith("CANNOT_ANSWER"):
        reason = t.split(":", 1)[1].strip() if ":" in t else "Schema cannot support this question."
        return True, reason
    return False, ""


def validate_sql(sql: str) -> str | None:
    """Return error message if SQL is unsafe / invalid shape."""
    if not sql or not sql.strip():
        return "Empty SQL"
    cleaned = sql.strip().rstrip(";")
    if FORBIDDEN_SQL.search(cleaned):
        return "Only read-only SELECT queries are allowed"
    if not re.match(r"(?is)^\s*(with|select)\b", cleaned):
        return "Query must start with SELECT or WITH"
    if ";" in cleaned:
        return "Multiple statements are not allowed"
    return None


def run_sql(conn: sqlite3.Connection, sql: str) -> tuple[list[str], list[dict[str, Any]]]:
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    return cols, rows


def generate_sql(question: str, schema: str, error_feedback: str | None = None) -> str:
    user = f"Schema:\n{schema}\n\nQuestion: {question}\n"
    if error_feedback:
        user += (
            f"\nThe previous SQL failed or was rejected:\n{error_feedback}\n"
            "Fix it or output CANNOT_ANSWER if truly impossible.\n"
        )
    return chat(SQL_SYSTEM, user, temperature=0.0)


def synthesize_answer(question: str, sql: str, columns: list[str], rows: list[dict[str, Any]]) -> str:
    payload = {
        "question": question,
        "sql": sql,
        "columns": columns,
        "rows": rows[:50],
        "row_count": len(rows),
    }
    return chat(ANSWER_SYSTEM, json.dumps(payload, default=str), temperature=0.1)


def ask(question: str, db_path: Path | None = None, max_retries: int = 2) -> AskResult:
    question = (question or "").strip()
    if not question:
        return AskResult(
            question=question,
            answer="Please ask a question about hospital data.",
            can_answer=False,
            error="empty_question",
        )

    conn = connect(db_path)
    attempts: list[dict[str, Any]] = []
    try:
        schema = schema_text(conn)
        feedback: str | None = None
        sql: str | None = None
        columns: list[str] = []
        rows: list[dict[str, Any]] = []

        for attempt in range(max_retries + 1):
            raw = generate_sql(question, schema, feedback)
            cannot, reason = is_cannot_answer(raw)
            if cannot:
                attempts.append({"raw": raw, "status": "cannot_answer"})
                return AskResult(
                    question=question,
                    answer=(
                        f"I can't answer that from the available hospital data. {reason}"
                    ).strip(),
                    can_answer=False,
                    attempts=attempts,
                )

            sql = extract_sql(raw)
            attempts.append({"raw": raw, "sql": sql})
            if not sql:
                feedback = f"Could not parse SQL from model output: {raw[:500]}"
                continue

            err = validate_sql(sql)
            if err:
                feedback = f"{err}. SQL was: {sql}"
                continue

            try:
                columns, rows = run_sql(conn, sql)
            except sqlite3.Error as e:
                feedback = f"SQLite error: {e}. SQL was: {sql}"
                continue

            answer = synthesize_answer(question, sql, columns, rows)
            return AskResult(
                question=question,
                answer=answer,
                can_answer=True,
                sql=sql,
                columns=columns,
                rows=rows,
                attempts=attempts,
            )

        return AskResult(
            question=question,
            answer=(
                "I couldn't produce a reliable query for that question after a few tries. "
                "Please rephrase, or ask about beds, admissions, visits, patients, or staff rosters."
            ),
            can_answer=False,
            sql=sql,
            error=feedback,
            attempts=attempts,
        )
    finally:
        conn.close()


def format_cli(result: AskResult) -> str:
    parts = [f"Q: {result.question}", "", f"A: {result.answer}", ""]
    if result.sql:
        parts.append("--- SQL ---")
        parts.append(result.sql)
        parts.append("")
    if result.columns:
        parts.append("--- Rows ---")
        if not result.rows:
            parts.append("(no rows)")
        else:
            # compact table
            cols = result.columns
            widths = [len(c) for c in cols]
            str_rows = []
            for row in result.rows[:30]:
                vals = [str(row.get(c, "")) for c in cols]
                str_rows.append(vals)
                for i, v in enumerate(vals):
                    widths[i] = max(widths[i], len(v))
            header = " | ".join(c.ljust(widths[i]) for i, c in enumerate(cols))
            parts.append(header)
            parts.append("-+-".join("-" * w for w in widths))
            for vals in str_rows:
                parts.append(" | ".join(vals[i].ljust(widths[i]) for i in range(len(cols))))
            if len(result.rows) > 30:
                parts.append(f"... ({len(result.rows) - 30} more rows)")
        parts.append("")
    if result.error and not result.can_answer:
        parts.append(f"(debug: {result.error})")
    return "\n".join(parts).rstrip() + "\n"
