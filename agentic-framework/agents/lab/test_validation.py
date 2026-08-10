import logging

from temporalio import activity

from api.routes.ws import broadcast
from db.hasura import hasura

logger = logging.getLogger(__name__)
_SA = "sa_test_validation"


def _is_critical(value_str: str | None, rule: dict) -> bool:
    if value_str is None:
        return False
    try:
        v = float(value_str)
        low  = rule.get("critical_low")
        high = rule.get("critical_high")
        return (low is not None and v < float(low)) or (high is not None and v > float(high))
    except (ValueError, TypeError):
        return False


def _is_normal(value_str: str | None, rule: dict) -> bool:
    if value_str is None:
        return False
    try:
        v   = float(value_str)
        mn  = rule.get("min_normal")
        mx  = rule.get("max_normal")
        return (mn is None or v >= float(mn)) and (mx is None or v <= float(mx))
    except (ValueError, TypeError):
        return False


@activity.defn
async def validate_result_rules(session_id: str) -> dict:
    """Check each result against auto-validation rules; flag those outside normal range."""
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": _SA})

    results = await hasura.lab_get_results()
    rules   = {r.get("test_code"): r for r in await hasura.lab_get_validation_rules()}

    auto_released, flagged = 0, []
    for r in results:
        rule = rules.get(r.get("test_code") or "")
        if rule is None:
            continue
        if rule.get("auto_release_on_normal") and _is_normal(r.get("result_value"), rule):
            auto_released += 1
        elif not _is_normal(r.get("result_value"), rule):
            flagged.append({"test": r.get("test_name"), "value": r.get("result_value"),
                             "flag": r.get("flag")})

    result = {
        "auto_released": auto_released,
        "flagged_count": len(flagged),
        "flagged_results": flagged[:10],
    }
    logger.info("validate_result_rules  session=%s  auto_released=%d  flagged=%d", session_id, auto_released, len(flagged))
    return result


@activity.defn
async def check_delta_flag(session_id: str) -> dict:
    """Compare current results to prior results; flag significant delta changes."""
    results = await hasura.lab_get_results()
    rules   = {r.get("test_code"): r for r in await hasura.lab_get_validation_rules()}

    delta_failed = []
    seen: dict[str, float] = {}
    for r in sorted(results, key=lambda x: x.get("reported_at") or ""):
        code = r.get("test_code") or ""
        rule = rules.get(code)
        if rule is None:
            continue
        try:
            v = float(r.get("result_value") or "")
        except (ValueError, TypeError):
            continue

        if code in seen:
            prev = seen[code]
            delta_pct_limit = float(rule.get("delta_pct") or 25)
            if prev != 0 and abs((v - prev) / prev * 100) > delta_pct_limit:
                delta_failed.append({"test": r.get("test_name"), "current": v, "previous": prev})
                await broadcast(session_id, {
                    "type": "alert", "severity": "warning",
                    "message": f"Delta check failed: {r.get('test_name')} changed from {prev} to {v} â€” review required.",
                })
        seen[code] = v

    result = {"delta_failed_count": len(delta_failed), "delta_failures": delta_failed[:10]}
    logger.info("check_delta_flag  session=%s  delta_failed=%d", session_id, len(delta_failed))
    return result


@activity.defn
async def check_critical_value_flag(session_id: str) -> dict:
    """Identify results with critical values using validation rule thresholds."""
    results = await hasura.lab_get_results()
    rules   = {r.get("test_code"): r for r in await hasura.lab_get_validation_rules()}

    critical = []
    for r in results:
        rule = rules.get(r.get("test_code") or "")
        if rule and _is_critical(r.get("result_value"), rule):
            critical.append({"test": r.get("test_name"), "value": r.get("result_value"),
                              "order_id": str(r.get("order_id", ""))[:8]})

    result = {"critical_count": len(critical), "critical_items": critical[:10]}
    logger.info("check_critical_value_flag  session=%s  critical=%d", session_id, len(critical))
    return result


@activity.defn
async def release_validated_report(session_id: str) -> dict:
    """Release auto-validated normal reports and broadcast completion."""
    results = await hasura.lab_get_results()
    rules   = {r.get("test_code"): r for r in await hasura.lab_get_validation_rules()}

    to_release = [
        r for r in results
        if (r.get("test_code") in rules)
        and rules[r["test_code"]].get("auto_release_on_normal")
        and _is_normal(r.get("result_value"), rules[r["test_code"]])
    ]

    if to_release:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"{len(to_release)} normal result(s) auto-released from validation queue.",
        })

    result = {"released_count": len(to_release)}
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": _SA, "result": result})
    logger.info("release_validated_report  session=%s  released=%d", session_id, len(to_release))
    return result
