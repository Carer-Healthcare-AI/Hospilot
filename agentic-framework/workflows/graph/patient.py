"""Patient-identification interrupt -- the flow-scoped sibling of `hitl.py`.

A flow runs patient-independent tasks freely. The FIRST time a patient-specific
task is reached, the body calls `require_patients()`, which pauses the graph
(LangGraph `interrupt()`) and asks the clerk for the incoming patient(s)' MOBILE
number(s). The clerk replies via POST /sessions/{sid}/identify-patient, the graph
resumes, we resolve each patient from the hospital DB (via Fabric), cache the
context, and hand it to the body.

HOW WE RESOLVE: the hospital records the patient + visit + vitals at admission time
(new patient -> new record; returning patient -> new visit). We do NOT store any of
that ourselves. We turn the mobile number into the patient via Fabric's
`/patients/by-mobile` (-> patient_token + demographics + current_visit_id), then read
the latest vitals by token via `/vitals/latest?patient={token}`. The token also
unlocks the patient's existing record (invoices/history) for billing-type tasks.

FLOW-SCOPED via the shared cache: the resolved contexts live under the existing
`session_patient:{sid}` Redis hook (read by the bed activities). Every
patient-dependent agent calls `require_patients()`, but only the FIRST to run
interrupts -- every later agent (and every sibling re-running after a resume) hits
the cache and proceeds. One prompt per flow.

THE IDEMPOTENCY HINGE: `require_patients` needs no separate pending-record guard.
The `session_patient:{sid}` cache IS the record, because we write it ONLY AFTER the
interrupt resolves:
  - first run -> cache miss -> interrupt() raises -> nothing written
  - resume    -> body re-runs -> cache still miss -> interrupt() RETURNS the mobiles
                 -> resolve from the hospital DB + cache + return
  - thereafter-> cache hit -> return, never interrupt again
The single invariant: never write the cache before `await_decision` returns.
"""

import datetime
import logging

from workflows.graph import hitl
from workflows.graph.step_rec import emit_step_recommendation
from cache import redis as cache
from db.hasura import hasura

logger = logging.getLogger(__name__)

_TTL = 86400  # 24h

# Patient-registration pause (sa_patient_registration). Must outlive the registration
# timeout window (reaper) -- the staff create the patient manually, so the flow may stay
# parked for hours. Kept well above settings.patient_registration_timeout_hours.
_REG_TTL = 7 * 86400  # 7 days

# Reference set: task ids that operate on a specific patient (audit point).
PATIENT_TASKS = {
    "ta_triage_patients", "ta_detect_cardiac_arrest", "ta_check_spo2_critical",
    "ta_detect_clinical_protocol", "ta_notify_specialist",
    "ta_rank_icu_requests", "ta_analyze_icu_status", "ta_rank_beds",
}


def _key(session_id: str) -> str:
    return f"session_patient:{session_id}"


def _prompt_flag(session_id: str) -> str:
    return f"session:{session_id}:patient_prompt_sent"


# -- Patient-registration keys (sa_patient_registration) ----------------------
def _reg_key(session_id: str) -> str:
    return f"session:{session_id}:patient_registration"


def _reg_index_key(norm_mobile_: str) -> str:
    """Reverse index mobile -> session_id, so the Kafka data consumer can find the
    paused session when the DB reports the newly-created patient back."""
    return f"patient_registration:mobile:{norm_mobile_}"


def norm_mobile(mobile) -> str:
    """Digits-only form of a mobile number for stable index keys. Fabric also
    normalises numbers on its side; this only has to be self-consistent here."""
    digits = "".join(ch for ch in str(mobile or "") if ch.isdigit())
    return digits or str(mobile or "")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _as_list(raw) -> list[dict]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [p for p in raw if isinstance(p, dict)]
    return []


async def get_cached(session_id: str) -> list[dict]:
    """Resolved patient contexts for the session, or [] if not yet identified."""
    return _as_list(await cache.get(_key(session_id)))


async def primary(session_id: str) -> dict | None:
    """First resolved patient context -- for legacy single-patient consumers."""
    patients = await get_cached(session_id)
    return patients[0] if patients else None


