"""LLM selector: given a flow's goal, pick the contested bed RESOURCE — or none.

One cheap classification (fast tier). Returns a resource id from cases.json (e.g. 'icu_bed'),
or None when the flow isn't requesting a bed (inspection / monitoring / forecasting, or any
LLM/parse failure — failing to None is safe: it just means no auction runs). The NL query the
engine needs is synthesized from the resource by the caller, so there are no canned strings.
"""

from __future__ import annotations

import logging
import re

from llm_client import llm_chat

from rl_gateway.queries import resource_options

log = logging.getLogger("rl_gateway.select")

_PROMPT = """You decide whether a hospital workflow is asking for a bed to be allocated, and if so which bed.

WORKFLOW GOAL:
{goal}

CONTEXT (what the agents are doing / found):
{context}

BED RESOURCES:
{options}
0. NONE of the above — the workflow is NOT requesting a bed (it is inspecting, monitoring, forecasting, or doing something unrelated to allocating a bed).

Reply with ONLY the number of the single best-matching resource (1-{n}), or 0 if none apply. Just the number, nothing else."""


def _parse_choice(reply: str) -> int | None:
    m = re.search(r"\d+", reply or "")
    return int(m.group()) if m else None


async def select_bed_resource(goal: str, context: str = "") -> str | None:
    """Return the chosen resource id (e.g. 'icu_bed'), or None if no bed is being requested."""
    resources = resource_options()  # [(resource, unit), ...] in config order
    options = "\n".join(f"{i + 1}. {res} (unit: {unit})" for i, (res, unit) in enumerate(resources))
    try:
        reply = await llm_chat(
            user=_PROMPT.format(goal=(goal or "(none)").strip(),
                                context=(context or "(none)").strip(),
                                options=options, n=len(resources)),
            max_tokens=4, tier="fast",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("bed-resource selection LLM failed (%s) — treating as no bed needed", exc)
        return None

    choice = _parse_choice(reply)
    if choice is None or choice <= 0 or choice > len(resources):
        log.info("bed-resource selection: no bed needed (reply=%r)", (reply or "").strip()[:20])
        return None

    resource = resources[choice - 1][0]
    log.info("bed-resource selection: chose #%d — %s", choice, resource)
    return resource
