const API_BASE = (import.meta.env.VITE_API_URL as string | undefined) ?? ''

export const WS_BASE = API_BASE
	? API_BASE.replace(/^http/, 'ws')
	: `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}`

// ── Types ─────────────────────────────────────────────────────────────────────

export interface BackendAgent {
	id: string
	label: string
	color: string
	role: string
	sub_agents: {
		id: string
		label: string
		role?: string
		subgoal?: string
		tasks: (string | { id: string; label: string; outputs?: string[]; condition?: unknown })[]
		task_edges?: { source: string; target: string; condition?: string }[]
	}[]
	sub_agent_edges?: { source: string; target: string; condition?: string; condition_label?: string }[]
}

export interface BackendPipeline {
	understood_goal: string
	priority: 'urgent' | 'high' | 'normal'
	agents: BackendAgent[]
	edges: { source: string; target: string; condition?: string; condition_label?: string }[]
}

export interface CreateSessionResponse {
	session_id: string
	status: string
	autonomous?: boolean
	pipeline?: BackendPipeline
}

export interface ExecuteSessionResponse {
	session_id: string
	status: string
}

export interface ReorchestratePipelineResponse {
	session_id: string
	scope: 'pipeline'
	pipeline: BackendPipeline
}

export interface ReorchestrateSubagentsResponse {
	session_id: string
	scope: 'subagents'
	agent_id: string
	selected_subagents: string[]
	sub_agent_edges?: { source: string; target: string; condition?: string; condition_label?: string }[]
	conditions?: Record<string, string>
}

export interface ReorchestrateTask {
	id: string
	label?: string
	condition?: unknown       // {symbol, op, value} | null -- carried through to the pipeline
	outputs?: string[]
}

export interface ReorchestrateTasksResponse {
	session_id: string
	scope: 'tasks'
	agent_id: string
	subagent_id: string
	selected_tasks: ReorchestrateTask[]
}

export type ReorchestrateResponse = ReorchestratePipelineResponse | ReorchestrateSubagentsResponse | ReorchestrateTasksResponse

// ── Auth token helpers ─────────────────────────────────────────────────────────

const TOKEN_KEY = 'hospilot_token'
export const getToken = () => localStorage.getItem(TOKEN_KEY)
export const setToken = (t: string) => localStorage.setItem(TOKEN_KEY, t)
export const clearToken = () => localStorage.removeItem(TOKEN_KEY)

function authHeader(): Record<string, string> {
	const t = getToken()
	return t ? { 'Authorization': `Bearer ${t}` } : {}
}

// ── super_admin org targeting ───────────────────────────────────────────────
// A super_admin's JWT carries org_id: null, so org-scoped routes need the target
// tenant supplied out-of-band via ?org_id=. The header org switcher persists the
// choice here; `withOrg()` appends it to session routes. No-op for regular users
// (none is ever set) — and the backend ignores org_id for non-super callers.

const ACTIVE_ORG_KEY = 'hospilot_active_org'
export const getActiveOrgId = () => localStorage.getItem(ACTIVE_ORG_KEY)
export const setActiveOrgId = (id: string) => localStorage.setItem(ACTIVE_ORG_KEY, id)
export const clearActiveOrgId = () => localStorage.removeItem(ACTIVE_ORG_KEY)

function withOrg(path: string): string {
	const org = getActiveOrgId()
	if (!org) return path
	return `${path}${path.includes('?') ? '&' : '?'}org_id=${encodeURIComponent(org)}`
}

// ── HTTP helpers ───────────────────────────────────────────────────────────────

function parseApiError(detail: unknown, fallback: string): string {
	if (Array.isArray(detail))
		return detail.map((d: { msg?: string }) => d.msg ?? JSON.stringify(d)).join('; ')
	if (typeof detail === 'string') return detail
	if (detail && typeof detail === 'object' && 'error' in detail) return String((detail as { error: unknown }).error)
	return fallback
}

