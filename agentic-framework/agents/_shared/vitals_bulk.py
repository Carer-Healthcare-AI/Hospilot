"""Bulk vitals prefetch -- shared by every agent that needs vitals for many patients.

The problem this solves: `/vitals/latest` is per-patient because `patient` is the
only patient-scoping search param the upstream FHIR server advertises (its
CapabilityStatement lists exactly patient, category, code, interpretation,
_count; $lastn, $export, batch Bundles and a CSV `patient` are all absent, and
`subject` is silently ignored). So agents that need N patients wrote a `for` loop
around one call each:

    agents/er/activities.py       -- one call per active ER visit (65 on the
                                     reference dataset, 65 serial calls)
    agents/icu/activities.py      -- one call per admission, hospital-wide
    agents/discharge/activities.py, agents/bed/agent_activities.py,
    rag/sql_engine.py, workflows/graph/patient.py

Fabric's /vitals/latest-bulk does one unfiltered vital-signs search and groups by
subject, so N patients cost ONE call. Measured: 27 patients in 0.24s vs 27 calls
in 3.49s, with byte-identical readings (same id and recorded_at) for every
patient.

Two failure modes are handled here rather than at each call site, because getting
either wrong means analysing a patient as though they had no vitals:

  * TRUNCATION -- the upstream cannot page (it ignores _offset and emits no
    `next` link), so a hospital with more Observations than one response carries
    would get a silent prefix. hasura.get_latest_vitals_bulk reports that and
    re-reads the missing tokens per-patient.
  * OUTAGE -- if the bulk endpoint fails outright we fall back to the original
    per-patient path (bounded concurrency) rather than return an empty map.
"""

import asyncio
import logging

from db.hasura import hasura
from fhirgw import repository as repo
from fhirgw.mappers import observation as obs_map
from fhirgw.mappers._common import ref_id

logger = logging.getLogger("vitals_bulk")

# Ceiling on patients pulled in one sweep. The bulk read is a single call, so this
# is not about call volume -- it bounds the per-patient fallback and the payload
# handed to the LLM. Sized above a large hospital's active census; if it ever
# bites it logs rather than silently analysing a subset.
VITALS_PATIENT_CAP = 500

_FALLBACK_CONCURRENCY = 10


def tokens_from_encounters(encounters) -> list[str]:
    """Deduped patient tokens from FHIR Encounters, order preserved.

    Dedup matters: the same patient can hold several admissions, and callers
    often merge two populations (ICU + non-ICU) that overlap.
    """
    out: list[str] = []
    seen: set[str] = set()
    for enc in encounters:
        tok = ref_id(enc.subject)
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


async def _per_patient(tokens: list[str]) -> dict[str, list]:
    """Original path: one call per token, bounded concurrency."""
    out: dict[str, list] = {}
    sem = asyncio.Semaphore(_FALLBACK_CONCURRENCY)

    async def _one(tok: str) -> None:
        async with sem:
            try:
                out[tok] = await repo.latest_vitals(tok)
            except Exception:  # noqa: BLE001 -- one patient must not fail the sweep
                out[tok] = []

    await asyncio.gather(*[_one(t) for t in tokens])
    return out


async def bulk_vitals_observations(tokens: list[str]) -> dict[str, list]:
    """{token: [FHIR Observation]} for many patients in one Fabric call.

    Returns the same shape repo.latest_vitals gives per patient, so callers keep
    feeding is_critical / vitals_to_internal / the ranking helpers unchanged.
    Tokens with no vitals are simply absent from the map.
    """
    if not tokens:
        return {}
    if len(tokens) > VITALS_PATIENT_CAP:
        logger.warning("vitals sweep capped at %d of %d patients",
                       VITALS_PATIENT_CAP, len(tokens))
        tokens = tokens[:VITALS_PATIENT_CAP]

    try:
        rows = await hasura.get_latest_vitals_bulk(tokens)
    except Exception as exc:  # noqa: BLE001 -- bulk is an optimization, never a hard dep
        logger.warning("bulk vitals unavailable, per-patient fallback: %s", exc)
        return await _per_patient(tokens)

    converted: dict[str, list] = {}
    for tok in tokens:
        row = rows.get(tok)
        if not row:
            continue
        try:
            converted[tok] = obs_map.vitals_to_fhir({**row, "patient_token": tok})
        except Exception as exc:  # noqa: BLE001 -- a bad row must not fail the sweep
            logger.warning("could not map bulk vitals for %s: %s", tok[:8], exc)
    return converted
