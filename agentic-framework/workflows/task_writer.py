"""
Worker restart signalling -- no longer needed with the dispatcher approach.
run_generated_task loads new functions from DB on demand, no restart required.
"""

import logging

logger = logging.getLogger("task_writer")


def restart_worker() -> bool:
    """No-op -- dispatcher activity handles new tasks without worker restart."""
    print("  WORKER   no restart needed (dispatcher loads tasks from DB on demand)")
    return True
