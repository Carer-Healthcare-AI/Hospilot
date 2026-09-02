import { create } from 'zustand'
import type { Node, Edge } from '@xyflow/react'
import { SCENARIOS, type ScenarioDef, type ApprovalGate, type PipelineEdgeDef } from '../data/scenarios'
import type { TaskDef } from '../data/agents'
import { computeLayout, CONDITION_LABELS } from '../lib/layout'
import { createSession, executeSession, decideApproval, commitSession as apiCommitSession, reorchestrateSession, fetchAgentRegistry, listSessions, getSession, updateSessionPipeline, fetchPendingApprovals, clearToken, pauseSession as apiPauseSession, resumeSession as apiResumeSession, cancelSession as apiCancelSession, fetchPausedQueue, fetchCheckpoints as apiFetchCheckpoints, getActiveOrgId, setActiveOrgId as apiSetActiveOrgId, clearActiveOrgId, ApiError, type BackendPipeline, type RegistryAgent, type PendingApproval, type AuthUser, type Checkpoint } from '../services/api'

// 'pausing' = requested, cooperative wait for the flow to park; 'paused' = confirmed
// parked (via the paused queue -- there's no dedicated WS "confirmed" event).
export type ExecutionStatus = 'idle' | 'running' | 'waiting_approval' | 'complete_pending' | 'submitted' | 'pausing' | 'paused' | 'cancelled'
export type NodeStatus = 'idle' | 'running' | 'complete' | 'waiting' | 'skipped'
export type ActiveView = 'orchestrator' | 'capabilities' | 'approvals' | 'admin' | 'workflows'
// Only 'assisted' is reachable from this UI (no mode switcher) -- the union stays
// so PipelineCanvas/useSessionWebSocket's autonomous-only branches keep compiling;
// they simply never trigger.
export type ExecutionMode = 'assisted' | 'autonomous' | 'advisory'

const ACTIVE_VIEWS: ActiveView[] = ['orchestrator', 'capabilities', 'approvals', 'admin', 'workflows']

// Restore the last-viewed nav tab across a browser refresh — otherwise every reload
// silently bounces back to the hardcoded default (Orchestrator), even if you were on
// Capabilities/Workflows. Role-based gating (App.tsx) still runs after
// this and will correct an inappropriate restored view (e.g. an approver landing
// anywhere but Approvals).
function loadActiveView(): ActiveView {
  const saved = localStorage.getItem('hospilot_active_view')
  return (ACTIVE_VIEWS as string[]).includes(saved ?? '') ? (saved as ActiveView) : 'orchestrator'
}

export interface SubAgentOverride {
  id: string
  label: string
  active: boolean
  tasks: string[]
}

// Autonomous-mode policy engine decision (WS `policy_decision` event).
export interface PolicyDecision {
  id: string
  outcome: 'auto_approve' | 'require_human' | 'escalate'
  kind: string
  agentId: string
  actionType: string
  risk: string
  reason: string
  ts: number
}

// Transient notification (autonomous exceptions: require_human / escalate / auto-reject).
export interface Toast {
  id: string
  severity: 'info' | 'warning' | 'critical'
  title: string
  message: string
  sticky?: boolean   // when true, does NOT auto-dismiss — stays until the user clicks X
}

// Sidebar conversation thread — types live here (not in Sidebar.tsx) because the
// thread itself (sidebarTurns below) must survive Sidebar unmounting, which happens
// every time the sub-agent drill-down view opens/closes (App.tsx swaps <Sidebar/>
// for <SubAgentView/> based on subAgentNodeId).
export interface SidebarAgentChip { id: string; label: string; emoji: string; color: string }

export interface SidebarTurn {
  id: string
  role: 'user' | 'system'
  text: string
  constraint?: string
  agents?: SidebarAgentChip[]
  isUpdate?: boolean
}

export interface SubAgentEvent {
  type: 'started' | 'completed' | 'alert'
  subAgentId: string
  result?: Record<string, unknown>
  message?: string
  severity?: string
}

// Runtime evaluation of a single task's condition, from the `task_condition` WS event
export interface TaskConditionState {
  status: 'passed' | 'skipped' | 'failed'
  actual?: unknown
  operator?: string
  threshold?: unknown
}

export interface NodeState {
  status: NodeStatus
  lines: string[]        // simulation / fallback mode
  events: SubAgentEvent[] // real execution mode
  reused?: boolean        // agent_completed.reused:true after an edit-resume — "carried
                          // over" from before the checkpoint, not actually re-executed
}

export interface WorkflowEntry {
  id: string              // session_id for real sessions, wf-xxx for local simulation
  prompt: string
  constraint: string
  scenarioId: string
  scenarioLabel: string
  timestamp: number
  agentIds: string[]
  status?: 'pending' | 'running' | 'complete_pending' | 'submitted' | 'completed' | 'failed' | 'cancelled'
  isLocal?: boolean       // true only for simulation fallback entries
}

// Extends the base ApprovalGate with backend approval ID when driven by real API
export type { ApprovalGate }
declare module '../data/scenarios' {
  interface ApprovalGate {
    approvalId?: string
  }
}

export interface AppState {
  currentUser: AuthUser | null
  setCurrentUser: (user: AuthUser | null) => void
  logout: () => void

  // super_admin only: the tenant the header switcher is currently targeting.
  // Threaded into org-scoped API calls via ?org_id=; null for regular users.
  activeOrgId: string | null
  setActiveOrgId: (orgId: string) => void

  activeScenarioId: string
  scenario: ScenarioDef
  promptText: string
  constraintText: string

  // Backend session state
  sessionId: string | null
  backendPipeline: BackendPipeline | null

  // Live runtime task data, streamed during execution
  livePlans: Record<string, TaskDef[]>              // subAgentId → tasks emitted by agent_plan
  taskConditions: Record<string, TaskConditionState> // taskId → condition evaluation

  pipelineGenerated: boolean
  pipelineLoading: boolean
  pipelineError: string | null

  nodes: Node[]
  edges: Edge[]

  executionStatus: ExecutionStatus
  nodeStates: Record<string, NodeState>
  selectedNodeId: string | null
  panelOpen: boolean

  pendingApprovals: ApprovalGate[]
  // When true, the canvas approval modal is collapsed to a floating "reopen" pill so the
  // user can keep working (e.g. start another query) while an approval sits parked. The
  // parked session keeps running server-side; a freshly-arriving approval un-minimizes.
  approvalMinimized: boolean
  patientIdentificationPending: boolean
  patientIdentificationCount: number | null
  clearPatientIdentification: () => void

  subAgentNodeId: string | null

  sessionRecommendation: { headline: string; actions: string[]; risk: string; summary: string } | null
  synthesisRunning: boolean
  committedSession: boolean

  // Pause / Checkpoint-Rewind / Edit-Resume (autonomous mode only)
  // Editing (rewind/skip/reorder) happens on the dedicated Edit Checkpoint screen,
  // which keeps its own local state rather than mutating the live canvas -- see
  // CheckpointEditorScreen. checkpointEditorOpen just toggles that screen.
  checkpoints: Checkpoint[]
  checkpointsLoading: boolean
  fetchCheckpointsForSession: () => Promise<void>
  checkpointEditorOpen: boolean
  openCheckpointEditor: () => void
  closeCheckpointEditor: () => void

  activeView: ActiveView
  executionMode: ExecutionMode

  // Autonomous mode: policy-engine decision stream + transient toasts.
  policyDecisions: PolicyDecision[]
  toasts: Toast[]

  agentOverrides: Record<string, SubAgentOverride[]>
  selectedSubagentsByAgent: Record<string, string[]>
  selectedTasksBySubagent: Record<string, string[]>
  reorchestratedEdgesByAgent: Record<string, { source: string; target: string; condition?: string; condition_label?: string }[]>

  // Agent registry — fetched from DB on load, replaces static agents.ts
  agentRegistry: RegistryAgent[]
  agentRegistryLoaded: boolean

