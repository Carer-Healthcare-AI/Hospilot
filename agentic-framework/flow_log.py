"""Flow execution logger + per-session explanation dump.

Two outputs, both under logs/:

  flow_YYYY-MM-DD.log          — one timing line per event (session/agent/task)
  session_{session_id[:8]}.txt — 2-3 line plain-English explanation + key snippet
                                  for every task function that ran, grouped by agent.
                                  Only written when FLOW_EXPLAIN=true (off by default
                                  because it makes an LLM call per unique function).

Call setup_flow_logging() once at startup (main.py lifespan).
Everything is a no-op until then.
"""

import functools
import inspect
import logging
import os
import time
from typing import Any

_flow_logger = logging.getLogger("hospilot.flow")
_active = False

_LOG_DIR = os.environ.get("FLOW_LOG_DIR", "logs")
_EXPLAIN = os.environ.get("FLOW_EXPLAIN", "false").lower() == "true"

# Per-session dedup: session_id -> set of "agent_id::fn_name" already written
_session_dumped: dict[str, set[str]] = {}
# Per-session last-written agent (for section headers in the dump file)
_session_last_agent: dict[str, str] = {}
# Cross-session explanation cache so the same function is only explained once
_explanation_cache: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def setup_flow_logging() -> None:
    """Wire up the flow file handler. Call once at startup."""
    global _active
    os.makedirs(_LOG_DIR, exist_ok=True)
    today = time.strftime("%Y-%m-%d")
    log_path = os.path.join(_LOG_DIR, f"flow_{today}.log")

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))

    _flow_logger.setLevel(logging.DEBUG)
    _flow_logger.propagate = False  # keep out of stdout
    _flow_logger.addHandler(handler)
    _active = True


# ---------------------------------------------------------------------------
# Explanation helpers
# ---------------------------------------------------------------------------

def _dump_path(session_id: str) -> str:
    return os.path.join(_LOG_DIR, f"session_{session_id[:8]}.txt")


def _unwrap(fn: Any) -> Any:
    while isinstance(fn, functools.partial):
        fn = fn.func
    return fn


async def _call_llm_explain(fn_name: str, source: str) -> str:
    """Ask the fast-tier model for a 2-3 line explanation + one key code snippet."""
    try:
        from llm_client import llm_chat
        text = await llm_chat(
            user=(
                f"You are documenting a task function from a hospital operations system.\n\n"
                f"Function: {fn_name}\n"
                f"Source:\n```python\n{source[:4000]}\n```\n\n"
                f"Write 2-3 plain English sentences explaining what this function does "
                f"(what data it fetches or computes, what it decides or returns). "
                f"Then add exactly ONE key line of code that best represents its core logic.\n\n"
                f"Reply in this exact format — nothing else:\n"
                f"EXPLAIN: <your 2-3 sentences here>\n"
                f"SNIPPET: <single key line of code>"
            ),
            max_tokens=200,
            tier="fast",
        )
        text = text.strip()
        explain = ""
        snippet = ""
        for line in text.splitlines():
            if line.startswith("EXPLAIN:"):
                explain = line[len("EXPLAIN:"):].strip()
            elif line.startswith("SNIPPET:"):
                snippet = line[len("SNIPPET:"):].strip()
        if explain:
            return f"{explain}\n\n    {snippet}" if snippet else explain
        return text[:300]
    except Exception:
        return "(explanation unavailable)"


def _init_session_dump(session_id: str, goal: str, agents: list[str]) -> None:
    """Create the session dump file with its header. Only when FLOW_EXPLAIN=true."""
    if not _EXPLAIN:
        return
    agent_list = ", ".join(agents[:15]) + ("…" if len(agents) > 15 else "")
    header = (
        f"{'=' * 68}\n"
        f"SESSION:  {session_id}\n"
        f"DATE:     {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"GOAL:     {goal}\n"
        f"AGENTS:   {agent_list}\n"
        f"{'=' * 68}\n"
    )
    with open(_dump_path(session_id), "w", encoding="utf-8") as f:
        f.write(header)
    _session_dumped[session_id] = set()
    _session_last_agent[session_id] = ""


