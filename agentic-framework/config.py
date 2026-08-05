from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Anthropic (cloud Claude)
    anthropic_api_key: str = ""

    # LLM provider -- "anthropic" uses Claude, "openai" uses any OpenAI-compatible
    # server (Ollama, vLLM, etc.). This is the single provider toggle for every
    # LLM call in the app (see llm_client.py) -- RAG_PROVIDER derives from it
    # by default (rag_provider_effective below).
    llm_provider: str = "anthropic"
    llm_base_url: str = "http://127.0.0.1:11434/v1"   # Ollama default via SSH tunnel
    llm_model: str = "qwen3:14b"                        # fallback model served by Ollama

    # Two tiers stand in for Claude's haiku/sonnet split: "fast" for cheap
    # classification/guardrail calls, "quality" for planning/synthesis/codegen.
    # Empty => provider-specific default (Claude model names for "anthropic",
    # llm_model for "openai"); set explicitly to run two different Ollama models
    # per tier.
    llm_fast_model: str = ""
    llm_quality_model: str = ""

    # Hasura
    hasura_url: str
    hasura_admin_secret: str

    # Redis
    redis_url: str = "redis://localhost:6380"

    # Kafka -- event/replay bus (broadcast() -> Kafka -> API relay -> WebSockets).
    # When kafka_enabled is False, broadcast() falls back to direct in-process WS
    # (single-process dev/tests). Topic carries the exact WS event envelope,
    # keyed by session_id so per-session ordering is preserved on one partition.
    kafka_brokers: str = "localhost:9092"
    kafka_enabled: bool = False
    kafka_events_topic: str = "hospilot.sessions.events"
    # Prefix for fabric data-change topics, e.g. "hospilot.data" →
    # "hospilot.data.bed", "hospilot.data.visit", etc.
    # Set to a single topic name if fabric publishes a multiplexed stream.
    kafka_data_topic_prefix: str = "hospilot.data"
    # Fabric publishes accepted/rejected write acks here after DB validates a /commit write.
    kafka_ack_topic: str = "hospilot.sync.ack"

    # Temporal -- execution plane. Every agent task runs as a durable activity.
    # When temporal_enabled is False, run_activity() falls back to calling the
    # activity function in-process (current behavior) so dev/tests run without a
    # Temporal server.
    temporal_enabled: bool = False
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "hospilot-tasks"

    # Postgres -- direct DSN for the LangGraph checkpointer (AsyncPostgresSaver).
    # MUST point at the same Postgres instance that sits behind Hasura.
    # e.g. postgresql://user:pass@host:5432/hospilot
    database_url: str = ""

    # Langfuse -- LLM/agent tracing. Tracing is a no-op when keys are absent.
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def rag_provider_effective(self) -> str:
        """rag_provider if explicitly set, else derived from llm_provider."""
        if self.rag_provider:
            return self.rag_provider
        return "ollama" if self.llm_provider == "openai" else "claude"

    # CarerOS scope -- filter beds/departments to this branch (leave blank for all)
    careros_branch_id: str = ""

    # EHR ingestion source -- selects the active connector (see ehr/registry.py)
    ehr_source: str = "careros"

    # Fabric -- transformation layer that serves clinical + financial data
    # from the DB's FHIR/REST APIs in the same shapes the agents expect.
    # Set FABRIC_BASE_URL to Fabric's deployed address (e.g. http://192.46.214.225:8001).
    fabric_base_url: str = ""
    fabric_api_key: str = ""        # empty => Fabric auth disabled (dev)

    # Patient registration -- when an incoming patient has no DB record, the patient
    # verification agent requests their registration via Fabric and PAUSES the flow
    # until the hospital staff create them (reported back via the `patient` Kafka data
    # event). Because the create is manual it can take a while; this is how long we
    # wait before the reaper resumes the flow with a timeout + escalation alert.
    patient_registration_timeout_hours: int = 24

    # FHIR -- outbound /fhir REST API (R5 / 5.0.0 models)
    fhir_enabled: bool = True
    fhir_base_url: str = "http://localhost:8000/fhir"
    fhir_api_key: str = ""          # empty => /fhir auth disabled (dev posture)
    fhir_default_count: int = 50
    fhir_max_count: int = 200

    # Inbound EHR FHIR source -- used by the /fhir gateway (fhirgw module) to
    # build outbound FHIR R5 responses. Not used for agent data (that goes via Fabric).
    fhir_ehr_base_url: str = ""
    fhir_ehr_api_key: str = ""

    # Simulation backend -- proxy target for /simulation/* routes.
    sim_base_url: str = ""   # e.g. http://192.46.212.81:9002

    # ML forecast service -- Hospilot forecasting models (ER surge, bed turnover,
    # pharmacy/lab demand, ICU demand, board KPIs). Called from inside prediction
    # subagents via util/forecast_client.py, which degrades gracefully (returns
    # None) when this is unset or the service is down. Leave blank to disable.
    forecast_base_url: str = ""   # e.g. http://192.46.212.81:18000
    forecast_api_key: str = ""    # REQUIRED: sent as X-API-Key header (:18000 returns 401 without it)

    # Legacy financial API -- fallback when fabric_base_url is unset.
    # Kept for backwards-compat; prefer FABRIC_BASE_URL in new deployments.
    financial_api_base_url: str = ""
    financial_api_key: str = ""

    # RAG / Q&A assistant -- natural-language questions answered from live
    # Fabric + Redis data (see rag/). The provider toggle picks which LLM
    # writes the routing + answer. Empty (recommended) => derives from
    # llm_provider via rag_provider_effective, so RAG follows the same
    # Claude/Ollama switch as the rest of the app; set explicitly only to run
    # RAG on a different backend than everything else.
    rag_provider: str = ""                         # "" | "claude" | "ollama"
    rag_anthropic_model: str = "claude-haiku-4-5-20251001"  # set claude-opus-4-8 for max quality
    rag_ollama_base_url: str = "http://122.176.148.138:11434/v1"  # OpenAI-compatible endpoint
    rag_ollama_model: str = "qwen2.5"
    rag_self_hosted_supports_tools: bool = False  # reserved; flip once Qwen tool-use verified
    rag_max_tokens: int = 1024
    rag_temperature: float = 0.0
    rag_request_timeout: float = 60.0
    # Text-to-SQL: cap rows returned from a generated query, and how many times the
    # model may auto-correct a failing query before giving up.
    rag_max_result_rows: int = 200
    rag_sql_repair_attempts: int = 1

    # Conversation memory (see rag/memory.py + the isolated `memory` sidecar).
    # History window = how many recent turns are fed verbatim; older turns are
    # folded into a rolling summary once the transcript beyond the last summarised
    # turn exceeds the token budget. memory_max caps per-user cross-session facts
    # injected per request. An empty memory_service_url disables summarisation +
    # fact-extraction entirely (RAG still answers, just without them).
    rag_history_window: int = 6
    rag_summary_token_budget: int = 2000
    rag_memory_max: int = 30           # candidate facts fetched per request (before ranking)
    memory_service_url: str = ""
    memory_service_api_key: str = ""

    # Cross-session fact retrieval is semantic: facts are embedded (OpenAI) on
    # write and ranked against the question's embedding by cosine similarity at
    # read time. Vectors are stored as JSONB and cosine is computed app-side
    # (the DB has no pgvector), which is fine at per-user scale. rag_memory_top_k
    # facts survive ranking and get injected. Empty openai_api_key disables
    # embedding -> retrieval degrades to recency order (RAG still answers).
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    rag_memory_top_k: int = 5

    # Auth
    jwt_secret: str = "change-me-in-production"
    jwt_expiry_hours: int = 8

    # Multi-tenancy bootstrap -- if set and no super_admin exists at startup,
    # main.py creates one (platform-level, org-less). Change in production.
    bootstrap_admin_username: str = ""
    bootstrap_admin_password: str = ""
    bootstrap_admin_display_name: str = "Platform Admin"
    # Privileged Postgres DSN used only by scripts/provision_org.py to
    # CREATE DATABASE for new tenants (not used by the app at runtime).
    postgres_admin_dsn: str = ""

    # Inter-agent pacing -- every agent node broadcasts agent_completed and then, if it
    # actually ran (not skipped/reused/a halting failure), sleeps this long before
    # LangGraph schedules the next node's agent_started. Applies in both assisted and
    # autonomous mode. Without this, independent/fast agents (e.g. a Hasura count
    # query) complete back-to-back fast enough that the pipeline canvas flashes
    # through states too quickly to follow. 0 disables pacing entirely.
    agent_step_delay_seconds: float = 3.0

    # Autonomous mode -- max number of session flows executing concurrently in the
    # background. Flows that reach execution while all slots are taken sit in a
    # `queued` state (visible in GET /api/queues/execution) until a slot frees.
    # Parked flows (waiting at an approval/input interrupt) do NOT hold a slot.
    autonomous_max_concurrency: int = 5

    # Autonomy policy engine (Phase 5) -- in autonomous mode, each mid-flow approval
    # interrupt is evaluated against a hospital-fillable rule structure to decide
    # auto_approve / require_human / escalate. Assisted mode is unaffected (every
    # approval always parks for a human).
    #   policy_rules_path        -- path to a JSON rules file (bind-mount into the
    #                               container); empty => built-in safe DEFAULT_POLICY.
    #                               See docs/agentic-framework/AUTONOMY_POLICY_TEMPLATE.md.
    #   autonomous_policy_enabled -- master gate; False => autonomous parks like assisted.
    #   notification_channel     -- logical channel label for policy notifications;
    #                               "none" disables the WS notification. Only "websocket"
    #                               is wired today (no SMS/email/Slack infra); other values
    #                               pass through as a label for the future channel hook.
    policy_rules_path: str = ""
    autonomous_policy_enabled: bool = True
    notification_channel: str = "websocket"

    # Scheduled recurring queries (Phase 6) -- a saved query re-run on a cadence
    # (fixed interval or cron) as an unattended autonomous background job. A loop
    # (workflows/graph/scheduler.py, launched in main.py lifespan like the reaper)
    # scans the hospilot_app.scheduled_queries table every scan interval and fires
    # every due row down the normal autonomous submission path.
    #   scheduler_enabled                  -- master gate for the loop.
    #   scheduler_scan_interval_seconds    -- how often the loop scans for due jobs.
    #   scheduled_query_min_interval_seconds -- floor guard on user-supplied
    #                                           intervals, so a typo can't hammer
    #                                           the executor (default 5 min).
    scheduler_enabled: bool = True
    scheduler_scan_interval_seconds: int = 30
    scheduled_query_min_interval_seconds: int = 300

    # Advisory engine -- notify-only rules evaluated event-first (nudged by the
    # Kafka data consumer on hospilot.data.* changes) with a clock fallback for
    # rules events can't carry (SLA timeouts, forecasts). The engine
    # (workflows/graph/advisory.py, launched in main.py lifespan like the
    # scheduler) persists fired rules to hospilot_app.advisories.
    advisory_engine_enabled: bool = True
    advisory_scan_interval_seconds: int = 30        # clock tick for interval rules
    advisory_min_check_interval_seconds: int = 60   # floor guard on DB-edited cadences
    advisory_event_debounce_seconds: int = 30       # min gap between event evals per rule

    # App
    app_env: str = "development"
    app_base_url: str = "http://localhost:8000"
    cors_origins: str = "*"

    @property
    def kafka_broker_list(self) -> list[str]:
        return [b.strip() for b in self.kafka_brokers.split(",")]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",")]

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
