"""Advisory API -- fired notifications + rule management.

Advisories are produced by the advisory engine (workflows/graph/advisory.py)
evaluating hospilot_app.advisory_rules. Any active user can list/acknowledge
their org's advisories; rule management (thresholds, cadence, enable/pause) is
admin-only. See docs/agentic-framework/ADVISORY_ENGINE.md.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.routes.auth import AuthContext, require_active_user, require_role
from config import settings
from db.hasura import hasura

logger = logging.getLogger("advisories")
router = APIRouter()

_SEVERITIES = ("info", "warning", "critical")
_STATUSES = ("active", "acknowledged", "resolved")


def _org_for(ctx: AuthContext, org_id: str | None = None) -> str | None:
    """Effective tenant for hasura routing: org users are pinned to their own org;
    super_admin may target another via ?org_id= (mirrors schedules.py)."""
    return org_id if ctx.is_super() else ctx.org_id


# ── advisories (fired notifications) ──────────────────────────────────────────

@router.get("/advisories")
async def list_advisories(
    status: str | None = None,
    topic: str | None = None,
    limit: int = 50,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    if status and status not in _STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {_STATUSES}")
    rows = await hasura.list_advisories(
        status=status, topic=topic, limit=max(1, min(limit, 200)),
        org_id=_org_for(ctx, org_id),
    )
    return {"advisories": rows}


@router.post("/advisories/{advisory_id}/ack")
async def acknowledge_advisory(
    advisory_id: str,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    row = await hasura.acknowledge_advisory(
        advisory_id, ctx.user_id, datetime.now(timezone.utc).isoformat(),
        org_id=_org_for(ctx, org_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Advisory not found")
    logger.info("[ok] advisory acknowledged  id=%s  user=%s", advisory_id, ctx.username)
    return row


# ── advisory rules (admin management) ─────────────────────────────────────────

class UpdateAdvisoryRuleRequest(BaseModel):
    """Partial update: any subset of the operator-editable fields. rule_key and
    bookkeeping (next_check_at, last_*_at, fire_count) are not editable -- rule_key
    is the evaluator binding, changing it would orphan the rule."""
    label: str | None = None
    condition_description: str | None = None
    suggested_action: str | None = None
    severity: str | None = None
    topic: str | None = None
    definition: dict | None = None   # declarative rule spec (condition logic + thresholds); DB-driven
    trigger_entities: list[str] | None = None
    check_interval_seconds: int | None = None
    clear_check_interval: bool = False   # explicit: make the rule event-only
    cooldown_seconds: int | None = None
    enabled: bool | None = None


@router.get("/advisory-rules")
async def list_advisory_rules(
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_role("admin")),
):
    rows = await hasura.list_advisory_rules(org_id=_org_for(ctx, org_id))
    return {"rules": rows}


@router.patch("/advisory-rules/{rule_id}")
async def update_advisory_rule(
    rule_id: str,
    body: UpdateAdvisoryRuleRequest,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_role("admin")),
):
    org = _org_for(ctx, org_id)
    row = await hasura.get_advisory_rule(rule_id, org_id=org)
    if not row:
        raise HTTPException(status_code=404, detail="Advisory rule not found")

    if body.severity is not None and body.severity not in _SEVERITIES:
        raise HTTPException(status_code=400, detail=f"severity must be one of {_SEVERITIES}")

    set_fields: dict = {}
    for field in ("label", "condition_description", "suggested_action",
                  "severity", "topic", "definition", "trigger_entities",
                  "cooldown_seconds", "enabled"):
        value = getattr(body, field)
        if value is not None:
            set_fields[field] = value

    if body.clear_check_interval:
        set_fields["check_interval_seconds"] = None
    elif body.check_interval_seconds is not None:
        set_fields["check_interval_seconds"] = max(
            body.check_interval_seconds, settings.advisory_min_check_interval_seconds)

    # The DB CHECK requires at least one trigger mode; validate here for a clean 400.
    merged = {**row, **set_fields}
    if not (merged.get("trigger_entities") or merged.get("check_interval_seconds")):
        raise HTTPException(status_code=400,
                            detail="Rule needs trigger_entities or check_interval_seconds")

    # Re-enabling a clock rule: check it on the next tick, not at a stale next_check_at.
    if body.enabled is True and not row.get("enabled") and merged.get("check_interval_seconds"):
        set_fields["next_check_at"] = datetime.now(timezone.utc).isoformat()

    if not set_fields:
        return row
    updated = await hasura.update_advisory_rule(rule_id, set_fields, org_id=org)
    logger.info("[ok] advisory rule updated  id=%s  fields=%s  user=%s",
                rule_id, list(set_fields), ctx.username)
    return updated