  workflowHistory: WorkflowEntry[]
  workflowHistoryLoaded: boolean
  pipelineSaveStatus: 'saved' | 'saving' | 'unsaved'
  sessionLoadKey: number   // incremented each time loadSession succeeds — lets Sidebar reset local turns

  // Sidebar conversation thread — see SidebarTurn above for why this lives in the
  // store instead of Sidebar's own useState.
  sidebarTurns: SidebarTurn[]
  setSidebarTurns: (update: SidebarTurn[] | ((prev: SidebarTurn[]) => SidebarTurn[])) => void

  rawEdgeDefs: PipelineEdgeDef[]

  setScenario: (id: string) => void
  setPrompt: (text: string) => void
  setConstraint: (text: string) => void
  setNodes: (nodes: Node[]) => void
  setEdges: (edges: Edge[]) => void
  selectNode: (id: string | null) => void
  generatePipeline: () => void
  updateEdgeCondition: (rawSource: string, rawTarget: string, condition: string | null) => void
  syncRawEdge: (source: string, target: string) => void
  removeRawEdge: (source: string, target: string) => void
  removeRawEdgesForNodes: (nodeIds: string[]) => void
  startExecution: () => void
  confirmAndExecute: () => void
  pauseFlow: () => Promise<void>
  reconcileExecutionStatus: (sessionId: string) => Promise<void>
  resumeFlow: () => Promise<void>
  cancelFlow: () => Promise<void>
  pushPolicyDecision: (d: PolicyDecision) => void
  pushToast: (t: Omit<Toast, 'id'>) => void
  dismissToast: (id: string) => void
  approveGate: () => void
  rejectGate: () => void
  focusApproval: (approvalId: string) => void
  setApprovalMinimized: (minimized: boolean) => void
  dismissExternalApproval: (approvalId: string) => void
  openSubAgent: (nodeId: string) => void
  closeSubAgent: () => void
  reorchestrateLoading: boolean
  reorchestrateWithFeedback: (feedback?: string, agentId?: string, subagentId?: string) => Promise<void>
  resetExecution: () => void
  reOrchestrate: () => void
  submitExecution: () => void
  setActiveView: (view: ActiveView) => void
  setExecutionMode: (mode: ExecutionMode) => void
  saveAgentOverride: (nodeId: string, overrides: SubAgentOverride[]) => void
  loadWorkflow: (id: string) => void
  loadSession: (sessionId: string) => Promise<void>
  fetchWorkflowHistory: () => Promise<void>
  planningStage: string | null
  applyPlanningPipeline: (pipeline: BackendPipeline, sessionId: string) => void
  // Rebuild the canvas from an edited pipeline after an edit-resume (checkpoint editor's
  // Apply & Resume) so the canvas reflects the edit — added agents appear, removed ones
  // vanish — instead of showing the stale pre-edit plan. completedAgentIds seed those
  // nodes as already-complete; live WS events drive the rest.
  applyEditedPipeline: (pipeline: BackendPipeline, completedAgentIds: string[]) => void
  triggerPipelineSave: () => void
  saveNow: () => Promise<void>
  commitSession: () => Promise<void>
  loadAgentRegistry: () => Promise<void>
}

function initialScenario() {
  return SCENARIOS[0]
}

// Converts a DB-fetched PendingApproval into the ApprovalGate shape that ApprovalModal expects.
export function pendingApprovalToGate(a: PendingApproval): ApprovalGate & { approvalId: string } {
  const p = a.payload
  switch (a.action_type) {
    case 'bed_reservation': {
      if (Array.isArray(p.assignments) && p.assignments.length > 0) {
        const count = p.assignments.length
        const summary = (p.summary as string[] | undefined) ?? []
        return {
          agentId: a.agent_id, approvalId: a.id,
          title: count > 1 ? `Batch Bed Reservation (${count} patients)` : 'Bed Reservation',
          recommendation: (summary.length ? summary.join('\n') + '\n\n' : '') +
            'Approving will reserve the beds. Rejecting will release them.',
          action: count > 1 ? `Approve ${count} Reservations` : 'Approve Reservation',
        }
      }
      return {
        agentId: a.agent_id, approvalId: a.id,
        title: 'Bed Reservation',
        recommendation: `Reserve Bed **${String(p.bed_id ?? 'Unknown')}** for patient ${String(p.patient_token ?? 'Unknown').slice(0, 8)}.\n\nApproving will confirm the reservation. Rejecting will release the bed.`,
        action: 'Approve Reservation',
      }
    }
    case 'icu_admission_request': {
      const ventNote = p.ventilator_dependent ? '\n\n⚠ Patient is ventilator dependent.' : ''
      return {
        agentId: a.agent_id, approvalId: a.id,
        title: 'ICU Admission Request',
        recommendation: `ICU admission requested for patient **${String(p.patient_token ?? 'Unknown').slice(0, 8)}** (Rank #${String(p.rank ?? 1)}).\n\n${p.reason ? `**Reason:** ${String(p.reason)}` : ''}${ventNote}\n\nApproving will initiate the ICU admission process.`,
        action: 'Approve Admission',
      }
    }
    case 'icu_transfer_recommendations': {
      const esc = (p.escalation_candidates as unknown[] | undefined) ?? []
      const sd  = (p.step_down_candidates  as unknown[] | undefined) ?? []
      const parts = [
        esc.length ? `${esc.length} patient${esc.length > 1 ? 's' : ''} flagged for ICU escalation` : '',
        sd.length  ? `${sd.length} patient${sd.length > 1 ? 's' : ''} recommended for step-down`    : '',
      ].filter(Boolean)
      return {
        agentId: a.agent_id, approvalId: a.id,
        title: 'ICU Transfer Recommendations',
        recommendation: [
          parts.join(' · ') + '.',
          p.summary ? String(p.summary) : '',
          'Approving will initiate the transfer orders. Rejecting will keep current placement unchanged.',
        ].filter(Boolean).join('\n\n'),
        action: 'Approve Transfers',
      }
    }
    case 'mark_discharge_ready': {
      const count = (p.ready_count as number | undefined) ?? (p.ready_ids as unknown[] | undefined)?.length ?? 0
      return {
        agentId: a.agent_id, approvalId: a.id,
        title: 'Discharge Readiness',
        recommendation: `${count} patient${count === 1 ? '' : 's'} assessed as clinically ready for discharge.\n\nApproving will mark them discharge-ready and trigger discharge summary notes. Rejecting will leave their status unchanged.`,
        action: `Approve ${count} Discharge${count === 1 ? '' : 's'}`,
      }
    }
    case 'ambulance_dispatch': {
      const asgn  = (p.assignment ?? {}) as Record<string, unknown>
      const lines: string[] = []
      if (asgn.assigned_vehicle_no) lines.push(`• **Vehicle:** ${String(asgn.assigned_vehicle_no)}`)
      if (asgn.eta_mins != null)    lines.push(`• **ETA:** ${String(asgn.eta_mins)} min`)
      return {
        agentId: a.agent_id, approvalId: a.id,
        title: 'Ambulance Dispatch',
        recommendation: [
          `**Emergency type:** ${String(p.emergency_type ?? 'Emergency')}`,
          lines.join('\n'),
          typeof asgn.summary === 'string' ? asgn.summary : '',
          'Approving will dispatch the unit. Rejecting will cancel the assignment.',
        ].filter(Boolean).join('\n\n'),
        action: 'Dispatch Ambulance',
      }
    }
    case 'staff_reallocation': {
      const count = (p.recommendations as unknown[] | undefined)?.length ?? 0
      return {
        agentId: a.agent_id, approvalId: a.id,
        title: 'Staffing Reallocation',
        recommendation: [
          `${count} nurse reallocation${count === 1 ? '' : 's'} recommended.`,
          p.summary ? String(p.summary) : '',
          'Approving will notify the float pool. Rejecting will leave current assignments unchanged.',
        ].filter(Boolean).join('\n\n'),
        action: 'Approve Reallocation',
      }
    }
    default: {
      const humanTitle = a.action_type.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
      return {
        agentId: a.agent_id, approvalId: a.id,
        title: humanTitle,
        recommendation: `Review and approve this action.\n\nApproving will proceed with the recommended action. Rejecting will cancel it.`,
        action: 'Approve',
      }
    }
  }
}

