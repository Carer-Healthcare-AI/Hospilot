import json
import os
import re
import sqlite3
import urllib.request
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from seed import DB_PATH, seed

seed()

app = FastAPI(title="Ask Hospilot", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

SCHEMA = {
    "beds": [
        "id",
        "branch_id",
        "ward",
        "bed_number",
        "room_type",
        "status",
        "is_active",
        "ventilation",
        "room_sharing",
        "floor",
        "wing",
        "natural_light",
        "noise_level",
        "features",
    ],
    "departments": ["id", "name", "type", "capacity", "target_occupancy_pct"],
    "ipd_admissions": [
        "id",
        "patient_token",
        "bed_id",
        "department_id",
        "admitted_at",
        "expected_discharge_at",
        "status",
        "discharge_ready",
        "discharge_blocked_reason",
        "transfer_pending",
    ],
    "staff_roster": [
        "id",
        "area",
        "area_label",
        "role",
        "shift",
        "headcount",
        "assigned_load",
        "load_per_staff",
        "branch_id",
    ],
    "appointments": [
        "id",
        "patient_id",
        "provider_id",
        "department_id",
        "appointment_time",
        "status",
        "type",
        "patient_name",
        "specialization",
        "department_name",
    ],
    "visits": [
        "id",
        "patient_token",
        "department_id",
        "arrived_at",
        "status",
        "chief_complaint",
        "triage_score",
        "visit_type",
        "appointment_id",
    ],
    "supplies": [
        "id",
        "item_code",
        "item_name",
        "category",
        "current_stock",
        "min_stock",
        "unit",
        "unit_cost",
    ],
    "lab_orders": [
        "id",
        "visit_id",
        "patient_token",
        "ordered_by",
        "status",
        "priority",
        "ordered_at",
        "completed_at",
    ],
    "lab_results": [
        "id",
        "order_id",
        "patient_token",
        "test_name",
        "test_code",
        "result_value",
        "flag",
        "reference_range",
        "unit",
        "reported_at",
    ],
}

TABLE_RE = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)
FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|ATTACH|DETACH|PRAGMA|REPLACE|VACUUM|LOAD_EXTENSION)\b",
    re.I,
)

UNSUPPORTED_KEYWORDS = [
    "satisfaction",
    "rating",
    "survey",
    "nps",
    "feedback score",
    "patient experience",
]


class AskRequest(BaseModel):
    question: str


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def run_sql(sql: str) -> list[dict[str, Any]]:
    sql = sql.strip().rstrip(";")
    if not re.match(r"^(SELECT|WITH)\b", sql, re.I) or FORBIDDEN.search(sql):
        raise ValueError("Only read-only SELECT queries are allowed.")
    tables = {x.lower() for x in TABLE_RE.findall(sql)}
    if not tables or not tables.issubset(SCHEMA):
        raise ValueError("Query references a table outside the approved schema.")
    if re.search(r"\bSELECT\s+\*\b", sql, re.I):
        raise ValueError("SELECT * is not allowed; select only required fields.")
    with db() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def extract_ward(text: str) -> str | None:
    wards = {
        "icu": "ICU",
        "general ward": "General Ward",
        "private": "Private",
        "semi-private": "Semi-Private",
        "semi private": "Semi-Private",
        "emergency": "Emergency",
        "cardiology": "Cardiology",
        "orthopedics": "Orthopedics",
        "pediatrics": "Pediatrics",
    }
    for key, label in wards.items():
        if key in text:
            return label
    return None


