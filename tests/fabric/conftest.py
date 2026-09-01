"""Bootstrap for the fabric test area.

Fabric runs with `src/` as its import root, so mirror that here and set the same
safe env defaults fabric/tests/conftest.py uses — settings then resolve without
any real upstream being contacted.
"""

import os
import sys
from pathlib import Path

FABRIC_SRC = Path(__file__).resolve().parents[2] / "fabric" / "src"
sys.path.insert(0, str(FABRIC_SRC))

os.environ.setdefault("EHR_FHIR_BASE_URL", "http://localhost:3001/fhir")
os.environ.setdefault("FINANCIAL_API_BASE_URL", "http://localhost:3001/api/financial")
os.environ.setdefault("FABRIC_API_KEY", "")  # fabric auth disabled in tests