const RESET_EDGE_STYLE = { stroke: '#2d4a7a', strokeWidth: 1.5, strokeDasharray: '6,3' }
const CONDITIONAL_EDGE_STYLE = { stroke: '#475569', strokeWidth: 1.5, strokeDasharray: '5,4' }

// Maps backend agent IDs → frontend AGENT_MAP keys
// Node `id` stays as backend ID (for WebSocket matching); `agentId` is the AGENT_MAP key
export const BACKEND_TO_FRONTEND: Record<string, string> = {
  bed_agent:             'bed',
  er_agent:              'er',
  icu_agent:             'icu',
  staff_agent:           'staffing',
  discharge_agent:       'discharge',
  pharmacy_agent:        'pharmacy',
  ot_agent:              'ot',
  bed_prediction_agent:  'bed',
  housekeeping_agent:    'bed',
  revenue_agent:         'revenue',
  billing_agent:         'billing',
  ambulance_agent:       'ambulance',
}

// Inverse map: frontend AGENT_MAP key → canonical backend agent ID
// Defined explicitly to avoid collisions (bed_prediction_agent and housekeeping_agent both map to 'bed')
export const FRONTEND_TO_BACKEND: Record<string, string> = {
  bed:          'bed_agent',
  er:           'er_agent',
  icu:          'icu_agent',
  staffing:     'staff_agent',
  discharge:    'discharge_agent',
  pharmacy:     'pharmacy_agent',
  ot:           'ot_agent',
  revenue:      'revenue_agent',
  billing:      'billing_agent',
  ambulance:    'ambulance_agent',
  lab:          'lab_agent',
}

let pipelineSaveTimer: ReturnType<typeof setTimeout> | null = null

