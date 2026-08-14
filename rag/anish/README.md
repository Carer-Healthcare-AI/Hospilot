# Ask Hospilot — Aniket

Plain-English hospital Q&A over a real SQLite database. Answers are produced by generating SQL, running it, and explaining the returned rows — not by guessing.

> **Note on assessment artifacts:** The emailed `schema.sql` / `example-qa.md` were not present in this workspace. This folder includes a portable schema + seed + example Q&A that cover the same domain (beds, patients, visits, admissions, staff rosters). If you have the emailed files, replace `schema.sql` / `example-qa.md` (and adjust `seed.sql` as needed), then run `python init_db.py --force`.

## How to run locally

### 1. Prerequisites

- Python 3.10+
- A free LLM API key (Groq recommended): https://console.groq.com/keys  
  Or Google AI Studio (Gemini): https://aistudio.google.com/apikey

### 2. Setup

```bash
cd rag/aniket
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS/Linux
# source .venv/bin/activate

pip install -r requirements.txt   # optional; app loads .env without python-dotenv
copy .env.example .env            # Windows
# cp .env.example .env            # macOS/Linux
```

Edit `.env` and set `GROQ_API_KEY=...` (or switch to Gemini — see `.env.example`).

### 3. Build the database

```bash
python init_db.py --force
```

Creates `data/hospital.db` from `schema.sql` + `seed.sql`.

### 4. Ask questions

**CLI (one-shot):**

```bash
python cli.py "How many ICU beds are free right now?"
```

**CLI (interactive):**

```bash
python cli.py --repl
```

**Web UI + JSON API:**

```bash
python server.py
# open http://127.0.0.1:8765
```

`POST /api/ask` with `{"question":"..."}` returns the answer, SQL, and rows.

## Architecture — how a question becomes an answer

```
Question
   │
   ▼
┌──────────────────┐
│ 1. Schema prompt │  tables/views + status enums + date conventions
└────────┬─────────┘
         ▼
┌──────────────────┐
│ 2. Text → SQL   │  LLM emits SELECT / WITH … SELECT
│                  │  or CANNOT_ANSWER if schema lacks the info
└────────┬─────────┘
         ▼
┌──────────────────┐
│ 3. Guardrails    │  allow only read-only SELECT; reject writes/DDL
└────────┬─────────┘
         ▼
┌──────────────────┐
│ 4. Execute SQL   │  SQLite — on error, retry with the error message
└────────┬─────────┘
         ▼
┌──────────────────┐
│ 5. Explain rows  │  LLM writes a short answer from SQL + result rows only
└────────┬─────────┘
         ▼
   Answer + SQL + rows  (shown in CLI and UI)
```

Correctness comes from **step 4**: the number in the answer is whatever the database returned. The LLM is used to write the query and to narrate the result — not to invent counts.

Unanswerable questions (e.g. patient satisfaction) take the `CANNOT_ANSWER` path and never fabricate a metric.

## Schema choices

I kept a small, portable SQLite schema and made a few deliberate tweaks for text-to-SQL reliability:

1. **`beds.ward_type` denormalized** — “free ICU beds” does not require a join, which is a common LLM failure mode.
2. **`staff_roster.required_headcount` / `actual_headcount`** — short-staffing is a direct comparison instead of joining schedules to assignments.
3. **Views** — `v_bed_availability`, `v_staffing_gaps`, `v_current_inpatients` give the model stable shapes for frequent operational questions.

Seed date for “today / tonight” is **2026-08-13**; night shift = `shift = 'night'`. See `example-qa.md` for expected grounded answers.

## Design choices

| Choice | Why |
|--------|-----|
| SQLite | Zero install, matches “portable SQL”, easy to inspect |
| Groq (Llama 3.3 70B) | Fast free tier; good at structured SQL |
| Stdlib HTTP server | No Flask/FastAPI needed for a bare form + JSON API |
| Visible SQL + rows | Requirement 6 — show the mechanism, not only the bubble |
| SQL allowlist | Prevents accidental writes if the model misbehaves |

## What I'd improve with more time

- Schema linking / table selection before SQL generation (better on larger HIS schemas)
- Soft validation: re-ask the model if the answer text disagrees with returned aggregates
- Few-shot examples drawn from `example-qa.md` in the SQL prompt
- Optional Ollama path for fully offline use
- Wire into the Part 1 widget as a “Ask data” action (nice-to-have)

## Project layout

```
rag/aniket/
  schema.sql          # tables + views
  seed.sql            # deterministic hospital snapshot
  example-qa.md       # quality bar + reference answers
  init_db.py          # build data/hospital.db
  ask.py              # text-to-SQL pipeline
  llm.py              # Groq / Gemini client
  cli.py              # command line
  server.py           # UI + /api/ask
  static/index.html   # bare form UI
  requirements.txt
  .env.example
```
