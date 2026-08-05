"""LangGraph-native orchestration for Hospilot.

Replaces the previous Temporal-workflow + Kafka-dispatch architecture with a
single LangGraph StateGraph per session:

  - the inter-agent pipeline DAG  -> a StateGraph built dynamically per session
  - each agent (was a Temporal workflow) -> one node (graph.agents.*)
  - sub-agent / task gating + LLM planning -> graph.planning (ported verbatim
    from workflows.temporal.workflow._condition_check)
  - OR-logic conditional edges + cascading skips -> graph.conditions routers
  - human-in-the-loop approval (was a Temporal `decide` signal) -> interrupt()
  - durability + cross-request approval resume -> AsyncPostgresSaver checkpointer
"""