export const useStore = create<AppState>((set, get) => {
  const scenario = initialScenario()

  return {
    activeScenarioId: scenario.id,
    scenario,
    promptText: '',
    constraintText: '',

    sessionId: null,
    backendPipeline: null,

    livePlans: {},
    taskConditions: {},

    rawEdgeDefs: [],
    pipelineGenerated: false,
    pipelineLoading: false,
    pipelineError: null,
    nodes: [],
    edges: [],
    executionStatus: 'idle',
    nodeStates: {},
    selectedNodeId: null,
    panelOpen: false,
    pendingApprovals: [],
    approvalMinimized: false,
    patientIdentificationPending: false,
    patientIdentificationCount: null,
    subAgentNodeId: null,
    sessionRecommendation: null,
    synthesisRunning: false,
    committedSession: false,
    checkpoints: [],
    checkpointsLoading: false,
    checkpointEditorOpen: false,
    activeView: loadActiveView(),
    executionMode: 'assisted',
    policyDecisions: [],
    toasts: [],
    agentOverrides: {},
    selectedSubagentsByAgent: {},
    selectedTasksBySubagent: {},
    reorchestratedEdgesByAgent: {},
    agentRegistry: [],
    agentRegistryLoaded: false,
    workflowHistory: [],
    workflowHistoryLoaded: false,
    pipelineSaveStatus: 'saved',
    planningStage: null,
    sessionLoadKey: 0,
    sidebarTurns: [],
    reorchestrateLoading: false,
    currentUser: null,
    activeOrgId: getActiveOrgId(),

    setSidebarTurns(update) {
      set((s) => ({
        sidebarTurns: typeof update === 'function'
          ? (update as (prev: SidebarTurn[]) => SidebarTurn[])(s.sidebarTurns)
          : update,
      }))
    },

    setCurrentUser(user) { set({ currentUser: user }) },
    logout() { clearToken(); clearActiveOrgId(); set({ currentUser: null, activeOrgId: null }) },

    // Switch the targeted tenant: persist, drop the current (other-org) session so
    // we never act across tenants, and reload this org's workflow history.
    setActiveOrgId(orgId) {
      if (get().activeOrgId === orgId) return
      apiSetActiveOrgId(orgId)
      set({ activeOrgId: orgId })
      get().resetExecution()
      get().fetchWorkflowHistory()
    },

    setScenario(id) {
      const s = SCENARIOS.find((sc) => sc.id === id) ?? SCENARIOS[0]
      set({
        activeScenarioId: id,
        scenario: s,
        sessionId: null,
        backendPipeline: null,
        livePlans: {},
        taskConditions: {},
        pipelineGenerated: false,
        pipelineLoading: false,
        pipelineError: null,
        checkpoints: [],
        nodes: [],
        edges: [],
        executionStatus: 'idle',
        nodeStates: {},
        selectedNodeId: null,
        panelOpen: false,
        pendingApprovals: [],
        subAgentNodeId: null,
        agentOverrides: {},
      })
    },

    setPrompt(text) { set({ promptText: text }) },
    setConstraint(text) { set({ constraintText: text }) },
    setNodes(nodes) { set({ nodes }) },
    setEdges(edges) { set({ edges }) },
    selectNode(id) { set({ selectedNodeId: id }) },

    // ── generatePipeline ─────────────────────────────────────────────────────
    // Calls POST /api/sessions → Claude Haiku guardrail + Claude Sonnet pipeline.
    // On failure (backend unreachable, guardrail rejection, server error), surface
    // a real error state — never fabricate a pipeline the user could mistake for
    // a genuine result.
    generatePipeline() {
      const { promptText, constraintText } = get()
      set({ pipelineLoading: true, pipelineGenerated: false, pipelineError: null, nodes: [], edges: [],
            sessionId: null, backendPipeline: null, livePlans: {}, taskConditions: {},
            selectedSubagentsByAgent: {}, selectedTasksBySubagent: {}, reorchestratedEdgesByAgent: {},
            policyDecisions: [], toasts: [], checkpoints: [] })

      ;(async () => {
        let data: Awaited<ReturnType<typeof createSession>> | null = null
        try {
          data = await createSession(promptText, constraintText)
        } catch (err) {
          console.error('[generatePipeline] createSession failed:', err)
          // A network-level failure (backend down/unreachable) throws before any
          // response, typically a bare TypeError — give that its own clear message
          // rather than whatever cryptic string the browser's fetch implementation used.
          const isNetworkFailure = err instanceof TypeError
          const message = isNetworkFailure
            ? "Couldn't reach the Hospilot backend. Check that it's running and try again."
            : (err instanceof Error ? err.message : 'Failed to generate workflow.')
          set({ pipelineLoading: false, pipelineGenerated: false, pipelineError: message })
          return
        }

        // Backend returns the pipeline synchronously (old flow) or asynchronously via
        // the plan_awaiting_approval WS event (new planning graph flow).
        const { session_id, pipeline } = data

        if (!pipeline?.agents) {
          // Async planning: set sessionId so WS connects and receives plan_awaiting_approval
          set({ sessionId: session_id ?? null, planningStage: 'Planning…' })
          return
        }

        // Synchronous path — build ReactFlow graph immediately
        get().applyPlanningPipeline(pipeline, session_id!)
      })()
    },

    updateEdgeCondition(rawSource, rawTarget, condition) {
      const { nodes: currentNodes, rawEdgeDefs: currentEdgeDefs } = get()

      // Preserve current positions — only newly injected nodes (vd_*) will get computed positions
      const existingPositions: Record<string, { x: number; y: number }> = {}
      for (const n of currentNodes) existingPositions[n.id] = n.position

      const nodeDefs = currentNodes
        .filter((n) => !n.id.startsWith('vd_'))
        .map((n) => ({
          id: n.id,
          agentId: (n.data as { agentId: string }).agentId,
          taskType: (n.data as { taskType?: string }).taskType,
        }))

      // Ensure the edge exists in rawEdgeDefs (handles manually-added edges)
      const edgeExists = currentEdgeDefs.some(e => e.source === rawSource && e.target === rawTarget)
      const baseEdgeDefs = edgeExists
        ? currentEdgeDefs
        : [...currentEdgeDefs, { source: rawSource, target: rawTarget }]

      const newEdgeDefs = baseEdgeDefs.map((e) => {
        if (e.source !== rawSource || e.target !== rawTarget) return e
        if (!condition) {
          const { condition: _c, condition_label: _cl, ...rest } = e
          return rest as PipelineEdgeDef
        }
        return { ...e, condition, condition_label: CONDITION_LABELS[condition] ?? condition.replace(/_/g, ' ') }
      })

      const layout = computeLayout(nodeDefs, newEdgeDefs, existingPositions)
      set({ rawEdgeDefs: newEdgeDefs, nodes: layout.nodes, edges: layout.edges })
      get().triggerPipelineSave()
    },

    syncRawEdge(source, target) {
      set((s) => {
        if (s.rawEdgeDefs.some((e) => e.source === source && e.target === target)) return {}
        return { rawEdgeDefs: [...s.rawEdgeDefs, { source, target }] }
      })
    },

    removeRawEdge(source, target) {
      set((s) => ({
        rawEdgeDefs: s.rawEdgeDefs.filter((e) => !(e.source === source && e.target === target)),
      }))
    },

    removeRawEdgesForNodes(nodeIds) {
      const ids = new Set(nodeIds)
      set((s) => ({
        rawEdgeDefs: s.rawEdgeDefs.filter((e) => !ids.has(e.source) && !ids.has(e.target)),
      }))
    },

    resetExecution() {
      set({
        promptText: '',
        constraintText: '',
        sessionId: null,
        backendPipeline: null,
        livePlans: {},
        taskConditions: {},
        rawEdgeDefs: [],
        pipelineGenerated: false,
        pipelineLoading: false,
        pipelineError: null,
        checkpoints: [],
        nodes: [],
        edges: [],
        executionStatus: 'idle',
        nodeStates: {},
        selectedNodeId: null,
        panelOpen: false,
        pendingApprovals: [],
        approvalMinimized: false,
        sessionRecommendation: null,
        synthesisRunning: false,
        committedSession: false,
        agentOverrides: {},
        reorchestratedEdgesByAgent: {},
        policyDecisions: [],
        toasts: [],
      })
    },

    reOrchestrate() {
      const { edges: currentEdges } = get()
      set({
        executionStatus: 'idle',
        nodeStates: {},
        selectedNodeId: null,
        panelOpen: false,
        pendingApprovals: [],
        edges: currentEdges.map((e) => ({
          ...e,
          animated: false,
          style: (e.data as { condition?: string } | undefined)?.condition
            ? CONDITIONAL_EDGE_STYLE
            : RESET_EDGE_STYLE,
        })),
      })
    },

    async reorchestrateWithFeedback(feedback?: string, agentId?: string, subagentId?: string) {
      const { sessionId } = get()

      if (!sessionId) {
        // Advisory / simulation — local state reset only
        get().reOrchestrate()
        return
      }

      set({ reorchestrateLoading: true })
      console.log('[reorchestrate] →', { sessionId, feedback, agentId })

      try {
        const response = await reorchestrateSession(sessionId, {
          ...(feedback ? { feedback } : {}),
          ...(agentId ? { agent_id: agentId } : {}),
          ...(subagentId ? { subagent_id: subagentId } : {}),
        })
        console.log('[reorchestrate] ←', response)

        if (response.scope === 'pipeline') {
          const pipeline = response.pipeline
          console.log('[reorchestrate] rebuilding pipeline:', pipeline.agents.map((a) => a.id))
          const seenIds = new Set<string>()
          const uniqueAgents = pipeline.agents.filter((a) => {
            if (seenIds.has(a.id)) return false
            seenIds.add(a.id)
            return true
          })
          const validAgentIds = new Set(uniqueAgents.map((a) => a.id))
          const validEdges = pipeline.edges.filter(
            (e) => validAgentIds.has(e.source) && validAgentIds.has(e.target)
          )
          const nodeDefs = uniqueAgents.map((a) => ({
            id: a.id,
            agentId: BACKEND_TO_FRONTEND[a.id] ?? BACKEND_TO_FRONTEND[a.id.split(':')[0]] ?? a.id,
            taskType: (a as { task_type?: string }).task_type,
          }))
          const edgeDefs = validEdges.map((e) => ({
            source: e.source,
            target: e.target,
            condition: e.condition,
            condition_label: e.condition_label,
          }))
          const layout = computeLayout(nodeDefs, edgeDefs)
          set({
            backendPipeline: { ...pipeline, agents: uniqueAgents, edges: validEdges },
            rawEdgeDefs: edgeDefs,
            nodes: layout.nodes,
            edges: layout.edges,
            executionStatus: 'idle',
            nodeStates: {},
            selectedNodeId: null,
            panelOpen: false,
            pendingApprovals: [],
            livePlans: {},
            taskConditions: {},
            agentOverrides: {},
            selectedSubagentsByAgent: {},
            selectedTasksBySubagent: {},
            reorchestratedEdgesByAgent: {},
          })
        } else if (response.scope === 'subagents') {
          console.log('[reorchestrate] selected subagents for', response.agent_id, ':', response.selected_subagents)
          const { edges: currentEdges } = get()
          set((s) => ({
            selectedSubagentsByAgent: {
              ...s.selectedSubagentsByAgent,
              [response.agent_id]: response.selected_subagents,
            },
            reorchestratedEdgesByAgent: response.sub_agent_edges?.length
              ? { ...s.reorchestratedEdgesByAgent, [response.agent_id]: response.sub_agent_edges }
              : s.reorchestratedEdgesByAgent,
            executionStatus: 'idle',
            nodeStates: {},
            selectedNodeId: null,
            panelOpen: false,
            pendingApprovals: [],
            edges: currentEdges.map((e) => ({
              ...e,
              animated: false,
              style: (e.data as { condition?: string } | undefined)?.condition
                ? CONDITIONAL_EDGE_STYLE
                : RESET_EDGE_STYLE,
            })),
          }))
        } else {
          // scope === 'tasks' — merge the reorchestrated tasks (WITH conditions) into
          // backendPipeline so Confirm & Execute carries them through materialization.
          console.log('[reorchestrate] selected tasks for', response.subagent_id, ':', response.selected_tasks)
          const tasks = response.selected_tasks
          set((s) => {
            const bp = s.backendPipeline
            if (bp) {
              for (const a of bp.agents) if (a.id === response.agent_id)
                for (const sa of a.sub_agents ?? []) if (sa.id === response.subagent_id)
                  sa.tasks = tasks.map((t) => ({ id: t.id, label: t.label ?? t.id, condition: t.condition, outputs: t.outputs }))
            }
            return {
              backendPipeline: bp ? { ...bp } : bp,
              selectedTasksBySubagent: {
                ...s.selectedTasksBySubagent,
                [response.subagent_id]: tasks.map((t) => t.id),
              },
              executionStatus: 'idle',
            }
          })
        }
      } catch (err) {
        console.error('[reorchestrateWithFeedback] error:', err)
        get().reOrchestrate()
      } finally {
        set({ reorchestrateLoading: false })
      }
    },

    submitExecution() {
      set({ executionStatus: 'submitted' })
    },

    // ── confirmAndExecute ────────────────────────────────────────────────────
    // Calls POST /api/sessions/{id}/execute.
    // WebSocket events from useSessionWebSocket drive all further UI updates.
    // Falls back to local simulation if no sessionId (API unavailable).
    confirmAndExecute() {
      const { pipelineGenerated, nodes, scenario, sessionId, backendPipeline, edges: currentEdges } = get()
      if (!pipelineGenerated) return

      const initial: Record<string, NodeState> = {}
      for (const n of nodes) initial[n.id] = { status: 'idle', lines: [], events: [] }
      const resetEdges = currentEdges.map((e) => ({
        ...e,
        animated: false,
        style: (e.data as { condition?: string } | undefined)?.condition
          ? CONDITIONAL_EDGE_STYLE
          : RESET_EDGE_STYLE,
      }))
      set({ executionStatus: 'running', nodeStates: initial, panelOpen: true, edges: resetEdges,
            livePlans: {}, taskConditions: {} })

      if (sessionId && backendPipeline) {
        // Real backend — just trigger execution; WebSocket drives the rest
        ;(async () => {
          try {
            await executeSession(sessionId, backendPipeline, get().agentOverrides)
          } catch (err) {
            console.error('[confirmAndExecute] execute API error:', err)
            set({ executionStatus: 'idle' })
          }
        })()
      } else {
        // Simulation fallback
        runExecution(scenario, nodes, resetEdges, set, get, false)
      }
    },

    // ── Pause / Resume / Cancel (autonomous mode only) ────────────────────────
    // Pause is cooperative and has no dedicated "confirmed" WS event, so after the
    // request we poll the paused queue (startPausePoll, below the store) until the
    // session actually shows up there with kind "user_paused".
    async pauseFlow() {
      const sessionId = get().sessionId
      if (!sessionId) return
      try {
        await apiPauseSession(sessionId)
        set({ executionStatus: 'pausing' })
        startPausePoll(sessionId)
      } catch (err) {
        console.error('[pauseFlow] pause API error:', err)
        // A 409 here means the backend's Redis "running" set already disagrees with
        // our local status (the flow finished / was cancelled / hasn't started its
        // drive yet) -- pausing is cooperative and has no live push for every one of
        // those transitions, so this is the only place we'd learn about it. Reconcile
        // from the server instead of leaving a stale "Running" banner whose Pause
        // button would just 409 again on every subsequent click.
        if (err instanceof ApiError && err.status === 409) {
          await get().reconcileExecutionStatus(sessionId)
          return
        }
        get().pushToast({ severity: 'critical', title: 'Pause failed',
          message: err instanceof Error ? err.message : 'Could not pause this workflow.' })
      }
    },

    // Re-derive `executionStatus` from server truth without the full destructive
    // `loadSession` rebuild (which wipes live per-node WS state). Used when a
    // Redis-tracked action (pause) 409s because our local status has drifted.
    async reconcileExecutionStatus(sessionId: string) {
      try {
        const { flows } = await fetchPausedQueue()
        if (flows.some((f) => f.session_id === sessionId && f.kind === 'user_paused')) {
          set({ executionStatus: 'paused' })
          return
        }
        const session = await getSession(sessionId)
        if (session.status === 'cancelled') {
          set({ executionStatus: 'cancelled' })
        } else if (session.status === 'complete_pending') {
          set({ executionStatus: 'complete_pending' })
        } else if (session.status === 'submitted' || session.status === 'completed' || session.status === 'failed') {
          set({ executionStatus: 'submitted' })
        }
        // Otherwise the DB still says "running" -- the flow just hasn't reached its
        // drive loop yet (still queued for a concurrency slot) or the park is mid-
        // flight. Leave local state as-is; the next WS event or pause retry will catch up.
      } catch (err) {
        console.error('[reconcileExecutionStatus] failed:', err)
      }
    },

    async resumeFlow() {
      const sessionId = get().sessionId
      if (!sessionId) return
      stopPausePoll()
      try {
        await apiResumeSession(sessionId)
        // WS session_resumed re-affirms this; set it optimistically so the UI doesn't
        // sit on "Paused" waiting for the round trip.
        set({ executionStatus: 'running' })
      } catch (err) {
        console.error('[resumeFlow] resume API error:', err)
        get().pushToast({ severity: 'critical', title: 'Resume failed',
          message: err instanceof Error ? err.message : 'Could not resume this workflow.' })
      }
    },

    async cancelFlow() {
      const sessionId = get().sessionId
      if (!sessionId) return
      stopPausePoll()
      try {
        await apiCancelSession(sessionId)
        set({ executionStatus: 'cancelled' })
      } catch (err) {
        console.error('[cancelFlow] cancel API error:', err)
        get().pushToast({ severity: 'critical', title: 'Cancel failed',
          message: err instanceof Error ? err.message : 'Could not cancel this workflow.' })
      }
    },

    // ── Checkpoints (revert points) — only meaningful while paused ────────────
    async fetchCheckpointsForSession() {
      const sessionId = get().sessionId
      if (!sessionId) return
      set({ checkpointsLoading: true })
      try {
        const { checkpoints } = await apiFetchCheckpoints(sessionId)
        // Filter out bookkeeping rows per the API doc: entries whose `next` is only
        // ["__start__"] or ["__synthesise__"] aren't real revert points.
        const real = checkpoints.filter((c) => {
          const n = c.next ?? []
          return !(n.length === 1 && (n[0] === '__start__' || n[0] === '__synthesise__'))
        })
        set({ checkpoints: real, checkpointsLoading: false })
      } catch (err) {
        console.error('[fetchCheckpointsForSession] failed:', err)
        set({ checkpointsLoading: false })
      }
    },

    // ── Edit Checkpoint screen (UI navigation only for now) ────────────────────
    // CheckpointEditorScreen builds its own local nodes/edges from backendPipeline
    // and a selected checkpoint — it doesn't mutate the live canvas state, so
    // opening/closing this screen is just a view toggle. The screen's Apply &
    // Resume button is not yet wired to editResumeSession (deliberately deferred);
    // when it is, it should call editResumeSession(sessionId, editedPipeline,
    // checkpointId) directly with its own local edited pipeline, then
    // fetchCheckpointsForSession() again per the doc ("each edit-resume starts a
    // fresh checkpoint lineage").
    openCheckpointEditor() {
      if (get().executionStatus !== 'paused') return
      set({ checkpointEditorOpen: true })
    },

    closeCheckpointEditor() {
      set({ checkpointEditorOpen: false })
    },

    // ── approveGate ──────────────────────────────────────────────────────────
    approveGate() {
      const { pendingApprovals, nodeStates } = get()
      const current = pendingApprovals[0]
      if (!current) return
      const remaining = pendingApprovals.slice(1)

      if (current.approvalId) {
        // REST-backed approval (real backend). Determine if there is a live execution
        // driving this session in the current tab — nodeStates is populated only when
        // confirmAndExecute ran in this tab. If empty, we loaded back an in-flight
        // session and there is no WS; go idle so the user can navigate freely.
        const hasLiveExecution = Object.keys(nodeStates).length > 0
        const nextStatus = remaining.length > 0
          ? undefined  // more approvals pending — keep current status (waiting_approval)
          : hasLiveExecution ? 'running' : 'idle'
        set({ pendingApprovals: remaining, ...(nextStatus ? { executionStatus: nextStatus as ExecutionStatus } : {}) })
        decideApproval(current.approvalId, 'approved').catch(console.error)
      } else {
        set({ pendingApprovals: remaining, ...(remaining.length === 0 ? { executionStatus: 'running' } : {}) })
        const resolver = (window as unknown as Record<string, unknown>).__approvalResolver as ((v: boolean) => void) | undefined
        if (resolver) resolver(true)
      }
    },

    rejectGate() {
      const { pendingApprovals } = get()
      const current = pendingApprovals[0]
      if (!current) return

      set({ pendingApprovals: [], executionStatus: 'idle' })
      if (current.approvalId) {
        decideApproval(current.approvalId, 'rejected').catch(console.error)
      } else {
        const resolver = (window as unknown as Record<string, unknown>).__approvalResolver as ((v: boolean) => void) | undefined
        if (resolver) resolver(false)
      }
    },

    // Bring a queued approval to the front so the modal renders it and approve/reject
    // (which always act on pendingApprovals[0]) operate on the one the user picked from
    // the "+N more" stack. No-op if the id isn't currently pending.
    focusApproval(approvalId) {
      const { pendingApprovals } = get()
      const idx = pendingApprovals.findIndex((a) => a.approvalId === approvalId)
      if (idx <= 0) return
      const next = [...pendingApprovals]
      const [picked] = next.splice(idx, 1)
      set({ pendingApprovals: [picked, ...next] })
    },

    setApprovalMinimized(minimized) {
      set({ approvalMinimized: minimized })
    },

    applyPlanningPipeline(pipeline, sessionId) {
      const { promptText, constraintText, scenario } = get()
      const seenIds = new Set<string>()
      const uniqueAgents = pipeline.agents.filter((a) => {
        if (seenIds.has(a.id)) return false
        seenIds.add(a.id)
        return true
      })
      const validAgentIds = new Set(uniqueAgents.map((a) => a.id))
      const validEdges = pipeline.edges.filter(
        (e) => validAgentIds.has(e.source) && validAgentIds.has(e.target)
      )
      const nodeDefs = uniqueAgents.map((a) => ({
        id: a.id,
        agentId: BACKEND_TO_FRONTEND[a.id] ?? BACKEND_TO_FRONTEND[a.id.split(':')[0]] ?? a.id,
        taskType: (a as { task_type?: string }).task_type,
      }))
      const edgeDefs = validEdges.map((e) => ({
        source: e.source,
        target: e.target,
        condition: e.condition,
        condition_label: e.condition_label,
      }))
      const layout = computeLayout(nodeDefs, edgeDefs)
      const entry: WorkflowEntry = {
        id: sessionId,
        prompt: promptText,
        constraint: constraintText,
        scenarioId: scenario.id,
        scenarioLabel: scenario.label,
        timestamp: Date.now(),
        agentIds: layout.nodes.map((n) => (n.data as { agentId: string }).agentId),
        status: 'pending',
      }
      set((s) => ({
        sessionId,
        backendPipeline: { ...pipeline, agents: uniqueAgents, edges: validEdges },
        rawEdgeDefs: edgeDefs,
        pipelineLoading: false,
        pipelineGenerated: true,
        planningStage: null,
        nodes: layout.nodes,
        edges: layout.edges,
        pipelineSaveStatus: 'saved',
        workflowHistory: [entry, ...s.workflowHistory.filter((w) => w.id !== sessionId)].slice(0, 30),
      }))
      get().fetchWorkflowHistory()
    },

    applyEditedPipeline(pipeline, completedAgentIds) {
      const seenIds = new Set<string>()
      const uniqueAgents = pipeline.agents.filter((a) => {
        if (seenIds.has(a.id)) return false
        seenIds.add(a.id)
        return true
      })
      const validAgentIds = new Set(uniqueAgents.map((a) => a.id))
      const validEdges = (pipeline.edges ?? []).filter(
        (e) => validAgentIds.has(e.source) && validAgentIds.has(e.target),
      )
      const nodeDefs = uniqueAgents.map((a) => ({
        id: a.id,
        agentId: BACKEND_TO_FRONTEND[a.id] ?? BACKEND_TO_FRONTEND[a.id.split(':')[0]] ?? a.id,
        taskType: (a as { task_type?: string }).task_type,
      }))
      const edgeDefs = validEdges.map((e) => ({
        source: e.source,
        target: e.target,
        condition: e.condition,
        condition_label: e.condition_label,
      }))
      const layout = computeLayout(nodeDefs, edgeDefs)
      // Carried-forward (completed-as-of-the-checkpoint) agents start 'complete' so they
      // don't flash idle; everything else starts idle and the WS drives it live.
      const completed = new Set(completedAgentIds)
      const nodeStates: Record<string, NodeState> = {}
      for (const a of uniqueAgents) {
        nodeStates[a.id] = { status: completed.has(a.id) ? 'complete' : 'idle', lines: [], events: [] }
      }
      set({
        backendPipeline: { ...pipeline, agents: uniqueAgents, edges: validEdges },
        rawEdgeDefs: edgeDefs,
        nodes: layout.nodes,
        edges: layout.edges,
        nodeStates,
        pipelineGenerated: true,
        // The flow is resuming — flip out of 'paused' optimistically so the canvas shows
        // the running pipeline, not the paused banner (WS session_started/resumed confirms).
        executionStatus: 'running',
        panelOpen: true,
      })
    },

    dismissExternalApproval(approvalId) {
      const { pendingApprovals, nodeStates } = get()
      const remaining = pendingApprovals.filter((a) => a.approvalId !== approvalId)
      if (remaining.length === pendingApprovals.length) return
      const hasLiveExecution = Object.keys(nodeStates).length > 0
      const nextStatus: ExecutionStatus = remaining.length > 0
        ? 'waiting_approval'
        : hasLiveExecution ? 'running' : 'idle'
      set({ pendingApprovals: remaining, executionStatus: nextStatus })
    },

    clearPatientIdentification() { set({ patientIdentificationPending: false, patientIdentificationCount: null }) },

    openSubAgent(nodeId) { set({ subAgentNodeId: nodeId }) },
    closeSubAgent() { set({ subAgentNodeId: null }) },
    setActiveView(view) { set({ activeView: view }) },
    setExecutionMode(mode) { set({ executionMode: mode }) },

    pushPolicyDecision(d) {
      set((s) => ({ policyDecisions: [...s.policyDecisions, d] }))
    },
    pushToast(t) {
      const id = `toast-${Date.now()}-${Math.round(Math.random() * 1e6)}`
      set((s) => ({ toasts: [...s.toasts, { ...t, id }] }))
    },
    dismissToast(id) {
      set((s) => ({ toasts: s.toasts.filter((x) => x.id !== id) }))
    },
    saveAgentOverride(nodeId, overrides) {
      set((s) => ({ agentOverrides: { ...s.agentOverrides, [nodeId]: overrides } }))
    },

    loadWorkflow(id) {
      const { workflowHistory } = get()
      const entry = workflowHistory.find((w) => w.id === id)
      if (!entry) return
      const s = SCENARIOS.find((sc) => sc.id === entry.scenarioId) ?? SCENARIOS[0]
      set({
        activeScenarioId: entry.scenarioId,
        scenario: s,
        promptText: entry.prompt,
        constraintText: entry.constraint,
        sessionId: null,
        backendPipeline: null,
        livePlans: {},
        taskConditions: {},
        pipelineGenerated: false,
        pipelineLoading: false,
        pipelineError: null,
        checkpoints: [],
        nodes: [],
        edges: [],
        executionStatus: 'idle',
        nodeStates: {},
        selectedNodeId: null,
        panelOpen: false,
        pendingApprovals: [],
        subAgentNodeId: null,
      })
    },

    async fetchWorkflowHistory() {
      try {
        const { sessions } = await listSessions(50)
        const { workflowHistory } = get()
        // Preserve agentIds from local entries (not returned by list endpoint)
        const localById: Record<string, string[]> = {}
        for (const e of workflowHistory) {
          if (e.agentIds.length > 0) localById[e.id] = e.agentIds
        }
        const entries: WorkflowEntry[] = sessions.map((s) => ({
          id: s.id,
          prompt: s.goal,
          constraint: '',
          scenarioId: s.id,
          scenarioLabel: s.goal,
          timestamp: new Date(s.created_at).getTime(),
          agentIds: localById[s.id] ?? [],
          status: s.status,
        }))
        set({ workflowHistory: entries, workflowHistoryLoaded: true })
      } catch (err) {
        console.error('[fetchWorkflowHistory] failed', err)
      }
    },

    async loadSession(sessionId: string) {
      set({ pipelineLoading: true, pipelineError: null, checkpoints: [] })
      try {
        const session = await getSession(sessionId)
        const pipeline = (session.pipeline || session.pipeline_snapshot) as BackendPipeline | null
        if (!pipeline || !pipeline.agents) {
          set({ pipelineLoading: false })
          return
        }

        const seenIds = new Set<string>()
        const uniqueAgents = pipeline.agents.filter((a) => {
          if (seenIds.has(a.id)) return false
          seenIds.add(a.id)
          return true
        })
        const validAgentIds = new Set(uniqueAgents.map((a) => a.id))
        const validEdges = (pipeline.edges ?? []).filter(
          (e) => validAgentIds.has(e.source) && validAgentIds.has(e.target),
        )
        const nodeDefs = uniqueAgents.map((a) => ({
          id: a.id,
          agentId: BACKEND_TO_FRONTEND[a.id] ?? BACKEND_TO_FRONTEND[a.id.split(':')[0]] ?? a.id,
          taskType: (a as { task_type?: string }).task_type,
        }))
        const edgeDefs = validEdges.map((e) => ({
          source: e.source,
          target: e.target,
          condition: e.condition,
          condition_label: e.condition_label,
        }))
        const layout = computeLayout(nodeDefs, edgeDefs)

        let executionStatus: ExecutionStatus = 'idle'
        let pendingApprovals: ApprovalGate[] = []

        if (session.status === 'complete_pending') {
          executionStatus = 'complete_pending'
          const approvals = await fetchPendingApprovals(sessionId)
          pendingApprovals = approvals.map(pendingApprovalToGate)
        } else if (session.status === 'running') {
          // Check DB for a pending action-level approval (ambulance dispatch, bed
          // reservation, etc.) — these survive WS reconnect via the approval_tasks table.
          const inFlight = await fetchPendingApprovals(sessionId)
          if (inFlight.length > 0) {
            executionStatus = 'waiting_approval'
            pendingApprovals = inFlight.map(pendingApprovalToGate)
          } else {
            // Pausing never changes the DB status column (still "running" underneath) --
            // it's tracked only in Redis. Check the paused queue to see if this session
            // is actually parked before defaulting to "running".
            let isPaused = false
            try {
              const { flows } = await fetchPausedQueue()
              isPaused = flows.some((f) => f.session_id === sessionId && f.kind === 'user_paused')
            } catch (err) {
              console.error('[loadSession] fetchPausedQueue failed:', err)
            }
            // Session is actively running (or paused) on the server — stay out of 'idle'
            // so we don't show "Confirm & Execute" (which would double-execute). The WS
            // connection (opened when sessionId is set below) will deliver live events.
            executionStatus = isPaused ? 'paused' : 'running'
          }
        } else if (session.status === 'cancelled') {
          executionStatus = 'cancelled'
        } else if (session.status === 'submitted' || session.status === 'completed' || session.status === 'failed') {
          executionStatus = 'submitted'
        }

        const isDone = session.status === 'submitted' || session.status === 'completed' || session.status === 'failed'

        const restoredRec = isDone ? (session.synthesis_result ?? null) : null

        set((s) => ({
          sessionId,
          promptText: session.goal,
          constraintText: session.constraints ?? '',
          // Mode is a fixed fact of this session (baked in at creation), not the live
          // dropdown preference — reflect the truth so the header's read-only badge
          // (gated on pipelineGenerated) never shows a mode this workflow didn't run in.
          executionMode: session.autonomous ? 'autonomous' : 'assisted',
          backendPipeline: { ...pipeline, agents: uniqueAgents, edges: validEdges },
          rawEdgeDefs: edgeDefs,
          pipelineLoading: false,
          pipelineGenerated: true,
          nodes: layout.nodes,
          edges: isDone
            ? layout.edges.map((e) => ({
                ...e,
                animated: true,
                style: { stroke: '#14b8a6', strokeWidth: 2, strokeDasharray: '0' },
              }))
            : layout.edges,
          executionStatus,
          pendingApprovals,
          approvalMinimized: false,   // opening a session should surface its approval, not a stale pill
          nodeStates: (() => {
            if (isDone || session.status === 'complete_pending') {
              const ns: Record<string, { status: 'complete'; lines: never[]; events: never[] }> = {}
              for (const a of uniqueAgents) ns[a.id] = { status: 'complete', lines: [], events: [] }
              return ns
            }
            if (session.status === 'running') {
              // Pre-populate as idle so nodes don't look blank; WS events will update
              // individual nodes to 'running'/'complete' as they arrive.
              const ns: Record<string, { status: 'idle'; lines: never[]; events: never[] }> = {}
              for (const a of uniqueAgents) ns[a.id] = { status: 'idle', lines: [], events: [] }
              return ns
            }
            return {}
          })(),
          selectedNodeId: null,
          panelOpen: executionStatus !== 'idle',
          livePlans: {},
          taskConditions: {},
          agentOverrides: {},
          selectedSubagentsByAgent: {},
          selectedTasksBySubagent: {},
          reorchestratedEdgesByAgent: {},
          committedSession: isDone,
          sessionRecommendation: restoredRec,
          pipelineSaveStatus: 'saved',
          sessionLoadKey: s.sessionLoadKey + 1,
        }))
        if (executionStatus === 'paused') get().fetchCheckpointsForSession()
      } catch (err) {
        console.error('[loadSession] failed', err)
        set({ pipelineLoading: false })
      }
    },

    triggerPipelineSave() {
      const { sessionId, backendPipeline } = get()
      if (!sessionId || !backendPipeline) return
      set({ pipelineSaveStatus: 'unsaved' })
      if (pipelineSaveTimer) clearTimeout(pipelineSaveTimer)
      pipelineSaveTimer = setTimeout(async () => {
        const { sessionId: sid, backendPipeline: bp, rawEdgeDefs: edges, nodes: currentNodes } = get()
        if (!sid || !bp) return
        set({ pipelineSaveStatus: 'saving' })
        try {
          // Derive agent list from current canvas nodes so add/remove/swap is captured correctly.
          // Original nodes keep their full backend data; palette-dropped nodes get a minimal entry.
          const agents = currentNodes
            .filter((n) => n.type === 'agentNode')
            .map((n) => {
              const existing = bp.agents.find((a) => a.id === n.id)
              if (existing) return existing
              const frontendId = (n.data as { agentId: string }).agentId
              return { id: FRONTEND_TO_BACKEND[frontendId] ?? `${frontendId}_agent` } as (typeof bp.agents)[number]
            })
          await updateSessionPipeline(sid, { ...bp, agents, edges })
          set({ pipelineSaveStatus: 'saved' })
        } catch (err) {
          console.error('[triggerPipelineSave] failed', err)
          set({ pipelineSaveStatus: 'unsaved' })
        }
      }, 1500)
    },

    async saveNow() {
      const { sessionId, backendPipeline, rawEdgeDefs, pipelineSaveStatus, nodes: currentNodes } = get()
      if (!sessionId || !backendPipeline || pipelineSaveStatus === 'saving') return
      if (pipelineSaveTimer) { clearTimeout(pipelineSaveTimer); pipelineSaveTimer = null }
      set({ pipelineSaveStatus: 'saving' })
      try {
        const agents = currentNodes
          .filter((n) => n.type === 'agentNode')
          .map((n) => {
            const existing = backendPipeline.agents.find((a) => a.id === n.id)
            if (existing) return existing
            const frontendId = (n.data as { agentId: string }).agentId
            return { id: FRONTEND_TO_BACKEND[frontendId] ?? `${frontendId}_agent` } as (typeof backendPipeline.agents)[number]
          })
        await updateSessionPipeline(sessionId, { ...backendPipeline, agents, edges: rawEdgeDefs })
        set({ pipelineSaveStatus: 'saved' })
      } catch (err) {
        console.error('[saveNow] failed', err)
        set({ pipelineSaveStatus: 'unsaved' })
      }
    },

    async commitSession() {
      const { sessionId } = get()
      if (!sessionId) return
      try {
        await apiCommitSession(sessionId)
        set({ committedSession: true })
      } catch (err) {
        console.error('[commitSession] failed', err)
      }
    },

    async loadAgentRegistry() {
      if (get().agentRegistryLoaded) return
      try {
        const registry = await fetchAgentRegistry()
        set({ agentRegistry: registry, agentRegistryLoaded: true })
      } catch (err) {
        console.error('[loadAgentRegistry] failed', err)
      }
    },

    startExecution() {
      const { pipelineGenerated, nodes } = get()
      if (!pipelineGenerated) return

      const initial: Record<string, NodeState> = {}
      for (const n of nodes) initial[n.id] = { status: 'idle', lines: [], events: [] }
      set({
        executionStatus: 'running',
        nodeStates: initial,
        panelOpen: true,
        subAgentNodeId: null,
      })

      const { scenario, edges } = get()
      runExecution(scenario, nodes, edges, set, get, false)
    },
  }
})

