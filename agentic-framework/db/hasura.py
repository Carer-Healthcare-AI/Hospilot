import asyncio
import json
import logging
import re
import httpx
from config import settings
from db.fabric import fget, fpost

logger = logging.getLogger("hasura")


def _op_name(gql: str) -> str:
    m = re.search(r'\b(query|mutation)\s+(\w+)', gql)
    return m.group(2) if m else gql.strip().split('\n')[0][:60]


def _exec_ctx_org() -> str | None:
    """Org of the currently-executing workflow, if any (contextvar set by the
    graph runner). Lazy import: workflows.graph imports db.hasura, so a
    module-level import here would be circular."""
    try:
        from workflows.graph.exec_context import get_exec_ctx
        ctx = get_exec_ctx()
        return (ctx or {}).get("org_id") or None
    except Exception:
        return None


# Keywords mapping a free-text medication_name to the 5 SKUs the /pharmacy/demand
# forecast model knows. Checked in order; first substring hit wins. Brand names
# and common synonyms included so the raw dispensing log classifies cleanly.
_PHARMACY_SKU_KEYWORDS: dict[str, tuple[str, ...]] = {
    "INSULIN":     ("insulin", "humalog", "lantus", "novorapid", "actrapid"),
    "PARACETAMOL": ("paracetamol", "acetaminophen", "crocin", "dolo", "calpol", "pcm"),
    "AMOXICILLIN": ("amoxicillin", "amoxi", "augmentin", "clav", "moxikind"),
    "HEPARIN":     ("heparin", "enoxaparin", "clexane", "lmwh", "dalteparin"),
    "METFORMIN":   ("metformin", "glucophage", "glyciphage", "glucon"),
}


def _classify_drug_sku(medication_name: str | None) -> str | None:
    """Map a dispensing-log medication_name to a /pharmacy/demand SKU, or None."""
    n = (medication_name or "").lower()
    for sku, keywords in _PHARMACY_SKU_KEYWORDS.items():
        if any(kw in n for kw in keywords):
            return sku
    return None


