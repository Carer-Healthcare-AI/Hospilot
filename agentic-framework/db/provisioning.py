"""Tenant provisioning (multi-tenancy, DB-per-tenant).

Creates the per-org database, applies the tenant table template, registers the
Hasura source (with the org's root-field / type-name prefix), tracks the four
tenant tables, and flips the org row to `active` with its routing info.

Synchronous by design (sync httpx + psycopg): the create-org route runs it in a
threadpool as a background task (see api/routes/orgs.py), and scripts/
provision_org.py is a thin CLI wrapper around provision_org() for ops-driven /
recovery runs. Every step is idempotent, so a partial failure is fixed by
re-running.

Requires (via config.settings / .env):
  HASURA_URL, HASURA_ADMIN_SECRET
  POSTGRES_ADMIN_DSN  -- privileged DSN for CREATE DATABASE; falls back to
                         DATABASE_URL when unset (same Postgres behind Hasura).
"""
import logging
import os
import re

import httpx
import psycopg

from config import settings

logger = logging.getLogger("provisioning")

TENANT_TABLES = ["sessions", "approval_tasks", "audit_log", "session_agent_overrides",
                 "rag_conversation", "rag_message", "rag_memory", "scheduled_queries",
                 "advisory_rules", "advisories"]
TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                             "db", "init", "tenant_template.sql")


class ProvisioningError(RuntimeError):
    """Raised when a provisioning step fails; the org row stays 'provisioning'."""


def _admin_dsn() -> str:
    dsn = settings.postgres_admin_dsn or settings.database_url
    if not dsn:
        raise ProvisioningError(
            "No privileged Postgres DSN: set POSTGRES_ADMIN_DSN (or DATABASE_URL)."
        )
    return dsn


def _hasura_base() -> str:
    return settings.hasura_url.replace("/v1/graphql", "")


def _headers() -> dict:
    return {"x-hasura-admin-secret": settings.hasura_admin_secret}


def _gql(query: str, variables: dict) -> dict:
    r = httpx.post(settings.hasura_url, json={"query": query, "variables": variables},
                   headers=_headers(), timeout=30.0)
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise ProvisioningError(f"Hasura GraphQL error: {data['errors']}")
    return data["data"]


def _metadata(payload: dict, ok_codes: tuple[str, ...] = ()) -> dict | None:
    """POST /v1/metadata; `ok_codes` are Hasura error codes to tolerate
    (e.g. 'already-exists' on re-runs)."""
    r = httpx.post(f"{_hasura_base()}/v1/metadata", json=payload,
                   headers=_headers(), timeout=60.0)
    if r.status_code == 400:
        code = r.json().get("code", "")
        if code in ok_codes:
            logger.info("  ~ %s: %s (ok, already done)", payload["type"], code)
            return None
    r.raise_for_status()
    return r.json()


def _swap_dbname(dsn: str, dbname: str) -> str:
    # postgresql://user:pass@host:port/dbname[?params]
    return re.sub(r"(postgres(?:ql)?://[^/]+/)[^?]*", rf"\g<1>{dbname}", dsn)


def provision_org(
    slug: str, name: str | None = None, hasura_db_url: str | None = None,
) -> dict:
    """Provision the tenant DB for `slug` and mark the org active.

    `name` creates the org row if it does not exist yet (ops-driven path); the
    normal app path creates the row first via POST /orgs and passes only `slug`.
    `hasura_db_url` overrides the connection string Hasura uses to reach the
    tenant DB (needed when Hasura runs in Docker and sees Postgres under a
    different host). Returns the routing info dict on success; raises
    ProvisioningError on any failure (org row left 'provisioning')."""
    slug = slug.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        raise ProvisioningError(f"Invalid slug: {slug!r}")
    admin_dsn = _admin_dsn()

    db_name = f"hospilot_org_{slug.replace('-', '_')}"
    source_name = f"org_{slug.replace('-', '_')}"
    prefix = f"t_{slug.replace('-', '_')}_"

    # 1. Org row (control plane)
    rows = _gql(
        """query OrgBySlug($slug: String!) {
             hospilot_app_organizations(where: {slug: {_eq: $slug}}, limit: 1) {
               id name slug status
             }
           }""",
        {"slug": slug},
    )["hospilot_app_organizations"]
    if rows:
        org = rows[0]
        logger.info("[1/5] org row exists  id=%s  status=%s", org["id"], org["status"])
    elif name:
        org = _gql(
            """mutation CreateOrg($name: String!, $slug: String!) {
                 insert_hospilot_app_organizations_one(object: {name: $name, slug: $slug}) {
                   id name slug status
                 }
               }""",
            {"name": name, "slug": slug},
        )["insert_hospilot_app_organizations_one"]
        logger.info("[1/5] org row created  id=%s", org["id"])
    else:
        raise ProvisioningError(
            f"No organization with slug '{slug}'. Create it via POST /api/orgs or pass name."
        )

    # 2. CREATE DATABASE
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (db_name,)
        ).fetchone()
        if exists:
            logger.info("[2/5] database %s exists (ok)", db_name)
        else:
            conn.execute(f'CREATE DATABASE "{db_name}"')
            logger.info("[2/5] database %s created", db_name)

    # 3. Apply tenant template
    tenant_dsn = _swap_dbname(admin_dsn, db_name)
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template_sql = f.read()
    with psycopg.connect(tenant_dsn, autocommit=True) as conn:
        conn.execute(template_sql)
    logger.info("[3/5] tenant_template.sql applied to %s", db_name)

    # 4. Hasura source + tracking
    db_url = hasura_db_url or tenant_dsn
    _metadata({
        "type": "pg_add_source",
        "args": {
            "name": source_name,
            "configuration": {
                "connection_info": {
                    "database_url": db_url,
                    "isolation_level": "read-committed",
                    "use_prepared_statements": False,
                },
            },
            "customization": {
                "root_fields": {"prefix": prefix},
                "type_names": {"prefix": prefix},
            },
        },
    }, ok_codes=("already-exists",))
    for table in TENANT_TABLES:
        _metadata({
            "type": "pg_track_table",
            "args": {"source": source_name,
                     "table": {"schema": "hospilot_app", "name": table}},
        }, ok_codes=("already-tracked",))
    _metadata({"type": "reload_metadata", "args": {"reload_remote_schemas": False}})
    logger.info("[4/5] Hasura source %s registered  prefix=%s", source_name, prefix)

    # 5. Activate org with routing info
    _gql(
        """mutation ActivateOrg($id: uuid!, $db: String!, $src: String!, $prefix: String!) {
             update_hospilot_app_organizations_by_pk(
               pk_columns: {id: $id},
               _set: {db_name: $db, hasura_source: $src, root_prefix: $prefix, status: "active"}
             ) { id status }
           }""",
        {"id": org["id"], "db": db_name, "src": source_name, "prefix": prefix},
    )
    logger.info("[5/5] org '%s' ACTIVE  db=%s  source=%s  prefix=%s",
                slug, db_name, source_name, prefix)
    return {"id": org["id"], "slug": slug, "db_name": db_name,
            "hasura_source": source_name, "root_prefix": prefix, "status": "active"}