// Persist the active sessionId and recommendation across page refreshes
useStore.subscribe((state, prev) => {
  if (state.sessionId !== prev.sessionId) {
    if (state.sessionId) localStorage.setItem('hospilot_session_id', state.sessionId)
    else localStorage.removeItem('hospilot_session_id')
    // When embedded in the Hospilot widget (iframe), tell the host so the minimized
    // panel tracks whatever session was started/loaded here (iframe → widget sync).
    if (typeof window !== 'undefined' && window.parent && window.parent !== window) {
      window.parent.postMessage({ type: 'session_active', sessionId: state.sessionId }, '*')
    }
  }
  if (state.sessionRecommendation !== prev.sessionRecommendation && state.sessionId && state.sessionRecommendation) {
    localStorage.setItem(`hospilot_rec_${state.sessionId}`, JSON.stringify(state.sessionRecommendation))
  }
  if (state.activeView !== prev.activeView) {
    localStorage.setItem('hospilot_active_view', state.activeView)
  }
})

// ─── Pause confirmation polling ────────────────────────────────────────────────
// Pause is cooperative (the flow finishes its current step before parking) and the
// backend has no dedicated "confirmed paused" WS event -- only session_pause_requested
// fires immediately on request. So confirmation works by polling the cheap,
// Redis-backed paused queue until this session_id shows up there with kind
// "user_paused", exactly as the API doc's own recipe recommends.
let pausePollTimer: ReturnType<typeof setInterval> | null = null

