# Top-level test tree

Unit tests for the modules that the per-app suites do not reach.

Each area runs as its **own pytest process** on purpose: `agentic-framework` and
`fabric` both define top-level modules named `config`, `db` and `cache`.
Collecting them in a single process would let one app's modules shadow the
other's, and the failure looks like an unrelated import error. Each area has its
own `conftest.py` that puts the right source root on `sys.path`.

```
tests/
  agentic_framework/   -> imports from agentic-framework/  (PYTHONPATH=.)
  fabric/              -> imports from fabric/src/
```

Nothing here contacts a real service. Settings are satisfied with dummy env
values set in each `conftest.py`, so the suite is safe to run anywhere.

## Running

```bash
bash tests/run.sh              # every area, one process each
python -m pytest tests/fabric -q
python -m pytest tests/agentic_framework -q
```

Requires `pytest`, plus each app's own requirements for the area you run.
In CI these are separate jobs — see `.github/workflows/ci.yml`.
