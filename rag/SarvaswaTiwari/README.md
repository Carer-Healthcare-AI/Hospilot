# Ask Hospilot — Part 2

See `../../README_SarvaswaTiwari.md` for full assessment documentation.

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python seed.py
uvicorn app:app --reload --port 8000
```

Open `http://localhost:8000`.

## API

`POST /ask`

```json
{ "question": "How many ICU beds are available right now?" }
```

Response includes `answer`, `sql`, `rows`, `grounded`, and `planner`.