// Exported so the WS hook can stop/(re)start confirmation polling on session_pause_
// requested / session_resumed / session_cancelled — those can arrive from another
// tab/client pausing the same session, not just this tab's own pauseFlow() call.
export function stopPausePoll() {
  if (pausePollTimer) {
    clearInterval(pausePollTimer)
    pausePollTimer = null
  }
}

export function startPausePoll(sessionId: string) {
  stopPausePoll()
  pausePollTimer = setInterval(async () => {
    const s = useStore.getState()
    // Abandon the poll if the user navigated away from this session, or a resume/
    // cancel already fired (executionStatus moved on from 'pausing').
    if (s.sessionId !== sessionId || s.executionStatus !== 'pausing') {
      stopPausePoll()
      return
    }
    try {
      const { flows } = await fetchPausedQueue()
      const parked = flows.some((f) => f.session_id === sessionId && f.kind === 'user_paused')
      if (parked) {
        stopPausePoll()
        useStore.setState({ executionStatus: 'paused' })
        useStore.getState().fetchCheckpointsForSession()
      }
    } catch (err) {
      console.error('[pausePoll] fetchPausedQueue failed:', err)
      // Transient network error -- keep polling rather than abandoning confirmation.
    }
  }, 1500)
}

// ─── Simulation execution engine (used as fallback when backend unreachable) ──