async def require_patients(session_id: str, *, prompt: str | None = None,
                           expected_count: int | None = None) -> list[dict]:
    """Resolve (or pause to ask for) the incoming patient(s) by mobile number.

    Raises GraphInterrupt on the first call of an unidentified session; returns the
    resolved contexts on resume. Subsequent calls return the cache without
    interrupting."""
    cached = await get_cached(session_id)
    if cached:
        return cached  # flow-scoped reuse -- no interrupt

    if not await cache.get(_prompt_flag(session_id)):
        await cache.set(_prompt_flag(session_id), True, ttl=_TTL)
        from api.routes.ws import broadcast
        _prompt = prompt or "Provide the mobile number(s) of the incoming patient(s)."
        await broadcast(session_id, {
            "type": "patient_identification_required",
            "session_id": session_id,
            "prompt": _prompt,
            "expected_count": expected_count,
        })
        # Surface the identity request in the unified mid-flow stream too, but do NOT
        # exclude from synthesis -- this is a prerequisite input step, not an action rec.
        await emit_step_recommendation(
            session_id, agent_id="patient_verification_agent", kind="patient_identification",
            headline=(f"Identify {expected_count} incoming patient(s)" if expected_count
                      else "Identify the incoming patient(s)"),
            actions=["Provide mobile number(s) to identify the incoming patient(s)"],
            rationale=_prompt,
            risk="low",
            extras={"prompt": _prompt, "expected_count": expected_count},
            exclude_from_synthesis=False,
        )
        # Surface this input-waiting pause in the Paused queue (Phase 4). Idempotent per
        # session, resolved on resume below; non-fatal if Hasura is unreachable.
        try:
            await hasura.create_approval_task(
                session_id, agent_id="patient_verification_agent",
                action_type="patient_identification", kind="patient_identification",
                payload={"prompt": _prompt, "expected_count": expected_count},
                idempotency_key=f"patient_id:{session_id}",
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not write patient_identification queue row  session=%s", session_id)

    payload = {"kind": "patient_identification", "session_id": session_id}
    if prompt:
        payload["prompt"] = prompt
    if expected_count is not None:
        payload["expected_count"] = expected_count

    raw = hitl.await_decision(payload)               # raises 1st run; returns on resume
    mobiles = raw.get("mobiles") if isinstance(raw, dict) else raw
    if isinstance(mobiles, str):
        mobiles = [mobiles]
    if not mobiles:
        await cache.delete(_prompt_flag(session_id))  # allow a corrected resend
        return []

    contexts = await resolve_patients(mobiles, session_id)
    await cache.set(_key(session_id), contexts, ttl=_TTL)  # written only AFTER resolve
    try:  # leaves the Paused queue (Phase 4)
        await hasura.resolve_approval_tasks(
            session_id, kind="patient_identification", status="resolved")
    except Exception:  # noqa: BLE001
        logger.exception("could not resolve patient_identification row  session=%s", session_id)
    logger.info("patient identity resolved  session=%s  count=%d  mobiles=%s",
                session_id, len(contexts), mobiles)
    return contexts


async def lookup_token_by_mobile(mobile: str) -> str | None:
    """Resolve a patient_token from a mobile number via Fabric. None if no match."""
    rec = await hasura.get_patient_by_mobile(mobile)
    return rec.get("patient_token") if rec else None


async def resolve_patients(mobiles: list[str], session_id: str = "") -> list[dict]:
    """Build a patient context per mobile from the hospital DB (via Fabric).

    mobile -> patient_token + demographics (`/patients/by-mobile`) -> latest vitals
    (`/vitals/latest?patient={token}`). The token also unlocks the patient's existing
    record (invoices/history) for billing-type tasks. CTAS / chief complaint are not
    in the resolver -- ER triage scores them from the real vitals downstream.

    Shape matches both consumers: the bed activities' `session_patient` read and the
    ER/ICU binding."""
    contexts: list[dict] = []
    for mobile in mobiles:
        rec = await hasura.get_patient_by_mobile(mobile)
        if not rec:
            logger.warning("no patient for mobile=%s  session=%s -- using provisional id",
                           mobile, session_id)
            contexts.append({
                "token": mobile, "patient_token": mobile, "mobile": mobile,
                "known_patient": False, "patient_name": f"Incoming {mobile}",
                "vitals": None, "chief_complaint": None, "triage_score": None,
                "acuity": None, "bed_type_needed": None, "required_bed_type": None,
                "isolation_required": False, "current_unit": None,
                "current_visit_id": None, "status": "incoming",
            })
            continue

        token = rec.get("patient_token")
        vitals = await hasura.get_latest_vitals(token) if token else None
        name = f"{rec.get('first_name', '')} {rec.get('last_name', '')}".strip()
        contexts.append({
            "token": token or mobile,
            "patient_token": token or mobile,
            "mobile": mobile,
            "known_patient": token is not None,
            "patient_name": name or f"Incoming {mobile}",
            "uhid": rec.get("uhid"),
            "current_visit_id": rec.get("current_visit_id"),
            "vitals": vitals,
            "chief_complaint": None,    # not in resolver; triage derives acuity from vitals
            "triage_score": None,
            "acuity": None,
            "bed_type_needed": None,
            "required_bed_type": None,
            "isolation_required": False,
            "current_unit": None,
            "status": "incoming",
        })
    return contexts


# -- sa_patient_registration ---------------------------------------------------
# Register incoming patient(s) that have no DB record yet, then PAUSE the flow until
# the hospital staff create them and Fabric reports the new record(s) back.
#
# Flow: for each unknown patient we POST a registration request to Fabric
# (`hasura.request_patient_registration`), which forwards it to the DB side. Staff
# create the patient MANUALLY; when the DB exposes the new record Fabric publishes a
# `patient` data event, which the Kafka data consumer turns into a graph resume
# (workflows.graph.runner.resume_patient_registration). On resume we re-resolve the
# now-existing patient(s) from Fabric and overwrite their provisional contexts in the
# `session_patient:{sid}` cache so downstream agents see known_patient=True.
#
# THE IDEMPOTENCY HINGE (mirrors hitl / require_patients): the `_reg_key` record IS the
# pending guard. We POST to Fabric + create it ONLY on the first run; the resume re-run
# (the node re-executes from the top after every interrupt) finds the record, skips the
# POST, and just consumes the wake-up signal. The resume VALUE is intentionally ignored
# -- the patient data is always re-read from Fabric (the source of truth), so a Kafka
# success and a reaper timeout are handled identically: whatever Fabric knows is what we
# return. This also makes the step robust to LangGraph's positional interrupt matching
# (require_patients no longer interrupts on the resume run, since identity is cached).

async def register_patients(session_id: str, unknown: list[dict]) -> list[dict]:
    """Request registration of the unknown incoming patient(s) and pause until the DB
    creates them. Raises GraphInterrupt on the first run; on resume returns the full,
    re-resolved session contexts (previously-unknown patients now known where Fabric
    has them, still provisional on timeout)."""
    unknown = [p for p in (unknown or []) if p and p.get("mobile")]
    if not unknown:
        return await get_cached(session_id)
    mobiles = [p["mobile"] for p in unknown]

    pending = await cache.get(_reg_key(session_id))
    if pending is None:
        # FIRST RUN -- send the registration request(s) to Fabric, record the pending
        # guard + per-mobile reverse index, refresh the identity-cache TTL so it outlives
        # the wait, and notify the UI. All done BEFORE the interrupt so it happens once.
        requests: list[dict] = []
        for p in unknown:
            try:
                resp = await hasura.request_patient_registration({
                    "mobile":     p.get("mobile"),
                    "name_hint":  p.get("patient_name"),
                    "session_id": session_id,
                    "source":     "patient_verification_agent",
                })
                requests.append({"mobile": p.get("mobile"),
                                 "request_id": (resp or {}).get("request_id"), "sent": True})
            except Exception as exc:  # noqa: BLE001
                logger.warning("patient registration request failed  mobile=%s  session=%s: %s",
                               p.get("mobile"), session_id, exc)
                requests.append({"mobile": p.get("mobile"), "request_id": None, "sent": False})

        pending = {"mobiles": mobiles, "registered": [], "requests": requests,
                   "created_at": _now_iso()}
        await cache.set(_reg_key(session_id), pending, ttl=_REG_TTL)
        for m in mobiles:
            await cache.set(_reg_index_key(norm_mobile(m)), session_id, ttl=_REG_TTL)
        await cache.expire(_key(session_id), _REG_TTL)  # keep provisional identity alive

        from api.routes.ws import broadcast
        _reg_msg = (f"{len(mobiles)} incoming patient(s) have no record -- a registration "
                    f"request was sent to the hospital staff. The flow is paused until "
                    f"the patient(s) are created.")
        await broadcast(session_id, {
            "type": "patient_registration_required",
            "session_id": session_id,
            "mobiles": mobiles,
            "message": _reg_msg,
        })
        # Mirror into the unified mid-flow stream (no synthesis exclusion -- input step).
        await emit_step_recommendation(
            session_id, agent_id="patient_verification_agent", kind="patient_registration",
            headline=f"Register {len(mobiles)} unknown patient(s)",
            actions=[f"Register patient(s): {', '.join(mobiles)}"],
            rationale=_reg_msg,
            risk="low",
            extras={"mobiles": mobiles},
            exclude_from_synthesis=False,
        )
        # Surface this input-waiting pause in the Paused queue (Phase 4). Idempotent per
        # session, resolved on resume below; non-fatal if Hasura is unreachable.
        try:
            await hasura.create_approval_task(
                session_id, agent_id="patient_verification_agent",
                action_type="patient_registration", kind="patient_registration",
                payload={"mobiles": mobiles, "message": _reg_msg},
                idempotency_key=f"patient_reg:{session_id}",
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not write patient_registration queue row  session=%s", session_id)
        logger.info("patient registration requested  session=%s  mobiles=%s -- pausing flow",
                    session_id, mobiles)

    # PAUSE -- raises GraphInterrupt on the first run; the value returned on resume is a
    # mere wake-up signal and is deliberately discarded (we re-resolve from Fabric below).
    hitl.await_decision({
        "kind": "patient_registration",
        "session_id": session_id,
        "mobiles": mobiles,
    })

    # RESUME -- re-resolve from Fabric (authoritative) and overwrite the provisional
    # contexts. Same path for a Kafka success and a reaper timeout.
    resolved = await resolve_patients(mobiles, session_id)
    await _merge_contexts(session_id, resolved)

    # Clean up the pending guard + reverse indexes.
    await cache.delete(_reg_key(session_id))
    for m in mobiles:
        await cache.delete(_reg_index_key(norm_mobile(m)))
    try:  # leaves the Paused queue (Phase 4)
        await hasura.resolve_approval_tasks(
            session_id, kind="patient_registration", status="resolved")
    except Exception:  # noqa: BLE001
        logger.exception("could not resolve patient_registration row  session=%s", session_id)

    newly_known = sum(1 for c in resolved if c.get("known_patient"))
    logger.info("patient registration resolved  session=%s  registered=%d/%d",
                session_id, newly_known, len(mobiles))
    from api.routes.ws import broadcast
    await broadcast(session_id, {
        "type": "patient_registration_completed",
        "session_id": session_id,
        "registered_count": newly_known,
        "pending_count": len(mobiles) - newly_known,
    })
    return await get_cached(session_id)


async def _merge_contexts(session_id: str, resolved: list[dict]) -> None:
    """Overwrite the cached contexts for the resolved mobiles, preserving the rest and
    the original ordering. Newly-resolved patients not previously present are appended."""
    current = await get_cached(session_id)
    by_mobile = {c.get("mobile"): c for c in resolved}
    merged: list[dict] = []
    seen: set = set()
    for c in current:
        m = c.get("mobile")
        if m in by_mobile:
            merged.append(by_mobile[m]); seen.add(m)
        else:
            merged.append(c)
    for m, c in by_mobile.items():
        if m not in seen:
            merged.append(c)
    await cache.set(_key(session_id), merged, ttl=_REG_TTL)


async def record_registration_and_check(mobile: str) -> str | None:
    """Called by the Kafka data consumer when the DB creates a patient. Marks the
    mobile as registered against any pending session and returns the session_id to
    RESUME once ALL of that session's pending patients exist -- else None.

    Deletes the mobile's reverse index on the way (so a duplicate `patient` event for
    the same number can't double-fire), but leaves the `_reg_key` record in place: the
    paused node needs it as its idempotency guard until it resumes and cleans up."""
    norm = norm_mobile(mobile)
    idx_key = _reg_index_key(norm)
    sid = await cache.get(idx_key)
    if not sid:
        return None
    await cache.delete(idx_key)

    record = await cache.get(_reg_key(sid))
    if not record:
        return None
    registered = {norm_mobile(m) for m in (record.get("registered") or [])}
    registered.add(norm)
    record["registered"] = sorted(registered)
    await cache.set(_reg_key(sid), record, ttl=_REG_TTL)

    needed = {norm_mobile(m) for m in record.get("mobiles", [])}
    if needed.issubset(registered):
        return sid
    logger.info("patient registration progress  session=%s  %d/%d registered",
                sid, len(registered), len(needed))
    return None


async def find_stale_registrations(timeout_hours: int) -> list[tuple[str, list[str]]]:
    """Pending registrations older than the timeout window, for the reaper. Marks each
    returned record `resume_requested` (and persists it) so a slow resume can't make the
    next reaper scan fire the same session twice. Returns [(session_id, mobiles), ...]."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=timeout_hours)
    out: list[tuple[str, list[str]]] = []
    for key in await cache.keys(_reg_key("*")):
        record = await cache.get(key)
        if not record or record.get("resume_requested"):
            continue
        try:
            created = datetime.datetime.fromisoformat(record.get("created_at", ""))
        except (TypeError, ValueError):
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=datetime.timezone.utc)
        if created > cutoff:
            continue
        record["resume_requested"] = True
        await cache.set(key, record, ttl=_REG_TTL)
        # key == session:{sid}:patient_registration ; sid is a uuid (no colons).
        out.append((key.split(":")[1], record.get("mobiles", [])))
    return out
