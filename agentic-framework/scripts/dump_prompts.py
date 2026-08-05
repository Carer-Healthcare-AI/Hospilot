"""One-off driver: run the 3-stage planner for a fixed query and dump every
final prompt (agents+edges, subagents, tasks) to prompts_dump.txt.

Run inside the backend container (has .env + DB access):
    docker compose exec backend python dump_prompts.py

Output lands at src/prompts_dump.txt on the host (src/ is bind-mounted).
Only print() output (the prompt dumps) goes to the file; logging stays on stderr.
"""
import asyncio
import contextlib
import json
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from workflows.planner import (
    generate_agents_and_edges,
    select_pipeline_subagents,
    plan_pipeline_tasks,
)

GOAL = (
    "A polytrauma patient arriving via ambulance needs an immediate OT slot — "
    "dispatch the nearest ALS unit, find which theatre is free or soonest available, "
    "check post-surgical ICU bed availability after this operation, rank this case "
    "against any currently scheduled elective cases that may need to be deferred, "
    "and pre-stage the billing record."
)


async def main() -> None:
    print("=" * 100)
    print("QUERY:")
    print(GOAL)
    print("=" * 100)

    print("\n\n>>>>> STAGE 1: AGENTS + EDGES BUILD <<<<<")
    pipeline = await generate_agents_and_edges(GOAL)

    print("\n\n>>>>> STAGE 2: SUBAGENTS BUILD / REORCHESTRATE SUBAGENTS PER AGENT <<<<<")
    pipeline = await select_pipeline_subagents(pipeline, GOAL)

    print("\n\n>>>>> STAGE 3: TASKS BUILD / REORCHESTRATE TASKS PER SUBAGENT <<<<<")
    pipeline = await plan_pipeline_tasks(pipeline, GOAL)

    print("\n\n" + "=" * 100)
    print("FINAL ASSEMBLED PIPELINE (for reference):")
    print("=" * 100)
    print(json.dumps(pipeline, indent=2, default=str))


if __name__ == "__main__":
    with open("prompts_dump.txt", "w", encoding="utf-8") as f:
        with contextlib.redirect_stdout(f):
            asyncio.run(main())
    print("Done -> src/prompts_dump.txt", file=sys.stderr)
