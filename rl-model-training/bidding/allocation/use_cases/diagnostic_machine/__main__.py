"""``python -m allocation.use_cases.diagnostic_machine`` — the diagnostic entry point.

Deliberately not wired into ``allocation/cli.py``. That module is the bed runtime and is
frozen; adding a diagnostic subcommand to it would mean a bed invocation imports diagnostic
code on every run.
"""

from allocation.use_cases.diagnostic_machine.cli import main

raise SystemExit(main())
