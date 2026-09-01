"""Bootstrap for the agentic-framework test area.

The app runs with the `agentic-framework/` directory as its import root
(PYTHONPATH=.), so `import config`, `from workflows... import ...` all resolve
from there. Insert that root here so tests import the same way the app does.
config.py reads a few settings at import time — dummy values are enough, since
no real service is contacted during import.
"""

import os
import sys
from pathlib import Path

AGENTIC_ROOT = Path(__file__).resolve().parents[2] / "agentic-framework"
sys.path.insert(0, str(AGENTIC_ROOT))

os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")
os.environ.setdefault("HASURA_URL", "http://localhost/v1/graphql")
os.environ.setdefault("HASURA_ADMIN_SECRET", "dummy")
