"""Deterministic idempotency keys for approval/audit writes.

Same logical action -> same key (so a Temporal activity retry or a LangGraph node
re-run dedups to ONE row); different actions -> different keys (so genuinely
distinct approvals/audits in the same session all survive). The key always
includes the session_id plus the action identity (agent, type, natural id).
"""
import hashlib
import json
from typing import Any


def make_idem_key(*parts: Any) -> str:
    canon = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()