// Multi-tenancy: the backend now issues v2 tokens (org_id + ver claim). Any
// pre-migration token gets a 401 on every endpoint -- clear it and land the
// user back on the AuthScreen for one re-login. Auth endpoints are exempt so
// a wrong password doesn't wipe the form/token.
function handleUnauthorized(path: string, status: number) {
	if (status === 401 && !path.startsWith('/api/auth/') && getToken()) {
		clearToken()
		location.reload()
	}
}

// Carries the HTTP status alongside the parsed message so callers can react to
// specific codes (e.g. a 409 "not in the expected state" conflict) instead of
// only surfacing the message as an unconditional failure.
export class ApiError extends Error {
	status: number
	constructor(message: string, status: number) {
		super(message)
		this.status = status
	}
}

async function post<T>(path: string, body: unknown): Promise<T> {
	const res = await fetch(`${API_BASE}${path}`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', ...authHeader() },
		body: JSON.stringify(body),
	})
	if (!res.ok) {
		handleUnauthorized(path, res.status)
		const err = await res.json().catch(() => ({ detail: res.statusText }))
		throw new ApiError(parseApiError(err?.detail, res.statusText), res.status)
	}
	return res.json() as Promise<T>
}

async function patch<T>(path: string, body: unknown): Promise<T> {
	const res = await fetch(`${API_BASE}${path}`, {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json', ...authHeader() },
		body: JSON.stringify(body),
	})
	if (!res.ok) {
		handleUnauthorized(path, res.status)
		const err = await res.json().catch(() => ({ detail: res.statusText }))
		throw new Error(parseApiError(err?.detail, res.statusText))
	}
	return res.json() as Promise<T>
}

async function get<T>(path: string): Promise<T> {
	const res = await fetch(`${API_BASE}${path}`, { headers: authHeader() })
	if (!res.ok) {
		handleUnauthorized(path, res.status)
		const err = await res.json().catch(() => ({ detail: res.statusText }))
		throw new Error(parseApiError((err as { detail?: unknown })?.detail, res.statusText))
	}
	return res.json() as Promise<T>
}

// ── Auth API ───────────────────────────────────────────────────────────────────

export type UserRole = 'doctor' | 'admin' | 'approver' | 'super_admin'

export interface AuthUser {
	id: string
	username: string
	display_name: string
	role: UserRole
	org_id: string | null     // null only for super_admin (platform-level)
	org_name?: string | null  // tenant display name ("Carer"); null for super_admin
}

export interface AuthResponse {
	token: string
	user: AuthUser
}

export async function loginUser(username: string, password: string): Promise<AuthResponse> {
	return post<AuthResponse>('/api/auth/login', { username, password })
}

// Signup no longer returns a token: the account lands `pending` until the org
// admin (or super admin) approves it.
export interface SignupResponse {
	status: 'pending'
	message: string
}

export async function signupUser(
	username: string, password: string, display_name: string, org_id: string, role = 'doctor'
): Promise<SignupResponse> {
	return post<SignupResponse>('/api/auth/signup', { username, password, display_name, org_id, role })
}

export async function getMe(): Promise<AuthUser> {
	return get<AuthUser>('/api/auth/me')
}

// ── Organizations (multi-tenancy) ─────────────────────────────────────────────

export interface PublicOrg {
	id: string
	name: string
}

export interface Organization extends PublicOrg {
	slug: string
	status: 'provisioning' | 'active' | 'disabled'
	db_name: string | null
	hasura_source: string | null
	root_prefix: string
	created_at: string
}

/** Active orgs for the signup picker -- unauthenticated. */
export async function fetchPublicOrgs(): Promise<PublicOrg[]> {
	const res = await get<{ organizations: PublicOrg[] }>('/api/orgs/public')
	return res.organizations
}

/** super_admin: full org list. */
export async function fetchOrgs(): Promise<Organization[]> {
	const res = await get<{ organizations: Organization[] }>('/api/orgs')
	return res.organizations
}

