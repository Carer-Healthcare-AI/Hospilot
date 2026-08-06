"""
Apply a SQL migration to EVERY tenant database (multi-tenancy, DB-per-tenant).

Run from agentic-framework/:
    python scripts/migrate_all_tenants.py db/migrations/0XX_some_tenant_change.sql

Any migration that touches the per-tenant app tables (hospilot_app.sessions /
approval_tasks / audit_log / session_agent_overrides) MUST be applied through
this script: it runs the file against the Hasura source of every organization
-- including 'default' (the Carer org, whose tenant tables live in the
control-plane DB) -- then reloads metadata once.

Also update schemas/sql/tenant_template.sql in the same commit so newly provisioned
tenants are created with the change already in place.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import httpx
from dotenv import load_dotenv

load_dotenv()

HASURA_URL = os.getenv("HASURA_URL", "http://localhost:8080/v1/graphql")
HASURA_ADMIN_SECRET = os.getenv("HASURA_ADMIN_SECRET", "")
HASURA_BASE = HASURA_URL.replace("/v1/graphql", "")
HEADERS = {"x-hasura-admin-secret": HASURA_ADMIN_SECRET}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("sql_file", nargs="?", help="path to the migration .sql file")
    ap.add_argument("--include-disabled", action="store_true",
                    help="also run against disabled orgs' sources")
    ap.add_argument("--track-only", metavar="TABLES",
                    help="skip run_sql; instead pg_track_table these comma-separated "
                         "hospilot_app tables on every source (for migrations that "
                         "CREATE new tables -- reload_metadata does NOT auto-track them). "
                         "Idempotent (tolerates already-tracked).")
    args = ap.parse_args()

    if not args.track_only and not args.sql_file:
        raise SystemExit("Provide a sql_file, or --track-only TABLES.")

    sql = ""
    if args.sql_file:
        with open(args.sql_file, encoding="utf-8") as f:
            sql = f.read()

    r = httpx.post(HASURA_URL, json={"query": """
        query Orgs { hospilot_app_organizations { id name slug status hasura_source } }
    """}, headers=HEADERS, timeout=30.0)
    r.raise_for_status()
    body = r.json()
    if "errors" in body:
        raise SystemExit(f"Hasura error: {body['errors']}")
    orgs = body["data"]["hospilot_app_organizations"]

    targets, seen = [], set()
    for o in orgs:
        src = o.get("hasura_source")
        if not src or src in seen:
            continue
        if o["status"] == "disabled" and not args.include_disabled:
            print(f"  - skip {o['slug']} (disabled)")
            continue
        if o["status"] == "provisioning":
            print(f"  - skip {o['slug']} (still provisioning -- template will cover it)")
            continue
        seen.add(src)
        targets.append((o["slug"], src))

    if not targets:
        raise SystemExit("No active tenant sources found. Run migration 050 first?")

    failed = []

    if args.track_only:
        tables = [t.strip() for t in args.track_only.split(",") if t.strip()]
        print(f"Tracking {tables} on {len(targets)} source(s): "
              f"{', '.join(s for _, s in targets)}")
        for slug, src in targets:
            src_ok = True
            for table in tables:
                resp = httpx.post(f"{HASURA_BASE}/v1/metadata",
                                  json={"type": "pg_track_table",
                                        "args": {"source": src,
                                                 "table": {"schema": "hospilot_app", "name": table}}},
                                  headers=HEADERS, timeout=60.0)
                # 'already-tracked' is the idempotent no-op case.
                if resp.status_code != 200 and "already-tracked" not in resp.text:
                    src_ok = False
                    print(f"  [x]  {slug} ({src}) {table}: {resp.status_code} {resp.text[:200]}")
            if src_ok:
                print(f"  [ok] {slug} ({src})")
            else:
                failed.append(slug)
    else:
        print(f"Applying {args.sql_file} to {len(targets)} source(s): "
              f"{', '.join(s for _, s in targets)}")
        for slug, src in targets:
            resp = httpx.post(f"{HASURA_BASE}/v2/query",
                              json={"type": "run_sql",
                                    "args": {"source": src, "sql": sql}},
                              headers=HEADERS, timeout=120.0)
            if resp.status_code == 200:
                print(f"  [ok] {slug} ({src})")
            else:
                failed.append(slug)
                print(f"  [x]  {slug} ({src}): {resp.status_code} {resp.text[:300]}")

    httpx.post(f"{HASURA_BASE}/v1/metadata",
               json={"type": "reload_metadata", "args": {"reload_remote_schemas": False}},
               headers=HEADERS, timeout=60.0).raise_for_status()
    print("Metadata reloaded.")

    if failed:
        raise SystemExit(f"FAILED on: {', '.join(failed)} -- fix and re-run "
                         f"(the SQL should be idempotent).")
    print("All tenants migrated.")


if __name__ == "__main__":
    main()
