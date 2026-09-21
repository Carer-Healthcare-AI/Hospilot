"""Bed-allocation command: compatibility entry point for ``python -m allocation``."""

from __future__ import annotations

from allocation.cli import main

raise SystemExit(main())