/** super_admin: create an org (status `provisioning` until the tenant DB is provisioned). */
export async function createOrg(name: string, slug: string): Promise<Organization> {
	return post<Organization>('/api/orgs', { name, slug })
}

/** super_admin: rename / enable / disable an org. */
export async function updateOrg(
	orgId: string,
	fields: {
		name?: string
		status?: 'active' | 'disabled'
	}
): Promise<Organization> {
	return patch<Organization>(`/api/orgs/${orgId}`, fields)
}

// ── User management (multi-tenancy RBAC) ──────────────────────────────────────

export interface ManagedUser {
	id: string
	username: string
	display_name: string
	role: UserRole
	org_id: string | null
	status: 'pending' | 'active' | 'rejected' | 'disabled'
	approved_by: string | null
	approved_at: string | null
	created_at: string
}

/** admin: users of the caller's org (super_admin: all, or ?org_id=). */
export async function fetchUsers(orgId?: string): Promise<ManagedUser[]> {
	const q = orgId ? `?org_id=${encodeURIComponent(orgId)}` : ''
	const res = await get<{ users: ManagedUser[] }>(`/api/users${q}`)
	return res.users
}

/** admin: the new-user approval queue. */
export async function fetchPendingUsers(orgId?: string): Promise<ManagedUser[]> {
	const q = orgId ? `?org_id=${encodeURIComponent(orgId)}` : ''
	const res = await get<{ users: ManagedUser[] }>(`/api/users/pending${q}`)
	return res.users
}

export async function approveUser(userId: string): Promise<ManagedUser> {
	return post<ManagedUser>(`/api/users/${userId}/approve`, {})
}

export async function rejectUser(userId: string): Promise<ManagedUser> {
	return post<ManagedUser>(`/api/users/${userId}/reject`, {})
}

/** admin: change a user's role (doctor <-> approver) or status (active/disabled). */
export async function updateUser(
	userId: string, fields: { role?: UserRole; status?: 'active' | 'disabled' }
): Promise<ManagedUser> {
	return patch<ManagedUser>(`/api/users/${userId}`, fields)
}

// ── API calls ─────────────────────────────────────────────────────────────────

// ── Agent Registry (read-only — editing lives on the internal build only) ─────

export interface RegistryTask {
	id: string
	label: string
	outputs: string[]
}

export interface RegistrySubAgent {
	id: string
	label: string
	description: string
	capabilities: string[]
	is_prefetch_eligible: boolean
	tasks: RegistryTask[]
}

export interface RegistryAgent {
	id: string
	label: string
	description: string
	emoji: string
	color: string
	subagents: RegistrySubAgent[]
}

export async function fetchAgentRegistry(): Promise<RegistryAgent[]> {
	const res = await fetch(`${API_BASE}/api/agents/registry`, { headers: authHeader() })
	if (!res.ok) throw new Error(`Registry fetch failed: ${res.statusText}`)
	return res.json() as Promise<RegistryAgent[]>
}

export interface PendingApproval {
	id: string
	agent_id: string
	action_type: string
	payload: Record<string, unknown>
}

export interface AllPendingApproval {
	id: string
	session_id: string
	agent_id: string
	action_type: string
	payload: Record<string, unknown>
	status: string
	created_at: string
	escalation_level: number
	user_display_name?: string
}

export interface SessionSummary {
	id: string
	goal: string
	name?: string | null      // editable display name (Workflows page); null → "New Workflow"
	autonomous?: boolean
	status: 'pending' | 'running' | 'complete_pending' | 'submitted' | 'completed' | 'failed' | 'cancelled'
	priority: 'urgent' | 'high' | 'normal'
	created_at: string
	updated_at: string
	// Links a run back to the recurring schedule that spawned it (Phase 6). Absent on
	// manually-run sessions and until the scheduler backend ships — the Workflows page's
	// "runs of this schedule" filter degrades to empty when it's missing.
	scheduled_query_id?: string | null
}

