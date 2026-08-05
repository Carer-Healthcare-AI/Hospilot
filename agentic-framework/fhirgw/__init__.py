"""
fhirgw -- Hospilot's in-app FHIR gateway (canonical FHIR R5 / 5.0.0).

Named `fhirgw` (not `fhir`) on purpose: the app runs with `src/` on the import
root (see server.sh), so a package literally named `fhir` would shadow the
`fhir.resources` library and break `from fhir.resources... import ...`.

Layout:
  terminology.py   -- code systems + LOINC/status/class/interpretation maps
  identifiers.py   -- internal id / patient_token -> FHIR identifier system+value
  extensions.py    -- Hospilot extension URLs + build/read helpers
  serialization.py -- fhir.resources model -> FastAPI/Starlette Response
  outcomes.py      -- OperationOutcome builders + FHIRError
  bundle.py        -- searchset Bundle assembly
  security.py      -- /fhir auth dependency
  views.py         -- to_internal projection re-exports (used by the poller)
  mappers/         -- bidirectional internal-dict <-> canonical FHIR
"""

FHIR_VERSION = "5.0.0"  # fhir.resources top-level == R5
