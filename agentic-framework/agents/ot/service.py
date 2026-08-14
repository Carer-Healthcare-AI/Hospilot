import logging

from workflows.unified_executor import execute

logger = logging.getLogger("ot")


async def predict_ot_delays(
    room_status: list[dict],
    upcoming_surgeries: list[dict],
    rooms_active: list[dict],
) -> dict:
    result = await execute(
        task_id="exec__predict_ot_delays",
        description=(
            "Identify which OT rooms face on-time start risks. "
            "For each room with an upcoming surgery, check if: room is still active/dirty, "
            "insufficient time to prepare, or equipment not ready. "
            "delay_risks is a list of {room_code, next_surgery, risk ('high'|'medium'), reason, minutes_to_start (int or null)}. "
            "high_risk_count is the count of high-risk rooms."
        ),
        input_schema={
            "room_status": "list of dicts — each has: room_code (str), status (str), last_case_ended (str or null)",
            "upcoming_surgeries": "list of dicts — each has: room_code (str), surgery_name (str), scheduled_start (str), estimated_duration_mins (int)",
            "rooms_active": "list of dicts — currently running surgeries with room_code and start_time",
        },
        output_fields=["delay_risks", "high_risk_count"],
        input_data={
            "room_status": room_status,
            "upcoming_surgeries": upcoming_surgeries[:15],
            "rooms_active": rooms_active,
        },
    )
    logger.info("OT delay prediction  risks=%d  high=%d",
                len(result.get("delay_risks", [])), result.get("high_risk_count", 0))
    return result


async def coordinate_ot_staff(
    delay_risks: list[dict],
    rooms_to_clean: list[dict],
    instrument_gaps: list[dict],
) -> dict:
    result = await execute(
        task_id="exec__coordinate_ot_staff",
        description=(
            "Produce specific OT staff action instructions given delay risks, cleaning needs, and instrument gaps. "
            "staff_actions is a list of {role ('housekeeping'|'scrub_tech'|'nurse'|'anaesthetist'), action (str), room (str)}."
        ),
        input_schema={
            "delay_risks": "list of dicts — {room_code, risk, reason}",
            "rooms_to_clean": "list of dicts — {room_code, priority}",
            "instrument_gaps": "list of dicts — {room_code, missing_item (str)}",
        },
        output_fields=["staff_actions"],
        input_data={
            "delay_risks": delay_risks,
            "rooms_to_clean": rooms_to_clean,
            "instrument_gaps": instrument_gaps,
        },
    )
    logger.info("OT staff coordination  actions=%d", len(result.get("staff_actions", [])))
    return result


async def handle_ot_emergencies(
    emergency_cases: list[dict],
    rooms: list[dict],
) -> dict:
    result = await execute(
        task_id="exec__handle_ot_emergencies",
        description=(
            "Determine immediate action for each emergency/urgent surgical case. "
            "Assign to the most suitable available theatre. "
            "emergency_actions is a list of {surgery_code, surgery_name, action (str), urgency ('immediate'|'urgent'), suggested_room (str or null)}."
        ),
        input_schema={
            "emergency_cases": "list of dicts — {surgery_code, surgery_name, urgency_level, patient_condition}",
            "rooms": "list of dicts — {room_code, status ('available'|'occupied'|'cleaning'), speciality}",
        },
        output_fields=["emergency_actions"],
        input_data={
            "emergency_cases": emergency_cases,
            "rooms": rooms,
        },
    )
    logger.info("OT emergency handling  actions=%d", len(result.get("emergency_actions", [])))
    return result


async def optimise_ot_slots(
    schedule: list[dict],
    rooms: list[dict],
    conflicts: dict,
) -> dict:
    result = await execute(
        task_id="exec__optimise_ot_slots",
        description=(
            "Recommend specific slot swaps or rearrangements to resolve OT scheduling conflicts. "
            "slot_optimizations is a list of {surgery_code, issue (str), recommendation (str)}."
        ),
        input_schema={
            "schedule": "list of dicts — {surgery_code, room_code, scheduled_start, estimated_duration_mins, surgeon}",
            "rooms": "list of dicts — {room_code, status, speciality}",
            "conflicts": "dict — {surgery_code: conflict_description (str)}",
        },
        output_fields=["slot_optimizations"],
        input_data={
            "schedule": schedule[:20],
            "rooms": rooms,
            "conflicts": conflicts,
        },
    )
    logger.info("OT slot optimisation  optimizations=%d", len(result.get("slot_optimizations", [])))
    return result


async def balance_ot_load(
    schedule: list[dict],
    rooms: list[dict],
    utilisation: dict,
) -> dict:
    result = await execute(
        task_id="exec__balance_ot_load",
        description=(
            "Assess OT load balance across theatres and recommend adjustments. "
            "load_balance is {assessment (str), recommendations (list of str)}. "
            "summary is a 2-3 sentence overall OT scheduling assessment."
        ),
        input_schema={
            "schedule": "list of dicts — {surgery_code, room_code, scheduled_start, estimated_duration_mins}",
            "rooms": "list of dicts — {room_code, status, speciality}",
            "utilisation": "dict — {room_code: scheduled_case_count (int)} — booked cases per theatre; higher = busier",
        },
        output_fields=["load_balance", "summary"],
        input_data={
            "schedule": schedule[:20],
            "rooms": rooms,
            "utilisation": utilisation,
        },
    )
    logger.info("OT load balancing complete")
    return result


