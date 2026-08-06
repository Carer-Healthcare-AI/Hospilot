"""Provision a tenant database for an organization (multi-tenancy, DB-per-tenant).

Ops / recovery CLI. The app now auto-provisions new orgs in the background when
they are created via POST /api/orgs (see api/routes/orgs.py); this script wraps
the same db.provisioning.provision_org() for ops-driven creation or to re-run
after a failed background provision (every step is idempotent).

Run from agentic-framework/:
    python scripts/provision_org.py --slug acme [--name "Acme Hospital"]

Steps (see db/provisioning.py):
  1. Ensure the org row exists in hospilot_app.organizations (control plane).
     --name lets the script create it directly for ops-driven provisioning.
  2. CREATE DATABASE hospilot_org_<slug> via POSTGRES_ADMIN_DSN (or DATABASE_URL).
  3. Apply schemas/sql/tenant_template.sql to the new database.
  4. Register it as Hasura source 'org_<slug>' with prefix 't_<slug>_', track the
     four tenant tables, reload metadata.
  5. Mark the org row active with its routing info.

Requires in .env:
  POSTGRES_ADMIN_DSN  privileged DSN for CREATE DATABASE (falls back to DATABASE_URL)
  HASURA_URL, HASURA_ADMIN_SECRET
Optional:
  --hasura-db-url     connection string Hasura should use to reach the tenant DB
                      (override when Hasura runs in Docker and sees Postgres
                      under a different host).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv

load_dotenv()

from db.provisioning import ProvisioningError, provision_org


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slug", required=True, help="org slug (lowercase, [a-z0-9-])")
    ap.add_argument("--name", help="org display name (creates the org row if missing)")
    ap.add_argument("--hasura-db-url",
                    help="connection string Hasura uses to reach the tenant DB")
    args = ap.parse_args()

    try:
        result = provision_org(slug=args.slug, name=args.name,
                               hasura_db_url=args.hasura_db_url)
    except ProvisioningError as exc:
        raise SystemExit(str(exc))

    print(f"org '{result['slug']}' ACTIVE  db={result['db_name']}  "
          f"source={result['hasura_source']}  prefix={result['root_prefix']}")
    print("Done. The app refreshes its org registry cache on next routed query "
          "(or restart it to warm immediately).")


if __name__ == "__main__":
    main()
