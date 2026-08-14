"""Initialize SQLite database from schema.sql + seed.sql."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "hospital.db"


def init_db(db_path: Path = DEFAULT_DB, force: bool = False) -> Path:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        if not force:
            return db_path
        db_path.unlink()

    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    seed = (ROOT / "seed.sql").read_text(encoding="utf-8")

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema)
        conn.executescript(seed)
        conn.commit()
    finally:
        conn.close()
    return db_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Ask Hospilot SQLite DB")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--force", action="store_true", help="Rebuild even if DB exists")
    args = parser.parse_args()
    path = init_db(args.db, force=args.force)
    print(f"Database ready: {path}")


if __name__ == "__main__":
    main()
