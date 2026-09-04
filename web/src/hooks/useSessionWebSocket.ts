import { useEffect } from 'react'
import { WS_BASE, fetchPendingApprovals, getToken } from '../services/api'
import type { BackendPipeline } from '../services/api'
import { useStore, BACKEND_TO_FRONTEND, pendingApprovalToGate, startPausePoll, stopPausePoll } from '../store'
import type { NodeStatus, SubAgentEvent, TaskConditionState, ExecutionStatus, NodeState, PolicyDecision } from '../store'
import type { TaskDef } from '../data/agents'
import { computeLayout } from '../lib/layout'

// ── WebSocket event types (mirrors backend api/ws.py broadcast events) ────────

interface WsEvent {
  type: string
  [key: string]: unknown
}

// Maps backend action type → dynamic approval payload
function _buildApproval(approvalId: string, event: WsEvent) {
  const action = event.action as string
  switch (action) {
    case 'bed_reservation': {
      const assignments = event.assignments as Array<{
        patient: Record<string, unknown>
        bed: Record<string, unknown>
        patient_name?: string
        patient_id?: string
      }> | undefined
      const bedCount = (event.bed_count as number) ?? (assignments?.length ?? 0)
      const isBatch  = bedCount > 1

      const lines: string[] = []
      for (const a of assignments ?? []) {
        const ward     = String(a.bed?.ward ?? a.bed?.type ?? 'Ward')
        const bedNum   = String(a.bed?.bed_number ?? a.bed?.name ?? '')
        const bedLabel = bedNum ? `${ward} – ${bedNum}` : ward
        const complaint = String(a.patient?.chief_complaint ?? '')
        const score     = a.patient?.triage_score
        const tail      = [complaint, score ? `CTAS ${score}` : ''].filter(Boolean).join(', ')

        const hasPatient = a.patient_name && a.patient_name !== 'Unknown Patient'
        if (hasPatient) {
          lines.push(`• **${a.patient_name}** (${a.patient_id}) → ${bedLabel}${tail ? ` — ${tail}` : ''}`)
        } else {
          lines.push(`• **${bedLabel}**${tail ? ` — ${tail}` : ''}`)
        }
      }

      const anyPatient = (assignments ?? []).some(
        (a) => a.patient_name && a.patient_name !== 'Unknown Patient'
      )
      const sections: string[] = []
      sections.push(
        isBatch
          ? `AI matched ${bedCount} patients to appropriate beds:`
          : anyPatient
            ? 'AI recommends the following bed assignment:'
            : 'AI recommends reserving the following bed:'
      )
      if (lines.length) sections.push(lines.join('\n'))
      sections.push(
        isBatch
          ? `Approving will reserve all ${bedCount} beds immediately. Rejecting will release them for other patients.`
          : 'Approving will confirm the reservation. Rejecting will release the bed for other patients.'
      )

      return {
        approvalId,
        agentId: 'bed_agent',
        title: isBatch ? `Batch Bed Reservation (${bedCount} patients)` : 'Bed Reservation',
        recommendation: sections.join('\n\n'),
        action: isBatch ? `Approve ${bedCount} Reservations` : 'Approve Reservation',
      }
    }
    case 'mark_discharge_ready': {
      const count = (event.ready_count as number) ?? 0
      return {
        approvalId,
        agentId: 'discharge_agent',
        title: 'Discharge Readiness',
        recommendation:
          `${count} patient${count === 1 ? '' : 's'} have been assessed as clinically ready for discharge.\n\n` +
          `Approving will mark them as discharge-ready and trigger AI-generated discharge summary notes. ` +
          `Rejecting will leave their status unchanged for further review.`,
        action: `Approve ${count} Discharge${count === 1 ? '' : 's'}`,
      }
    }
    case 'icu_transfer_recommendations': {
      const stepDownCount   = (event.step_down_count as number) ?? 0
      const escalationCount = (event.escalation_count as number) ?? 0
      const escalations = event.escalations as Array<{ patient_name: string; patient_id: string; reason: string; urgency: string }> | undefined
      const stepDowns   = event.step_downs   as Array<{ patient_name: string; patient_id: string; reason: string; confidence: string }> | undefined

      const parts: string[] = []
      if (stepDownCount > 0)   parts.push(`${stepDownCount} patient${stepDownCount === 1 ? '' : 's'} recommended for step-down to PCU`)
      if (escalationCount > 0) parts.push(`${escalationCount} patient${escalationCount === 1 ? '' : 's'} flagged for ICU escalation`)

      const lines: string[] = []
      if (escalations && escalations.length > 0) {
        lines.push('**ICU Escalations:**')
        for (const p of escalations) {
          const tag = p.urgency ? ` [${p.urgency}]` : ''
          lines.push(`• ${p.patient_name} (${p.patient_id}) — ${p.reason}${tag}`)
        }
      }
      if (stepDowns && stepDowns.length > 0) {
        lines.push('**Step-Downs to PCU:**')
        for (const p of stepDowns) {
          const tag = p.confidence ? ` [${p.confidence}]` : ''
          lines.push(`• ${p.patient_name} (${p.patient_id}) — ${p.reason}${tag}`)
        }
      }

      const sections: string[] = []
      if (parts.length)  sections.push(parts.join(' · ') + '.')
      if (lines.length)  sections.push(lines.join('\n'))
      sections.push('Approving will initiate the transfer orders. Rejecting will keep current placement unchanged.')

      return {
        approvalId,
        agentId: 'icu_agent',
        title: 'ICU Transfer Recommendations',
        recommendation: sections.join('\n\n'),
        action: 'Approve Transfers',
      }
    }
    case 'staff_reallocation': {
      const count   = (event.recommendation_count as number) ?? 0
      const summary = (event.summary as string) ?? ''
      return {
        approvalId,
        agentId: 'staff_agent',
        title: 'Staffing Reallocation',
        recommendation:
          `${count} nurse reallocation${count === 1 ? '' : 's'} recommended to address understaffed units.\n\n` +
          (summary ? summary + '\n\n' : '') +
          'Approving will notify the float pool. Rejecting will leave current assignments unchanged.',
        action: `Approve Reallocation`,
      }
    }
    case 'ambulance_dispatch': {
      const vehicle        = event.assigned_vehicle as string | undefined
      const etaMins        = event.eta_mins as number | null | undefined
      const escalate       = event.escalate as boolean | undefined
      const escalationReason = event.escalation_reason as string | null | undefined
      const summary        = event.summary as string | undefined
      const lines: string[] = []
      if (vehicle)                          lines.push(`• **Vehicle:** ${vehicle}`)
      if (etaMins != null)                  lines.push(`• **ETA:** ${etaMins} min`)
      if (escalate && escalationReason)     lines.push(`• **Escalation:** ${escalationReason}`)
      const sections: string[] = []
      if (lines.length) sections.push(lines.join('\n'))
      if (summary) sections.push(summary)
      sections.push('Approving will dispatch the unit. Rejecting will cancel the assignment.')
      return {
        approvalId,
        agentId: 'ambulance_agent',
        title: 'Ambulance Dispatch',
        recommendation: sections.join('\n\n'),
        action: 'Dispatch Ambulance',
      }
    }
    case 'appointment_booking': {
      const assignments = event.assignments as Array<{
        patient: { name?: string; phone?: string }
        patient_name?: string
        slot: { time?: string; specialty?: string }
      }> | undefined
      const isBatch = (assignments?.length ?? 0) > 1

      const lines: string[] = []
      for (const a of assignments ?? []) {
        const name      = a.patient_name ?? a.patient?.name ?? 'Unknown Patient'
        const phone     = a.patient?.phone ? ` (${a.patient.phone})` : ''
        const specialty = a.slot?.specialty ?? (event.specialization as string) ?? 'Unknown'
        const time      = a.slot?.time
          ? new Date(a.slot.time).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
          : (event.appointment_time as string) ?? ''
        lines.push(`• **${name}**${phone} → ${specialty} — ${time}`)
      }

      const sections: string[] = []
      sections.push(
        isBatch
          ? `AI scheduled ${assignments!.length} appointments:`
          : 'AI recommends the following appointment:'
      )
      if (lines.length) sections.push(lines.join('\n'))
      sections.push(
        isBatch
          ? `Approving will confirm all ${assignments!.length} bookings. Rejecting will cancel them.`
          : 'Approving will confirm the booking. Rejecting will cancel it.'
      )

      return {
        approvalId,
        agentId: 'appointment_agent',
        title: isBatch ? `Batch Appointment Booking (${assignments!.length})` : 'Appointment Booking',
        recommendation: sections.join('\n\n'),
        action: isBatch ? `Approve ${assignments!.length} Bookings` : 'Approve Booking',
      }
    }
    default:
      console.log('[approval_required] unhandled action — raw event:', JSON.stringify(event, null, 2))
      return {
        approvalId,
        agentId: 'bed_agent',
        title: `Approval Required`,
        recommendation: `Review and approve this action: ${action}.`,
        action: 'Approve',
      }
  }
}