def deterministic_plan(question: str) -> tuple[str | None, str | None]:
    q = question.lower().strip()

    if any(k in q for k in UNSUPPORTED_KEYWORDS):
        return None, "unsupported"

    if any(k in q for k in ["icu bed", "icu beds"]) and any(
        k in q for k in ["available", "free", "vacant", "open"]
    ):
        return (
            "SELECT COUNT(*) AS available_icu_beds FROM beds "
            "WHERE LOWER(ward) LIKE '%icu%' AND LOWER(status) = 'available' AND is_active = 1",
            "icu_available",
        )

    if ("bed" in q or "beds" in q) and any(k in q for k in ["how many", "count", "total"]) and "available" in q:
        ward = extract_ward(q)
        if ward:
            return (
                f"SELECT COUNT(*) AS available_beds FROM beds "
                f"WHERE ward = '{ward}' AND LOWER(status) = 'available' AND is_active = 1",
                "ward_available",
            )
        return (
            "SELECT COUNT(*) AS available_beds FROM beds "
            "WHERE LOWER(status) = 'available' AND is_active = 1",
            "all_available",
        )

    if any(k in q for k in ["how are beds", "bed status", "bed statuses", "beds doing"]):
        return (
            "SELECT ward, status, COUNT(*) AS count FROM beds "
            "WHERE is_active = 1 GROUP BY ward, status ORDER BY ward, status",
            "bed_breakdown",
        )

    if any(
        k in q
        for k in [
            "highest bed occupancy",
            "highest occupancy",
            "most occupied wards",
            "ward occupancy",
            "occupancy right now",
        ]
    ):
        return (
            """SELECT b.ward,
                      COUNT(*) AS occupied_beds,
                      (SELECT COUNT(*) FROM beds b2 WHERE b2.ward = b.ward AND b2.is_active = 1) AS total_beds,
                      ROUND(100.0 * COUNT(*) /
                        (SELECT COUNT(*) FROM beds b2 WHERE b2.ward = b.ward AND b2.is_active = 1), 1) AS occupancy_percent
               FROM beds b
               JOIN ipd_admissions a ON b.id = a.bed_id
               WHERE b.is_active = 1 AND LOWER(a.status) != 'discharged'
               GROUP BY b.ward
               ORDER BY occupancy_percent DESC""",
            "occupancy_ranking",
        )

    if "staff" in q or "nurs" in q:
        where = []
        if "icu" in q:
            where.append("LOWER(area)='icu'")
        if "emergency" in q or " er " in f" {q} ":
            where.append("LOWER(area)='emergency'")
        if "night" in q:
            where.append("LOWER(shift)='night'")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        if any(k in q for k in ["short", "understaff", "headcount", "staff available", "staffing"]):
            return (
                f"""SELECT area_label, role, shift, headcount, assigned_load, load_per_staff,
                           ROUND(CAST(assigned_load AS REAL) / NULLIF(headcount, 0), 1) AS load_per_person
                    FROM staff_roster{clause}
                    ORDER BY load_per_person DESC""",
                "staffing",
            )

    if any(k in q for k in ["stock", "inventory", "supplies", "supply"]) and any(
        k in q for k in ["low", "short", "below", "reorder", "running out"]
    ):
        return (
            """SELECT item_name, current_stock, min_stock, unit,
                      ROUND(min_stock - current_stock, 2) AS shortage
               FROM supplies
               WHERE current_stock < min_stock
               ORDER BY shortage DESC""",
            "low_stock",
        )

    if "appointment" in q and any(k in q for k in ["today", "scheduled", "upcoming", "booked"]):
        return (
            """SELECT department_name, specialization, status, COUNT(*) AS appointment_count
               FROM appointments
               WHERE date(appointment_time) = date('now')
               GROUP BY department_name, specialization, status
               ORDER BY appointment_count DESC""",
            "appointments_today",
        )

    if "lab" in q and "pending" in q:
        return (
            """SELECT status, priority, COUNT(*) AS order_count
               FROM lab_orders
               WHERE LOWER(status) IN ('pending', 'ordered', 'in_progress')
               GROUP BY status, priority
               ORDER BY order_count DESC""",
            "pending_labs",
        )

    return None, None


def groq_sql(question: str) -> str | None:
    key = os.getenv("GROQ_API_KEY")
    if not key:
        return None
    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    schema_text = "\n".join(f"- {t}({', '.join(cols)})" for t, cols in SCHEMA.items())
    prompt = f"""You translate a hospital operations question into ONE SQLite SELECT query.
Only use these tables/columns:
{schema_text}
Rules:
- Return JSON only: {{"sql":"..."}}
- Read-only SELECT/WITH only; never invent tables or columns.
- Do not use SELECT *.
- For "right now", use the current stored rows; don't fabricate timestamps.
- Prefer exact equality or case-insensitive matching for ward/status.
- If the schema cannot answer the question, return {{"sql":null}}.
Question: {question}"""
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a strict SQL translator. Never invent data or schema."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
    ).encode()
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            body = json.loads(response.read().decode())
        content = body["choices"][0]["message"]["content"]
        return json.loads(content).get("sql")
    except Exception:
        return None