function delay(ms: number) {
  return new Promise<void>((r) => setTimeout(r, ms))
}

function waitForApproval(): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    ;(window as unknown as Record<string, unknown>).__approvalResolver = resolve
  })
}

function appendLine(nodeId: string, line: string, set: (p: Partial<AppState>) => void, get: () => AppState) {
  const prev = get().nodeStates
  set({ nodeStates: { ...prev, [nodeId]: { ...prev[nodeId], lines: [...(prev[nodeId]?.lines ?? []), line] } } })
}

function setNodeStatus(nodeId: string, status: NodeStatus, set: (p: Partial<AppState>) => void, get: () => AppState) {
  const prev = get().nodeStates
  set({ nodeStates: { ...prev, [nodeId]: { ...prev[nodeId], status } } })
}

function genericOutput(agentId: string): string[] {
  return [
    `Initialising ${agentId} agent...`,
    'Loading relevant patient and system data...',
    'Running analysis sub-routines...',
    '✓ Analysis complete. Preparing recommendations.',
    `✓ ${agentId} agent complete.`,
  ]
}

async function runNode(
  nodeId: string,
  agentId: string,
  outputs: string[],
  set: (p: Partial<AppState>) => void,
  get: () => AppState
) {
  setNodeStatus(nodeId, 'running', set, get)
  set({ selectedNodeId: nodeId })
  const lines = outputs.length > 0 ? outputs : genericOutput(agentId)
  for (const line of lines) {
    await delay(550 + Math.random() * 350)
    appendLine(nodeId, line, set, get)
  }
  setNodeStatus(nodeId, 'complete', set, get)
}


