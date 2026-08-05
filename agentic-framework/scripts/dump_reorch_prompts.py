"""One-off driver: reproduce the EXACT prompts that
POST /api/sessions/{id}/reorchestrate builds, for two payloads:

  1. {"agent_id": "ot_agent"}                                  -> subagent reorchestration
  2. {"agent_id": "ot_agent", "subagent_id": "sa_ot_scheduling"} -> task reorchestration

It replicates reorchestrate_session() from api/sessions.py line-for-line (hardcoded
SUB_AGENTS catalog, goal augmentation, _build_pipeline_context) so the instrumented
_dump_prompt() in services.planner fires with identical inputs.

The current pipeline is loaded from prompts_dump.txt (the plan we built earlier),
exactly as the endpoint would read session["pipeline"] from Hasura.

Run inside the backend container:
    docker compose exec backend python dump_reorch_prompts.py

Appends to src/prompts_dump.txt. Only print()/dump output goes to the file.
"""
import asyncio
import contextlib
import json
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from workflows.planner import (
    select_subagents,
    plan_subagent_tasks,
    PlanSubagentInput,
    SUB_AGENTS,
    build_graph_context,
)

GOAL = (
    "A polytrauma patient arriving via ambulance needs an immediate OT slot — "
    "dispatch the nearest ALS unit, find which theatre is free or soonest available, "
    "check post-surgical ICU bed availability after this operation, rank this case "
    "against any currently scheduled elective cases that may need to be deferred, "
    "and pre-stage the billing record."
)


# Mirrors api/sessions.py:_build_pipeline_context (graph-aware, include_subagents=True).
def _build_pipeline_context(pipeline: dict, exclude_agent_id: str) -> str:
    return build_graph_context(pipeline, exclude_agent_id, include_subagents=True)


def _load_pipeline_from_dump(path: str = "prompts_dump.txt") -> dict:
    """Pull the FINAL ASSEMBLED PIPELINE JSON (last object in the dump file)."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    marker = "FINAL ASSEMBLED PIPELINE (for reference):"
    brace = text.index("{", text.index(marker))
    return json.loads(text[brace:])


async def main() -> None:
    pipeline = _load_pipeline_from_dump()

    # ============================================================ CASE 1
    print("\n\n" + "*" * 100)
    print('*** REORCHESTRATE (API) :: POST /sessions/{id}/reorchestrate  payload={"agent_id":"ot_agent"}')
    print("*** scope = subagents   (reorchestrate the subagents inside ot_agent)")
    print("*" * 100)

    agent_base_id = "ot_agent"
    subagents = SUB_AGENTS.get(agent_base_id)

    current_agent = next((a for a in pipeline.get("agents", []) if a["id"] == agent_base_id), None)
    current_ids = [sa["id"] for sa in (current_agent or {}).get("sub_agents", [])]

    augmented_goal = GOAL  # body.feedback is None -> goal unchanged
    if current_ids:
        augmented_goal += (
            f"\n\nCurrent subagent order for {agent_base_id}: {current_ids}"
            f"\nApply the user feedback to reorder or adjust this list."
        )

    pipeline_context = _build_pipeline_context(pipeline, agent_base_id)
    await select_subagents(agent_base_id, subagents, augmented_goal, pipeline_context)  # returns (ids, subgoals)

    # ============================================================ CASE 2
    print("\n\n" + "*" * 100)
    print('*** REORCHESTRATE (API) :: POST /sessions/{id}/reorchestrate  '
          'payload={"agent_id":"ot_agent","subagent_id":"sa_ot_scheduling"}')
    print("*** scope = tasks   (reorchestrate the tasks inside sa_ot_scheduling)")
    print("*" * 100)

    subagent = next((sa for sa in subagents if sa.id == "sa_ot_scheduling"), None)
    # Endpoint reads the subgoal from the persisted pipeline's sub-agent dict.
    cur_sa = next(
        (sa for sa in (current_agent or {}).get("sub_agents", []) if sa.get("id") == "sa_ot_scheduling"),
        None,
    )
    await plan_subagent_tasks(PlanSubagentInput(
        agent_id=agent_base_id,
        subagent_id="sa_ot_scheduling",
        available_tasks=[{"id": t.id, "label": t.label, "outputs": t.outputs} for t in subagent.tasks],
        goal=GOAL,
        session_id="dump-driver",
        subgoal=(cur_sa or {}).get("subgoal", ""),
    ))


if __name__ == "__main__":
    with open("prompts_dump.txt", "a", encoding="utf-8") as f:
        with contextlib.redirect_stdout(f):
            asyncio.run(main())
    print("Done -> appended to src/prompts_dump.txt", file=sys.stderr)
