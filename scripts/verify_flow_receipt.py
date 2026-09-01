#!/usr/bin/env python3
"""Verify the flow-run receipt pasted into a PR description.

Usage:
    python scripts/verify_flow_receipt.py --body-file pr_body.txt
    python scripts/verify_flow_receipt.py --body-file pr_body.txt --require

Reads a ```flow-receipt``` block out of the PR body and checks it against the
code in the current checkout:

  * the receipt parses and is a version this script understands
  * every flow in the catalog appears in it
  * its code_fingerprint matches the tree being merged

The fingerprint is the part that does the work. It covers the flow catalog, the
orchestration layer and every agent package, so a receipt from before those
changed will not verify — which is exactly the "I ran it last week" case a
checkbox cannot catch.

Exit codes: 0 pass (or no receipt and --require not set), 1 fail.

This is a good-faith gate. It catches stale and forgotten runs; it does not stop
someone determined to forge a receipt, and it is not meant to.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "agentic-framework" / "tests" / "flows"))

_BLOCK = re.compile(r"```flow-receipt\s*(.*?)```", re.DOTALL | re.IGNORECASE)

# Paths whose change means a prior flow run no longer proves anything. Mirrors
# _receipt.FINGERPRINT_PATHS, relative to the repo root.
WATCHED_PREFIXES = (
    "agentic-framework/tests/flows/_flows.py",
    "agentic-framework/tests/flows/_driver.py",
    "agentic-framework/workflows/",
    "agentic-framework/agents/",
)


def fail(msg: str) -> None:
    print(f"::error::{msg}" if _in_actions() else f"FAIL: {msg}")
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"OK: {msg}")


def _in_actions() -> bool:
    import os
    return bool(os.environ.get("GITHUB_ACTIONS"))


def extract(body: str) -> dict | None:
    m = _BLOCK.search(body)
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        fail(f"the flow-receipt block is not valid JSON ({e}). Paste the block "
             f"exactly as the test run printed it.")
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--body-file", required=True,
                    help="file containing the PR description")
    ap.add_argument("--require", action="store_true",
                    help="fail when no receipt is present (set when the PR "
                         "touches flow-relevant code)")
    ap.add_argument("--changed-files", default="",
                    help="newline-separated changed paths; when given, --require "
                         "only bites if one of them is flow-relevant")
    args = ap.parse_args()

    body = Path(args.body_file).read_text(encoding="utf-8", errors="replace")
    receipt = extract(body)

    required = args.require
    if required and args.changed_files:
        changed = [f.strip() for f in args.changed_files.splitlines() if f.strip()]
        relevant = [f for f in changed if f.startswith(WATCHED_PREFIXES)]
        if not relevant:
            ok("no flow-relevant files changed — receipt not required")
            return
        print(f"flow-relevant changes:\n  " + "\n  ".join(relevant))

    if receipt is None:
        if not required:
            ok("no receipt present, and none required")
            return
        fail(
            "this PR changes flow-relevant code but carries no flow-receipt.\n"
            "Run the live flows and paste the block they print:\n"
            "    cd agentic-framework && pytest tests/flows -m live\n"
            "If you cannot run the stack, say so in the PR and ask a maintainer "
            "to run it for you."
        )

    from _receipt import RECEIPT_VERSION, code_fingerprint  # noqa: E402
    from _flows import ALL_FLOWS  # noqa: E402

    version = receipt.get("receipt_version")
    if version != RECEIPT_VERSION:
        fail(f"receipt_version {version!r} — this checkout expects "
             f"{RECEIPT_VERSION}. Re-run the flows to get a current receipt.")

    listed = {f.get("name") for f in receipt.get("flows", [])}
    expected = {f["name"] for f in ALL_FLOWS}
    missing = expected - listed
    if missing:
        fail(f"receipt is missing flows {sorted(missing)} — it came from a "
             f"partial run. Run the whole suite: pytest tests/flows -m live")

    actual = code_fingerprint()
    claimed = receipt.get("code_fingerprint")
    if claimed != actual:
        fail(
            f"receipt is STALE.\n"
            f"  receipt fingerprint : {claimed}\n"
            f"  this checkout       : {actual}\n"
            f"Flow-relevant code changed after that run, so it no longer proves "
            f"anything. Re-run: pytest tests/flows -m live"
        )

    ok(f"receipt verified — {len(listed)} flows, fingerprint {actual}, "
       f"run at {receipt.get('generated_at', '?')}")
    for f in sorted(receipt.get("flows", []), key=lambda x: x.get("name", "")):
        print(f"  - {f.get('name')}: {len(f.get('agents_ran', []))} agents ran, "
              f"{f.get('supersteps')} supersteps, {f.get('duration_s')}s")


if __name__ == "__main__":
    main()