export interface SessionDetail extends SessionSummary {
	constraints: string
	pipeline: BackendPipeline | null
	pipeline_snapshot: BackendPipeline | null
	synthesis_result: { headline: string; actions: string[]; risk: string; summary: string } | null
}

export async function fetchAllPendingApprovals(): Promise<AllPendingApproval[]> {
	const res = await fetch(`${API_BASE}/api/approvals/pending`, { headers: authHeader() })
	if (!res.ok) return []
	const data = await res.json() as { approvals: AllPendingApproval[] }
	return data.approvals
}

export async function fetchPendingApprovals(sessionId: string): Promise<PendingApproval[]> {
	const res = await fetch(`${API_BASE}${withOrg(`/api/sessions/${sessionId}/pending-approvals`)}`, { headers: authHeader() })
	if (!res.ok) return []
	return res.json() as Promise<PendingApproval[]>
}

export async function createSession(
	goal: string,
	constraints: string,
): Promise<CreateSessionResponse> {
	return post<CreateSessionResponse>(withOrg('/api/sessions'), { goal, constraints, autonomous: false })
}

export async function executeSession(
	sessionId: string,
	pipeline: BackendPipeline,
	agentTaskOverrides: Record<string, { id: string; label: string; active: boolean; tasks: string[] }[]> = {},
): Promise<ExecuteSessionResponse> {
	return post<ExecuteSessionResponse>(withOrg(`/api/sessions/${sessionId}/execute`), {
		pipeline,
		agent_task_overrides: agentTaskOverrides,
	})
}

export async function commitSession(
	sessionId: string,
): Promise<{ session_id: string; committed_beds: number }> {
	return post<{ session_id: string; committed_beds: number }>(
		withOrg(`/api/sessions/${sessionId}/commit`),
		{},
	)
}

export async function reorchestrateSession(
	sessionId: string,
	body: { feedback?: string; agent_id?: string; subagent_id?: string } = {},
): Promise<ReorchestrateResponse> {
	return post<ReorchestrateResponse>(withOrg(`/api/sessions/${sessionId}/reorchestrate`), body)
}

export async function identifyPatient(
	sessionId: string,
	mobiles: string[],
): Promise<{ session_id: string; mobiles: string[]; status: string }> {
	return post(withOrg(`/api/sessions/${sessionId}/identify-patient`), { mobiles })
}

export async function decideApproval(
	approvalId: string,
	decision: 'approved' | 'rejected',
	overrideVehicleNo?: string,
): Promise<void> {
	// The server records the approver from the JWT -- no approver_id in the body.
	await post(`/api/approvals/${approvalId}/decide`, {
		decision,
		...(overrideVehicleNo ? { override_vehicle_no: overrideVehicleNo } : {}),
	})
}

export async function listSessions(limit = 50): Promise<{ sessions: SessionSummary[] }> {
	const res = await fetch(`${API_BASE}${withOrg(`/api/sessions?limit=${limit}`)}`, { headers: authHeader() })
	if (!res.ok) throw new Error(`listSessions failed: ${res.statusText}`)
	return res.json() as Promise<{ sessions: SessionSummary[] }>
}

export async function getSession(sessionId: string): Promise<SessionDetail> {
	const res = await fetch(`${API_BASE}${withOrg(`/api/sessions/${sessionId}`)}`, { headers: authHeader() })
	if (!res.ok) throw new Error(`getSession failed: ${res.statusText}`)
	return res.json() as Promise<SessionDetail>
}

// Rename a workflow (Workflows page). Blank clears back to the "New Workflow" default.
export async function renameSession(sessionId: string, name: string): Promise<{ session_id: string; name: string | null }> {
	return patch(`/api/sessions/${sessionId}/name`, { name })
}

export async function updateSessionPipeline(
	sessionId: string,
	pipeline: BackendPipeline,
): Promise<void> {
	await patch(withOrg(`/api/sessions/${sessionId}`), { pipeline })
}

