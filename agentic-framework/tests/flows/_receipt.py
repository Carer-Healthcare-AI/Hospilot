"""Run receipts: proof that the live flows were actually run, on this code.

A checkbox in a PR template is unenforceable — anyone can tick it. A receipt is
evidence: the live run writes one, the author pastes it into the PR, and CI
recomputes the fingerprint from the checked-out tree and refuses the receipt if
it doesn't match.

What the fingerprint covers is the point. It hashes the files whose behaviour the
flow run actually proves — the flow catalog, the graph/orchestration layer, and
every agent package. Touch any of those and a stale receipt stops verifying, so
"I ran it last week before I rewrote the bed agent" is caught. Edit a README or a
docstring elsewhere and the receipt still stands, so the gate doesn't nag over
changes it has no opinion about.

This is a good-faith gate, not a security control. It stops the honest mistake —
a stale or forgotten run — and it does not stop someone determined to forge a
receipt. That tradeoff is deliberate: the alternative (running the stack in CI)
needs secrets that fork PRs cannot have.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

RECEIPT_VERSION = 1

# Repo root: .../agentic-framework/tests/flows/_receipt.py -> up 3
AGENTIC_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = AGENTIC_ROOT.parent

# Everything whose change could invalidate a flow run. Paths are relative to
# agentic-framework/.
FINGERPRINT_PATHS = [
    "tests/flows/_flows.py",
    "tests/flows/_driver.py",
    "workflows/graph",
    "workflows/planner.py",
    "workflows/strategies.py",
    "workflows/unified_executor.py",
    "agents",
]

_SKIP_DIRS = {"__pycache__", ".pytest_cache"}


def _iter_files() -> list[Path]:
    out: list[Path] = []
    for rel in FINGERPRINT_PATHS:
        p = AGENTIC_ROOT / rel
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            out.extend(
                f for f in p.rglob("*.py")
                if not any(part in _SKIP_DIRS for part in f.parts)
            )
    return sorted(set(out))


def code_fingerprint() -> str:
    """A stable hash of the code the flow run exercises.

    Content-based, not git-based: it must be identical whether computed on the
    author's dirty working tree or on CI's clean checkout of the same content.
    """
    h = hashlib.sha256()
    for f in _iter_files():
        h.update(str(f.relative_to(AGENTIC_ROOT)).encode())
        h.update(b"\0")
        h.update(f.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:16]


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def build_receipt(outcomes: list[dict]) -> dict:
    """Assemble the receipt payload from per-flow outcomes."""
    return {
        "receipt_version": RECEIPT_VERSION,
        "code_fingerprint": code_fingerprint(),
        "git_commit": _git("rev-parse", "HEAD"),
        "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "flows": sorted(outcomes, key=lambda o: o["name"]),
    }


def render(receipt: dict) -> str:
    """The block a contributor pastes into their PR."""
    lines = [
        "```flow-receipt",
        json.dumps(receipt, indent=2, sort_keys=True),
        "```",
    ]
    return "\n".join(lines)


def write(receipt: dict, path: Path | None = None) -> Path:
    path = path or (REPO_ROOT / "flow-receipt.json")
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return path
