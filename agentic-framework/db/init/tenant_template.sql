-- Tenant database template (multi-tenancy, DB-per-tenant).
--
-- Applied by scripts/provision_org.py to every fresh tenant database
-- (hospilot_org_<slug>). Holds the per-tenant app-layer tables; everything
-- else stays central in the control-plane DB:
--   users / organizations / agent+subagent+task registries / LangGraph
--   checkpointer -> control plane.
--   Clinical hospilot.* tables -> still the default DB this phase (shared),
--   pending Fabric/Kafka ingestion becoming org-aware.
--
-- Column definitions mirror the live hospilot_app tables in the default DB
-- (including 017 user_id, 018 synthesis_result, 041 autonomous, 042 kind, 052 name,
-- 054 scheduled_queries + sessions.scheduled_query_id + widened status CHECKs).
-- user_id / approver_id reference users in the CONTROL-PLANE DB, so they are plain
-- uuid/text here -- no cross-DB FK.
--
-- After applying, the provisioning script registers this DB as Hasura source
-- 'org_<slug>' with root-field prefix 't_<slug>_' and tracks the app tables.

CREATE SCHEMA IF NOT EXISTS hospilot_app;

-- Hasura-style updated_at trigger helper
CREATE OR REPLACE FUNCTION hospilot_app.set_current_timestamp_updated_at()
RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ── sessions ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hospilot_app.sessions (
    id                uuid PRIMARY KEY,
    goal              text NOT NULL,
    constraints       text,
    status            text NOT NULL DEFAULT 'pending'
        CONSTRAINT sessions_status_check
        CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
    priority          text,
    pipeline          jsonb NOT NULL DEFAULT '{}'::jsonb,
    pipeline_snapshot jsonb,
    synthesis_result  jsonb,
    user_id           uuid,             -- control-plane users.id (no cross-DB FK)
    autonomous        boolean NOT NULL DEFAULT false,
    name              text,             -- editable display name (Workflows page); UI shows "New Workflow" when null
    scheduled_query_id uuid,            -- 054: set when a scheduler fire spawned this session (run history)
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON hospilot_app.sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON hospilot_app.sessions (created_at DESC);

DROP TRIGGER IF EXISTS set_hospilot_app_sessions_updated_at ON hospilot_app.sessions;
CREATE TRIGGER set_hospilot_app_sessions_updated_at
  BEFORE UPDATE ON hospilot_app.sessions
  FOR EACH ROW EXECUTE FUNCTION hospilot_app.set_current_timestamp_updated_at();

-- ── approval_tasks ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hospilot_app.approval_tasks (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id       uuid NOT NULL REFERENCES hospilot_app.sessions(id) ON DELETE CASCADE,
    agent_id         text NOT NULL,
    action_type      text NOT NULL,
    payload          jsonb NOT NULL DEFAULT '{}'::jsonb,
    status           text NOT NULL DEFAULT 'pending'
        CONSTRAINT approval_tasks_status_check
        CHECK (status IN ('pending', 'approved', 'rejected', 'resolved', 'cancelled')),
    kind             text NOT NULL DEFAULT 'approval',
    decision         text,
    approver_id      text,             -- control-plane users.id, stored as text
    decided_at       timestamptz,
    escalation_level integer NOT NULL DEFAULT 0,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_approval_tasks_session ON hospilot_app.approval_tasks (session_id);
CREATE INDEX IF NOT EXISTS idx_approval_tasks_status ON hospilot_app.approval_tasks (status, created_at DESC);

-- ── audit_log ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hospilot_app.audit_log (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id uuid NOT NULL REFERENCES hospilot_app.sessions(id) ON DELETE CASCADE,
    agent_id   text NOT NULL,
    event_type text NOT NULL,
    payload    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_session ON hospilot_app.audit_log (session_id);

-- ── session_agent_overrides ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hospilot_app.session_agent_overrides (
    session_id uuid NOT NULL REFERENCES hospilot_app.sessions(id) ON DELETE CASCADE,
    agent_id   text NOT NULL,
    tasks      jsonb NOT NULL,
    -- Constraint name must match hasura.save_agent_overrides on_conflict
    CONSTRAINT session_agent_overrides_session_id_agent_id_key UNIQUE (session_id, agent_id)
);

-- ── RAG conversation memory (migration 053) ──────────────────────────────────
-- Q&A assistant (POST /api/ask): conversation storage + rolling summary +
-- cross-session per-user facts. Keep in sync with
-- db/migrations/053_rag_conversation_memory.sql.

CREATE TABLE IF NOT EXISTS hospilot_app.rag_conversation (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             uuid,             -- control-plane users.id (no cross-DB FK)
    title               text,
    running_summary     text,
    summary_through_seq integer NOT NULL DEFAULT 0,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rag_conversation_user ON hospilot_app.rag_conversation (user_id);
CREATE INDEX IF NOT EXISTS idx_rag_conversation_created ON hospilot_app.rag_conversation (created_at DESC);

DROP TRIGGER IF EXISTS set_hospilot_app_rag_conversation_updated_at ON hospilot_app.rag_conversation;
CREATE TRIGGER set_hospilot_app_rag_conversation_updated_at
  BEFORE UPDATE ON hospilot_app.rag_conversation
  FOR EACH ROW EXECUTE FUNCTION hospilot_app.set_current_timestamp_updated_at();

CREATE TABLE IF NOT EXISTS hospilot_app.rag_message (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL REFERENCES hospilot_app.rag_conversation(id) ON DELETE CASCADE,
    seq             integer NOT NULL,
    role            text NOT NULL
        CONSTRAINT rag_message_role_check CHECK (role IN ('user', 'assistant')),
    content         text NOT NULL,
    sql             text,
    mode            text,
    row_count       integer,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT rag_message_conversation_seq_key UNIQUE (conversation_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_rag_message_conversation ON hospilot_app.rag_message (conversation_id, seq);

CREATE TABLE IF NOT EXISTS hospilot_app.rag_memory (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid NOT NULL,
    kind            text NOT NULL DEFAULT 'semantic'
        CONSTRAINT rag_memory_kind_check CHECK (kind IN ('semantic', 'episodic', 'procedural')),
    content         jsonb NOT NULL DEFAULT '{}'::jsonb,
    salience        real NOT NULL DEFAULT 0,
    embedding       jsonb,     -- OpenAI vector (float array); JSONB since no pgvector (migration 054)
    embedding_model text,
    embedding_dim   integer,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rag_memory_user ON hospilot_app.rag_memory (user_id, updated_at DESC);

DROP TRIGGER IF EXISTS set_hospilot_app_rag_memory_updated_at ON hospilot_app.rag_memory;
CREATE TRIGGER set_hospilot_app_rag_memory_updated_at
  BEFORE UPDATE ON hospilot_app.rag_memory
  FOR EACH ROW EXECUTE FUNCTION hospilot_app.set_current_timestamp_updated_at();

-- ── scheduled_queries (migration 054) ────────────────────────────────────────
-- Saved queries the scheduler re-runs on a cadence (autonomous mode, Phase 6).
-- Keep in sync with db/migrations/054_scheduled_queries.sql.
CREATE TABLE IF NOT EXISTS hospilot_app.scheduled_queries (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name              text,
    goal              text NOT NULL,
    constraints       text,
    schedule_kind     text NOT NULL DEFAULT 'interval'
        CONSTRAINT scheduled_queries_kind_check
        CHECK (schedule_kind IN ('interval', 'cron')),
    interval_seconds  integer
        CONSTRAINT scheduled_queries_interval_positive
        CHECK (interval_seconds IS NULL OR interval_seconds > 0),
    cron_expr         text,
    timezone          text NOT NULL DEFAULT 'UTC',
    enabled           boolean NOT NULL DEFAULT true,
    autonomous        boolean NOT NULL DEFAULT true,
    next_run_at       timestamptz NOT NULL,
    last_run_at       timestamptz,
    last_session_id   uuid,
    run_count         integer NOT NULL DEFAULT 0,
    user_id           uuid,             -- control-plane users.id (no cross-DB FK)
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_scheduled_queries_due  ON hospilot_app.scheduled_queries (enabled, next_run_at);
CREATE INDEX IF NOT EXISTS idx_scheduled_queries_user ON hospilot_app.scheduled_queries (user_id);

DROP TRIGGER IF EXISTS set_hospilot_app_scheduled_queries_updated_at ON hospilot_app.scheduled_queries;
CREATE TRIGGER set_hospilot_app_scheduled_queries_updated_at
  BEFORE UPDATE ON hospilot_app.scheduled_queries
  FOR EACH ROW EXECUTE FUNCTION hospilot_app.set_current_timestamp_updated_at();

CREATE INDEX IF NOT EXISTS idx_sessions_scheduled_query
  ON hospilot_app.sessions (scheduled_query_id);

-- ── advisory engine (migration 058) ──────────────────────────────────────────
-- Notify-only rules engine: advisory_rules is the rule catalog (event- and/or
-- clock-triggered), advisories are the fired notifications.
-- Keep in sync with db/migrations/058_advisory_engine.sql.
CREATE TABLE IF NOT EXISTS hospilot_app.advisory_rules (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_key               text NOT NULL UNIQUE,
    topic                  text NOT NULL,
    label                  text NOT NULL,
    condition_description  text NOT NULL,
    suggested_action       text NOT NULL,
    severity               text NOT NULL DEFAULT 'warning'
        CONSTRAINT advisory_rules_severity_check
        CHECK (severity IN ('info', 'warning', 'critical')),
    params                 jsonb NOT NULL DEFAULT '{}'::jsonb,  -- TRANSIENT: staged then folded into definition + dropped at end of file (071)
    definition             jsonb NOT NULL DEFAULT '{}'::jsonb,  -- declarative rule spec (070/071): the single source of rule logic + thresholds
    trigger_entities       jsonb NOT NULL DEFAULT '[]'::jsonb,
    check_interval_seconds integer
        CONSTRAINT advisory_rules_interval_positive
        CHECK (check_interval_seconds IS NULL OR check_interval_seconds > 0),
    cooldown_seconds       integer NOT NULL DEFAULT 3600
        CONSTRAINT advisory_rules_cooldown_nonneg
        CHECK (cooldown_seconds >= 0),
    enabled                boolean NOT NULL DEFAULT true,
    next_check_at          timestamptz DEFAULT now(),
    last_checked_at        timestamptz,
    last_fired_at          timestamptz,
    fire_count             integer NOT NULL DEFAULT 0,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT advisory_rules_has_trigger
        CHECK (jsonb_array_length(trigger_entities) > 0 OR check_interval_seconds IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_advisory_rules_due ON hospilot_app.advisory_rules (enabled, next_check_at);

DROP TRIGGER IF EXISTS set_hospilot_app_advisory_rules_updated_at ON hospilot_app.advisory_rules;
CREATE TRIGGER set_hospilot_app_advisory_rules_updated_at
  BEFORE UPDATE ON hospilot_app.advisory_rules
  FOR EACH ROW EXECUTE FUNCTION hospilot_app.set_current_timestamp_updated_at();

CREATE TABLE IF NOT EXISTS hospilot_app.advisories (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_key         text NOT NULL,
    topic            text NOT NULL,
    severity         text NOT NULL DEFAULT 'warning',
    title            text NOT NULL,
    detail           text,
    data             jsonb NOT NULL DEFAULT '{}'::jsonb,
    suggested_action text,
    status           text NOT NULL DEFAULT 'active'
        CONSTRAINT advisories_status_check
        CHECK (status IN ('active', 'acknowledged', 'resolved')),
    acknowledged_by  uuid,             -- control-plane users.id (no cross-DB FK)
    acknowledged_at  timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_advisories_status_created ON hospilot_app.advisories (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_advisories_rule           ON hospilot_app.advisories (rule_key, created_at DESC);

DROP TRIGGER IF EXISTS set_hospilot_app_advisories_updated_at ON hospilot_app.advisories;
CREATE TRIGGER set_hospilot_app_advisories_updated_at
  BEFORE UPDATE ON hospilot_app.advisories
  FOR EACH ROW EXECUTE FUNCTION hospilot_app.set_current_timestamp_updated_at();

-- Seed rules (migrations 059-060). Keep in sync with db/migrations/059_*.sql, 060_*.sql.
INSERT INTO hospilot_app.advisory_rules
  (rule_key, topic, label, condition_description, suggested_action,
   severity, params, trigger_entities, check_interval_seconds, cooldown_seconds)
VALUES
  ('bed_occupancy_high', 'Bed Management', 'High bed occupancy',
   'Bed occupancy > 90%',
   'Prioritize discharge-ready patients and optimize bed allocation',
   'warning', '{"occupancy_pct_threshold": 90}', '["bed", "admission"]', 300, 3600),
  ('bed_occupancy_forecast_critical', 'Bed Management', 'Occupancy forecast critical',
   'Bed occupancy predicted >95% in next 6 hours',
   'Run capacity simulation and notify operations command center',
   'critical', '{"predicted_occupancy_pct_threshold": 95, "horizon_hours": 6}', '[]', 900, 7200),
  ('dirty_beds_backlog', 'Bed Management', 'Housekeeping backlog',
   'Beds awaiting cleaning > 10',
   'Create housekeeping tasks and reprioritize cleaning staff',
   'warning', '{"max_dirty_beds": 10}', '["bed"]', 300, 3600),
  ('er_boarding_pressure', 'Bed Management', 'ER boarding pressure',
   'ER boarding patients > threshold',
   'Reserve next available inpatient beds automatically',
   'warning', '{"max_boarders": 5}', '["visit"]', 300, 3600),
  ('isolation_beds_full', 'Bed Management', 'Isolation capacity exhausted',
   'Isolation beds full',
   'Identify suitable isolation candidates and trigger escalation',
   'critical', '{"min_available_isolation_beds": 1}', '["bed"]', 600, 7200),
  ('icu_stepdown_pending', 'Bed Management', 'ICU step-down candidates',
   'ICU step-down patients identified',
   'Initiate transfer workflow to ward',
   'info', '{"min_candidates": 1}', '["admission", "discharge_ready"]', 900, 14400),
  ('discharged_bed_blocked', 'Bed Management', 'Discharged patient still in bed',
   'Discharged patient still occupying bed',
   'Notify ward, billing, pharmacy, and housekeeping teams',
   'warning', '{"min_blocked_beds": 1}', '["bed", "admission", "discharge_ready"]', 600, 3600),
  ('bed_turnaround_sla', 'Bed Management', 'Bed turnaround SLA breach',
   'Bed turnaround time exceeds SLA',
   'Escalate housekeeping and monitor completion',
   'warning', '{"sla_minutes": 90}', '[]', 600, 3600),
  ('ot_first_case_delayed', 'OT', 'First surgery delayed',
   'First surgery delayed',
   'Notify OT coordinator and adjust downstream schedule',
   'warning', '{"delay_minutes": 15}', '["ot_surgery", "ot_schedule"]', 300, 3600),
  ('ot_surgery_overrun', 'OT', 'Surgery overrun',
   'Surgery overrun >30 min',
   'Recalculate OT schedule and notify affected teams',
   'warning', '{"overrun_minutes": 30}', '["ot_surgery"]', 300, 3600),
  ('ot_room_idle', 'OT', 'Theatre idle with pending cases',
   'OT idle >1 hour',
   'Suggest advancing next surgery',
   'info', '{"idle_minutes": 60}', '["ot_room_status"]', 600, 3600),
  ('ot_emergency_waiting', 'OT', 'Emergency surgery waiting',
   'Emergency surgery waiting',
   'Reprioritize OT schedule automatically',
   'critical', '{"min_waiting": 1, "emergency_priorities": ["Emergency", "Urgent"]}', '["ot_surgery", "ot_schedule"]', 300, 1800),
  ('ot_icu_capacity_post_surgery', 'OT', 'ICU capacity short for surgery',
   'ICU bed unavailable after surgery',
   'Delay elective case or reserve ICU capacity',
   'critical', '{"min_free_icu_beds": 1, "lookahead_hours": 4, "icu_surgery_types": ["Cardiac", "Neuro", "Transplant"]}', '["ot_surgery", "bed"]', 600, 7200),
  ('ot_equipment_unavailable', 'OT', 'Theatre equipment unavailable',
   'Equipment unavailable',
   'Notify biomedical team and suggest alternate OT',
   'warning', '{"min_affected": 1}', '["ot_room_status", "ot_room"]', 600, 3600),
  ('discharge_fit_pending', 'Discharge', 'Medically fit, discharge pending',
   'Patient medically fit but pending discharge',
   'Launch discharge completion workflow',
   'warning', '{"min_pending": 1, "grace_minutes": 60}', '["admission", "discharge_ready"]', 600, 3600),
  ('discharge_billing_pending', 'Discharge', 'Billing blocking discharge',
   'Billing pending',
   'Trigger Billing Agent for immediate processing',
   'warning', '{"min_pending": 1}', '["admission", "discharge_ready"]', 600, 3600),
  ('discharge_pharmacy_pending', 'Discharge', 'Discharge medication not ready',
   'Pharmacy pending',
   'Prioritize discharge medication preparation',
   'warning', '{"min_pending": 1, "pending_statuses": ["pending", "on_hold"]}', '["admission", "discharge_ready", "pharmacy_order"]', 600, 3600),
  ('discharge_summary_pending', 'Discharge', 'Discharge summary missing',
   'Discharge summary pending',
   'Notify treating physician automatically',
   'warning', '{"min_pending": 1}', '["admission", "discharge_ready", "discharge_summary"]', 600, 3600),
  ('discharge_insurance_pending', 'Discharge', 'Insurance approval stuck',
   'Insurance approval pending',
   'Escalate to insurance desk',
   'info', '{"min_pending": 1, "pending_hours": 4}', '["admission", "discharge_ready"]', 900, 7200),
  ('discharge_delayed', 'Discharge', 'Discharge delayed',
   'Delayed discharge >2 hours',
   'Notify operations manager and department head',
   'warning', '{"min_pending": 1, "delay_hours": 2}', '["admission", "discharge_ready"]', 600, 3600),
  ('lab_tat_sla', 'Laboratory', 'Lab turnaround SLA breach',
   'Lab TAT exceeds SLA',
   'Prioritize pending samples',
   'warning', '{"sla_minutes": 120, "stat_sla_minutes": 60, "stat_priorities": ["stat", "urgent", "asap"]}', '["lab_order", "lab_result"]', 300, 3600),
  ('lab_critical_result', 'Laboratory', 'Critical result uncommunicated',
   'Critical result pending communication',
   'Notify treating physician immediately',
   'critical', '{"min_pending": 1, "pending_minutes": 15, "max_age_hours": 24, "critical_flags": ["critical", "critical high", "critical low", "panic"]}', '["lab_result"]', 120, 900),
  ('lab_analyzer_down', 'Laboratory', 'Analyzer down',
   'Analyzer downtime',
   'Redirect samples to alternate analyzer',
   'warning', '{"min_down": 1, "up_statuses": ["Online"]}', '["lab_analyzer"]', 300, 3600),
  ('lab_collection_delayed', 'Laboratory', 'Sample collection delayed',
   'Sample collection delayed',
   'Notify nursing staff',
   'warning', '{"min_pending": 1, "delay_minutes": 60}', '["lab_order", "lab_sample"]', 300, 3600),
  ('lab_sample_rejections', 'Laboratory', 'Sample rejections rising',
   'Sample rejection increasing',
   'Alert lab supervisor',
   'warning', '{"max_rejections": 3, "window_hours": 24, "rejected_statuses": ["Rejected", "Missing"]}', '["lab_sample"]', 600, 7200),
  ('rc_claims_pending', 'Revenue Cycle', 'Claims pending high',
   'Claims pending > threshold',
   'Prioritize claim submission',
   'warning', '{"max_pending": 10, "pending_statuses": ["Submitted", "Query"]}', '[]', 1800, 14400),
  ('rc_claim_denial_spike', 'Revenue Cycle', 'Claim denial spike',
   'Claim rejection spike',
   'Launch denial management workflow',
   'warning', '{"max_denials": 3, "window_days": 7}', '[]', 1800, 14400),
  ('rc_billing_backlog', 'Revenue Cycle', 'Billing backlog',
   'Billing backlog',
   'Redistribute billing workload',
   'warning', '{"max_draft_invoices": 15}', '[]', 1800, 14400),
  ('rc_collections_overdue', 'Revenue Cycle', 'Collections below target',
   'Collections below target',
   'Notify finance team with recovery list',
   'warning', '{"max_overdue_amount": 100000, "overdue_grace_days": 7}', '[]', 3600, 28800),
  ('rc_revenue_leakage', 'Revenue Cycle', 'Revenue leakage detected',
   'Revenue leakage detected',
   'Trigger billing audit',
   'warning', '{"min_leakage_amount": 50000, "window_days": 30, "settled_statuses": ["Paid", "Approved"]}', '[]', 3600, 28800),
  ('exec_stress_index', 'Executive', 'Hospital stress index high',
   'Hospital Stress Index high',
   'Launch hospital-wide optimization workflow',
   'critical', '{"stress_index_threshold": 70, "weights": {"bed_occupancy": 0.35, "er_boarding": 0.25, "ot_backlog": 0.15, "discharge_delays": 0.15, "lab_tat": 0.10}, "component_norms": {"bed_occupancy": 90, "er_boarding": 5, "ot_backlog": 2, "discharge_delays": 5, "lab_tat": 5}}', '[]', 1800, 21600),
  ('exec_sla_breaches', 'Executive', 'Multiple SLA breaches',
   'Multiple SLA breaches',
   'Escalate to command center dashboard',
   'warning', '{"min_breaches": 3, "window_hours": 4, "sla_rule_keys": ["bed_turnaround_sla", "lab_tat_sla", "lab_collection_delayed", "ot_first_case_delayed", "discharge_delayed"]}', '[]', 900, 14400),
  ('exec_capacity_forecast', 'Executive', 'Capacity forecast critical',
   'Capacity forecast critical',
   'Recommend surge capacity plan',
   'critical', '{"predicted_occupancy_pct": 90, "horizon_hours": 24, "min_wards_critical": 2, "min_ward_beds": 5}', '[]', 3600, 21600),
  ('exec_kpi_deteriorating', 'Executive', 'Operational KPI deteriorating',
   'Operational KPI deteriorating',
   'Generate executive action plan',
   'warning', '{"min_pct_deterioration": 25, "window_hours": 24, "baseline_days": 7, "min_baseline_fires": 5}', '[]', 3600, 86400),
  ('exec_utilization_imbalance', 'Executive', 'Resource utilization imbalance',
   'Resource utilization imbalance',
   'Rebalance beds, staff, and operational resources automatically',
   'warning', '{"max_occupancy_spread_pct": 30, "min_ward_beds": 5, "min_wards": 2}', '[]', 3600, 21600)
ON CONFLICT (rule_key) DO NOTHING;

-- Emergency (ER) rules (migration 060). Keep in sync with db/migrations/060_advisory_rules_er.sql.
INSERT INTO hospilot_app.advisory_rules
  (rule_key, topic, label, condition_description, suggested_action,
   severity, params, trigger_entities, check_interval_seconds, cooldown_seconds)
VALUES
  ('er_wait_time_high', 'Emergency', 'ER wait time exceeded',
   'ER patients waiting longer than 30 minutes',
   'Review the ER triage queue and allocate staff to reduce wait times',
   'warning', '{"wait_threshold_minutes": 30, "min_waiting_patients": 1}',
   '["visit"]', 300, 1800),
  ('er_triage_queue_high', 'Emergency', 'Triage queue increasing',
   'Untriaged ER patients exceed the backlog threshold',
   'Assign a triage nurse and fast-track waiting patients',
   'warning', '{"max_untriaged": 5}',
   '["visit"]', 300, 1800),
  ('er_critical_patient_waiting', 'Emergency', 'Critical patient waiting',
   'One or more critical patients are awaiting care in the ER',
   'Escalate to the on-call physician and prioritize critical patients immediately',
   'critical', '{"min_critical": 1}',
   '["visit"]', 180, 900),
  ('er_ambulance_arrivals_high', 'Emergency', 'Ambulance arrivals increasing',
   'Inbound ambulances exceed the threshold',
   'Prepare resuscitation bays and pre-stage triage staff for incoming patients',
   'info', '{"max_incoming_ambulances": 3}',
   '["ambulance"]', 300, 1800),
  ('er_occupancy_high', 'Emergency', 'ER occupancy critical',
   'ER occupancy above 95% of capacity',
   'Open surge capacity and expedite admissions/discharges out of the ER',
   'critical', '{"occupancy_pct_threshold": 95, "er_capacity": 50}',
   '["visit"]', 300, 1800),
  ('er_boarding_patients_high', 'Emergency', 'Boarding patients increasing',
   'Patients awaiting inpatient admission (boarding) exceed the threshold',
   'Coordinate with bed management to expedite inpatient bed assignment',
   'warning', '{"max_boarding": 20}',
   '["visit", "admission"]', 300, 3600)
ON CONFLICT (rule_key) DO NOTHING;

-- ICU rules (migration 061). Keep in sync with db/migrations/061_advisory_rules_icu.sql.
INSERT INTO hospilot_app.advisory_rules
  (rule_key, topic, label, condition_description, suggested_action,
   severity, params, trigger_entities, check_interval_seconds, cooldown_seconds)
VALUES
  ('icu_occupancy_high', 'ICU', 'High ICU occupancy',
   'ICU occupancy > 90%',
   'Expedite step-down transfers and review pending ICU admissions',
   'warning', '{"occupancy_pct_threshold": 90}',
   '["bed", "admission"]', 300, 3600),
  ('icu_predicted_full', 'ICU', 'ICU predicted full',
   'ML forecast projects ICU census to reach capacity within 24h',
   'Pre-plan overnight ICU beds and anaesthetist/intensivist cover',
   'warning', '{}',
   '[]', 900, 3600),
  ('icu_ventilator_utilization_high', 'ICU', 'Ventilator utilization high',
   'In-use ventilators > 85% of operational units',
   'Audit weaning candidates and check maintenance-held ventilators',
   'warning', '{"utilization_pct_threshold": 85}',
   '["ventilator"]', 300, 3600),
  ('icu_step_down_eligible', 'ICU', 'Step-down eligible patients',
   'ICU patients flagged ready for step-down / discharge',
   'Coordinate step-down transfers to free ICU capacity',
   'info', '{"min_eligible": 1}',
   '["admission", "discharge_ready"]', 300, 3600),
  ('icu_nurse_ratio_below_policy', 'ICU', 'ICU nurse ratio below policy',
   'ICU nurse-to-patient load exceeds policy',
   'Reallocate nursing staff or call in additional ICU nurses',
   'warning', '{"roster_area": "icu", "max_patients_per_nurse": 2}',
   '["staff_roster", "admission"]', 600, 3600),
  ('icu_admission_pending', 'ICU', 'ICU admission pending',
   'ICU has no free beds -- incoming admissions will queue',
   'Trigger overflow protocol and expedite discharges out of ICU',
   'critical', '{"max_free_beds": 0}',
   '["bed", "admission"]', 300, 1800)
ON CONFLICT (rule_key) DO NOTHING;

-- Staffing rules (migration 062). Keep in sync with db/migrations/062_advisory_rules_staffing.sql.
INSERT INTO hospilot_app.advisory_rules
  (rule_key, topic, label, condition_description, suggested_action,
   severity, params, trigger_entities, check_interval_seconds, cooldown_seconds)
VALUES
  ('staffing_shortage_detected', 'Staffing', 'Staff shortage detected',
   'On-duty staff below the required minimum',
   'Call in on-call staff and redistribute workload across departments',
   'warning', '{"min_on_duty_staff": 40}',
   '["staff"]', 600, 3600),
  ('staffing_nurse_ratio_below_threshold', 'Staffing', 'Nurse ratio below threshold',
   'A nursing area exceeds the patients-per-nurse threshold',
   'Reallocate nurses to the affected area or call in float-pool nurses',
   'warning', '{"max_patients_per_nurse": 6}',
   '["staff_roster"]', 600, 3600),
  ('staffing_high_absenteeism', 'Staffing', 'High absenteeism',
   'Share of staff off duty exceeds the absenteeism threshold',
   'Activate absence-cover roster and confirm shift backfills',
   'warning', '{"absenteeism_pct_threshold": 15}',
   '["staff"]', 600, 3600),
  ('staffing_overtime_above_limit', 'Staffing', 'Overtime above limit',
   'Staff recorded overtime hours above the per-staff limit',
   'Rotate rest, backfill shifts and review overtime spend',
   'info', '{"overtime_hours_limit": 12, "min_staff_over": 1}',
   '["staff"]', 900, 3600),
  ('staffing_icu_shortage', 'Staffing', 'ICU staffing shortage',
   'ICU nurse-to-patient load exceeds policy',
   'Assign additional ICU-certified nurses or trigger critical-care cover',
   'critical', '{"roster_area": "icu", "max_patients_per_nurse": 2}',
   '["staff_roster", "admission"]', 600, 1800)
ON CONFLICT (rule_key) DO NOTHING;

-- Ambulance rules (migration 063). Keep in sync with db/migrations/063_advisory_rules_ambulance.sql.
INSERT INTO hospilot_app.advisory_rules
  (rule_key, topic, label, condition_description, suggested_action,
   severity, params, trigger_entities, check_interval_seconds, cooldown_seconds)
VALUES
  ('ambulance_none_available', 'Ambulance', 'No ambulance available',
   'No ambulance available in the active fleet',
   'Dispatch nearest available ambulance',
   'critical', '{"min_available": 1}',
   '["ambulance"]', 120, 900),
  ('ambulance_eta_exceeds_sla', 'Ambulance', 'ETA exceeds SLA',
   'A dispatched ambulance ETA exceeds the SLA',
   'Notify ER and suggest alternate ambulance',
   'warning', '{"sla_minutes": 15, "min_over": 1}',
   '["ambulance"]', 120, 900),
  ('ambulance_multiple_emergency_calls', 'Ambulance', 'Multiple emergency calls',
   'Multiple concurrent emergency calls in progress',
   'Prioritize cases based on severity',
   'warning', '{"max_concurrent_emergencies": 3}',
   '["ambulance"]', 120, 900),
  ('ambulance_maintenance_overdue', 'Ambulance', 'Ambulance maintenance overdue',
   'One or more ambulances are overdue for maintenance',
   'Remove vehicle from active fleet',
   'info', '{"min_overdue": 1}',
   '["ambulance"]', 3600, 86400),
  ('ambulance_demand_surge_predicted', 'Ambulance', 'Demand surge predicted',
   'ML forecast predicts an emergency-demand surge',
   'Position ambulances strategically',
   'info', '{}',
   '["ambulance"]', 900, 3600)
ON CONFLICT (rule_key) DO NOTHING;

-- Pharmacy rules (migration 064). Keep in sync with db/migrations/064_advisory_rules_pharmacy.sql.
INSERT INTO hospilot_app.advisory_rules
  (rule_key, topic, label, condition_description, suggested_action,
   severity, params, trigger_entities, check_interval_seconds, cooldown_seconds)
VALUES
  ('pharmacy_drug_out_of_stock', 'Pharmacy', 'Drug out of stock',
   'One or more drugs have zero stock on hand',
   'Recommend substitute medication and notify procurement',
   'critical', '{"min_out_of_stock": 1}',
   '["pharmacy_inventory"]', 600, 3600),
  ('pharmacy_queue_increasing', 'Pharmacy', 'Pharmacy queue increasing',
   'Undispensed pharmacy orders exceed the queue threshold',
   'Open additional dispensing counter',
   'warning', '{"max_queue": 5}',
   '["pharmacy_order"]', 300, 1800),
  ('pharmacy_delivery_delayed', 'Pharmacy', 'Medication delivery delayed',
   'Medication orders undispensed beyond the delivery SLA',
   'Prioritize discharge medications',
   'warning', '{"delay_minutes": 120, "min_delayed": 1}',
   '["pharmacy_order"]', 300, 1800),
  ('pharmacy_controlled_discrepancy', 'Pharmacy', 'Controlled drug discrepancy',
   'Controlled-drug log shows a count variance or incomplete documentation',
   'Trigger compliance audit',
   'critical', '{"min_issues": 1, "lookback_hours": 24}',
   '["pharmacy_order"]', 600, 1800),
  ('pharmacy_inventory_below_reorder', 'Pharmacy', 'Inventory below reorder level',
   'Inventory items at or below their reorder level',
   'Create procurement request',
   'warning', '{"min_below": 1}',
   '["pharmacy_inventory"]', 600, 3600)
ON CONFLICT (rule_key) DO NOTHING;

-- Patient Flow rules (migration 065). Keep in sync with db/migrations/065_advisory_rules_patient_flow.sql.
INSERT INTO hospilot_app.advisory_rules
  (rule_key, topic, label, condition_description, suggested_action,
   severity, params, trigger_entities, check_interval_seconds, cooldown_seconds)
VALUES
  ('patient_admission_waiting', 'Patient Flow', 'Admission waiting >30 min',
   'Admitted patients awaiting bed assignment longer than 30 minutes',
   'Optimize bed assignment',
   'warning', '{"wait_threshold_minutes": 30, "min_waiting": 1}',
   '["admission", "bed"]', 300, 1800),
  ('patient_transfer_pending', 'Patient Flow', 'Transfer pending',
   'One or more patient transfers are pending',
   'Initiate patient transfer workflow',
   'warning', '{"min_transfers": 1}',
   '["admission"]', 300, 1800),
  ('patient_diagnostic_delay_discharge', 'Patient Flow', 'Diagnostic delay affecting discharge',
   'Discharge blocked by pending diagnostics/investigations',
   'Escalate pending investigations',
   'warning', '{"min_delayed": 1}',
   '["admission", "lab_result", "lab_order"]', 600, 1800),
  ('patient_referral_pending', 'Patient Flow', 'Referral pending',
   'Patients awaiting a specialty referral/consult (heuristic)',
   'Notify specialty department',
   'info', '{"min_pending": 1}',
   '["admission"]', 600, 3600),
  ('patient_readmission_risk', 'Patient Flow', 'Readmission risk identified',
   'Patients discharged then readmitted (readmission-risk proxy)',
   'Schedule care coordination follow-up',
   'warning', '{"min_readmissions": 1}',
   '["admission", "discharge_ready"]', 600, 3600)
ON CONFLICT (rule_key) DO NOTHING;

-- ── Advisory rule definitions (migration 070) ────────────────────────────────
-- Seed each rule's logic JSON AFTER the rule INSERTs above so fresh tenants are
-- DB-driven (28 declarative rules have no code evaluator). Keep in sync with
-- db/migrations/070_advisory_rule_definition.sql.
-- 1. Default EVERY rule to a handler-ref to its existing evaluator (byte-identical
--    behaviour; makes all rules DB-driven). Only fills rules without a condition.
UPDATE hospilot_app.advisory_rules
SET definition = jsonb_build_object('condition', jsonb_build_object('handler', rule_key))
WHERE NOT (definition ? 'condition');

-- 2. Promote the threshold-style rules to declarative conditions. Each replaces
--    the handler-ref set in step 1 (guarded so operator-edited rules are kept).
--    detail_template wording matches the pre-declarative evaluators exactly.

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"beds_summary","kind":"dict","field":"occupancy_pct","operator":">","threshold":90,"detail_template":"Bed occupancy is {occupancy_pct:.0f}% ({occupied_beds}/{total_beds} beds), threshold {threshold:.0f}%"}}'
  WHERE rule_key='bed_occupancy_high' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"dirty_beds","aggregate":"count","operator":">","threshold":10,"detail_template":"{value} beds awaiting cleaning (threshold {threshold:.0f})"}}'
  WHERE rule_key='dirty_beds_backlog' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"icu_admissions","aggregate":"count","operator":">=","threshold":1,"filter":[{"field":"discharge_ready","op":"truthy"}],"detail_template":"{value} ICU patient(s) ready for step-down to ward"}}'
  WHERE rule_key='icu_stepdown_pending' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"er_long_wait","args":{"minutes":30},"aggregate":"count","operator":">=","threshold":1,"labels":{"minutes":30},"detail_template":"{value} ER patient(s) waiting over {minutes:g} min (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='er_wait_time_high' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"untriaged","aggregate":"count","operator":">","threshold":5,"detail_template":"{value} patient(s) awaiting triage (threshold {threshold:.0f})"}}'
  WHERE rule_key='er_triage_queue_high' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"critical_vitals","aggregate":"count","operator":">=","threshold":1,"filter":[{"field":"is_critical","op":"truthy"}],"detail_template":"{value} critical patient(s) awaiting care (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='er_critical_patient_waiting' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"ambulances","aggregate":"count","operator":">","threshold":3,"filter":[{"field":"status","op":"in","value":["En Route","En-Route","Enroute","Dispatched","Incoming"]}],"detail_template":"{value} ambulance(s) inbound (threshold {threshold:.0f})"}}'
  WHERE rule_key='er_ambulance_arrivals_high' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"active_er","aggregate":"pct_of","denominator":50,"operator":">","threshold":95,"require_source_nonempty":true,"labels":{"capacity":50},"detail_template":"ER occupancy is {value:.0f}% ({count}/{capacity} capacity), threshold {threshold:.0f}%"}}'
  WHERE rule_key='er_occupancy_high' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"er_pressure","kind":"dict","field":"est_admissions","operator":">","threshold":20,"detail_template":"{value:.0f} patient(s) awaiting admission/boarding (threshold {threshold:.0f})"}}'
  WHERE rule_key='er_boarding_patients_high' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"beds_summary","kind":"dict","field":"icu_pct","operator":">","threshold":90,"require_positive":["icu_total"],"detail_template":"ICU occupancy is {icu_pct:.0f}% ({icu_occupied}/{icu_total} beds), threshold {threshold:.0f}%"}}'
  WHERE rule_key='icu_occupancy_high' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"icu_admissions","aggregate":"count","operator":">=","threshold":1,"filter":[{"field":"discharge_ready","op":"truthy"}],"detail_template":"{value} ICU patient(s) eligible for step-down (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='icu_step_down_eligible' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"beds_summary","kind":"dict","field":"icu_available","operator":"<=","threshold":0,"require_positive":["icu_total"],"detail_template":"ICU has {icu_available} free bed(s) of {icu_total} -- incoming admissions will queue"}}'
  WHERE rule_key='icu_admission_pending' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"ventilators","aggregate":"ratio","operator":">","threshold":85,"numerator_filter":[{"field":"status","op":"contains_any","value":["use"]}],"denominator_filter":[{"field":"status","op":"contains_any","value":["use","avail"]}],"detail_template":"Ventilator utilization {value:.0f}% ({numerator}/{denominator} in use), threshold {threshold:.0f}%"}}'
  WHERE rule_key='icu_ventilator_utilization_high' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"ambulances","aggregate":"count","operator":"<","threshold":1,"require_source_nonempty":true,"filter":[{"field":"status","op":"==","value":"available"}],"detail_template":"{value} ambulance(s) available (minimum {threshold:.0f}) of {total} fleet"}}'
  WHERE rule_key='ambulance_none_available' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"ambulances","aggregate":"count","operator":">=","threshold":1,"labels":{"sla":15},"filter":[{"field":"eta_mins","op":"not_null"},{"field":"eta_mins","op":">","value":15}],"detail_template":"{value} ambulance(s) with ETA over {sla:g} min SLA (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='ambulance_eta_exceeds_sla' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"ambulances","aggregate":"count","operator":">","threshold":3,"filter":[{"field":"emergency_type","op":"not_null"}],"detail_template":"{value} active emergency call(s) in progress (threshold {threshold:.0f})"}}'
  WHERE rule_key='ambulance_multiple_emergency_calls' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"ambulances","aggregate":"count","operator":">=","threshold":1,"filter":[{"any":[{"field":"status","op":"in","value":["maintenance","out of service","overdue"]},{"field":"next_maintenance","op":"age_gt_minutes","value":0},{"field":"maintenance_due","op":"age_gt_minutes","value":0},{"field":"next_service","op":"age_gt_minutes","value":0}]}],"detail_template":"{value} ambulance(s) overdue for maintenance (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='ambulance_maintenance_overdue' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"pharmacy_inventory","aggregate":"count","operator":">=","threshold":1,"filter":[{"field":"stock_quantity","op":"<=","value":0}],"detail_template":"{value} drug(s) out of stock (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='pharmacy_drug_out_of_stock' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"pharmacy_orders","aggregate":"count","operator":">","threshold":5,"filter":[{"field":"dispensed_at","op":"is_null"},{"field":"status","op":"in","value":["pending","on_hold","dispensing","ordered"]}],"detail_template":"{value} order(s) in the dispensing queue (threshold {threshold:.0f})"}}'
  WHERE rule_key='pharmacy_queue_increasing' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"pharmacy_orders","aggregate":"count","operator":">=","threshold":1,"labels":{"delay":120},"filter":[{"field":"dispensed_at","op":"is_null"},{"field":"prescribed_at","op":"age_gt_minutes","value":120}],"detail_template":"{value} medication order(s) delayed over {delay:g} min (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='pharmacy_delivery_delayed' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"pharmacy_inventory","aggregate":"count","operator":">=","threshold":1,"filter":[{"field":"stock_quantity","op":"le_field","value":"reorder_level"}],"detail_template":"{value} item(s) at/below reorder level (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='pharmacy_inventory_below_reorder' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"pharmacy_controlled_logs","args":{"hours":24},"aggregate":"count","operator":">=","threshold":1,"labels":{"hours":24},"filter":[{"any":[{"field":"variance_detected","op":"truthy"},{"field":"documentation_complete","op":"falsy"}]}],"detail_template":"{value} controlled-drug discrepancy/documentation issue(s) in {hours}h (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='pharmacy_controlled_discrepancy' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"admissions_with_wards","aggregate":"count","operator":">=","threshold":1,"labels":{"minutes":30},"filter":[{"field":"bed_id","op":"is_null"},{"field":"admitted_at","op":"age_gt_minutes","value":30}],"detail_template":"{value} admission(s) awaiting bed assignment over {minutes:g} min (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='patient_admission_waiting' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"admissions_with_wards","aggregate":"count","operator":">=","threshold":1,"filter":[{"field":"transfer_pending","op":"truthy"}],"detail_template":"{value} patient transfer(s) pending (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='patient_transfer_pending' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"admissions_with_wards","aggregate":"count","operator":">=","threshold":1,"filter":[{"field":"discharge_ready","op":"falsy"},{"field":"discharge_blocked_reason","op":"contains_any","value":["pending_tasks","lab","diagnostic","investigation","imaging","result","test"]}],"detail_template":"{value} discharge(s) delayed by pending investigations (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='patient_diagnostic_delay_discharge' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"admissions_with_wards","aggregate":"count","operator":">=","threshold":1,"filter":[{"field":"discharge_blocked_reason","op":"contains_any","value":["referral","consult","specialist","specialty","needs_review","review"]}],"detail_template":"{value} patient(s) with a pending referral/specialty review (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='patient_referral_pending' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"staff","aggregate":"count","operator":"<","threshold":40,"require_source_nonempty":true,"filter":[{"field":"on_duty_status","op":"in","value":["on_duty","on-duty","present","available","working","active","duty"]}],"detail_template":"{value} staff on duty (minimum {threshold:.0f}) of {total} total"}}'
  WHERE rule_key='staffing_shortage_detected' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"staff","aggregate":"pct","operator":">","threshold":15,"require_source_nonempty":true,"filter":[{"field":"on_duty_status","op":"not_in","value":["on_duty","on-duty","present","available","working","active","duty"]}],"detail_template":"Absenteeism {value:.0f}% ({count}/{total} staff off duty), threshold {threshold:.0f}%"}}'
  WHERE rule_key='staffing_high_absenteeism' AND definition->'condition' ? 'handler';

-- ── Fold params into definition + drop the transient params column (migration 071) ──
-- Keep in sync with db/migrations/071_advisory_fold_params.sql.
UPDATE hospilot_app.advisory_rules
SET definition = jsonb_set(definition, '{condition,params}', COALESCE(params, '{}'::jsonb), true)
WHERE definition ? 'condition' AND definition->'condition' ? 'handler';
ALTER TABLE hospilot_app.advisory_rules DROP COLUMN IF EXISTS params;