// ── Pause / Checkpoint-Rewind / Edit-Resume (autonomous mode only) ──────────────
// Pause is cooperative and has no dedicated "confirmed paused" WS event — the caller
// must confirm parking by polling fetchPausedQueue() until the session appears with
// kind "user_paused" (see store.ts).

export async function pauseSession(sessionId: string): Promise<{ session_id: string; status: string }> {
	return post(`/api/sessions/${sessionId}/pause`, {})
}

export async function resumeSession(sessionId: string): Promise<{ session_id: string; status: string }> {
	return post(`/api/sessions/${sessionId}/resume`, {})
}

export async function cancelSession(sessionId: string): Promise<{ session_id: string; status: string }> {
	return post(`/api/sessions/${sessionId}/cancel`, {})
}

export interface Checkpoint {
	checkpoint_id: string
	step: number
	completed_agents: string[]
	skipped: string[]
	next: string[]      // ["__start__"] / ["__synthesise__"] are bookkeeping rows — filter out for display
	created_at: string
}

export async function fetchCheckpoints(sessionId: string): Promise<{ checkpoints: Checkpoint[] }> {
	const res = await fetch(`${API_BASE}/api/sessions/${sessionId}/checkpoints`, { headers: authHeader() })
	if (!res.ok) throw new Error(`fetchCheckpoints failed: ${res.statusText}`)
	return res.json() as Promise<{ checkpoints: Checkpoint[] }>
}

export async function editResumeSession(
	sessionId: string,
	pipeline: BackendPipeline,
	checkpointId?: string,
): Promise<{ session_id: string; status: string; checkpoint_id: string }> {
	return post(`/api/sessions/${sessionId}/edit-resume`, {
		pipeline,
		...(checkpointId ? { checkpoint_id: checkpointId } : {}),
	})
}

export interface PausedFlow {
	session_id: string
	kind: 'user_paused' | 'approval' | 'patient_identification' | 'patient_registration'
	action_type?: string
	goal: string
	autonomous: boolean
	agent_id?: string
	current_step?: unknown
	elapsed_seconds: number
	user_display_name?: string
	created_at: string
	approval_id?: string
}

// Cheap, Redis-backed — safe to poll (used both for pause confirmation and to
// annotate "Paused" onto rows in the Workflows table, whose DB status column
// never changes to "paused" -- pausing is Redis-tracked, not a persisted status).
export async function fetchPausedQueue(): Promise<{ paused: number; flows: PausedFlow[] }> {
	const res = await fetch(`${API_BASE}/api/queues/paused`, { headers: authHeader() })
	if (!res.ok) throw new Error(`fetchPausedQueue failed: ${res.statusText}`)
	return res.json() as Promise<{ paused: number; flows: PausedFlow[] }>
}

// ── Execution Trace ─────────────────────────────────────────────────────────
// Read-only "Langfuse logs" feed. Both channels are unauthenticated per the
// frontend contract; values in inputs/outputs are pre-humanized display strings.

export interface TraceField {
	label: string
	value: string   // ALWAYS a display string
}

export interface TraceStep {
	seq: number      // 0-based, unique & increasing per session — order / dedupe key
	ts: number       // epoch seconds (float)
	kind: 'agent' | 'task' | 'decision'
	agent_id: string | null
	task_id: string | null
	title: string
	status: 'running' | 'completed' | 'failed' | 'skipped'
	summary: string
	inputs: TraceField[]
	outputs: TraceField[]
	error: string | null
}

export interface TraceResponse {
	session_id: string
	steps: TraceStep[]   // server-sorted by seq ascending
}

// Backfill the full ordered step list for a session (Redis-backed, 24h retention).
export async function fetchTrace(sessionId: string): Promise<TraceResponse> {
	const res = await fetch(`${API_BASE}${withOrg(`/api/sessions/${sessionId}/trace`)}`, { headers: authHeader() })
	if (!res.ok) throw new Error(`fetchTrace failed: ${res.statusText}`)
	return res.json() as Promise<TraceResponse>
}