async def dump_task_source(session_id: str, agent_id: str, fn: Any) -> None:
    """Append a 2-3 line explanation + snippet to the session dump file.

    No-op when FLOW_EXPLAIN is not true.
    Uses an in-process cache so the same function is only explained once
    across all sessions (avoids repeated LLM calls for common activities).
    """
    if not _active or not _EXPLAIN:
        return
    dumped = _session_dumped.get(session_id)
    if dumped is None:
        return

    fn = _unwrap(fn)
    fn_name = getattr(fn, "__name__", repr(fn))
    key = f"{agent_id}::{fn_name}"
    if key in dumped:
        return
    dumped.add(key)

    # --- get or generate explanation ---
    if fn_name not in _explanation_cache:
        try:
            source = inspect.getsource(fn)
        except (OSError, TypeError):
            source = ""
        _explanation_cache[fn_name] = await _call_llm_explain(fn_name, source)
    explanation = _explanation_cache[fn_name]

    # --- agent section header (only when agent changes) ---
    last = _session_last_agent.get(session_id, "")
    agent_section = ""
    if agent_id != last:
        _session_last_agent[session_id] = agent_id
        agent_section = f"\n\n{'─' * 68}\nAGENT: {agent_id}\n{'─' * 68}\n"

    divider = "·" * min(len(fn_name) + 4, 48)
    entry = (
        f"{agent_section}\n"
        f"  [{time.strftime('%H:%M:%S')}]  {fn_name}\n"
        f"  {divider}\n"
        f"  {explanation}\n"
    )

    with open(_dump_path(session_id), "a", encoding="utf-8") as f:
        f.write(entry)


# ---------------------------------------------------------------------------
# Timing log helpers  (one line per event, no value dumps)
# ---------------------------------------------------------------------------

def _ts() -> str:
    return time.strftime("%H:%M:%S")


def log_session_start(session_id: str, goal: str, agents: list[str]) -> None:
    if not _active:
        return
    agent_list = ", ".join(agents[:12]) + ("…" if len(agents) > 12 else "")
    _flow_logger.info(
        "[%s] SESSION ▶  session=%s  goal=%.80s  agents=[%s]",
        _ts(), session_id[:8], goal, agent_list,
    )
    _init_session_dump(session_id, goal, agents)


def log_session_end(session_id: str, elapsed: float) -> None:
    if not _active:
        return
    _flow_logger.info(
        "[%s] SESSION ✓  session=%s  elapsed=%.1fs",
        _ts(), session_id[:8], elapsed,
    )


def log_agent_start(session_id: str, agent_id: str, label: str, ctx: dict) -> None:
    if not _active:
        return
    _flow_logger.info(
        "[%s] AGENT ▶   session=%s  agent=%s  label=%s",
        _ts(), session_id[:8], agent_id, label,
    )


def log_agent_done(session_id: str, agent_id: str, result: dict) -> None:
    if not _active:
        return
    status = result.get("status", "?") if isinstance(result, dict) else "?"
    _flow_logger.info(
        "[%s] AGENT ✓   session=%s  agent=%s  status=%s",
        _ts(), session_id[:8], agent_id, status,
    )


def log_agent_skip(session_id: str, agent_id: str, reason: str | None) -> None:
    if not _active:
        return
    _flow_logger.info(
        "[%s] AGENT -   session=%s  agent=%s  skipped  reason=%s",
        _ts(), session_id[:8], agent_id, reason or "skipped",
    )


def log_agent_fail(session_id: str, agent_id: str, error: str) -> None:
    if not _active:
        return
    _flow_logger.info(
        "[%s] AGENT ✗   session=%s  agent=%s  FAILED  error=%.200s",
        _ts(), session_id[:8], agent_id, error,
    )


def log_task_start(session_id: str, agent_id: str, task_name: str) -> None:
    if not _active:
        return
    _flow_logger.info(
        "[%s] TASK ▶    session=%s  agent=%s  task=%s",
        _ts(), session_id[:8], agent_id, task_name,
    )


def log_task_done(session_id: str, agent_id: str, task_name: str) -> None:
    if not _active:
        return
    _flow_logger.info(
        "[%s] TASK ✓    session=%s  agent=%s  task=%s",
        _ts(), session_id[:8], agent_id, task_name,
    )
