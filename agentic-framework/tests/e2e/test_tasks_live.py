"""Live E2E sweep: one case per task.

WHAT THIS TESTS
    Every discovered @activity.defn task is invoked against the REAL backends
    (Fabric / Hasura / forecast / Claude) with a throwaway session_id. Because live
    data is non-deterministic, we do NOT assert exact values — we assert the response
    CONTRACT holds for any valid run:

        * the task completes without raising,
        * it returns a dict or list (never None / a bare scalar),
        * every number in the payload is finite (no NaN / Inf),
        * count-like fields are >= 0 and percentage/rate-like fields stay in range.

    That catches the failures L2 actually surfaces: a task crashing on real data, a
    malformed backend payload, a negative/NaN count, or a dropped return value.

SCOPE
    This net only sweeps Tier-A tasks (session_id-only) — the tasks it can invoke
    with no fixture. Tier-B tasks (they take an `inp` dataclass) and the fenced
    live-writers are DESELECTED here (not skipped, so they don't clutter the output);
    they are covered for real in the per-domain test files (test_<domain>.py).
"""
import pytest

from _discovery import FENCED, collect_tasks
from _helpers import SESSION_ID, assert_sane_shape

# Only the session_id-only, non-fenced tasks. Everything else is deselected at
# collection (Tier-B/fenced live in the domain files), so a full run shows no skips.
TASKS = [t for t in collect_tasks() if t.tier == "A" and t.name not in FENCED]


@pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
async def test_task_returns_sane_shape(task, capsys):
    """Invoke `task` live and assert its response contract (see module docstring)."""
    with capsys.disabled():
        print(f"\n▶ {task.id} — "
              f"runs live and returns a dict/list with finite numbers and non-negative counts")

    result = await task.fn(SESSION_ID)
    assert_sane_shape(task.id, result)