def answer_text(question: str, rows: list[dict[str, Any]], intent: str | None) -> str:
    if intent == "unsupported":
        return (
            "I don't have access to that metric in the hospital database I can query. "
            "This information is not currently available in the schema."
        )

    if intent == "icu_available":
        count = rows[0]["available_icu_beds"] if rows else 0
        return f"There are **{count} ICU beds** available right now."

    if intent == "ward_available":
        count = rows[0]["available_beds"] if rows else 0
        return f"There are **{count} available beds** in the requested ward."

    if intent == "all_available":
        count = rows[0]["available_beds"] if rows else 0
        return f"There are **{count} active beds** currently marked as available."

    if intent == "bed_breakdown":
        totals: dict[str, int] = {}
        by_ward: dict[str, dict[str, int]] = {}
        for row in rows:
            totals[row["status"]] = totals.get(row["status"], 0) + row["count"]
            by_ward.setdefault(row["ward"], {})[row["status"]] = row["count"]

        lines = [
            "Here's the bed status across all active wards:",
            "",
            "**Summary:**",
            ", ".join(f"**{status}: {count}** beds" for status, count in sorted(totals.items())),
            "",
            "**By Ward:**",
        ]
        for ward, statuses in sorted(by_ward.items()):
            parts = ", ".join(f"{count} {status.lower()}" for status, count in sorted(statuses.items()))
            lines.append(f"- **{ward}:** {parts}")
        return "\n".join(lines)

    if intent == "occupancy_ranking":
        if not rows:
            return "No active bed records were found."
        top = rows[0]
        answer = (
            f"The **{top['ward']}** ward has the highest bed occupancy at "
            f"**{top['occupancy_percent']}%**, with {top['occupied_beds']} out of "
            f"{top['total_beds']} beds currently occupied.\n\n"
            "The ranking of all wards by occupancy is:"
        )
        for i, row in enumerate(rows):
            answer += (
                f"\n{i + 1}. {row['ward']}: {row['occupancy_percent']}% "
                f"({row['occupied_beds']}/{row['total_beds']} beds)"
            )
        return answer

    if intent == "staffing":
        if not rows:
            return "No matching staff-roster records were found."
        return "Matching staffing records:\n" + "\n".join(
            f"- **{r['area_label']} / {r['role']} / {r['shift']}**: "
            f"{r['headcount']} staff, assigned load {r['assigned_load']}, "
            f"load/person {r['load_per_person']}"
            for r in rows
        )

    if intent == "low_stock":
        if not rows:
            return "No supplies are currently below their minimum stock level."
        return "Supplies below minimum stock:\n" + "\n".join(
            f"- **{r['item_name']}**: {r['current_stock']} {r['unit']} (minimum {r['min_stock']})"
            for r in rows
        )

    if intent == "appointments_today":
        if not rows:
            return "No appointments were found for today."
        return "Today's appointment breakdown:\n" + "\n".join(
            f"- {r['department_name']}: {r['appointment_count']}" for r in rows
        )

    if intent == "pending_labs":
        if not rows:
            return "No pending lab orders were found."
        return "Pending lab orders:\n" + "\n".join(
            f"- {r['status']} / {r['priority']}: {r['order_count']}" for r in rows
        )

    return (
        f"I retrieved **{len(rows)} matching row(s)** from the hospital database. "
        "See the generated SQL and retrieved rows below."
    )


@app.get("/")
def home() -> FileResponse:
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


@app.get("/health")
def health() -> dict[str, Any]:
    with db() as conn:
        counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in SCHEMA}
    return {"ok": True, "database": os.path.basename(DB_PATH), "rows": counts}


@app.post("/ask")
def ask(req: AskRequest) -> dict[str, Any]:
    question = req.question.strip()
    if not question or len(question) > 500:
        return {"error": "Please provide a question up to 500 characters."}

    sql, intent = deterministic_plan(question)
    source = "deterministic intent router"

    if intent == "unsupported":
        return {
            "answer": answer_text(question, [], intent),
            "sql": None,
            "rows": [],
            "grounded": False,
            "reason": "Question references data not present in the schema.",
        }

    if not sql:
        sql = groq_sql(question)
        source = "Groq NL-to-SQL"

    if not sql:
        return {
            "answer": (
                "I can't answer that from the hospital database schema I can query. "
                "I don't want to invent an answer."
            ),
            "sql": None,
            "rows": [],
            "grounded": False,
            "reason": "No supported data field or safe query plan was found.",
        }

    try:
        rows = run_sql(sql)
    except Exception as exc:
        return {
            "answer": "I couldn't safely execute a query for that question, so I won't guess.",
            "sql": sql,
            "rows": [],
            "grounded": False,
            "reason": str(exc),
        }

    return {
        "answer": answer_text(question, rows, intent),
        "sql": sql,
        "rows": rows[:100],
        "row_count": len(rows),
        "grounded": True,
        "planner": source,
    }