# OT cases are classified Elective / Non-Elective (the priority column no longer
# carries emergency/urgent). Non-Elective = the case that must not be deferred.
# Legacy emergency/urgent kept so older data still reads as high-acuity.
_NON_ELECTIVE_VALUES = {"non elective", "emergency", "urgent"}
_DISP_RANK = {"proceed": 0, "delay": 1, "escalate": 2}


def is_non_elective(c: dict | None) -> bool:
    """High-acuity / must-not-defer case. Reads priority AND surgery_type,
    normalising 'Non-Elective' -> 'non elective'. Legacy emergency/urgent retained.

    Shared acuity predicate for both per-case disposition (analyze_ot_capacity)
    and emergency detection (find_ot_emergencies) so the two stay in agreement.
    """
    if not c:
        return False
    vals = {(c.get(k) or "").strip().lower().replace("-", " ") for k in ("priority", "surgery_type")}
    return bool(vals & _NON_ELECTIVE_VALUES)


async def analyze_ot_capacity(
    schedule: list[dict],
    rooms: list[dict],
    conflicts: dict,
    emergencies: list[dict],
    resources: dict | None = None,
) -> dict:
    """Deterministic per-case disposition: proceed / delay / escalate.

    Resolution principle (acuity dominates conflict; conflict dominates capacity):
      - non-elective vs non-elective conflict        -> both escalate (human must arbitrate)
      - non-elective vs elective conflict            -> non-elective proceeds, elective delays
      - elective vs elective conflict                -> later-starting one delays, earlier proceeds
      - standalone non-elective with no free theatre -> escalate
      - standalone elective in an over-subscribed room (ta_ot_check_resources) -> delay
      - everything else                              -> proceed

    Acuity is read per-case from `priority`/`surgery_type` (Non-Elective = high).
    `emergencies` is accepted for interface parity but acuity is derived per-case.
    """
    resources = resources or {}

    def _code(c: dict) -> str:
        return c.get("surgery_code") or c.get("id") or ""

    def _start_key(c: dict):
        # None / unparseable sorts last; surgery_code as a stable final tiebreak.
        t = c.get("scheduled_start_time")
        return (t is None, str(t or ""), _code(c))

    by_code         = {_code(c): c for c in schedule if _code(c)}
    free_theatres   = sum(1 for r in rooms if (r.get("status") or "").lower() == "available")
    under_resourced = {
        e.get("room_code")
        for e in (resources.get("under_resourced") or [])
        if e.get("room_code")
    }

    # surgery_code -> [(rival_code, "room"|"surgeon")]
    partners: dict[str, list[tuple]] = {}
    for kind, key in (("room", "room_conflicts"), ("surgeon", "surgeon_conflicts")):
        for cf in (conflicts.get(key) or []):
            a, b = cf.get("surgery_a"), cf.get("surgery_b")
            if a and b:
                partners.setdefault(a, []).append((b, kind))
                partners.setdefault(b, []).append((a, kind))

    recommendations = []
    for case in schedule:
        code, high = _code(case), is_non_elective(case)
        candidates: list[tuple[str, str]] = []   # (disposition, reason)

        for rival_code, kind in partners.get(code, []):
            rival      = by_code.get(rival_code)
            rival_high = is_non_elective(rival)
            if high and rival_high:
                candidates.append(("escalate", f"{kind} conflict with non-elective {rival_code} -- needs coordinator arbitration"))
            elif high and not rival_high:
                candidates.append(("proceed", f"non-elective takes priority over elective {rival_code} in {kind} conflict"))
            elif not high and rival_high:
                candidates.append(("delay", f"elective yields to non-elective {rival_code} ({kind} conflict) -- reschedule"))
            elif rival is not None and _start_key(case) > _start_key(rival):
                candidates.append(("delay", f"later of two electives sharing a {kind} with {rival_code} -- reschedule"))
            else:
                candidates.append(("proceed", f"earlier case keeps slot over {rival_code} ({kind} conflict)"))

        if high and free_theatres == 0:
            candidates.append(("escalate", "non-elective case but no free theatre available"))
        elif not high and case.get("room_code") in under_resourced:
            candidates.append(("delay", f"room {case.get('room_code')} over-subscribed -- defer elective"))

        # Lowest-priority fallback so a proceed always carries a reason.
        candidates.append(("proceed", "non-elective; theatre available" if high else "no conflict; capacity adequate"))

        # Most severe wins; max() keeps the first reason among equal-severity candidates.
        disp, reason = max(candidates, key=lambda c: _DISP_RANK[c[0]])
        recommendations.append({"surgery_code": code, "recommendation": disp, "reason": reason})

    counts = {"proceed": 0, "delay": 0, "escalate": 0}
    for r in recommendations:
        counts[r["recommendation"]] += 1

    summary = (
        f"{len(recommendations)} case(s): {counts['proceed']} proceed, "
        f"{counts['delay']} delay, {counts['escalate']} escalate; {free_theatres} theatre(s) free."
    )
    logger.info("OT capacity analysis  cases=%d  proceed=%d  delay=%d  escalate=%d",
                len(recommendations), counts["proceed"], counts["delay"], counts["escalate"])
    return {"case_recommendations": recommendations, "summary": summary}
