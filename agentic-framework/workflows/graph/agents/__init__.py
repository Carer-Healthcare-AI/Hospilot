"""Agent node bodies -- each agent (formerly a Temporal workflow) is ported to a
plain async body function `run_<agent>_body(session_id, ctx, agent_cfg) -> dict`.

The bodies are near-verbatim ports of temporal.workflow.*_workflow.run: the only
mechanical change is `await workflow.execute_activity(fn, X, timeout=..., retry=...)`
becomes `await execute_activity(fn, X)` (see graph.agents._activity), and the
human-in-the-loop `workflow.wait_condition(decide signal)` becomes
`langgraph.types.interrupt(...)`.
"""
