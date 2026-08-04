"""HTTP write exit — the HIS pulls queued changes from us.

The default exit, used in change_api and polling mode. Unusual among Fabric's HTTP
routes in that **the hospital calls these, not hospilot's agents**, and on the
hospital's own schedule rather than any agent's critical path.

Under INTEGRATION_MODE=kafka these routes return 409: proposals are pushed by the
sibling writeback/kafka/ instead, and both draining the same queue would double-apply.

See pending_changes.py for the three-step protocol ($pending-changes → $acknowledge
→ $confirm) and the soft-lock timeout that makes it at-least-once.
"""

from writeback.http.pending_changes import router

__all__ = ["router"]
