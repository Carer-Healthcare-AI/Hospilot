# Hospilot Full-Stack Assessment — SarvaswaTiwari

This folder contains both required assessment parts.

## Part 1 — Widget + Live Plan Viewer

Location: `widget/SarvaswaTiwari/`

### Architecture

```
Browser UI (index.html)
    → POST /api/session (local backend)
        → Hospilot login + create session
    → GET /api/sessions/:id (poll until pipeline ready)
    → iframe + postMessage({ type, token, sessionId })
        → Hospilot dashboard shows the planned pipeline
```

The browser never calls Hospilot directly. Credentials stay server-side in environment variables.

### Local run

```bash
cd widget/SarvaswaTiwari
node server.js
```

Open `http://localhost:3000`.

### Deploy to Vercel

1. Import the `widget/SarvaswaTiwari` folder as a Vercel project.
2. Set environment variables:
   - `HOSPILOT_USERNAME`
   - `HOSPILOT_PASSWORD`
   - `HOSPILOT_BASE_URL=https://hospilot.carer.ai`
   - `CANDIDATE_NAME=SarvaswaTiwari`
3. Deploy. Put the live URL in your PR description.

**Live deployment:** https://hospilot-widget-sujit.vercel.app

Goals are automatically prefixed with `[CANDIDATE-SarvaswaTiwari]`.

---

## Part 2 — Ask Hospilot

Location: `rag/SarvaswaTiwari/`

### Architecture

```
Question
   ↓
Deterministic intent router (beds, occupancy, staffing, supplies, labs)
   ↓ (if no match)
Optional Groq NL-to-SQL fallback
   ↓
SQL safety validator (SELECT only, approved tables, no SELECT *)
   ↓
SQLite
   ↓
Answer + SQL + retrieved rows
```

### Why SQLite

The supplied schema is portable SQL. SQLite keeps the submission local, reproducible, and dependency-light.

### Seed data

`seed.py` loads demo data aligned with the provided `example-qa.md` counts (ICU availability, ward breakdown, occupancy ranking via admissions).

### Run locally

```bash
cd rag/SarvaswaTiwari
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python seed.py
uvicorn app:app --reload --port 8000
```

Open `http://localhost:8000` or `http://localhost:8000/docs`.

### Optional Groq setup

Copy `.env.example` to `.env` and set `GROQ_API_KEY`. Without it, common operational questions still work via the deterministic router.

### Test questions

1. `How many ICU beds are available right now?` → 6
2. `Which wards have the highest bed occupancy right now?`
3. `how are beds doing?`
4. `Which supplies are below minimum stock?`
5. `What is our average patient satisfaction rating this month?` → honest refusal

### Improvements with more time

- SQL AST validator instead of regex checks
- Automated regression tests for example Q&A paraphrases
- Read-only DB user in production
- Structured citations from retrieved rows
