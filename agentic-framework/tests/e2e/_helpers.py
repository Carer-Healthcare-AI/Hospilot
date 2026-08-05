"""Shared building blocks for the live E2E task tests.

`assert_sane_shape` is the generic response contract used by the baseline sweep and
available to per-domain files as a first assertion before their task-specific checks.
"""
import math

import uuid

# A per-run UUID. It must be a valid uuid (some tasks write an audit row keyed by
# session_id into a uuid column) AND unique per run — approval tasks start Temporal
# workflows whose id embeds session_id, so a fixed value collides ("already started")
# on reruns. One value is shared across all tests in a run (module imported once).
SESSION_ID = str(uuid.uuid4())

# Count-like fields must be >= 0. We deliberately do NOT bound percentage/rate fields:
# deltas (change_percent), growth, and over-utilisation legitimately exceed 100 or go
# negative, so a generic 0..100 rule produces false positives. Finiteness + count sign
# are the checks that hold for any valid payload.
_COUNT_HINTS = ("count", "total", "num", "pending", "collected", "available", "occupied")


def _check_number(task_id: str, key: str, value, path: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return
    assert math.isfinite(value), f"[{task_id}] {path} = {value!r} is not a finite number"
    kl = key.lower()
    if any(h in kl for h in _COUNT_HINTS):
        assert value >= 0, f"[{task_id}] count-like field {path} = {value}, expected >= 0"


def _walk(task_id: str, obj, key: str = "", path: str = "result") -> None:
    """Recurse the payload, range-checking numbers by their field name."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            _walk(task_id, v, key=str(k), path=f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _walk(task_id, v, key=key, path=f"{path}[{i}]")
    else:
        _check_number(task_id, key, obj, path)


def announce(capsys, task_id: str, what: str) -> None:
    """Print a one-line banner naming the task and what this test asserts.

    Uses capsys.disabled() so it shows even under pytest's output capture. Every
    per-task test should call this first so a run reads as a plain-English checklist.
    """
    with capsys.disabled():
        print(f"\n▶ {task_id} — {what}")


def assert_sane_shape(task_id: str, result) -> None:
    """The generic contract: non-None dict/list, all numbers finite and in range."""
    assert result is not None, f"[{task_id}] returned None — task produced no output"
    assert isinstance(result, (dict, list)), (
        f"[{task_id}] returned {type(result).__name__}, expected a dict or list payload"
    )
    _walk(task_id, result)