async function runExecution(
  scenario: ScenarioDef,
  nodes: Node[],
  storeEdges: Edge[],
  set: (p: Partial<AppState>) => void,
  get: () => AppState,
  autoApprove = false,
) {
  const ids = nodes.map((n) => n.id)
  const edgeDefs = storeEdges.map((e) => ({ source: e.source, target: e.target }))

  const inDegree: Record<string, number> = {}
  const children: Record<string, string[]> = {}
  for (const id of ids) { inDegree[id] = 0; children[id] = [] }
  for (const e of edgeDefs) {
    inDegree[e.target] = (inDegree[e.target] ?? 0) + 1
    children[e.source].push(e.target)
  }

  const remaining = { ...inDegree }
  let wave = ids.filter((id) => remaining[id] === 0)

  const getAgentId = (nodeId: string) =>
    (nodes.find((n) => n.id === nodeId)?.data as { agentId: string })?.agentId ?? nodeId

  const getOutputs = (nodeId: string) => scenario.streamingOutputs[nodeId] ?? []

  while (wave.length > 0) {
    const gatesThisWave = scenario.approvalGates.filter((g) => wave.includes(g.agentId))

    if (gatesThisWave.length > 0 && !autoApprove) {
      const gate = gatesThisWave[0]
      const gateNodeId = gate.agentId
      const outputs = getOutputs(gateNodeId)
      const halfIdx = Math.ceil(outputs.length / 2)

      setNodeStatus(gateNodeId, 'waiting', set, get)
      set({ selectedNodeId: gateNodeId })
      for (const line of outputs.slice(0, halfIdx)) {
        await delay(500)
        appendLine(gateNodeId, line, set, get)
      }

      set({ executionStatus: 'waiting_approval', pendingApprovals: [...get().pendingApprovals, gate] })
      const approved = await waitForApproval()

      if (!approved) {
        set({ executionStatus: 'idle' })
        return
      }

      set({ executionStatus: 'running' })
      const otherWave = wave.filter((id) => id !== gateNodeId)

      await Promise.all([
        (async () => {
          for (const line of outputs.slice(halfIdx)) {
            await delay(500)
            appendLine(gateNodeId, line, set, get)
          }
          setNodeStatus(gateNodeId, 'complete', set, get)
        })(),
        ...otherWave.map((id) => runNode(id, getAgentId(id), getOutputs(id), set, get)),
      ])
    } else {
      await Promise.all(wave.map((id) => runNode(id, getAgentId(id), getOutputs(id), set, get)))
    }

    const nextWave: string[] = []
    for (const id of wave) {
      for (const child of children[id]) {
        remaining[child]--
        if (remaining[child] === 0) nextWave.push(child)
      }
    }
    wave = nextWave
  }

  const { edges: currentEdges } = get()
  set({
    executionStatus: 'submitted',
    edges: currentEdges.map((e) => ({
      ...e,
      animated: true,
      style: { stroke: '#14b8a6', strokeWidth: 2, strokeDasharray: '0' },
    })),
  })
}
