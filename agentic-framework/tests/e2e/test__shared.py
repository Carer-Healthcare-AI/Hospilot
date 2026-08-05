"""Per-domain live E2E tests — _shared (framework runners).

Owner: TODO   ·   Status: complete

These are framework infrastructure tasks (not clinical agents): the agent catalog,
builtin/dynamic/generated task runners, condition evaluator, and the prefetch cache.
Inputs are framework specs rather than patient data. Run: pytest tests/e2e/test__shared.py -v
"""
import uuid

from agents._shared import (
    builtin_tasks as bt, condition_activities as ca, generic_activities as ga,
    generated_activities as gen, prefetch_activities as pa,
)
from agents._shared.builtin_tasks import BuiltinTaskInput
from agents._shared.condition_activities import EvaluateTaskConditionInput
from agents._shared.generic_activities import GenericAgentInput, DynamicTaskInput
from agents._shared.prefetch_activities import CachePrefetchInput, GetPrefetchInput
from _helpers import SESSION_ID, announce, assert_sane_shape


def _has(r, *keys):
    missing = {k for k in keys} - set(r)
    assert not missing, f"missing expected keys {missing}; got {sorted(r)}"


async def test_fetch_agent_catalog(capsys):
    announce(capsys, "_shared:fetch_agent_catalog", "returns the task catalog (list) for an agent")
    r = await bt.fetch_agent_catalog("lab_agent")
    assert_sane_shape("fetch_agent_catalog", r)
    assert isinstance(r, list) and len(r) > 0


async def test_run_builtin_task(capsys):
    announce(capsys, "_shared:run_builtin_task", "runs a builtin task by id and returns a status")
    r = await bt.run_builtin_task(BuiltinTaskInput("lab_agent", "get_lab_tat_status", SESSION_ID, {}, {}))
    assert_sane_shape("run_builtin_task", r)
    _has(r, "status")


async def test_evaluate_task_condition(capsys):
    announce(capsys, "_shared:evaluate_task_condition", "evaluates a condition expression to a bool")
    r = await ca.evaluate_task_condition(
        EvaluateTaskConditionInput("t1", "sa1", "1 == 1", {}, SESSION_ID))
    assert isinstance(r, bool)
    assert r is True


async def test_run_generic_stub(capsys):
    announce(capsys, "_shared:run_generic_stub", "returns agent_id + status for a generic agent stub")
    r = await ga.run_generic_stub(GenericAgentInput(SESSION_ID, "lab_agent", 0, "Lab"))
    assert_sane_shape("run_generic_stub", r)
    _has(r, "agent_id", "status")


async def test_run_dynamic_task(capsys):
    announce(capsys, "_shared:run_dynamic_task", "generates+runs a dynamic task and returns a status")
    r = await ga.run_dynamic_task(
        DynamicTaskInput("dyn_e2e", "Count available beds", ["count"], "bed_agent", SESSION_ID, {}))
    assert_sane_shape("run_dynamic_task", r)
    _has(r, "status")


async def test_run_generated_task(capsys):
    announce(capsys, "_shared:run_generated_task", "runs a DB-registered generated task (graceful status when absent)")
    r = await gen.run_generated_task("nonexistent_task_e2e", SESSION_ID)
    assert_sane_shape("run_generated_task", r)
    _has(r, "status")


async def test_cache_prefetch_result(capsys):
    announce(capsys, "_shared:cache_prefetch_result", "caches a prefetch result without error")
    tid = f"e2e-{uuid.uuid4().hex[:8]}"
    r = await pa.cache_prefetch_result(CachePrefetchInput(SESSION_ID, tid, {"cached": True}))
    assert r is None or isinstance(r, (dict, list))


async def test_get_prefetch_cache(capsys):
    announce(capsys, "_shared:get_prefetch_cache", "round-trips: a cached result reads back for the same task")
    tid = f"e2e-{uuid.uuid4().hex[:8]}"
    await pa.cache_prefetch_result(CachePrefetchInput(SESSION_ID, tid, {"marker": 42}))
    r = await pa.get_prefetch_cache(GetPrefetchInput(SESSION_ID, tid))
    assert_sane_shape("get_prefetch_cache", r)
    assert r.get("marker") == 42, f"cached value did not round-trip; got {r}"