// ── Map backend events → store updates ────────────────────────────────────────

// "ta_detect_claim_discrepancies" → "Detect claim discrepancies".
// Fallback only — SubAgentView prefers the real label from the pipeline by task id.
function humaniseTaskId(id: string): string {
  const words = id.replace(/^ta_/, '').replace(/^lp_/, '').replace(/_/g, ' ').trim()
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : id
}

function handleEvent(event: WsEvent) {
  switch (event.type) {
    case 'ping':
      break

    // Debug-only stream consumed exclusively by the secret /llm_trace page.
    // Ignore here so full-prompt payloads don't spam the orchestrator handler.
    case 'llm_trace':
      break

    case 'plan_stage_started': {
      const stage = event.stage as string
      const label = stage === 'agents' ? 'Planning agents…'
        : stage === 'subagents' ? 'Planning sub-agents…'
        : 'Planning tasks…'
      useStore.setState({ planningStage: label })
      break
    }

    case 'plan_stage_completed':
      break

    case 'session_started': {
      // The client that executed already set 'running' + opened the output panel via
      // confirmAndExecute. An observer client (e.g. the web app joining a session the
      // widget executed) only learns execution started from this event — flip it out of
      // the compose view AND open the Agent Output panel, matching confirmAndExecute.
      // Also fired by edit-resume (doc: "New run launched, also fired by edit-resume"),
      // which launches from 'paused'/'pausing', not 'idle' -- include those too.
      useStore.setState((s) =>
        s.executionStatus === 'idle' || s.executionStatus === 'paused' || s.executionStatus === 'pausing'
          ? { executionStatus: 'running', panelOpen: true, checkpointEditorOpen: false }
          : {}
      )
      if (event.reverted_to) {
        useStore.getState().pushToast({
          severity: 'info',
          title: 'Resumed from checkpoint',
          message: 'Reverted to a checkpoint and resumed — steps after that point are re-running.',
        })
      }
      break
    }

    case 'agent_started': {
      const agentId = event.agent_id as string
      useStore.setState((s) => ({
        // Safety net for an observer client that connected AFTER session_started fired
        // and so missed it: live agent events prove execution is underway. Only flip
        // from 'idle' — never clobber 'waiting_approval'. Open the output panel on the
        // same transition so the observer sees agent output (confirmAndExecute does this
        // for the originating client). Guarded, so it won't fight a manual close mid-run.
        ...(s.executionStatus === 'idle' ? { executionStatus: 'running' as ExecutionStatus, panelOpen: true } : {}),
        selectedNodeId: agentId,
        nodeStates: {
          ...s.nodeStates,
          [agentId]: { status: 'running' as NodeStatus, lines: s.nodeStates[agentId]?.lines ?? [], events: s.nodeStates[agentId]?.events ?? [] },
        },
      }))
      break
    }

    case 'sub_agent_started': {
      const subAgentId = event.sub_agent as string
      const agentId = _agentForSubAgent(subAgentId)
      if (!agentId) break
      _pushEvent(agentId, { type: 'started', subAgentId })
      break
    }

    case 'sub_agent_completed': {
      const subAgentId = event.sub_agent as string
      const agentId = _agentForSubAgent(subAgentId)
      if (!agentId) break
      _pushEvent(agentId, {
        type: 'completed',
        subAgentId,
        result: event.result as Record<string, unknown>,
      })
      break
    }

    case 'agent_plan': {
      // The dynamic task plan a sub-agent generated at runtime. Each task is
      // { id, selected, condition } — no label (resolved from the pipeline by id in
      // SubAgentView). Only `selected` tasks are part of the plan; condition is a
      // typed dict { symbol, op, value } or null.
      const subAgentId = event.subagent_id as string
      if (!subAgentId) break
      const rawTasks = (event.tasks as Array<Record<string, unknown>> | undefined) ?? []
      const tasks: TaskDef[] = rawTasks
        .filter((t) => t.selected !== false)
        .map((t, i) => {
          const id = typeof t.id === 'string' && t.id ? t.id : `lp_${subAgentId}_${i}`
          const c = t.condition as { symbol?: string; op?: string; value?: unknown } | null | undefined
          const condition = c
            ? `${(c.symbol ?? '').split('.').pop() ?? ''} ${c.op ?? ''} ${c.value ?? ''}`.trim()
            : undefined
          // Label is filled in by SubAgentView from the pipeline; humanised id as fallback.
          const label = humaniseTaskId(id)
          return { id, label, condition }
        })
      useStore.setState((s) => ({ livePlans: { ...s.livePlans, [subAgentId]: tasks } }))
      break
    }

    case 'task_condition': {
      // Runtime result of a single task's gate. Backend sends an explicit `skipped`
      // flag for planner-deselected tasks (passed=false, unresolvable=false there too),
      // so check it before falling through to 'failed'.
      const taskId = event.task_id as string
      if (!taskId) break
      const status: TaskConditionState['status'] =
        event.skipped ? 'skipped'
          : event.passed ? 'passed'
          : event.unresolvable ? 'skipped'
          : 'failed'
      // condition is a typed dict { symbol, op, value } or null
      const cond = event.condition as { symbol?: string; op?: string; value?: unknown } | null
      useStore.setState((s) => ({
        taskConditions: {
          ...s.taskConditions,
          [taskId]: { status, actual: event.actual, operator: cond?.op, threshold: cond?.value },
        },
      }))
      break
    }

    case 'branch_skipped': {
      const agentId = event.agent_id as string
      _setNodeStatus(agentId, 'skipped')
      useStore.setState((s) => ({
        edges: s.edges.map((e) => {
          // Dim any edge directly targeting the skipped agent
          if (e.target === agentId) {
            return {
              ...e,
              animated: false,
              style: { stroke: '#1e293b', strokeWidth: 1, strokeDasharray: '5,4', opacity: 0.35 },
              data: { ...(e.data as object), isSkipped: true },
            }
          }
          return e
        }),
      }))
      break
    }

    case 'agent_completed': {
      const agentId = event.agent_id as string
      // reused:true after an edit-resume = this agent was carried over from before
      // the chosen checkpoint, not actually re-executed (see the API doc's note on
      // agent_completed). Surfaced as a badge on the Agents list.
      const reused = event.reused === true
      _setNodeStatus(agentId, 'complete')

      useStore.setState((s) => {
        let edges = s.edges.map((e) =>
          e.source === agentId
            ? { ...e, animated: true, style: { stroke: '#14b8a6', strokeWidth: 2 } }
            : e,
        )
        // Always set explicitly (not just when true) -- a genuine re-run after a
        // *later* edit-resume must clear a stale reused:true left over from an
        // earlier one, not just skip touching it.
        const nodeStates = {
          ...s.nodeStates,
          [agentId]: { ...s.nodeStates[agentId], status: 'complete' as const, reused },
        }

        // If this agent feeds a virtual DecisionNode (vd_<agentId>), auto-complete it
        const decId = `vd_${agentId}`
        const hasDec = s.nodes.some((n) => n.id === decId)
        if (hasDec) {
          edges = edges.map((e) =>
            e.source === decId
              ? { ...e, animated: true, style: { stroke: '#14b8a6', strokeWidth: 2 } }
              : e,
          )
          return {
            edges,
            nodeStates: {
              ...nodeStates,
              [decId]: { status: 'complete' as const, lines: [], events: [] },
            },
          }
        }
        return { edges, nodeStates }
      })
      break
    }

    case 'patient_identification_required': {
      const count = typeof event.expected_count === 'number' ? event.expected_count : null
      useStore.setState({ patientIdentificationPending: true, patientIdentificationCount: count })
      break
    }

    case 'patients_identified': {
      useStore.setState({ patientIdentificationPending: false })
      break
    }

    case 'approval_required': {
      const approvalId = event.approval_id as string
      const sid = useStore.getState().sessionId

      // Optimistic placeholder so the modal opens instantly. The live event payload
      // is thin (its _buildApproval switch lacks several action types → generic text),
      // so we immediately reconcile against the DB, which rebuilds the rich,
      // action-specific card via pendingApprovalToGate and replaces this placeholder.
      useStore.setState((s) => {
        const exists = s.pendingApprovals.some((a) => a.approvalId === approvalId)
        return {
          executionStatus: 'waiting_approval',
          pendingApprovals: exists
            ? s.pendingApprovals
            : [...s.pendingApprovals, _buildApproval(approvalId, event)],
          // A newly-arriving approval must surface even if a previous one was minimized.
          approvalMinimized: exists ? s.approvalMinimized : false,
        }
      })
      if (sid) reconcileApprovals(sid, { allowClear: false })
      break
    }

    case 'alert': {
      const agentId = (event.agent_id as string | undefined) ?? _currentAgent()
      if (!agentId) break
      _pushEvent(agentId, {
        type: 'alert',
        subAgentId: '__alert__',
        message: event.message as string,
        severity: event.severity as string,
      })
      break
    }

    // ── RL bed-allocation: the flow's contending agents bid; the engine picked a winner.
    // Surface the bid ladder as a live card. Full payload (utilities, auction_id) is on the
    // event for a richer inline panel later.
    case 'bed_auction': {
      const resource = String((event.resource as string) ?? 'bed').replace('_bed', ' bed')
      const winner = String((event.winner as string) ?? (event.winner_node as string) ?? '?')
      useStore.getState().pushToast({
        severity: 'info',
        sticky: true,   // bed-auction result stays until the user dismisses it (X button)
        title: `${resource} → ${winner} won the bid`,
        message: '',
      })
      break
    }

    // ── Autonomous policy engine (autonomous mode only) ─────────────────────
    case 'policy_decision': {
      const outcome = (event.outcome as PolicyDecision['outcome']) ?? 'auto_approve'
      const kind = (event.kind as string) ?? ''
      const actionType = (event.action_type as string) ?? kind
      const agentId = (event.agent_id as string) ?? ''
      const st = useStore.getState()
      st.pushPolicyDecision({
        id: `pd-${Date.now()}-${st.policyDecisions.length}`,
        outcome,
        kind,
        agentId,
        actionType,
        risk: (event.risk as string) ?? 'low',
        reason: (event.reason as string) ?? '',
        ts: Date.now(),
      })
      // Only the exceptions (needs a human / escalated) get a transient toast; the
      // companion `alert` (category "policy") already lands in the Agent Output panel.
      if (outcome !== 'auto_approve') {
        st.pushToast({
          severity: outcome === 'escalate' ? 'critical' : 'warning',
          title: outcome === 'escalate' ? 'Decision escalated' : 'Approval needed',
          message: (event.message as string)
            ?? `${actionType || kind} (${(event.risk as string) ?? 'risk'}) needs attention`,
        })
      }
      break
    }

    case 'approval_escalated': {
      useStore.getState().pushToast({
        severity: 'warning',
        title: `Escalated to level ${(event.level as number) ?? 1}`,
        message: `${(event.action as string) ?? 'An action'} was escalated to the next approver.`,
      })
      break
    }

    case 'approval_auto_rejected': {
      const rejectedId = event.approval_id as string | undefined
      useStore.setState((s) => ({
        pendingApprovals: rejectedId
          ? s.pendingApprovals.filter((p) => p.approvalId !== rejectedId)
          : s.pendingApprovals,
      }))
      useStore.getState().pushToast({
        severity: 'critical',
        title: 'Auto-rejected',
        message: `${(event.action as string) ?? 'An action'} was auto-rejected (${(event.reason as string) ?? 'policy limit reached'}).`,
      })
      break
    }

    case 'approval_decided': {
      useStore.setState((s) => {
        const remaining = s.pendingApprovals.slice(1)
        if ((event.decision as string) === 'approved') {
          return { pendingApprovals: remaining, executionStatus: remaining.length > 0 ? 'waiting_approval' : 'running' }
        } else {
          return { pendingApprovals: [], executionStatus: 'idle' }
        }
      })
      break
    }

    // ── Pause / Resume / Cancel (autonomous mode only) ──────────────────────
    // These can arrive from another tab/client acting on the same session, not just
    // this tab's own pauseFlow()/resumeFlow()/cancelFlow() calls, so handle them
    // unconditionally rather than assuming this tab originated the action.
    case 'session_pause_requested': {
      useStore.setState({ executionStatus: 'pausing' })
      startPausePoll(event.session_id as string)
      break
    }

    case 'session_resumed': {
      stopPausePoll()
      useStore.setState({ executionStatus: 'running' })
      if (event.reverted_to) {
        useStore.getState().pushToast({
          severity: 'info',
          title: 'Resumed from checkpoint',
          message: `Reverted to a checkpoint and resumed — steps after that point are re-running.`,
        })
      }
      break
    }

    case 'session_cancelled': {
      stopPausePoll()
      useStore.setState({ executionStatus: 'cancelled' })
      useStore.getState().pushToast({
        severity: 'warning',
        title: 'Workflow cancelled',
        message: 'This workflow was cancelled and will not continue.',
      })
      break
    }

    case 'synthesis_started': {
      useStore.setState({ synthesisRunning: true })
      break
    }

    case 'session_recommendation': {
      // The flow has reached its completion checkpoint (backend moves the session to
      // complete_pending here) and is waiting on the user's Save & Confirm — without
      // this, executionStatus stays 'running' and the canvas keeps showing the
      // Autonomous Run/Pause/Cancel banner over a workflow that's actually done.
      useStore.setState({
        synthesisRunning: false,
        executionStatus: 'complete_pending',
        sessionRecommendation: {
          headline: event.headline as string,
          actions:  event.actions as string[],
          risk:     event.risk as string,
          summary:  event.summary as string,
        },
      })
      break
    }

    case 'session_completed': {
      useStore.setState((s) => ({
        executionStatus: 'submitted',
        edges: s.edges.map((e) => ({
          ...e,
          animated: true,
          style: { stroke: '#14b8a6', strokeWidth: 2, strokeDasharray: '0' },
        })),
      }))
      break
    }

    case 'task_failed': {
      const agentId = event.agent_id as string
      const taskId  = event.task_id as string | undefined
      const err     = event.error as string | undefined
      if (!agentId) break
      _pushEvent(agentId, {
        type: 'alert',
        subAgentId: `__fail_${taskId ?? 'unknown'}__`,
        message: `${taskId ? humaniseTaskId(taskId) : 'Task'} failed: ${err ?? 'Unknown error'}`,
        severity: 'critical',
      })
      _setNodeStatus(agentId, 'complete')
      useStore.setState({ selectedNodeId: agentId, panelOpen: true })
      break
    }

    case 'reorchestration_recommended': {
      const failedTask = event.failed_task as Record<string, unknown> | undefined
      const attempt   = (event.attempt as number | undefined) ?? 1
      const agentId   = (failedTask?.agent_id as string | undefined) ?? _currentAgent()
      if (!agentId) break
      _pushEvent(agentId, {
        type: 'alert',
        subAgentId: `__reorch_${attempt}__`,
        message: `Pipeline configuration error detected — re-orchestrating to correct the execution order (attempt ${attempt})…`,
        severity: 'warning',
      })
      break
    }

    case 'session_failed': {
      const err = event.error as string | undefined
      console.error('[WS] session_failed:', err)
      const { selectedNodeId, nodeStates } = useStore.getState()
      const targetId = selectedNodeId
        ?? Object.keys(nodeStates).find((id) => nodeStates[id]?.status === 'running')
      if (targetId) {
        _pushEvent(targetId, {
          type: 'alert',
          subAgentId: '__session_failed__',
          message: `Execution failed: ${err ?? 'An unexpected error occurred — check your pipeline configuration and try again.'}`,
          severity: 'critical',
        })
        _setNodeStatus(targetId, 'complete')
      }
      useStore.setState({ executionStatus: 'idle', panelOpen: true })
      break
    }

    case 'agent_error': {
      const agentId = event.agent_id as string
      _pushEvent(agentId, {
        type: 'alert',
        subAgentId: '__error__',
        message: `Error: ${event.error as string}`,
        severity: 'critical',
      })
      _setNodeStatus(agentId, 'idle')
      break
    }

    case 'plan_awaiting_approval': {
      const pipeline = event.pipeline as BackendPipeline
      if (!pipeline?.agents) break
      const attempt = (event.attempt as number | undefined) ?? 0
      const isFailureReplan = attempt > 0

      const seenIds = new Set<string>()
      const uniqueAgents = pipeline.agents.filter((a) => {
        if (seenIds.has(a.id)) {
          console.warn('[plan_awaiting_approval] duplicate agent id dropped:', a.id)
          return false
        }
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

      useStore.setState({
        backendPipeline: { ...pipeline, agents: uniqueAgents, edges: validEdges },
        rawEdgeDefs: edgeDefs,
        pipelineLoading: false,
        pipelineGenerated: true,
        nodes: layout.nodes,
        edges: layout.edges,
        pipelineSaveStatus: 'saved',
        ...(isFailureReplan ? { executionStatus: 'idle', panelOpen: true } : {}),
      })

      // For failure re-plans: surface a notification in the Agent Output panel
      if (isFailureReplan) {
        const targetId = _currentAgent()
          ?? Object.keys(useStore.getState().nodeStates)[0]
        if (targetId) {
          _pushEvent(targetId, {
            type: 'alert',
            subAgentId: `__replan_ready_${attempt}__`,
            message: `Revised pipeline ready (attempt ${attempt}) — the failed task has been routed around. Review the updated plan on the canvas and re-run when ready.`,
            severity: 'warning',
          })
        }
      }

      useStore.getState().fetchWorkflowHistory()
      break
    }

    case 'plan_auto_approved': {
      // Autonomous mode: the planning graph approved its own plan and is already
      // executing in the background. Render the pipeline and go straight to the
      // running view — no human confirm/execute gate (mirror of plan_awaiting_approval
      // minus the approval pause).
      const pipeline = event.pipeline as BackendPipeline
      if (!pipeline?.agents) break

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

      // Seed node states so the Agent Output panel is ready (confirmAndExecute,
      // which normally does this, is skipped in autonomous mode).
      const initial: Record<string, NodeState> = {}
      for (const n of layout.nodes) initial[n.id] = { status: 'idle', lines: [], events: [] }

      useStore.setState({
        backendPipeline: { ...pipeline, agents: uniqueAgents, edges: validEdges },
        rawEdgeDefs: edgeDefs,
        pipelineLoading: false,
        pipelineGenerated: true,
        nodes: layout.nodes,
        edges: layout.edges,
        pipelineSaveStatus: 'saved',
        executionStatus: 'running',
        panelOpen: true,
        nodeStates: initial,
      })
      useStore.getState().fetchWorkflowHistory()
      break
    }

    case 'session_reorchestrated': {
      const scope = event.scope as string
      if (scope === 'pipeline') {
        const pipeline = event.pipeline as BackendPipeline
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
        useStore.setState({
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
        })
      }
      if (scope === 'subagents') {
        const agentId = event.agent_id as string
        const selected = event.selected_subagents as string[]
        useStore.setState((s) => ({
          selectedSubagentsByAgent: { ...s.selectedSubagentsByAgent, [agentId]: selected },
        }))
      } else if (scope === 'tasks') {
        const agentId = event.agent_id as string
        const subagentId = event.subagent_id as string
        const tasks = (event.selected_tasks ?? []) as Array<{ id: string; label?: string; condition?: unknown; outputs?: string[] }>
        useStore.setState((s) => {
          const bp = s.backendPipeline
          if (bp) {
            for (const a of bp.agents) if (a.id === agentId)
              for (const sa of a.sub_agents ?? []) if (sa.id === subagentId)
                sa.tasks = tasks.map((t) => ({ id: t.id, label: t.label ?? t.id, condition: t.condition, outputs: t.outputs }))
          }
          return {
            backendPipeline: bp ? { ...bp } : bp,
            selectedTasksBySubagent: { ...s.selectedTasksBySubagent, [subagentId]: tasks.map((t) => t.id) },
          }
        })
      }
      break
    }
  }
}

// ── Console logger ────────────────────────────────────────────────────────────

function _log(e: WsEvent): void {
  if (e.type === 'agent_plan') {
    const tasks = e.tasks as Array<Record<string, unknown> | string>
    const lines = tasks?.map((t, i) => {
      if (typeof t === 'string') return `  ${i + 1}. ${t}`
      const label = t.label ?? t.description ?? t.task ?? t.name ?? t.id ?? '?'
      const condRaw = t.condition ?? t.condition_key ?? t.run_if ?? t.when
      const cond = typeof condRaw === 'string' ? condRaw
        : condRaw ? JSON.stringify(condRaw) : null
      return `  ${i + 1}. ${label}${cond ? `  [if ${cond}]` : ''}`
    }).join('\n') ?? ''
    console.log(`[WS] plan  ${e.subagent_id}\n${lines}`)
  } else if (e.type === 'task_condition') {
    const status   = e.passed ? 'pass' : (e.unresolvable ? 'skip' : 'fail')
    const cond     = e.condition as { operator?: string; threshold?: unknown } | undefined
    const expected = cond?.threshold !== undefined ? `  (expected ${cond.operator ?? '?'} ${cond.threshold})` : ''
    console.log(`[WS] task  ${e.task_id}  ${status}  ${e.actual ?? 'null'}${expected}`)
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function _agentForSubAgent(subAgentId: string): string | null {
  // Authoritative: which agent owns this sub-agent in the live pipeline. Handles
  // every agent (incl. billing_agent) without per-prefix heuristics.
  const bp = useStore.getState().backendPipeline
  if (bp) {
    for (const a of bp.agents) {
      if (a.sub_agents?.some((sa) => sa.id === subAgentId)) return a.id
    }
  }
  // Fallback heuristic for simulation / events arriving before the pipeline is set.
  if (subAgentId.startsWith('sa_bed_pred_')) return 'bed_prediction_agent'
  if (subAgentId.startsWith('sa_hk_'))       return 'bed_agent'
  const first = subAgentId.replace('sa_', '').split('_')[0]
  const map: Record<string, string> = {
    er:           'er_agent',
    icu:          'icu_agent',
    bed:          'bed_agent',
    discharge:    'discharge_agent',
    staff:        'staff_agent',
    pharmacy:     'pharmacy_agent',
    ot:           'ot_agent',
    rev:          'revenue_agent',
    ic:           'infection_control_agent',
    sc:           'supply_chain_agent',
    lab:          'lab_agent',
    ambulance:    'ambulance_agent',
    patient:      'patient_verification_agent',
  }
  return map[first] ?? null
}

function _currentAgent(): string | null {
  return useStore.getState().selectedNodeId
}

function _pushEvent(nodeId: string, event: SubAgentEvent) {
  useStore.setState((s) => ({
    nodeStates: {
      ...s.nodeStates,
      [nodeId]: {
        ...s.nodeStates[nodeId],
        events: [...(s.nodeStates[nodeId]?.events ?? []), event],
      },
    },
  }))
}

function _setNodeStatus(nodeId: string, status: NodeStatus) {
  useStore.setState((s) => ({
    nodeStates: {
      ...s.nodeStates,
      [nodeId]: { ...s.nodeStates[nodeId], status },
    },
  }))
}

// ── Approval reconciliation ───────────────────────────────────────────────────

// The live `approval_required` WS push is best-effort: with Kafka disabled, an
// approval raised on the Temporal worker never reaches this process's WS clients
// (see api/routes/ws.py — deliver_local is process-local). The approval_tasks DB
// row is the durable source of truth. So we reconcile against the DB: add any
// pending approval we're missing, and drop any we hold that's been decided
// elsewhere. Runs on WS open AND on a timer while the session is live.
async function reconcileApprovals(sessionId: string, { allowClear = true }: { allowClear?: boolean } = {}) {
  let pending
  try {
    pending = await fetchPendingApprovals(sessionId)
  } catch {
    return
  }
  const s = useStore.getState()
  // Don't touch terminal states — a finished session has no live approvals to gate.
  if (s.executionStatus === 'submitted' || s.executionStatus === 'complete_pending') return

  // Race guard: the reconcile fired straight from an approval_required event may
  // beat the DB write. Don't let an empty fetch wipe an optimistic card we just
  // queued — the timer reconcile (allowClear) handles genuine clears.
  if (pending.length === 0 && !allowClear && s.pendingApprovals.length > 0) return

  // The DB is the source of truth. Rebuild EVERY pending card via pendingApprovalToGate
  // (rich, action-specific) — this also replaces any optimistic/generic card the live
  // approval_required event queued, so live matches reload for all action types. Not
  // _buildApproval, whose switch lacks several DB action types and falls back to generic.
  const merged = pending.map((p) => pendingApprovalToGate(p))

  // Skip the write when nothing meaningfully changed, so a steady state doesn't
  // re-render the modal on every timer tick. Compare order-independently by
  // approvalId + recommendation text (rebuilt cards are equal only if content matches).
  const cardKey = (a: { approvalId?: string | null; recommendation?: string }) =>
    `${a.approvalId ?? ''}|${a.recommendation ?? ''}`
  const currentKeys = new Set(s.pendingApprovals.map(cardKey))
  const unchanged =
    merged.length === s.pendingApprovals.length &&
    merged.every((m) => currentKeys.has(cardKey(m)))
  if (unchanged) return

  console.log('[WS] reconcile:', merged.length, 'approval(s) from DB')
  useStore.setState({
    pendingApprovals: merged,
    executionStatus: merged.length > 0
      ? 'waiting_approval'
      : (s.executionStatus === 'waiting_approval' ? 'running' : s.executionStatus),
  })
}

// ── Hook ──────────────────────────────────────────────────────────────────────

export function useSessionWebSocket() {
  const sessionId = useStore((s) => s.sessionId)

  useEffect(() => {
    if (!sessionId) return

    // Multi-tenancy: the WS endpoint authenticates via ?token= (the browser
    // WebSocket API can't set an Authorization header) and org-checks the session.
    const token = getToken()
    const url = `${WS_BASE}/ws/${sessionId}${token ? `?token=${encodeURIComponent(token)}` : ''}`
    console.log('[WS] connecting', `${WS_BASE}/ws/${sessionId}`)
    const ws = new WebSocket(url)

    ws.onopen = () => {
      console.log('[WS] connected  session=', sessionId)
      reconcileApprovals(sessionId)
    }

    // Safety net for the best-effort live push: poll the DB while the session is
    // live so an approval raised on the worker still surfaces on connected clients.
    const reconcileTimer = setInterval(() => reconcileApprovals(sessionId), 4000)

    ws.onmessage = (ev) => {
      try {
        const data: WsEvent = JSON.parse(ev.data as string)
        if (data.type !== 'ping' && data.type !== 'llm_trace') _log(data)
        handleEvent(data)
      } catch (err) {
        console.error('[WS] parse error', err)
      }
    }

    ws.onerror  = (err) => console.error('[WS] error', err)
    ws.onclose  = ()    => console.log('[WS] disconnected  session=', sessionId)

    return () => {
      clearInterval(reconcileTimer)
      ws.close()
    }
  }, [sessionId])
}
