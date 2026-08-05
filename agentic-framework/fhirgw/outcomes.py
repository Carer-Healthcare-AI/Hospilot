"""
OperationOutcome helpers + FHIRError.

FHIR errors must be returned as an OperationOutcome resource (not FastAPI's
default {"detail": ...}). Routes/dependencies raise FHIRError; an app-level
handler (registered in main.py) renders it via fhir_response.
"""

from fhir.resources.operationoutcome import OperationOutcome

from fhirgw.serialization import fhir_response


def operation_outcome(severity: str, code: str, diagnostics: str) -> OperationOutcome:
    return OperationOutcome(issue=[{
        "severity": severity,     # fatal | error | warning | information
        "code": code,             # e.g. not-found | not-supported | invalid | security | exception
        "diagnostics": diagnostics,
    }])


def outcome_response(severity: str, code: str, diagnostics: str, status_code: int, headers: dict | None = None):
    return fhir_response(operation_outcome(severity, code, diagnostics), status_code, headers)


class FHIRError(Exception):
    """Raised inside /fhir handlers; rendered as an OperationOutcome by main.py."""

    def __init__(self, status_code: int, code: str, diagnostics: str,
                 severity: str = "error", headers: dict | None = None):
        super().__init__(diagnostics)
        self.status_code = status_code
        self.code = code
        self.diagnostics = diagnostics
        self.severity = severity
        self.headers = headers

    def response(self):
        return outcome_response(self.severity, self.code, self.diagnostics,
                                self.status_code, self.headers)


def not_found(diagnostics: str) -> FHIRError:
    return FHIRError(404, "not-found", diagnostics)


def not_supported(diagnostics: str) -> FHIRError:
    return FHIRError(404, "not-supported", diagnostics)


def invalid(diagnostics: str) -> FHIRError:
    return FHIRError(400, "invalid", diagnostics)


def security(diagnostics: str) -> FHIRError:
    return FHIRError(401, "security", diagnostics, headers={"WWW-Authenticate": "Bearer"})
