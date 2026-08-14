#!/usr/bin/env python3
"""Command-line Ask Hospilot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

from llm import load_dotenv_file  # noqa: E402

load_dotenv_file(ROOT / ".env")

from ask import AskResult, ask, format_cli  # noqa: E402
from init_db import DEFAULT_DB, init_db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Ask Hospilot — grounded hospital Q&A")
    parser.add_argument("question", nargs="?", help="Question to ask (or use --repl)")
    parser.add_argument("--repl", action="store_true", help="Interactive prompt loop")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--init", action="store_true", help="(Re)build the database first")
    args = parser.parse_args()

    if args.init or not args.db.exists():
        init_db(args.db, force=args.init)

    if args.repl:
        print("Ask Hospilot (empty line or Ctrl+C to exit)")
        while True:
            try:
                q = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not q:
                return 0
            try:
                _print(ask(q, db_path=args.db), as_json=args.json)
            except RuntimeError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 2
        return 0

    if not args.question:
        parser.error("Provide a question or use --repl")

    try:
        result = ask(args.question, db_path=args.db)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    _print(result, as_json=args.json)
    return 0 if result.can_answer or result.answer else 1


def _print(result: AskResult, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        print(format_cli(result))


if __name__ == "__main__":
    sys.exit(main())