class HasuraClient:
    """GraphQL client over Hasura with multi-tenant (DB-per-tenant) routing.

    Tenant app tables (sessions / approval_tasks / audit_log /
    session_agent_overrides) live in one Hasura *source per organization*; each
    source's GraphQL root fields and type names carry the org's prefix
    (organizations.root_prefix, e.g. ``t_acme_``). Queries against those tables
    are written with a ``{P}`` placeholder which `query()` resolves per call:
    explicit ``org_id`` arg -> workflow exec-context org -> "" (the default
    source = the Carer org, whose tenant tables are the original unprefixed
    ones). Control-plane tables (users, organizations, registries) and shared
    clinical tables have no placeholder and always hit the default source.
    """

    def __init__(self):
        self._headers = {
            "Content-Type": "application/json",
            "x-hasura-admin-secret": settings.hasura_admin_secret,
        }
        # org_id -> {slug, status, root_prefix, hasura_source}; None until loaded
        self._orgs: dict[str, dict] | None = None

    # ── org registry (routing table, control plane) ──────────────────────────

    async def load_org_registry(self) -> dict[str, dict]:
        """(Re)load the org routing cache from the control-plane DB."""
        data = await self._post(
            """
            query OrgRegistry {
              hospilot_app_organizations {
                id name slug status root_prefix hasura_source
              }
            }
            """,
            {},
        )
        self._orgs = {o["id"]: o for o in data.get("hospilot_app_organizations", [])}
        logger.info("org registry loaded  orgs=%d", len(self._orgs))
        return self._orgs

    async def _resolve_prefix(self, org_id: str | None) -> str:
        org = org_id or _exec_ctx_org()
        if not org:
            return ""  # default source (Carer / control plane)
        if self._orgs is None or org not in self._orgs:
            await self.load_org_registry()
        rec = (self._orgs or {}).get(org)
        if rec is None:
            raise Exception(f"Unknown organization: {org}")
        return rec.get("root_prefix") or ""

    async def ensure_org_registry(self) -> dict[str, dict]:
        if self._orgs is None:
            await self.load_org_registry()
        return self._orgs or {}

    def active_orgs(self) -> list[dict]:
        """Cached active orgs (for super_admin cross-org aggregation)."""
        return [o for o in (self._orgs or {}).values() if o.get("status") == "active"]

    # ── transport ────────────────────────────────────────────────────────────

    async def _post(self, gql: str, variables: dict) -> dict:
        op = _op_name(gql)
        logger.debug(">> %s  vars=%s", op, json.dumps(variables or {}, default=str)[:200])
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                settings.hasura_url,
                json={"query": gql, "variables": variables or {}},
                headers=self._headers,
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                logger.error("[x] %s  errors=%s", op, data["errors"])
                raise Exception(f"Hasura error: {data['errors']}")
            logger.debug("[ok] %s  keys=%s", op, list(data.get("data", {}).keys()))
            return data["data"]

    async def query(
        self, gql: str, variables: dict | None = None, org_id: str | None = None,
    ) -> dict:
        if "{P}" in gql:
            prefix = await self._resolve_prefix(org_id)
            # .replace, NOT .format -- GraphQL bodies are full of literal braces.
            gql = gql.replace("{P}", prefix)
        return await self._post(gql, variables or {})

    async def mutate(
        self, gql: str, variables: dict | None = None, org_id: str | None = None,
    ) -> dict:
        return await self.query(gql, variables, org_id=org_id)

    # =========================================================================
    # HOSPILOT -- System tables (direct Hasura -- computation tables)
    # =========================================================================

    # =========================================================================
    # AUTH -- users table
    # =========================================================================

    async def create_user(
        self, username: str, password_hash: str, display_name: str,
        role: str = "doctor", org_id: str | None = None, status: str = "pending",
    ) -> dict:
        data = await self.mutate(
            """
            mutation CreateUser(
              $username: String!, $password_hash: String!,
              $display_name: String!, $role: String!,
              $org_id: uuid, $status: String!
            ) {
              insert_hospilot_app_users_one(object: {
                username: $username, password_hash: $password_hash,
                display_name: $display_name, role: $role,
                org_id: $org_id, status: $status
              }) { id username display_name role org_id status created_at }
            }
            """,
            {"username": username, "password_hash": password_hash,
             "display_name": display_name, "role": role,
             "org_id": org_id, "status": status},
        )
        return data["insert_hospilot_app_users_one"]

    async def get_user_by_username(self, username: str) -> dict | None:
        data = await self.query(
            """
            query GetUserByUsername($username: String!) {
              hospilot_app_users(where: {username: {_eq: $username}}, limit: 1) {
                id username password_hash display_name role org_id status
              }
            }
            """,
            {"username": username},
        )
        rows = data.get("hospilot_app_users", [])
        return rows[0] if rows else None

    async def get_user_by_id(self, user_id: str) -> dict | None:
        data = await self.query(
            """
            query GetUserById($id: uuid!) {
              hospilot_app_users_by_pk(id: $id) {
                id username display_name role org_id status approved_by approved_at
              }
            }
            """,
            {"id": user_id},
        )
        return data.get("hospilot_app_users_by_pk")

    async def count_users_by_role(self, role: str) -> int:
        data = await self.query(
            """
            query CountUsersByRole($role: String!) {
              hospilot_app_users_aggregate(where: {role: {_eq: $role}}) {
                aggregate { count }
              }
            }
            """,
            {"role": role},
        )
        return (data.get("hospilot_app_users_aggregate", {})
                .get("aggregate", {}).get("count", 0))

    async def list_users(
        self, org_id: str | None = None, status: str | None = None,
    ) -> list[dict]:
        """Control-plane user list, optionally filtered by org and/or status.
        Never selects password_hash."""
        where: dict = {}
        if org_id:
            where["org_id"] = {"_eq": org_id}
        if status:
            where["status"] = {"_eq": status}
        data = await self.query(
            """
            query ListUsers($where: hospilot_app_users_bool_exp!) {
              hospilot_app_users(where: $where, order_by: {created_at: desc}) {
                id username display_name role org_id status
                approved_by approved_at created_at
              }
            }
            """,
            {"where": where},
        )
        return data.get("hospilot_app_users", [])

    async def update_user_status(
        self, user_id: str, status: str,
        approved_by: str | None = None, org_id: str | None = None,
    ) -> dict | None:
        """Set a user's status (approve / reject / disable / re-enable).

        When `org_id` is given the update is org-guarded in the WHERE clause,
        so an org admin physically cannot touch another org's users. Returns
        the updated row, or None if no row matched."""
        where: dict = {"id": {"_eq": user_id}}
        if org_id:
            where["org_id"] = {"_eq": org_id}
        set_fields: dict = {"status": status}
        if status == "active" and approved_by:
            set_fields["approved_by"] = approved_by
            set_fields["approved_at"] = "now()"
        data = await self.mutate(
            """
            mutation UpdateUserStatus(
              $where: hospilot_app_users_bool_exp!,
              $set: hospilot_app_users_set_input!
            ) {
              update_hospilot_app_users(where: $where, _set: $set) {
                returning { id username display_name role org_id status }
              }
            }
            """,
            {"where": where, "set": set_fields},
        )
        rows = data.get("update_hospilot_app_users", {}).get("returning", [])
        return rows[0] if rows else None

    async def update_user_role(
        self, user_id: str, role: str, org_id: str | None = None,
    ) -> dict | None:
        """Change a user's role, org-guarded like update_user_status."""
        where: dict = {"id": {"_eq": user_id}}
        if org_id:
            where["org_id"] = {"_eq": org_id}
        data = await self.mutate(
            """
            mutation UpdateUserRole(
              $where: hospilot_app_users_bool_exp!, $role: String!
            ) {
              update_hospilot_app_users(where: $where, _set: {role: $role}) {
                returning { id username display_name role org_id status }
              }
            }
            """,
            {"where": where, "role": role},
        )
        rows = data.get("update_hospilot_app_users", {}).get("returning", [])
        return rows[0] if rows else None

    # =========================================================================
    # ORGANIZATIONS -- control plane (multi-tenancy)
    # =========================================================================

    async def create_org(self, name: str, slug: str, created_by: str) -> dict:
        data = await self.mutate(
            """
            mutation CreateOrg($name: String!, $slug: String!, $created_by: uuid!) {
              insert_hospilot_app_organizations_one(object: {
                name: $name, slug: $slug, created_by: $created_by
              }) { id name slug status root_prefix hasura_source created_at }
            }
            """,
            {"name": name, "slug": slug, "created_by": created_by},
        )
        await self.load_org_registry()
        return data["insert_hospilot_app_organizations_one"]

    async def get_org(self, org_id: str) -> dict | None:
        data = await self.query(
            """
            query GetOrg($id: uuid!) {
              hospilot_app_organizations_by_pk(id: $id) {
                id name slug status db_name hasura_source root_prefix created_at
              }
            }
            """,
            {"id": org_id},
        )
        return data.get("hospilot_app_organizations_by_pk")

    async def list_orgs(self, include_disabled: bool = True) -> list[dict]:
        where = {} if include_disabled else {"status": {"_neq": "disabled"}}
        data = await self.query(
            """
            query ListOrgs($where: hospilot_app_organizations_bool_exp!) {
              hospilot_app_organizations(where: $where, order_by: {created_at: asc}) {
                id name slug status db_name hasura_source root_prefix created_at
              }
            }
            """,
            {"where": where},
        )
        return data.get("hospilot_app_organizations", [])

    async def list_active_orgs_public(self) -> list[dict]:
        """Signup picker: id + name of active orgs only (no routing internals)."""
        data = await self.query(
            """
            query PublicOrgs {
              hospilot_app_organizations(
                where: {status: {_eq: "active"}}, order_by: {name: asc}
              ) { id name }
            }
            """,
            {},
        )
        return data.get("hospilot_app_organizations", [])

    async def update_org(self, org_id: str, set_fields: dict) -> dict | None:
        data = await self.mutate(
            """
            mutation UpdateOrg($id: uuid!, $set: hospilot_app_organizations_set_input!) {
              update_hospilot_app_organizations_by_pk(pk_columns: {id: $id}, _set: $set) {
                id name slug status db_name hasura_source root_prefix
              }
            }
            """,
            {"id": org_id, "set": set_fields},
        )
        await self.load_org_registry()
        return data.get("update_hospilot_app_organizations_by_pk")

    async def get_sessions_user_info(
        self, session_ids: list[str], org_id: str | None = None,
    ) -> dict[str, str]:
        """Returns {session_id: display_name} for the given session IDs.

        Session rows come from the caller's tenant source; display names from
        the central users table (control plane)."""
        if not session_ids:
            return {}
        sess_data = await self.query(
            """
            query GetSessionUserIds($ids: [uuid!]!) {
              hospilot_app_sessions: {P}hospilot_app_sessions(where: {id: {_in: $ids}}) { id user_id }
            }
            """,
            {"ids": session_ids},
            org_id=org_id,
        )
        sessions = sess_data.get("hospilot_app_sessions", [])
        user_id_map: dict[str, str] = {
            s["id"]: s["user_id"] for s in sessions if s.get("user_id")
        }
        user_ids = list(set(user_id_map.values()))
        if not user_ids:
            return {}
        user_data = await self.query(
            """
            query GetUserDisplayNames($ids: [uuid!]!) {
              hospilot_app_users(where: {id: {_in: $ids}}) { id display_name }
            }
            """,
            {"ids": user_ids},
        )
        display_map: dict[str, str] = {
            u["id"]: u["display_name"] for u in user_data.get("hospilot_app_users", [])
        }
        return {
            sid: display_map.get(uid, "Unknown")
            for sid, uid in user_id_map.items()
        }

    async def get_sessions_min(
        self, session_ids: list[str], org_id: str | None = None,
    ) -> dict[str, dict]:
        """Batch-fetch minimal session fields for the Execution queue, keyed by id.

        One `_in` query (no N+1) returning just what the queue row needs:
        goal / status / created_at / autonomous. Routed to the caller's tenant
        source -- IDs belonging to other orgs simply don't resolve."""
        if not session_ids:
            return {}
        data = await self.query(
            """
            query SessionsMin($ids: [uuid!]!) {
              hospilot_app_sessions: {P}hospilot_app_sessions(where: {id: {_in: $ids}}) {
                id goal status created_at autonomous user_id
              }
            }
            """,
            {"ids": session_ids},
            org_id=org_id,
        )
        return {s["id"]: s for s in data.get("hospilot_app_sessions", [])}

    # =========================================================================
    # SCHEDULED QUERIES -- saved queries re-run on a cadence (Phase 6)
    # =========================================================================

    _SCHEDULE_FIELDS = (
        "id name goal constraints schedule_kind interval_seconds cron_expr timezone "
        "enabled autonomous next_run_at last_run_at last_session_id run_count "
        "user_id created_at updated_at"
    )

    async def create_scheduled_query(
        self, *, goal: str, constraints: str | None, name: str | None,
        schedule_kind: str, interval_seconds: int | None, cron_expr: str | None,
        timezone: str, next_run_at: str, user_id: str | None = None,
        org_id: str | None = None,
    ) -> dict:
        data = await self.mutate(
            f"""
            mutation CreateScheduledQuery(
              $goal: String!, $constraints: String, $name: String,
              $schedule_kind: String!, $interval_seconds: Int, $cron_expr: String,
              $timezone: String!, $next_run_at: timestamptz!, $user_id: uuid
            ) {{
              insert_hospilot_app_scheduled_queries_one: insert_{{P}}hospilot_app_scheduled_queries_one(object: {{
                goal: $goal, constraints: $constraints, name: $name,
                schedule_kind: $schedule_kind, interval_seconds: $interval_seconds,
                cron_expr: $cron_expr, timezone: $timezone, next_run_at: $next_run_at,
                user_id: $user_id
              }}) {{ {self._SCHEDULE_FIELDS} }}
            }}
            """,
            {"goal": goal, "constraints": constraints, "name": name,
             "schedule_kind": schedule_kind, "interval_seconds": interval_seconds,
             "cron_expr": cron_expr, "timezone": timezone, "next_run_at": next_run_at,
             "user_id": user_id},
            org_id=org_id,
        )
        return data["insert_hospilot_app_scheduled_queries_one"]

    async def list_scheduled_queries(
        self, user_id: str | None = None, org_id: str | None = None,
    ) -> list[dict]:
        where: dict = {}
        if user_id:
            where["user_id"] = {"_eq": user_id}
        data = await self.query(
            f"""
            query ListScheduledQueries($where: {{P}}hospilot_app_scheduled_queries_bool_exp!) {{
              hospilot_app_scheduled_queries: {{P}}hospilot_app_scheduled_queries(
                where: $where, order_by: {{created_at: desc}}
              ) {{ {self._SCHEDULE_FIELDS} }}
            }}
            """,
            {"where": where},
            org_id=org_id,
        )
        return data.get("hospilot_app_scheduled_queries", [])

    async def get_scheduled_query(
        self, schedule_id: str, org_id: str | None = None,
    ) -> dict | None:
        data = await self.query(
            f"""
            query GetScheduledQuery($id: uuid!) {{
              hospilot_app_scheduled_queries_by_pk: {{P}}hospilot_app_scheduled_queries_by_pk(id: $id) {{
                {self._SCHEDULE_FIELDS}
              }}
            }}
            """,
            {"id": schedule_id},
            org_id=org_id,
        )
        return data.get("hospilot_app_scheduled_queries_by_pk")

    async def update_scheduled_query(
        self, schedule_id: str, set_fields: dict, org_id: str | None = None,
    ) -> dict | None:
        data = await self.mutate(
            f"""
            mutation UpdateScheduledQuery(
              $id: uuid!, $set: {{P}}hospilot_app_scheduled_queries_set_input!
            ) {{
              update_hospilot_app_scheduled_queries_by_pk: update_{{P}}hospilot_app_scheduled_queries_by_pk(
                pk_columns: {{id: $id}}, _set: $set
              ) {{ {self._SCHEDULE_FIELDS} }}
            }}
            """,
            {"id": schedule_id, "set": set_fields},
            org_id=org_id,
        )
        return data.get("update_hospilot_app_scheduled_queries_by_pk")

    async def delete_scheduled_query(
        self, schedule_id: str, org_id: str | None = None,
    ) -> bool:
        data = await self.mutate(
            """
            mutation DeleteScheduledQuery($id: uuid!) {
              delete_hospilot_app_scheduled_queries_by_pk: {P}delete_hospilot_app_scheduled_queries_by_pk(id: $id) { id }
            }
            """,
            {"id": schedule_id},
            org_id=org_id,
        )
        return data.get("delete_hospilot_app_scheduled_queries_by_pk") is not None

    async def fetch_due_scheduled_queries(
        self, now_iso: str, org_id: str | None = None,
    ) -> list[dict]:
        """Enabled schedules whose next_run_at has passed -- the scheduler loop's scan."""
        data = await self.query(
            f"""
            query DueScheduledQueries($now: timestamptz!) {{
              hospilot_app_scheduled_queries: {{P}}hospilot_app_scheduled_queries(
                where: {{enabled: {{_eq: true}}, next_run_at: {{_lte: $now}}}}
                order_by: {{next_run_at: asc}}
              ) {{ {self._SCHEDULE_FIELDS} }}
            }}
            """,
            {"now": now_iso},
            org_id=org_id,
        )
        return data.get("hospilot_app_scheduled_queries", [])

    async def mark_scheduled_query_fired(
        self, schedule_id: str, next_run_at: str, last_session_id: str,
        run_count: int, last_run_at: str, org_id: str | None = None,
    ) -> None:
        """Record a successful fire: bump next_run_at + run bookkeeping. run_count is
        the already-incremented value (Hasura has no atomic _inc on by_pk _set)."""
        await self.mutate(
            """
            mutation MarkFired($id: uuid!, $set: {P}hospilot_app_scheduled_queries_set_input!) {
              update_hospilot_app_scheduled_queries_by_pk: {P}update_hospilot_app_scheduled_queries_by_pk(
                pk_columns: {id: $id}, _set: $set
              ) { id }
            }
            """,
            {"id": schedule_id, "set": {
                "next_run_at": next_run_at, "last_run_at": last_run_at,
                "last_session_id": last_session_id, "run_count": run_count,
            }},
            org_id=org_id,
        )

    async def bump_scheduled_query_next_run(
        self, schedule_id: str, next_run_at: str, org_id: str | None = None,
    ) -> None:
        """Advance next_run_at only (skip-on-overlap path -- no run bookkeeping)."""
        await self.mutate(
            """
            mutation BumpNextRun($id: uuid!, $next_run_at: timestamptz!) {
              update_hospilot_app_scheduled_queries_by_pk: {P}update_hospilot_app_scheduled_queries_by_pk(
                pk_columns: {id: $id}, _set: {next_run_at: $next_run_at}
              ) { id }
            }
            """,
            {"id": schedule_id, "next_run_at": next_run_at},
            org_id=org_id,
        )

    async def list_scheduled_query_runs(
        self, schedule_id: str, limit: int = 50, org_id: str | None = None,
    ) -> list[dict]:
        """Sessions spawned by a schedule, newest first (run history)."""
        data = await self.query(
            """
            query ScheduledRuns($id: uuid!, $limit: Int!) {
              hospilot_app_sessions: {P}hospilot_app_sessions(
                where: {scheduled_query_id: {_eq: $id}},
                order_by: {created_at: desc}, limit: $limit
              ) { id goal name status created_at updated_at autonomous }
            }
            """,
            {"id": schedule_id, "limit": limit},
            org_id=org_id,
        )
        return data.get("hospilot_app_sessions", [])

    # =========================================================================
    # ADVISORY ENGINE -- notify-only rules + fired advisories
    # =========================================================================

    _ADVISORY_RULE_FIELDS = (
        "id rule_key topic label condition_description suggested_action severity "
        "definition trigger_entities check_interval_seconds cooldown_seconds enabled "
        "next_check_at last_checked_at last_fired_at fire_count created_at updated_at"
    )
    _ADVISORY_FIELDS = (
        "id rule_key topic severity title detail data suggested_action status "
        "acknowledged_by acknowledged_at created_at"
    )

    async def fetch_due_advisory_rules(
        self, now_iso: str, org_id: str | None = None,
    ) -> list[dict]:
        """Enabled clock-scheduled rules whose next_check_at has passed."""
        data = await self.query(
            f"""
            query DueAdvisoryRules($now: timestamptz!) {{
              hospilot_app_advisory_rules: {{P}}hospilot_app_advisory_rules(
                where: {{enabled: {{_eq: true}},
                         check_interval_seconds: {{_is_null: false}},
                         next_check_at: {{_lte: $now}}}}
                order_by: {{next_check_at: asc}}
              ) {{ {self._ADVISORY_RULE_FIELDS} }}
            }}
            """,
            {"now": now_iso},
            org_id=org_id,
        )
        return data.get("hospilot_app_advisory_rules", [])

    async def fetch_event_advisory_rules(self, org_id: str | None = None) -> list[dict]:
        """Enabled event-triggered rules (non-empty trigger_entities); the entity
        intersection with the changed set is done in Python (rows are few)."""
        data = await self.query(
            f"""
            query EventAdvisoryRules($empty: jsonb!) {{
              hospilot_app_advisory_rules: {{P}}hospilot_app_advisory_rules(
                where: {{enabled: {{_eq: true}}, trigger_entities: {{_neq: $empty}}}}
              ) {{ {self._ADVISORY_RULE_FIELDS} }}
            }}
            """,
            {"empty": []},
            org_id=org_id,
        )
        return data.get("hospilot_app_advisory_rules", [])

    async def update_advisory_rule(
        self, rule_id: str, set_fields: dict, org_id: str | None = None,
    ) -> dict | None:
        """Generic rule update -- engine bookkeeping and the PATCH route both use it.
        fire_count is passed pre-incremented (Hasura has no atomic _inc on by_pk _set)."""
        data = await self.mutate(
            f"""
            mutation UpdateAdvisoryRule($id: uuid!, $set: {{P}}hospilot_app_advisory_rules_set_input!) {{
              update_hospilot_app_advisory_rules_by_pk: {{P}}update_hospilot_app_advisory_rules_by_pk(
                pk_columns: {{id: $id}}, _set: $set
              ) {{ {self._ADVISORY_RULE_FIELDS} }}
            }}
            """,
            {"id": rule_id, "set": set_fields},
            org_id=org_id,
        )
        return data.get("update_hospilot_app_advisory_rules_by_pk")

    async def list_advisory_rules(self, org_id: str | None = None) -> list[dict]:
        data = await self.query(
            f"""
            query ListAdvisoryRules {{
              hospilot_app_advisory_rules: {{P}}hospilot_app_advisory_rules(
                order_by: [{{topic: asc}}, {{label: asc}}]
              ) {{ {self._ADVISORY_RULE_FIELDS} }}
            }}
            """,
            org_id=org_id,
        )
        return data.get("hospilot_app_advisory_rules", [])

    async def get_advisory_rule(
        self, rule_id: str, org_id: str | None = None,
    ) -> dict | None:
        data = await self.query(
            f"""
            query GetAdvisoryRule($id: uuid!) {{
              hospilot_app_advisory_rules_by_pk: {{P}}hospilot_app_advisory_rules_by_pk(id: $id) {{
                {self._ADVISORY_RULE_FIELDS}
              }}
            }}
            """,
            {"id": rule_id},
            org_id=org_id,
        )
        return data.get("hospilot_app_advisory_rules_by_pk")

    async def insert_advisory(
        self, *, rule_key: str, topic: str, severity: str, title: str,
        detail: str | None, data: dict, suggested_action: str | None,
        org_id: str | None = None,
    ) -> dict:
        resp = await self.mutate(
            f"""
            mutation InsertAdvisory(
              $rule_key: String!, $topic: String!, $severity: String!, $title: String!,
              $detail: String, $data: jsonb!, $suggested_action: String
            ) {{
              insert_hospilot_app_advisories_one: {{P}}insert_hospilot_app_advisories_one(object: {{
                rule_key: $rule_key, topic: $topic, severity: $severity, title: $title,
                detail: $detail, data: $data, suggested_action: $suggested_action
              }}) {{ {self._ADVISORY_FIELDS} }}
            }}
            """,
            {"rule_key": rule_key, "topic": topic, "severity": severity, "title": title,
             "detail": detail, "data": data, "suggested_action": suggested_action},
            org_id=org_id,
        )
        return resp["insert_hospilot_app_advisories_one"]

    async def list_advisories(
        self, *, status: str | None = None, topic: str | None = None,
        limit: int = 50, org_id: str | None = None,
    ) -> list[dict]:
        where: dict = {}
        if status:
            where["status"] = {"_eq": status}
        if topic:
            where["topic"] = {"_eq": topic}
        data = await self.query(
            f"""
            query ListAdvisories($where: {{P}}hospilot_app_advisories_bool_exp!, $limit: Int!) {{
              hospilot_app_advisories: {{P}}hospilot_app_advisories(
                where: $where, order_by: {{created_at: desc}}, limit: $limit
              ) {{ {self._ADVISORY_FIELDS} }}
            }}
            """,
            {"where": where, "limit": limit},
            org_id=org_id,
        )
        return data.get("hospilot_app_advisories", [])

    async def list_advisories_since(
        self, since_iso: str, org_id: str | None = None, limit: int = 2000,
    ) -> list[dict]:
        """Slim fire history for the Executive meta-rules (SLA-breach count,
        per-topic KPI trend)."""
        data = await self.query(
            f"""
            query AdvisoriesSince($since: timestamptz!, $limit: Int!) {{
              hospilot_app_advisories: {{P}}hospilot_app_advisories(
                where: {{created_at: {{_gte: $since}}}}
                order_by: {{created_at: desc}}, limit: $limit
              ) {{ rule_key topic severity created_at }}
            }}
            """,
            {"since": since_iso, "limit": limit},
            org_id=org_id,
        )
        return data.get("hospilot_app_advisories", [])

    async def acknowledge_advisory(
        self, advisory_id: str, user_id: str, at_iso: str, org_id: str | None = None,
    ) -> dict | None:
        data = await self.mutate(
            f"""
            mutation AckAdvisory($id: uuid!, $user_id: uuid!, $at: timestamptz!) {{
              update_hospilot_app_advisories_by_pk: {{P}}update_hospilot_app_advisories_by_pk(
                pk_columns: {{id: $id}},
                _set: {{status: "acknowledged", acknowledged_by: $user_id, acknowledged_at: $at}}
              ) {{ {self._ADVISORY_FIELDS} }}
            }}
            """,
            {"id": advisory_id, "user_id": user_id, "at": at_iso},
            org_id=org_id,
        )
        return data.get("update_hospilot_app_advisories_by_pk")

    # =========================================================================
    # HOSPILOT -- System tables (direct Hasura -- computation tables)
    # =========================================================================

    async def create_session(
        self, session_id: str, goal: str, constraints: str, pipeline: dict,
        user_id: str | None = None, autonomous: bool = False,
        org_id: str | None = None, scheduled_query_id: str | None = None,
    ) -> None:
        # scheduled_query_id links a session back to the schedule that spawned it
        # (Phase 6 run history); null for ordinary ad-hoc sessions.
        await self.mutate(
            """
            mutation CreateSession(
              $id: uuid!, $goal: String!, $constraints: String!,
              $pipeline: jsonb!, $user_id: uuid, $autonomous: Boolean!,
              $scheduled_query_id: uuid
            ) {
              insert_hospilot_app_sessions_one: {P}insert_hospilot_app_sessions_one(object: {
                id: $id, goal: $goal, constraints: $constraints,
                status: "pending", pipeline: $pipeline, user_id: $user_id,
                autonomous: $autonomous, scheduled_query_id: $scheduled_query_id
              }) { id }
            }
            """,
            {"id": session_id, "goal": goal, "constraints": constraints,
             "pipeline": pipeline, "user_id": user_id, "autonomous": autonomous,
             "scheduled_query_id": scheduled_query_id},
            org_id=org_id,
        )

    async def update_session_status(
        self, session_id: str, status: str, pipeline_snapshot: dict | None = None,
        synthesis_result: dict | None = None, org_id: str | None = None,
    ) -> None:
        set_fields: dict = {"status": status}
        if pipeline_snapshot is not None:
            set_fields["pipeline_snapshot"] = pipeline_snapshot
        if synthesis_result is not None:
            set_fields["synthesis_result"] = synthesis_result
        await self.mutate(
            """
            mutation UpdateSession($id: uuid!, $set: {P}hospilot_app_sessions_set_input!) {
              update_hospilot_app_sessions_by_pk: {P}update_hospilot_app_sessions_by_pk(
                pk_columns: {id: $id}, _set: $set
              ) { id }
            }
            """,
            {"id": session_id, "set": set_fields},
            org_id=org_id,
        )

    async def update_session_pipeline(
        self, session_id: str, pipeline: dict, org_id: str | None = None,
    ) -> None:
        await self.mutate(
            """
            mutation UpdatePipeline($id: uuid!, $pipeline: jsonb!) {
              update_hospilot_app_sessions_by_pk: {P}update_hospilot_app_sessions_by_pk(
                pk_columns: {id: $id},
                _set: {pipeline: $pipeline, status: "pending", pipeline_snapshot: null}
              ) { id }
            }
            """,
            {"id": session_id, "pipeline": pipeline},
            org_id=org_id,
        )

    async def save_agent_overrides(
        self, session_id: str, agent_id: str, tasks: list, org_id: str | None = None,
    ) -> None:
        await self.mutate(
            """
            mutation SaveOverrides($session_id: uuid!, $agent_id: String!, $tasks: jsonb!) {
              insert_hospilot_app_session_agent_overrides_one: {P}insert_hospilot_app_session_agent_overrides_one(
                object: { session_id: $session_id, agent_id: $agent_id, tasks: $tasks }
                on_conflict: {
                  constraint: session_agent_overrides_session_id_agent_id_key
                  update_columns: [tasks]
                }
              ) { session_id }
            }
            """,
            {"session_id": session_id, "agent_id": agent_id, "tasks": tasks},
            org_id=org_id,
        )

    async def list_sessions(
        self, limit: int = 50, user_id: str | None = None, org_id: str | None = None,
    ) -> list[dict]:
        """List sessions in one org's tenant source, optionally per-user."""
        where: dict = {}
        if user_id:
            where["user_id"] = {"_eq": user_id}
        data = await self.query(
            """
            query ListSessions($limit: Int!, $where: {P}hospilot_app_sessions_bool_exp!) {
              hospilot_app_sessions: {P}hospilot_app_sessions(
                where: $where, order_by: {created_at: desc}, limit: $limit
              ) { id goal name status priority created_at updated_at autonomous user_id }
            }
            """,
            {"limit": limit, "where": where},
            org_id=org_id,
        )
        return data.get("hospilot_app_sessions", [])

    async def list_sessions_all_orgs(self, limit: int = 50) -> list[dict]:
        """super_admin view: sessions across every active org, newest first.
        One query per tenant source (small N; only super_admin pays this)."""
        if self._orgs is None:
            await self.load_org_registry()
        merged: list[dict] = []
        for org in self.active_orgs():
            try:
                rows = await self.list_sessions(limit=limit, org_id=org["id"])
            except Exception as exc:
                logger.warning("list_sessions_all_orgs: org %s failed: %s", org["slug"], exc)
                continue
            for r in rows:
                r["org_id"] = org["id"]
                r["org_name"] = org["name"]
            merged.extend(rows)
        merged.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return merged[:limit]

    async def get_session(self, session_id: str, org_id: str | None = None) -> dict | None:
        data = await self.query(
            """
            query GetSession($id: uuid!) {
              hospilot_app_sessions_by_pk: {P}hospilot_app_sessions_by_pk(id: $id) {
                id goal name constraints status priority autonomous user_id
                pipeline pipeline_snapshot synthesis_result created_at updated_at
              }
            }
            """,
            {"id": session_id},
            org_id=org_id,
        )
        return data.get("hospilot_app_sessions_by_pk")

    async def update_session_name(self, session_id: str, name: str, org_id: str | None = None) -> dict | None:
        """Rename a session (Workflows page). Empty/blank clears back to null so the
        UI falls back to its "New Workflow" default."""
        clean = (name or "").strip() or None
        data = await self.mutate(
            """
            mutation RenameSession($id: uuid!, $set: {P}hospilot_app_sessions_set_input!) {
              update_hospilot_app_sessions_by_pk: {P}update_hospilot_app_sessions_by_pk(
                pk_columns: {id: $id}, _set: $set
              ) { id name }
            }
            """,
            {"id": session_id, "set": {"name": clean}},
            org_id=org_id,
        )
        return data.get("update_hospilot_app_sessions_by_pk")

    async def create_approval_task(
        self, session_id: str, agent_id: str, action_type: str, payload: dict,
        idempotency_key: str | None = None,
        escalation_level: int = 0,
        kind: str = "approval",
        org_id: str | None = None,
    ) -> dict:
        # Idempotency (no-DDL): the key is stored as `_idem` inside the payload
        # jsonb. On a retry / node re-run we find the existing row and reuse it
        # instead of creating a duplicate approval. Safe because retries/re-runs
        # are sequential per session (no concurrent insert race).
        if idempotency_key:
            found = await self.query(
                """
                query FindApprovalByIdem($sid: uuid!, $contains: jsonb!) {
                  hospilot_app_approval_tasks: {P}hospilot_app_approval_tasks(
                    where: { session_id: {_eq: $sid}, payload: {_contains: $contains} }
                    limit: 1
                  ) { id session_id }
                }
                """,
                {"sid": session_id, "contains": {"_idem": idempotency_key}},
                org_id=org_id,
            )
            rows = found.get("hospilot_app_approval_tasks", [])
            if rows:
                logger.info("approval idempotent hit  key=%s  id=%s",
                            idempotency_key[:12], rows[0]["id"])
                return rows[0]
            payload = {**payload, "_idem": idempotency_key}
        data = await self.mutate(
            """
            mutation CreateApproval(
              $session_id: uuid!, $agent_id: String!,
              $action_type: String!, $payload: jsonb!, $escalation_level: Int!,
              $kind: String!
            ) {
              insert_hospilot_app_approval_tasks_one: {P}insert_hospilot_app_approval_tasks_one(object: {
                session_id: $session_id, agent_id: $agent_id,
                action_type: $action_type, payload: $payload, status: "pending",
                escalation_level: $escalation_level, kind: $kind
              }) { id session_id }
            }
            """,
            {"session_id": session_id, "agent_id": agent_id,
             "action_type": action_type, "payload": payload,
             "escalation_level": escalation_level, "kind": kind},
            org_id=org_id,
        )
        return data["insert_hospilot_app_approval_tasks_one"]

    async def decide_approval(
        self, approval_id: str, decision: str, approver_id: str,
        org_id: str | None = None,
    ) -> dict | None:
        """Decide a pending approval in the caller's tenant source.

        WHERE-based (not by_pk) so it only lands on rows that are still
        pending -- a second decide, or a decide against another org's source,
        matches nothing and returns None (route answers 404)."""
        data = await self.mutate(
            """
            mutation DecideApproval($id: uuid!, $decision: String!, $approver_id: String!) {
              update_hospilot_app_approval_tasks: {P}update_hospilot_app_approval_tasks(
                where: { id: {_eq: $id}, status: {_eq: "pending"} }
                _set: { status: $decision, decision: $decision,
                        approver_id: $approver_id, decided_at: "now()" }
              ) { returning { id session_id agent_id action_type } }
            }
            """,
            {"id": approval_id, "decision": decision, "approver_id": approver_id},
            org_id=org_id,
        )
        rows = data.get("update_hospilot_app_approval_tasks", {}).get("returning", [])
        return rows[0] if rows else None

    async def fetch_pending_approvals(
        self, session_id: str, org_id: str | None = None,
    ) -> list[dict]:
        """Pending rows of kind 'approval' only -- the Paused-queue bookkeeping kinds
        (user_paused / patient_identification / patient_registration / step_recommendation,
        see migration 042) are a park-state marker, not a decision for a human/the policy
        engine to make, and must never surface here as "waiting on approval"."""
        data = await self.query(
            """
            query PendingApprovals($session_id: uuid!) {
              hospilot_app_approval_tasks: {P}hospilot_app_approval_tasks(
                where: { session_id: { _eq: $session_id }, status: { _eq: "pending" }, kind: { _eq: "approval" } }
                order_by: { created_at: asc }
              ) { id agent_id action_type kind payload }
            }
            """,
            {"session_id": session_id},
            org_id=org_id,
        )
        return data.get("hospilot_app_approval_tasks", [])

    async def get_approval_task(
        self, approval_id: str, org_id: str | None = None,
    ) -> dict | None:
        """Single approval-task row by id (incl. the already-enriched `payload`), for
        trace enrichment -- turns a bare `approval_id` into "what was approved" (action
        type + patient/bed labels the creating activity stored in the payload). Returns
        None when not found or on any query error; callers must tolerate None."""
        try:
            data = await self.query(
                """
                query ApprovalTask($id: uuid!) {
                  hospilot_app_approval_tasks: {P}hospilot_app_approval_tasks(
                    where: { id: { _eq: $id } }
                    limit: 1
                  ) { id agent_id action_type kind payload status }
                }
                """,
                {"id": approval_id},
                org_id=org_id,
            )
        except Exception:  # noqa: BLE001 -- best-effort; enrichment must never break the run
            return None
        rows = data.get("hospilot_app_approval_tasks", [])
        return rows[0] if rows else None

    async def list_pending_approvals(self, org_id: str | None = None) -> list[dict]:
        """All pending rows of kind 'approval' in one org's tenant source
        (backs GET /api/approvals/pending)."""
        data = await self.query(
            """
            query AllPendingApprovals {
              hospilot_app_approval_tasks: {P}hospilot_app_approval_tasks(
                where: {status: {_eq: "pending"}, kind: {_eq: "approval"}},
                order_by: {created_at: desc}
              ) {
                id session_id agent_id action_type payload
                status created_at escalation_level
              }
            }
            """,
            {},
            org_id=org_id,
        )
        return data.get("hospilot_app_approval_tasks", [])

    async def list_pending_approvals_all_orgs(self) -> list[dict]:
        """super_admin view: pending approvals across every active org."""
        if self._orgs is None:
            await self.load_org_registry()
        merged: list[dict] = []
        for org in self.active_orgs():
            try:
                rows = await self.list_pending_approvals(org_id=org["id"])
            except Exception as exc:
                logger.warning("pending_approvals_all_orgs: org %s failed: %s",
                               org["slug"], exc)
                continue
            for r in rows:
                r["org_id"] = org["id"]
                r["org_name"] = org["name"]
            merged.extend(rows)
        merged.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return merged

    async def fetch_paused_queue(self, org_id: str | None = None) -> list[dict]:
        """Every pending approval-task row across all sessions, of every `kind`
        (approval / patient_identification / patient_registration / user_paused / ...).

        Backs the Paused queue (GET /api/queues/paused). Generalises the session-scoped
        fetch_pending_approvals to the whole system; the route enriches these with
        session goal / autonomous / display-name via get_sessions_min +
        get_sessions_user_info (no N+1). Routed to one org's tenant source."""
        data = await self.query(
            """
            query PausedQueue {
              hospilot_app_approval_tasks: {P}hospilot_app_approval_tasks(
                where: { status: { _eq: "pending" } }
                order_by: { created_at: asc }
              ) { id session_id agent_id action_type kind payload status created_at escalation_level }
            }
            """,
            {},
            org_id=org_id,
        )
        return data.get("hospilot_app_approval_tasks", [])

    async def resolve_approval_tasks(
        self, session_id: str, kind: str | None = None,
        status: str = "resolved", decision: str | None = None,
        org_id: str | None = None,
    ) -> int:
        """Un-pend every pending approval-task row for a session, optionally scoped to one
        `kind`. Used when a paused/parked flow leaves the Paused queue: patient rows on
        resume, user_paused rows on resume (status="resolved"), and all rows on cancel
        (status="cancelled"). `status` must be an allowed terminal value (approval_tasks
        CHECK: pending|approved|rejected|resolved|cancelled); `decision` is free text for
        the human-readable reason. Returns the number of rows affected."""
        where: dict = {"session_id": {"_eq": session_id}, "status": {"_eq": "pending"}}
        if kind is not None:
            where["kind"] = {"_eq": kind}
        set_fields: dict = {"status": status, "decided_at": "now()"}
        if decision is not None:
            set_fields["decision"] = decision
        data = await self.mutate(
            """
            mutation ResolveApprovals(
              $where: {P}hospilot_app_approval_tasks_bool_exp!,
              $set: {P}hospilot_app_approval_tasks_set_input!
            ) {
              update_hospilot_app_approval_tasks: {P}update_hospilot_app_approval_tasks(
                where: $where, _set: $set
              ) { affected_rows }
            }
            """,
            {"where": where, "set": set_fields},
            org_id=org_id,
        )
        return data.get("update_hospilot_app_approval_tasks", {}).get("affected_rows", 0)

    async def write_audit(
        self, session_id: str, agent_id: str, event_type: str, payload: dict,
        idempotency_key: str | None = None, org_id: str | None = None,
    ) -> None:
        # Same no-DDL idempotency as create_approval_task: skip the insert if an
        # audit row with this key already exists for the session.
        if idempotency_key:
            found = await self.query(
                """
                query FindAuditByIdem($sid: uuid!, $contains: jsonb!) {
                  hospilot_app_audit_log: {P}hospilot_app_audit_log(
                    where: { session_id: {_eq: $sid}, payload: {_contains: $contains} }
                    limit: 1
                  ) { id }
                }
                """,
                {"sid": session_id, "contains": {"_idem": idempotency_key}},
                org_id=org_id,
            )
            if found.get("hospilot_app_audit_log"):
                return
            payload = {**payload, "_idem": idempotency_key}
        await self.mutate(
            """
            mutation WriteAudit(
              $session_id: uuid!, $agent_id: String!,
              $event_type: String!, $payload: jsonb!
            ) {
              insert_hospilot_app_audit_log_one: {P}insert_hospilot_app_audit_log_one(object: {
                session_id: $session_id, agent_id: $agent_id,
                event_type: $event_type, payload: $payload
              }) { id }
            }
            """,
            {"session_id": session_id, "agent_id": agent_id,
             "event_type": event_type, "payload": payload},
            org_id=org_id,
        )

    async def list_reorchestration_feedback(
        self, session_id: str, org_id: str | None = None,
    ) -> list[str]:
        """Every past reorchestration feedback for this session, oldest first.

        Reorchestration is a regeneration, not a patch -- each round replans from the
        goal, so the goal string is the ONLY carrier of earlier user revisions. Without
        replaying this history round N would silently undo rounds 1..N-1 (e.g. "ICU is
        at 57%" reverting to the original 92%). Read from audit_log so the history is
        durable across restarts and needs no schema change."""
        data = await self.query(
            """
            query ReorchFeedback($sid: uuid!) {
              hospilot_app_audit_log: {P}hospilot_app_audit_log(
                where: { session_id: {_eq: $sid}, event_type: {_eq: "reorchestrated"} }
                order_by: { created_at: asc }
              ) { payload }
            }
            """,
            {"sid": session_id},
            org_id=org_id,
        )
        out: list[str] = []
        for row in data.get("hospilot_app_audit_log") or []:
            fb = (row.get("payload") or {}).get("feedback")
            if isinstance(fb, str) and fb.strip() and (not out or out[-1] != fb.strip()):
                out.append(fb.strip())
        return out

    # =========================================================================
    # RAG CONVERSATION MEMORY -- conversation storage + cross-session facts
    # (per-tenant hospilot_app tables; migration 053). Backs POST /api/ask.
    # =========================================================================

    async def create_conversation(
        self, user_id: str | None, title: str | None = None, org_id: str | None = None,
    ) -> str:
        data = await self.mutate(
            """
            mutation CreateConversation($user_id: uuid, $title: String) {
              insert_rag_conversation_one: {P}insert_hospilot_app_rag_conversation_one(object: {
                user_id: $user_id, title: $title
              }) { id }
            }
            """,
            {"user_id": user_id, "title": title},
            org_id=org_id,
        )
        return data["insert_rag_conversation_one"]["id"]

    async def get_conversation(
        self, conversation_id: str, org_id: str | None = None,
    ) -> dict | None:
        data = await self.query(
            """
            query GetConversation($id: uuid!) {
              rag_conversation_by_pk: {P}hospilot_app_rag_conversation_by_pk(id: $id) {
                id user_id title running_summary summary_through_seq created_at
              }
            }
            """,
            {"id": conversation_id},
            org_id=org_id,
        )
        return data.get("rag_conversation_by_pk")

    async def list_conversations(
        self, user_id: str, limit: int = 50, org_id: str | None = None,
    ) -> list[dict]:
        """A user's conversations in their tenant source, most-recently-active first."""
        data = await self.query(
            """
            query ListConversations($uid: uuid!, $limit: Int!) {
              rag_conversation: {P}hospilot_app_rag_conversation(
                where: { user_id: { _eq: $uid } }
                order_by: { updated_at: desc }, limit: $limit
              ) { id title created_at updated_at }
            }
            """,
            {"uid": user_id, "limit": limit},
            org_id=org_id,
        )
        return data.get("rag_conversation", [])

    async def list_conversation_messages(
        self, conversation_id: str, org_id: str | None = None,
    ) -> list[dict]:
        """Every turn of a conversation, oldest-first (for replaying a thread in the UI)."""
        data = await self.query(
            """
            query ConversationMessages($cid: uuid!) {
              rag_message: {P}hospilot_app_rag_message(
                where: { conversation_id: { _eq: $cid } }
                order_by: { seq: asc }
              ) { seq role content sql mode row_count created_at }
            }
            """,
            {"cid": conversation_id},
            org_id=org_id,
        )
        return data.get("rag_message", [])

    async def get_max_message_seq(
        self, conversation_id: str, org_id: str | None = None,
    ) -> int:
        """Highest message seq in a conversation (0 if none) -- callers add 1 for the next turn."""
        data = await self.query(
            """
            query MaxSeq($cid: uuid!) {
              agg: {P}hospilot_app_rag_message_aggregate(
                where: { conversation_id: { _eq: $cid } }
              ) { aggregate { max { seq } } }
            }
            """,
            {"cid": conversation_id},
            org_id=org_id,
        )
        mx = (((data.get("agg") or {}).get("aggregate") or {}).get("max") or {}).get("seq")
        return mx or 0

    async def append_message(
        self, conversation_id: str, seq: int, role: str, content: str,
        sql: str | None = None, mode: str | None = None, row_count: int | None = None,
        org_id: str | None = None,
    ) -> str:
        data = await self.mutate(
            """
            mutation AppendMessage(
              $conversation_id: uuid!, $seq: Int!, $role: String!, $content: String!,
              $sql: String, $mode: String, $row_count: Int
            ) {
              insert_rag_message_one: {P}insert_hospilot_app_rag_message_one(object: {
                conversation_id: $conversation_id, seq: $seq, role: $role, content: $content,
                sql: $sql, mode: $mode, row_count: $row_count
              }) { id }
            }
            """,
            {"conversation_id": conversation_id, "seq": seq, "role": role,
             "content": content, "sql": sql, "mode": mode, "row_count": row_count},
            org_id=org_id,
        )
        return data["insert_rag_message_one"]["id"]

    async def list_recent_messages(
        self, conversation_id: str, limit: int = 6, org_id: str | None = None,
    ) -> list[dict]:
        """The most recent `limit` turns, returned oldest-first (chronological)."""
        data = await self.query(
            """
            query RecentMessages($cid: uuid!, $limit: Int!) {
              rag_message: {P}hospilot_app_rag_message(
                where: { conversation_id: { _eq: $cid } }
                order_by: { seq: desc }, limit: $limit
              ) { seq role content sql mode row_count }
            }
            """,
            {"cid": conversation_id, "limit": limit},
            org_id=org_id,
        )
        rows = data.get("rag_message", [])
        return list(reversed(rows))

    async def list_messages_after_seq(
        self, conversation_id: str, after_seq: int, org_id: str | None = None,
    ) -> list[dict]:
        """All turns with seq > after_seq, oldest-first (what still needs summarising)."""
        data = await self.query(
            """
            query MessagesAfter($cid: uuid!, $after: Int!) {
              rag_message: {P}hospilot_app_rag_message(
                where: { conversation_id: { _eq: $cid }, seq: { _gt: $after } }
                order_by: { seq: asc }
              ) { seq role content }
            }
            """,
            {"cid": conversation_id, "after": after_seq},
            org_id=org_id,
        )
        return data.get("rag_message", [])

    async def update_running_summary(
        self, conversation_id: str, summary: str, through_seq: int,
        org_id: str | None = None,
    ) -> None:
        await self.mutate(
            """
            mutation UpdateSummary($id: uuid!, $summary: String!, $through: Int!) {
              update_rag_conversation_by_pk: {P}update_hospilot_app_rag_conversation_by_pk(
                pk_columns: { id: $id },
                _set: { running_summary: $summary, summary_through_seq: $through }
              ) { id }
            }
            """,
            {"id": conversation_id, "summary": summary, "through": through_seq},
            org_id=org_id,
        )

    async def get_user_memories(
        self, user_id: str, limit: int = 30, org_id: str | None = None,
    ) -> list[dict]:
        data = await self.query(
            """
            query UserMemories($uid: uuid!, $limit: Int!) {
              rag_memory: {P}hospilot_app_rag_memory(
                where: { user_id: { _eq: $uid } }
                order_by: { salience: desc, updated_at: desc }, limit: $limit
              ) { id kind content salience embedding embedding_model embedding_dim }
            }
            """,
            {"uid": user_id, "limit": limit},
            org_id=org_id,
        )
        return data.get("rag_memory", [])

    async def replace_user_memories(
        self, user_id: str, rows: list[dict], org_id: str | None = None,
    ) -> int:
        """Replace a user's cross-session facts with the consolidated set langmem returned.

        langmem's memory manager returns the FULL post-consolidation active set
        (inserts + updates, minus removals), so a delete-then-insert is the exact
        mapping and avoids per-row id reconciliation. Runs in the background."""
        objects = [{
            "user_id": user_id,
            "kind": (r.get("kind") or "semantic"),
            "content": (r.get("content") if isinstance(r.get("content"), dict)
                        else {"text": r.get("content")}),
            "salience": float(r.get("salience") or 0),
            # Embedding is optional: null when OpenAI is unset or the embed call
            # failed for this fact (retrieval then falls back to recency order).
            "embedding": r.get("embedding"),
            "embedding_model": r.get("embedding_model"),
            "embedding_dim": r.get("embedding_dim"),
        } for r in rows]
        data = await self.mutate(
            """
            mutation ReplaceMemories(
              $uid: uuid!, $objects: [{P}hospilot_app_rag_memory_insert_input!]!
            ) {
              del: {P}delete_hospilot_app_rag_memory(where: { user_id: { _eq: $uid } }) { affected_rows }
              ins: {P}insert_hospilot_app_rag_memory(objects: $objects) { affected_rows }
            }
            """,
            {"uid": user_id, "objects": objects},
            org_id=org_id,
        )
        return (data.get("ins") or {}).get("affected_rows", 0)

    async def create_billing_requests(self, session_id: str, items: list[dict]) -> list[dict]:
        """Insert staged bill-generation requests into hospilot.billing_requests.

        Called from commit_session (the HIS push). Each row lands as status
        'pending'; the DB side polls these and turns them into actual bills,
        writing back invoice_id / status. We pass only patient + encounter
        references -- never fabricated line items or amounts."""
        if not items:
            return []
        rows = [{
            "session_id":            session_id,
            "agent_id":              it.get("agent_id") or "revenue_agent",
            "patient_token":         it.get("patient_token"),
            "patient_name":          it.get("patient_name"),
            "uhid":                  it.get("uhid"),
            "visit_id":              it.get("visit_id"),
            "admission_id":          it.get("admission_id"),
            "invoice_type":          it.get("invoice_type") or "IPD",
            "generate_from_charges": bool(it.get("generate_from_charges", True)),
            "source":                it.get("source") or "initiate_billing",
            "status":                "pending",
        } for it in items]
        data = await self.mutate(
            """
            mutation CreateBillingRequests($rows: [hospilot_billing_requests_insert_input!]!) {
              insert_hospilot_billing_requests(objects: $rows) {
                returning { id patient_token status }
              }
            }
            """,
            {"rows": rows},
        )
        return data["insert_hospilot_billing_requests"]["returning"]

    # =========================================================================
    # AGENT REGISTRY -- agents / subagents / tasks
    # =========================================================================

    async def fetch_agent_registry(self) -> list[dict]:
        data = await self.query("""
            query GetAgentRegistry {
              hospilot_app_agent_registry(
                where: { is_active: { _eq: true } }
                order_by: { sort_order: asc }
              ) {
                id label description emoji color
                subagents: subagent_registries(
                  where: { is_active: { _eq: true } }
                  order_by: { sort_order: asc }
                ) {
                  id label description capabilities is_prefetch_eligible
                  tasks: task_registries(
                    where: { is_active: { _eq: true } }
                    order_by: { sort_order: asc }
                  ) {
                    id label outputs
                  }
                }
              }
            }
        """)
        rows = data.get("hospilot_app_agent_registry", [])
        # Strip internal codegen tasks (exec__*) from the plan-facing catalog.
        # unified_executor persists generated functions to task_registry under a
        # real sub-agent (e.g. exec__analyze_staffing -> sa_ratio_monitor), but
        # these are NOT real plan tasks -- if surfaced they leak into plans as
        # phantom tasks with empty outputs (planner-query-gaps G12). The codegen
        # path loads them by pk (fetch_function_code), not via this query, so
        # filtering here is safe. This is the single chokepoint feeding the
        # planner (_build_registry_from_rows / plan_subagent_tasks) and the
        # generic registry body (fetch_agent_catalog).
        for agent_row in rows:
            for sa_row in agent_row.get("subagents", []):
                tasks = sa_row.get("tasks")
                if tasks:
                    sa_row["tasks"] = [t for t in tasks if not str(t.get("id", "")).startswith("exec__")]
        return rows

    async def insert_task_to_registry(
        self,
        task_id: str,
        subagent_id: str,
        label: str,
        description: str,
        outputs: list,
        function_code: str = "",
    ) -> dict:
        data = await self.mutate(
            """
            mutation InsertDynamicTask(
              $id: String!, $subagent_id: String!,
              $label: String!, $description: String!, $outputs: jsonb!,
              $function_code: String!
            ) {
              insert_hospilot_app_task_registry_one(object: {
                id: $id
                subagent_id: $subagent_id
                label: $label
                description: $description
                outputs: $outputs
                function_code: $function_code
                is_dynamic: true
                is_active: true
                sort_order: 9999
              }) { id subagent_id label description outputs is_dynamic function_code }
            }
            """,
            {
                "id": task_id,
                "subagent_id": subagent_id,
                "label": label,
                "description": description,
                "outputs": outputs,
                "function_code": function_code,
            },
        )
        return data.get("insert_hospilot_app_task_registry_one", {})

    async def deactivate_task_in_registry(self, task_id: str) -> dict | None:
        # Soft delete -- matches insert's is_active-filtered visibility (see
        # fetch_agent_registry / fetch_dynamic_tasks). The task disappears from the
        # catalog and future plans immediately; historical sessions that already
        # referenced it in a saved pipeline snapshot are unaffected.
        data = await self.mutate(
            """
            mutation DeactivateTask($id: String!) {
              update_hospilot_app_task_registry_by_pk(
                pk_columns: { id: $id }
                _set: { is_active: false }
              ) { id }
            }
            """,
            {"id": task_id},
        )
        return data.get("update_hospilot_app_task_registry_by_pk")

    async def insert_subagent_to_registry(
        self,
        subagent_id: str,
        agent_id: str,
        label: str,
        description: str,
        capabilities: list | None = None,
        is_prefetch_eligible: bool = False,
    ) -> dict:
        # New sub-agents sort to the end (sort_order 9999) and start with no tasks.
        data = await self.mutate(
            """
            mutation InsertSubAgent(
              $id: String!, $agent_id: String!,
              $label: String!, $description: String!,
              $capabilities: jsonb!, $is_prefetch_eligible: Boolean!
            ) {
              insert_hospilot_app_subagent_registry_one(object: {
                id: $id
                agent_id: $agent_id
                label: $label
                description: $description
                capabilities: $capabilities
                is_prefetch_eligible: $is_prefetch_eligible
                is_active: true
                sort_order: 9999
              }) { id label description capabilities is_prefetch_eligible }
            }
            """,
            {
                "id": subagent_id,
                "agent_id": agent_id,
                "label": label,
                "description": description,
                "capabilities": capabilities or [],
                "is_prefetch_eligible": is_prefetch_eligible,
            },
        )
        return data.get("insert_hospilot_app_subagent_registry_one", {})

    async def deactivate_subagent_in_registry(self, subagent_id: str) -> dict | None:
        # Soft delete the sub-agent AND cascade-deactivate its tasks in one mutation,
        # so no active task is left orphaned under a hidden sub-agent. Matches the
        # is_active-filtered visibility used everywhere else (fetch_agent_registry).
        data = await self.mutate(
            """
            mutation DeactivateSubAgent($id: String!) {
              subagent: update_hospilot_app_subagent_registry_by_pk(
                pk_columns: { id: $id }
                _set: { is_active: false }
              ) { id }
              tasks: update_hospilot_app_task_registry(
                where: { subagent_id: { _eq: $id } }
                _set: { is_active: false }
              ) { affected_rows }
            }
            """,
            {"id": subagent_id},
        )
        return data.get("subagent")

    async def fetch_all_function_codes(self) -> list[dict]:
        data = await self.query(
            """
            query GetFunctionCodes {
              hospilot_app_task_registry(
                where: {
                  is_dynamic:     { _eq: true }
                  is_active:      { _eq: true }
                  function_code:  { _is_null: false, _neq: "" }
                }
                order_by: { sort_order: asc }
              ) { id function_code }
            }
            """
        )
        return data.get("hospilot_app_task_registry", [])

    async def upsert_executor_code(
        self,
        task_id: str,
        subagent_id: str,
        function_code: str,
        label: str = "",
        description: str = "",
    ) -> None:
        # label/description are user-facing (shown in the plan UI), so fall back to a
        # readable form of the task_id rather than the raw "exec__..." id when not given.
        readable = task_id.removeprefix("exec__").replace("_", " ").strip().title()
        await self.mutate(
            """
            mutation UpsertExecutorCode(
              $id: String!, $subagent_id: String!, $code: String!,
              $label: String!, $description: String!
            ) {
              insert_hospilot_app_task_registry_one(object: {
                id:            $id
                subagent_id:   $subagent_id
                label:         $label
                description:   $description
                outputs:       []
                function_code: $code
                is_dynamic:    false
                is_active:     true
                sort_order:    9999
              }, on_conflict: {
                constraint:    task_registry_pkey
                update_columns: [function_code, label, description]
              }) { id }
            }
            """,
            {
                "id": task_id,
                "subagent_id": subagent_id,
                "code": function_code,
                "label": label or readable,
                "description": description or readable,
            },
        )

    async def fetch_function_code(self, task_id: str) -> dict | None:
        data = await self.query(
            """
            query GetFunctionCode($id: String!) {
              hospilot_app_task_registry_by_pk(id: $id) { id function_code }
            }
            """,
            {"id": task_id},
        )
        return data.get("hospilot_app_task_registry_by_pk")

    async def fetch_dynamic_tasks(self, subagent_id: str) -> list[dict]:
        data = await self.query(
            """
            query GetDynamicTasks($subagent_id: String!) {
              hospilot_app_task_registry(
                where: {
                  subagent_id: { _eq: $subagent_id }
                  is_dynamic:  { _eq: true }
                  is_active:   { _eq: true }
                }
                order_by: { sort_order: asc }
              ) {
                id label outputs
              }
            }
            """,
            {"subagent_id": subagent_id},
        )
        return data.get("hospilot_app_task_registry", [])

    # =========================================================================
    # CLINICAL DATA -- reads and writes via Fabric
    # =========================================================================

    async def get_enriched_beds(self) -> list[dict]:
        return await fget("/beds")

    async def get_departments(self) -> list[dict]:
        return await fget("/departments")

    async def get_available_icu_beds(self) -> list[dict]:
        return await fget("/beds/available-icu")

    async def get_dirty_icu_beds(self) -> list[dict]:
        return await fget("/beds/dirty-icu")

    async def get_dirty_beds(self) -> list[dict]:
        return await fget("/beds/dirty")

    async def get_available_postop_beds(self) -> list[dict]:
        return await fget("/beds/postop")

    async def get_beds_summary(self) -> dict:
        return await fget("/beds/summary")

    async def get_icu_admissions(self) -> list[dict]:
        return await fget("/admissions/icu")

    async def get_non_icu_admissions(self) -> list[dict]:
        return await fget("/admissions/non-icu")

    async def get_admissions_with_wards(self) -> list[dict]:
        return await fget("/admissions/with-wards")

    async def get_discharge_eligible_admissions(self) -> list[dict]:
        return await fget("/admissions/discharge-eligible")

    async def get_discharge_ready_with_summaries(self) -> list[dict]:
        return await fget("/admissions/discharge-ready")

    async def get_discharge_ready_count(self) -> int:
        data = await fget("/admissions/discharge-ready-count")
        return data.get("count", 0) if isinstance(data, dict) else 0

    async def get_discharge_horizon(self, hours: int) -> int:
        data = await fget("/admissions/discharge-horizon", hours=hours)
        return data.get("count", 0) if isinstance(data, dict) else 0

    async def get_latest_vitals(self, patient_token: str) -> dict | None:
        return await fget("/vitals/latest", patient=patient_token)

    async def get_latest_vitals_bulk(self, patient_tokens: list[str]) -> dict[str, dict]:
        """{token: latest vitals} for many patients in ONE Fabric call.

        Replaces N calls to /vitals/latest. Fabric can only do this as an
        unfiltered read (the upstream FHIR server scopes Observations by a single
        `patient` param and supports no batch form), and that read cannot be
        paged -- so it reports `complete`. When it comes back False the map is a
        prefix and we must not present it as the whole truth: fall back to
        per-patient reads for whatever is missing, which is slow but correct.
        Vitals decide escalation, so a silently-absent critical reading is the
        one outcome worth paying for."""
        if not patient_tokens:
            return {}
        try:
            resp = await fget("/vitals/latest-bulk", patients=",".join(patient_tokens))
        except Exception as exc:  # noqa: BLE001 -- bulk is an optimization, never a hard dep
            logger.warning("bulk vitals failed, falling back to per-patient: %s", exc)
            resp = None

        out: dict[str, dict] = {}
        complete = False
        if isinstance(resp, dict):
            out = {t: v for t, v in (resp.get("vitals") or {}).items() if v}
            complete = bool(resp.get("complete"))

        missing = [t for t in patient_tokens if t not in out]
        # A truncated read means "absent" is not trustworthy, so re-check every
        # missing token. A complete read means absent == genuinely no vitals, and
        # re-fetching those would put back the N calls this exists to remove.
        if missing and not complete:
            logger.warning("bulk vitals incomplete -- per-patient fallback for %d token(s)",
                           len(missing))
            sem = asyncio.Semaphore(10)

            async def _one(tok: str) -> None:
                async with sem:
                    try:
                        v = await fget("/vitals/latest", patient=tok)
                        if v:
                            out[tok] = v
                    except Exception:  # noqa: BLE001
                        pass

            await asyncio.gather(*[_one(t) for t in missing])
        return out

    async def get_vital_observation(self, observation_id: str) -> dict | None:
        try:
            return await fget(f"/vitals/observations/{observation_id}")
        except Exception:
            return None

    async def get_critical_vitals(self) -> list[dict]:
        return await fget("/vitals/critical")

    async def get_untriaged_visits(self) -> list[dict]:
        return await fget("/visits/untriaged")

    async def get_er_visits(self) -> list[dict]:
        return await fget("/visits/er")

    async def get_active_er_visits(self) -> list[dict]:
        return await fget("/visits/er")

    async def get_er_pressure(self) -> dict:
        return await fget("/er/pressure")

    async def get_all_incomplete_tasks(self) -> list[dict]:
        return await fget("/tasks/incomplete")

    async def get_overdue_nursing_tasks(self) -> list[dict]:
        return await fget("/tasks/overdue")

    async def get_pending_nursing_tasks(self, admission_id: str) -> list[dict]:
        return await fget("/tasks", admission=admission_id)

    async def get_completed_nursing_task_count(self, admission_id: str) -> int:
        data = await fget("/tasks/completed-count", admission=admission_id)
        return data.get("count", 0) if isinstance(data, dict) else 0

    async def get_pending_lab_orders(self) -> list[dict]:
        return await fget("/labs/orders/pending")

    async def get_recent_lab_results(self, hours: int = 24) -> list[dict]:
        return await fget("/labs/results")

    async def get_patient_names(self, patient_tokens: list[str]) -> dict[str, dict]:
        if not patient_tokens:
            return {}
        data = await fget("/patients", ids=",".join(patient_tokens))
        return data if isinstance(data, dict) else {}

    async def get_patient_by_mobile(self, mobile: str) -> dict | None:
        """Resolve a patient by mobile number via Fabric's /patients/by-mobile.
        Returns {patient_token, first_name, last_name, uhid, current_visit_id} when a
        patient exists, else None. Fabric normalises the number (any format)."""
        data = await fget("/patients/by-mobile", mobile=mobile)
        if isinstance(data, dict) and data.get("exists"):
            return data
        return None

    async def request_patient_registration(self, body: dict) -> dict:
        """Ask Fabric to register a NEW patient that has no record yet.

        Contract -- POST /patients/register with:
          {mobile, name_hint?, session_id, source}
        Fabric forwards the request to the DB side, where the hospital staff create
        the patient MANUALLY (so it is asynchronous and may take a while). Fabric
        returns an immediate ack, e.g. {"request_id": "...", "status": "pending"}.
        The newly-created patient is reported back later as a `patient` Kafka data
        event (messaging.data_consumer), which resumes the paused flow -- see
        workflows.graph.patient.register_patients."""
        return await fpost("/patients/register", body)

    async def get_long_wait_er_visits(self, minutes: int = 120) -> list[dict]:
        from datetime import datetime, timezone, timedelta
        visits = await fget("/visits/er")
        if not visits:
            return []
        threshold = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        result = []
        for v in visits:
            arrived = v.get("arrived_at") or v.get("visit_date") or ""
            try:
                dt = datetime.fromisoformat(arrived.replace("Z", "+00:00"))
                if dt <= threshold:
                    result.append(v)
            except Exception:
                pass
        return sorted(result, key=lambda x: x.get("arrived_at", ""))

    # -- Writes ----------------------------------------------------------------

    async def update_bed_status(self, bed_id: str, status: str) -> None:
        await fpost(f"/beds/{bed_id}/status", {"status": status})

    async def update_discharge_ready(
        self, admission_id: str, ready: bool, blocked_reason: str | None = None
    ) -> None:
        await fpost(
            f"/admissions/{admission_id}/discharge-ready",
            {"ready": ready, "blocked_reason": blocked_reason},
        )

    async def set_admissions_transfer_pending(self, admission_ids: list) -> None:
        if not admission_ids:
            return
        await fpost("/admissions/transfer-pending", {"ids": admission_ids})

    async def flag_critical_vitals(self, vital_id: str) -> None:
        await fpost(f"/vitals/{vital_id}/critical")

    async def set_triage_score(self, visit_id: str, score: int) -> None:
        await fpost(f"/visits/{visit_id}/triage", {"score": score})

    async def bulk_set_triage_scores(self, items: list[dict]) -> None:
        await fpost("/visits/triage/bulk", {"items": items})

    async def set_ai_discharge_note(self, admission_id: str, note: str) -> None:
        await fpost(f"/discharge-summaries/{admission_id}/ai-note", {"note": note})

    # =========================================================================
    # FINANCIAL DATA -- via Fabric /financial
    # =========================================================================

    async def get_outstanding_invoices(self) -> list[dict]:
        return await fget("/financial/invoices", payment_status="Unpaid,Partial")

    async def get_todays_collections(self) -> dict | None:
        from datetime import date
        return await fget(f"/financial/collections/{date.today().isoformat()}")

    async def get_yesterday_collections(self) -> dict | None:
        from datetime import date, timedelta
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        return await fget(f"/financial/collections/{yesterday}")

    async def get_patient_invoices(self, patient_token: str) -> list[dict]:
        return await fget("/financial/invoices", patient=patient_token)

    async def get_patient_claims(self, visit_ids: list) -> list[dict]:
        if not visit_ids:
            return []
        return await fget("/financial/claims", visit_id=",".join(str(v) for v in visit_ids))

    async def carerOS_get_claims(self) -> list[dict]:
        return await fget("/financial/claims")

    async def carerOS_get_daily_collections(self) -> list[dict]:
        from datetime import date
        result = await fget(f"/financial/collections/{date.today().isoformat()}")
        return [result] if result else []

    # =========================================================================
    # NOT YET IN FABRIC -- direct Hasura (section 7 of INTEGRATION.md)
    # Keep these until the DB exposes them via FHIR/REST endpoints.
    # =========================================================================

    async def get_critical_escalation_backlog(self) -> int:
        data = await self.query(
            """
            query CriticalBacklog {
              hospilot_vitals_aggregate(where: {
                is_critical: {_eq: true}
              }) { aggregate { count } }
            }
            """
        )
        return data["hospilot_vitals_aggregate"]["aggregate"]["count"] or 0

    async def carerOS_get_discharge_summaries(self) -> list[dict]:
        data = await self.query(
            """
            query GetDischargeSummaries {
              hospilot_discharge_summaries(order_by: {created_at: desc}, limit: 200) {
                id admission_id summary_text created_at
              }
            }
            """
        )
        return data.get("hospilot_discharge_summaries", [])

    async def get_active_infection_cases(self) -> list[dict]:
        data = await self.query(
            """
            query GetActiveInfectionCases {
              hospilot_infection_cases(where: {status: {_eq: "active"}}) {
                id patient_token admission_id ward pathogen severity
                isolation_required isolation_confirmed isolation_room
                status reported_at notes
              }
            }
            """
        )
        return data.get("hospilot_infection_cases", [])

    async def get_non_isolated_infection_cases(self) -> list[dict]:
        data = await self.query(
            """
            query GetNonIsolatedCases {
              hospilot_infection_cases(where: {
                status: {_eq: "active"}
                isolation_required: {_eq: true}
                isolation_confirmed: {_eq: false}
              }) {
                id patient_token ward pathogen severity reported_at notes
              }
            }
            """
        )
        return data.get("hospilot_infection_cases", [])

    async def get_all_supplies(self) -> list[dict]:
        data = await self.query(
            """
            query GetAllSupplies {
              hospilot_supplies {
                id item_code item_name category
                current_stock min_stock unit unit_cost
                last_ordered_at last_received_at
              }
            }
            """
        )
        return data.get("hospilot_supplies", [])

    async def carerOS_get_ot_surgeries(self) -> list[dict]:
        data = await self.query(
            """
            query GetOTSurgeries {
              hospilot_ot_surgeries(where: {status: {_neq: "Completed"}}) {
                id admission_id patient_token ward status priority synced_at
              }
            }
            """
        )
        return data.get("hospilot_ot_surgeries", [])

    async def carerOS_get_purchase_orders(self) -> list[dict]:
        data = await self.query(
            """
            query GetPurchaseOrders {
              hospilot_purchase_orders(where: {status: {_in: ["Pending Approval", "Approved"]}}) {
                id po_number vendor_id status total
                order_date expected_delivery
              }
            }
            """
        )
        return data.get("hospilot_purchase_orders", [])

    async def get_recently_discharged_beds(self) -> list[dict]:
        data = await self.query(
            """
            query RecentlyDischargedBeds {
              hospilot_ipd_admissions(
                where: {discharge_ready: {_eq: true}},
                order_by: {admitted_at: desc},
                limit: 20
              ) {
                id bed_id patient_token admitted_at
              }
            }
            """
        )
        admissions = data.get("hospilot_ipd_admissions", [])
        beds = []
        for a in admissions:
            if a.get("bed_id"):
                beds.append({
                    "id":            a["bed_id"],
                    "admission_id":  a["id"],
                    "patient_token": a.get("patient_token"),
                })
        return beds

    # =========================================================================
    # FHIR OUTBOUND -- read-only lab access for the /fhir Observation endpoint
    # =========================================================================

    async def fhir_get_lab_results(
        self, patient_token: str | None = None, test_code: str | None = None, limit: int = 200
    ) -> list[dict]:
        where: dict = {}
        if patient_token:
            where["patient_token"] = {"_eq": patient_token}
        if test_code:
            where["test_code"] = {"_eq": test_code}
        data = await self.query(
            """
            query FhirLabResults($where: hospilot_lab_results_bool_exp!, $limit: Int!) {
              hospilot_lab_results(where: $where, order_by: {reported_at: desc}, limit: $limit) {
                id order_id patient_token test_name test_code
                result_value flag reference_range unit reported_at
              }
            }
            """,
            {"where": where, "limit": limit},
        )
        return data.get("hospilot_lab_results", [])

    async def fhir_get_lab_result_by_id(self, result_id: str) -> dict | None:
        data = await self.query(
            """
            query FhirLabResult($id: uuid!) {
              hospilot_lab_results_by_pk(id: $id) {
                id order_id patient_token test_name test_code
                result_value flag reference_range unit reported_at
              }
            }
            """,
            {"id": result_id},
        )
        return data.get("hospilot_lab_results_by_pk")

    # =========================================================================
    # INFECTION CONTROL AGENT
    # =========================================================================

    async def get_active_infection_cases(self) -> list[dict]:
        """All active infection cases."""
        data = await self.query(
            """
            query GetActiveInfectionCases {
              hospilot_infection_cases(where: {status: {_eq: "active"}}) {
                id patient_token admission_id ward pathogen severity
                isolation_required isolation_confirmed isolation_room
                status reported_at notes
              }
            }
            """
        )
        return data.get("hospilot_infection_cases", [])

    async def get_non_isolated_infection_cases(self) -> list[dict]:
        """Cases requiring isolation that are not yet confirmed isolated."""
        data = await self.query(
            """
            query GetNonIsolatedCases {
              hospilot_infection_cases(where: {
                status: {_eq: "active"}
                isolation_required: {_eq: true}
                isolation_confirmed: {_eq: false}
              }) {
                id patient_token ward pathogen severity reported_at notes
              }
            }
            """
        )
        return data.get("hospilot_infection_cases", [])

    # =========================================================================
    # SUPPLY CHAIN AGENT
    # =========================================================================

    async def get_all_supplies(self) -> list[dict]:
        """All supply items from hospilot_supplies."""
        data = await self.query(
            """
            query GetAllSupplies {
              hospilot_supplies {
                id item_code item_name category
                current_stock min_stock unit unit_cost
                last_ordered_at last_received_at
              }
            }
            """
        )
        return data.get("hospilot_supplies", [])

    async def carerOS_get_purchase_orders(self) -> list[dict]:
        """CarerOS purchase orders that are Pending Approval or Approved."""
        data = await self.query(
            """
            query GetPurchaseOrders {
              purchase_orders(where: {status: {_in: ["Pending Approval", "Approved"]}}) {
                id po_number vendor_id status total
                order_date expected_delivery created_at
              }
            }
            """
        )
        return data.get("purchase_orders", [])

    async def get_recently_discharged_beds(self) -> list[dict]:
        """Beds from discharge-ready admissions -- candidates for housekeeping dispatch."""
        data = await self.query(
            """
            query RecentlyDischargedBeds {
              hospilot_ipd_admissions(
                where: {discharge_ready: {_eq: true}},
                order_by: {admitted_at: desc},
                limit: 20
              ) {
                id bed_id patient_token admitted_at
              }
            }
            """
        )
        admissions = data.get("hospilot_ipd_admissions", [])
        beds = []
        for a in admissions:
            if a.get("bed_id"):
                beds.append({
                    "id":           a["bed_id"],
                    "admission_id": a["id"],
                    "patient_token": a.get("patient_token"),
                })
        return beds

    # =========================================================================
    # REVENUE AGENT -- CarerOS source queries
    # =========================================================================

    async def carerOS_get_invoices(self) -> list[dict]:
        """Active invoices from CarerOS (excludes Draft and Cancelled)."""
        data = await self.query(
            """
            query GetInvoices {
              invoices(
                where: {status: {_nin: ["Cancelled", "Draft"]}}
                order_by: {invoice_date: desc}
                limit: 500
              ) {
                id invoice_number patient_id admission_id visit_id
                invoice_type invoice_date due_date
                grand_total paid_amount balance status payment_status
              }
            }
            """
        )
        return data.get("invoices", [])

    async def carerOS_get_payments(self) -> list[dict]:
        return await fget("/financial/payments")

    async def carerOS_get_daily_collections(self) -> list[dict]:
        """Last 30 days of daily collection summaries from CarerOS."""
        data = await self.query(
            """
            query GetDailyCollections {
              daily_collections(
                order_by: {collection_date: desc}
                limit: 30
              ) {
                id collection_date total_collection
                cash_total upi_total card_total bank_transfer_total
                invoice_count payment_count is_reconciled variance
              }
            }
            """
        )
        return data.get("daily_collections", [])

    async def carerOS_get_claims(self) -> list[dict]:
        """Insurance claims from CarerOS -- full fields for billing agent."""
        data = await self.query(
            """
            query GetClaims {
              claims(order_by: {created_at: desc}, limit: 300) {
                id patient_id visit_id tpa_id tpa_name claim_amount status created_at
                submitted_date approved_amount denial_reason claim_number payer_type
                risk_level risk_score stage compliance_status diagnosis_code branch_id
              }
            }
            """
        )
        return data.get("claims", [])

    async def carerOS_get_claim_line_items(self) -> list[dict]:
        data = await self.query(
            """
            query GetClaimLineItems {
              claim_line_items(limit: 2000) {
                id claim_id service_code service_name description quantity rate amount
                approved_amount approved_quantity approved_rate status category unit rejection_reason
              }
            }
            """
        )
        return data.get("claim_line_items", [])

    async def carerOS_get_claim_history(self) -> list[dict]:
        data = await self.query(
            """
            query GetClaimHistory {
              claim_history(order_by: {changed_at: desc}, limit: 1000) {
                id claim_id from_status to_status action changed_at changed_by remarks
              }
            }
            """
        )
        return data.get("claim_history", [])

    async def carerOS_get_claim_queries(self) -> list[dict]:
        data = await self.query(
            """
            query GetClaimQueries {
              claim_queries(order_by: {created_at: desc}, limit: 500) {
                id claim_id query_type query_text status raised_at raised_by
                responded_by response_date response_text created_at
              }
            }
            """
        )
        return data.get("claim_queries", [])

    async def carerOS_get_insurance_contracts(self) -> list[dict]:
        return await fget("/financial/contracts")

    async def carerOS_get_contract_service_rates(self) -> list[dict]:
        data = await self.query(
            """
            query GetContractRates {
              contract_service_rates(where: {is_active: {_eq: true}}, limit: 5000) {
                id contract_id service_id service_code service_name
                contract_rate hospital_rate discount_percentage is_active
              }
            }
            """
        )
        return data.get("contract_service_rates", [])

    async def carerOS_get_invoice_line_items(self) -> list[dict]:
        data = await self.query(
            """
            query GetInvoiceLineItems {
              invoice_line_items(limit: 5000) {
                id invoice_id service_id service_code service_name description
                quantity rate amount total gst_rate gst_amount discount_amount
                source_type source_id
              }
            }
            """
        )
        return data.get("invoice_line_items", [])

    async def carerOS_get_payment_entries(self) -> list[dict]:
        data = await self.query(
            """
            query GetPaymentEntries {
              payment_entries(order_by: {created_at: desc}, limit: 1000) {
                id payment_id payment_mode amount transaction_reference
                bank_name card_last_four created_at
              }
            }
            """
        )
        return data.get("payment_entries", [])

    async def carerOS_get_refunds(self) -> list[dict]:
        return await fget("/financial/refunds")

    async def carerOS_get_payment_reconciliation(self) -> list[dict]:
        data = await self.query(
            """
            query GetPaymentReconciliation {
              payment_reconciliation(order_by: {reconciliation_date: desc}, limit: 30) {
                id reconciliation_date total_expected total_actual total_variance
                actual_cash actual_card actual_upi actual_bank
                cash_variance card_variance upi_variance bank_variance
                status created_at
              }
            }
            """
        )
        return data.get("payment_reconciliation", [])

    async def carerOS_get_lab_orders(self) -> list[dict]:
        data = await self.query(
            """
            query GetLabOrders {
              lab_orders(order_by: {created_at: desc}, limit: 500) {
                id visit_id ordered_by status created_at
              }
            }
            """
        )
        return data.get("lab_orders", [])

    async def carerOS_get_lab_results(self) -> list[dict]:
        data = await self.query(
            """
            query GetLabResults {
              lab_results(order_by: {created_at: desc}, limit: 1000) {
                id patient_id result_value flag created_at
              }
            }
            """
        )
        return data.get("lab_results", [])

    async def carerOS_get_patients(self) -> list[dict]:
        data = await self.query(
            """
            query GetPatients {
              patients(limit: 5000) {
                id first_name last_name uhid
              }
            }
            """
        )
        return data.get("patients", [])

    # =========================================================================
    # REVENUE AGENT -- Hospilot upserts (Step 1 of two-step sync)
    # =========================================================================

    async def upsert_invoices(self, invoices: list[dict]) -> int:
        if not invoices:
            return 0
        data = await self.mutate(
            """
            mutation UpsertInvoices($rows: [hospilot_invoices_insert_input!]!) {
              insert_hospilot_invoices(
                objects: $rows
                on_conflict: {
                  constraint: invoices_pkey
                  update_columns: [
                    invoice_number, patient_id, admission_id, visit_id,
                    invoice_type, invoice_date, due_date,
                    grand_total, paid_amount, balance,
                    status, payment_status, synced_at
                  ]
                }
              ) { affected_rows }
            }
            """,
            {"rows": invoices},
        )
        return data["insert_hospilot_invoices"]["affected_rows"]

    async def upsert_payments(self, payments: list[dict]) -> int:
        if not payments:
            return 0
        data = await self.mutate(
            """
            mutation UpsertPayments($rows: [hospilot_payments_insert_input!]!) {
              insert_hospilot_payments(
                objects: $rows
                on_conflict: {
                  constraint: payments_pkey
                  update_columns: [
                    invoice_id, patient_id, payment_date, total_amount,
                    status, synced_at
                  ]
                }
              ) { affected_rows }
            }
            """,
            {"rows": payments},
        )
        return data["insert_hospilot_payments"]["affected_rows"]

    async def upsert_daily_collections(self, collections: list[dict]) -> int:
        if not collections:
            return 0
        data = await self.mutate(
            """
            mutation UpsertDailyCollections($rows: [hospilot_daily_collections_insert_input!]!) {
              insert_hospilot_daily_collections(
                objects: $rows
                on_conflict: {
                  constraint: daily_collections_org_id_collection_date_key
                  update_columns: [
                    total_collection, cash_total, upi_total, card_total,
                    cheque_total, bank_transfer_total, invoice_count, payment_count,
                    is_reconciled, variance, synced_at
                  ]
                }
              ) { affected_rows }
            }
            """,
            {"rows": collections},
        )
        return data["insert_hospilot_daily_collections"]["affected_rows"]

    # =========================================================================
    # REVENUE AGENT -- Agent query methods (Step 2: Hospilot tables -> activities)
    # =========================================================================

    async def get_outstanding_invoices(self) -> list[dict]:
        """Invoices with unpaid or partial balance -- the revenue gap."""
        data = await self.query(
            """
            query OutstandingInvoices {
              hospilot_invoices(
                where: {payment_status: {_in: ["Unpaid", "Partial"]}}
                order_by: {invoice_date: asc}
              ) {
                id invoice_number patient_id admission_id visit_id
                invoice_type invoice_date due_date
                grand_total paid_amount balance payment_status
              }
            }
            """
        )
        return data.get("hospilot_invoices", [])

    async def get_todays_collections(self) -> dict | None:
        """Today's collection summary row."""
        from datetime import date
        today = date.today().isoformat()
        data = await self.query(
            """
            query TodaysCollections($today: date!) {
              hospilot_daily_collections(
                where: {collection_date: {_eq: $today}}
                limit: 1
              ) {
                collection_date total_collection
                cash_total upi_total card_total bank_transfer_total
                invoice_count payment_count is_reconciled variance
              }
            }
            """,
            {"today": today},
        )
        rows = data.get("hospilot_daily_collections", [])
        return rows[0] if rows else None

    async def get_yesterday_collections(self) -> dict | None:
        """Yesterday's collection summary for day-over-day comparison."""
        from datetime import date, timedelta
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        data = await self.query(
            """
            query YesterdayCollections($day: date!) {
              hospilot_daily_collections(
                where: {collection_date: {_eq: $day}}
                limit: 1
              ) {
                collection_date total_collection invoice_count payment_count
              }
            }
            """,
            {"day": yesterday},
        )
        rows = data.get("hospilot_daily_collections", [])
        return rows[0] if rows else None

    async def get_patient_invoices(self, patient_token: str) -> list[dict]:
        """All invoices for a specific patient, newest first."""
        data = await self.query(
            """
            query GetPatientInvoices($token: uuid!) {
              hospilot_invoices(
                where: {patient_id: {_eq: $token}}
                order_by: {invoice_date: desc}
              ) {
                id invoice_number invoice_type invoice_date due_date
                grand_total paid_amount balance status payment_status
                visit_id admission_id
              }
            }
            """,
            {"token": patient_token},
        )
        return data.get("hospilot_invoices", [])

    async def get_patient_claims(self, visit_ids: list) -> list[dict]:
        """Insurance claims for a given list of visit IDs."""
        if not visit_ids:
            return []
        data = await self.query(
            """
            query GetPatientClaims($visit_ids: [String!]!) {
              hospilot_claims(
                where: {visit_id: {_in: $visit_ids}}
                order_by: {created_at: desc}
              ) {
                id visit_id tpa_id claim_amount status created_at
              }
            }
            """,
            {"visit_ids": visit_ids},
        )
        return data.get("hospilot_claims", [])

    # =========================================================================
    # LAB AGENT — via Fabric /labs/*
    # =========================================================================

    async def lab_get_orders(self) -> list[dict]:
        return await fget("/labs/orders")

    async def lab_get_results(self) -> list[dict]:
        return await fget("/labs/results")

    async def lab_get_samples(self) -> list[dict]:
        return await fget("/labs/samples")

    async def lab_get_analyzers(self) -> list[dict]:
        return await fget("/labs/analyzers")

    async def lab_get_qc_logs(self, hours: int = 24) -> list[dict]:
        return await fget("/labs/qc-logs", hours=hours)

    async def lab_get_critical_escalations(self) -> list[dict]:
        return await fget("/labs/critical-escalations")

    async def lab_get_reflex_rules(self) -> list[dict]:
        return await fget("/labs/reflex-rules")

    async def lab_get_validation_rules(self) -> list[dict]:
        return await fget("/labs/validation-rules")

    async def lab_get_capacity_history(self, days: int = 30) -> list[dict]:
        return await fget("/labs/capacity", days=days)

    async def lab_upsert_escalation(self, payload: dict) -> str:
        data = await self.mutate(
            """
            mutation LabUpsertEscalation($obj: hospilot_lab_critical_escalations_insert_input!) {
              insert_hospilot_lab_critical_escalations_one(
                object: $obj
                on_conflict: {
                  constraint: lab_critical_escalations_pkey
                  update_columns: [physician_notified, physician_acknowledged_at,
                                   escalation_level, action_documented, closed_at]
                }
              ) { id }
            }
            """,
            {"obj": payload},
        )
        return (data.get("insert_hospilot_lab_critical_escalations_one") or {}).get("id", "")

    # =========================================================================
    # PHARMACY AGENT — via Fabric /pharmacy/*
    # =========================================================================

    async def pharmacy_get_orders(self) -> list[dict]:
        return await fget("/pharmacy/orders")

    async def pharmacy_get_pending_orders(self) -> list[dict]:
        return await fget("/pharmacy/orders/pending")

    async def pharmacy_get_stat_orders(self) -> list[dict]:
        return await fget("/pharmacy/orders/stat")

    async def pharmacy_get_inventory(self) -> list[dict]:
        return await fget("/pharmacy/inventory")

    async def pharmacy_get_dispensing_log(self, hours: int = 8) -> list[dict]:
        return await fget("/pharmacy/dispensing-log", hours=hours)

    async def pharmacy_get_interaction_rules(self) -> list[dict]:
        return await fget("/pharmacy/interactions")

    async def pharmacy_get_substitution_rules(self) -> list[dict]:
        return await fget("/pharmacy/substitutions")

    async def pharmacy_get_controlled_logs(self, hours: int = 24) -> list[dict]:
        return await fget("/pharmacy/controlled-log", hours=hours)

    async def pharmacy_get_capacity_history(self, days: int = 30) -> list[dict]:
        return await fget("/pharmacy/capacity", days=days)

    async def pharmacy_get_drug_dispensing_history(self, days: int = 30) -> dict:
        """Per-SKU daily dispensing history + rolling 7d/30d means for the 5 drugs
        the /pharmacy/demand forecast model knows (INSULIN, PARACETAMOL,
        AMOXICILLIN, HEPARIN, METFORMIN).

        Fabric only exposes aggregate total_orders/day, not per-drug counts, so we
        derive this from the raw dispensing log: each entry is classified to a SKU
        by medication_name and bucketed by calendar date. NOTE: one log entry is
        counted as one unit (dispensing event); we do not have per-line quantities,
        so the means are event-rate proxies -- adequate for the model's demand
        signal and well within its 0-2000 input range.

        Returns {sku: {rolling_7d_dispensing_mean, rolling_30d_dispensing_mean,
        units_7d, units_30d, days_observed}} for every SKU seen in the window.
        SKUs with no dispensing in the window are omitted (caller defaults them).
        """
        from datetime import date, datetime, timedelta, timezone

        log = await fget("/pharmacy/dispensing-log", hours=days * 24) or []
        today = datetime.now(timezone.utc).date()
        # Inclusive windows of exactly N calendar days (today + N-1 prior).
        cutoff_7d = today - timedelta(days=6)
        cutoff_30d = today - timedelta(days=29)

        # sku -> {iso_date: unit_count}
        buckets: dict[str, dict[str, int]] = {}
        for rec in log:
            sku = _classify_drug_sku(rec.get("medication_name"))
            if not sku:
                continue
            ts = rec.get("created_at") or rec.get("dispensed_at") or rec.get("timestamp") or ""
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except ValueError:
                continue
            day = dt.date().isoformat()
            per_day = buckets.setdefault(sku, {})
            per_day[day] = per_day.get(day, 0) + 1

        result: dict[str, dict] = {}
        for sku, per_day in buckets.items():
            units_7d = sum(c for d, c in per_day.items() if date.fromisoformat(d) >= cutoff_7d)
            units_30d = sum(c for d, c in per_day.items() if date.fromisoformat(d) >= cutoff_30d)
            result[sku] = {
                "rolling_7d_dispensing_mean":  round(units_7d / 7, 2),
                "rolling_30d_dispensing_mean": round(units_30d / 30, 2),
                "units_7d":       units_7d,
                "units_30d":      units_30d,
                "days_observed":  len(per_day),
            }
        logger.info("pharmacy_get_drug_dispensing_history  window=%dd  skus=%d  records=%d",
                    days, len(result), len(log))
        return result

    async def pharmacy_upsert_order_status(self, order_id: str, status: str) -> str:
        data = await self.mutate(
            """
            mutation PharmacyUpdateOrderStatus($id: uuid!, $status: String!) {
              update_hospilot_pharmacy_orders_by_pk(
                pk_columns: {id: $id}
                _set: {status: $status}
              ) { id }
            }
            """,
            {"id": order_id, "status": status},
        )
        return (data.get("update_hospilot_pharmacy_orders_by_pk") or {}).get("id", "")

    async def pharmacy_upsert_dispensing_record(self, payload: dict) -> str:
        data = await self.mutate(
            """
            mutation PharmacyUpsertDispensing($obj: hospilot_pharmacy_dispensing_log_insert_input!) {
              insert_hospilot_pharmacy_dispensing_log_one(
                object: $obj
                on_conflict: {
                  constraint: pharmacy_dispensing_log_pkey
                  update_columns: [verification_status, patient_verified,
                                   prescription_matched, dosage_correct, tat_minutes]
                }
              ) { id }
            }
            """,
            {"obj": payload},
        )
        return (data.get("insert_hospilot_pharmacy_dispensing_log_one") or {}).get("id", "")

    # =========================================================================
    # APPOINTMENT AGENT -- hospilot-owned tables (direct Hasura)
    # =========================================================================

    async def appt_list_appointments(self, limit: int = 500) -> list[dict]:
        data = await self.query(
            """
            query ApptList($limit: Int!) {
              hospilot_appointments(order_by: {appointment_time: desc}, limit: $limit) {
                id status type appointment_time patient_id provider_id department_id
                patient_name phone email specialization department_name
              }
            }
            """,
            {"limit": limit},
        )
        return data.get("hospilot_appointments", [])

    async def appt_available_slots(self) -> list[dict]:
        return await fget("/appointments/slots")

    async def appt_list_waitlist(self, limit: int = 500) -> list[dict]:
        """Active patient waitlist (hospilot.waitlist) -- read source for the waitlist
        matcher (planner-query-gaps G1/G2). hospilot.* is HIS-owned data; Fabric has
        no /waitlist endpoint yet, so the agent reads the mirror directly via Hasura
        (same fallback pattern as appt_list_appointments). Oldest request first."""
        data = await self.query(
            """
            query WaitlistList($limit: Int!) {
              hospilot_waitlist(
                where: { status: { _eq: "waitlisted" } }
                order_by: { created_at: asc }
                limit: $limit
              ) {
                id patient_id patient_name phone email
                specialization priority requested_date status created_at
              }
            }
            """,
            {"limit": limit},
        )
        return data.get("hospilot_waitlist", [])

    async def staff_list_roster(self, areas: list[str] | None = None, limit: int = 500) -> list[dict]:
        """Staff roster by area/shift (hospilot.staff_roster) -- read source for the
        staff-area dimension (planner-query-gaps G11/G20/G24/G28/lab-staff). HIS-owned;
        Fabric has no roster endpoint yet, so the agent reads the mirror via Hasura."""
        if areas:
            query = """
              query StaffRoster($areas: [String!], $limit: Int!) {
                hospilot_staff_roster(where: { area: { _in: $areas } }, limit: $limit) {
                  id area area_label role shift headcount assigned_load load_per_staff
                }
              }
            """
            variables = {"areas": areas, "limit": limit}
        else:
            query = """
              query StaffRoster($limit: Int!) {
                hospilot_staff_roster(limit: $limit) {
                  id area area_label role shift headcount assigned_load load_per_staff
                }
              }
            """
            variables = {"limit": limit}
        data = await self.query(query, variables)
        return data.get("hospilot_staff_roster", [])

    async def appt_list_service_slots(self, slot_type: str | None = None, limit: int = 500) -> list[dict]:
        """Open non-OPD bookable slots (hospilot.service_slots) -- sample_collection /
        pharmacy_pickup (planner-query-gaps G23/G39). HIS-owned; no Fabric endpoint, so
        read the mirror via Hasura (same pattern as waitlist/staff_roster)."""
        if slot_type:
            query = """
              query ServiceSlots($t: String!, $limit: Int!) {
                hospilot_service_slots(
                  where: { status: { _eq: "open" }, slot_type: { _eq: $t } }
                  order_by: { slot_date: asc, slot_start: asc }
                  limit: $limit
                ) { id slot_type slot_date slot_start slot_end location specialization max_patients booked_count status }
              }
            """
            variables = {"t": slot_type, "limit": limit}
        else:
            query = """
              query ServiceSlots($limit: Int!) {
                hospilot_service_slots(
                  where: { status: { _eq: "open" } }
                  order_by: { slot_date: asc, slot_start: asc }
                  limit: $limit
                ) { id slot_type slot_date slot_start slot_end location specialization max_patients booked_count status }
              }
            """
            variables = {"limit": limit}
        data = await self.query(query, variables)
        return data.get("hospilot_service_slots", [])

    async def appt_book_service_slot(self, slot_id: str) -> dict:
        """Increment booked_count on a service slot (and close it when full). Used on
        commit -- service slots are not in Fabric, so they're booked via Hasura."""
        data = await self.mutate(
            """
            mutation BookServiceSlot($id: uuid!) {
              update_hospilot_service_slots(
                where: { id: { _eq: $id } }
                _inc: { booked_count: 1 }
              ) { returning { id booked_count max_patients } }
            }
            """,
            {"id": slot_id},
        )
        rows = (data.get("update_hospilot_service_slots") or {}).get("returning") or []
        row = rows[0] if rows else {}
        # Close the slot once it reaches capacity.
        if row and (row.get("booked_count") or 0) >= (row.get("max_patients") or 1):
            await self.mutate(
                """
                mutation CloseServiceSlot($id: uuid!) {
                  update_hospilot_service_slots(where: { id: { _eq: $id } }, _set: { status: "booked" }) { affected_rows }
                }
                """,
                {"id": slot_id},
            )
        return row

    async def appt_create_appointment(
        self, patient_id: str, provider_id: str, department_id: str | None,
        appointment_time: str, appt_type: str = "New Consultation",
        patient_name: str | None = None, phone: str | None = None,
        email: str | None = None, specialization: str | None = None,
    ) -> dict:
        data = await self.mutate(
            """
            mutation ApptCreate($obj: hospilot_appointments_insert_input!) {
              insert_hospilot_appointments_one(object: $obj) {
                id status appointment_time
              }
            }
            """,
            {"obj": {
                "patient_id": patient_id, "provider_id": provider_id,
                "department_id": department_id, "appointment_time": appointment_time,
                "status": "Scheduled", "type": appt_type,
                "patient_name": patient_name, "phone": phone, "email": email,
                "specialization": specialization,
            }},
        )
        return data.get("insert_hospilot_appointments_one") or {}

    async def appt_mark_slot_booked(self, slot_id: str) -> dict:
        data = await self.mutate(
            """
            mutation ApptBookSlot($id: uuid!) {
              update_hospilot_doctor_slots_by_pk(
                pk_columns: {id: $id}
                _set: {status: "Booked"}
              ) { id status }
            }
            """,
            {"id": slot_id},
        )
        return data.get("update_hospilot_doctor_slots_by_pk") or {}


hasura = HasuraClient()
