import { useEffect, useState, useCallback } from 'react'
import { ShieldAlert, CheckCircle2, XCircle, RefreshCw, Clock, Loader2, Truck, ChevronDown, ChevronUp, Fuel, Check, ChevronRight, Flag, Workflow, MessageSquare } from 'lucide-react'
import { fetchAllPendingApprovals, decideApproval, getSession, type AllPendingApproval, type SessionDetail } from '../services/api'
import { computeWaves } from '../lib/waves'

const ACTION_TITLES: Record<string, string> = {
  bed_reservation:               'Bed Reservation',
  icu_admission_request:         'ICU Admission',
  icu_transfer_recommendations:  'ICU Transfer',
  mark_discharge_ready:          'Discharge Auth',
  ambulance_dispatch:            'Ambulance Dispatch',
  staff_reallocation:            'Staff Reallocation',
}

const ACTION_COLORS: Record<string, { bg: string; text: string; border: string; accent: string }> = {
  bed_reservation:               { bg: 'bg-blue-500/15',   text: 'text-blue-300',   border: 'border-blue-500/30',   accent: '#3b82f6' },
  icu_admission_request:         { bg: 'bg-red-500/15',    text: 'text-red-300',    border: 'border-red-500/30',    accent: '#ef4444' },
  icu_transfer_recommendations:  { bg: 'bg-orange-500/15', text: 'text-orange-300', border: 'border-orange-500/30', accent: '#f97316' },
  mark_discharge_ready:          { bg: 'bg-teal-500/15',   text: 'text-teal-300',   border: 'border-teal-500/30',   accent: '#14b8a6' },
  ambulance_dispatch:            { bg: 'bg-sky-500/15',    text: 'text-sky-300',    border: 'border-sky-500/30',    accent: '#0ea5e9' },
  staff_reallocation:            { bg: 'bg-purple-500/15', text: 'text-purple-300', border: 'border-purple-500/30', accent: '#a855f7' },
}

function timeAgo(iso: string): string {
  const secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000)
  if (secs < 60) return `${secs}s ago`
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`
  return `${Math.floor(secs / 3600)}h ago`
}

function Description({ a }: { a: AllPendingApproval }) {
  const p = a.payload

  if (a.action_type === 'bed_reservation') {
    if (Array.isArray(p.assignments) && p.assignments.length > 0) {
      const count = p.assignments.length
      const summary = (p.summary as string[] | undefined) ?? []
      return (
        <div className="space-y-1">
          {summary.slice(0, 3).map((s, i) => (
            <div key={i} className="flex items-start gap-1.5">
              <span className="text-teal-400 mt-0.5 flex-shrink-0 text-[10px]">•</span>
              <span className="text-xs text-slate-300 leading-snug">{s}</span>
            </div>
          ))}
          {count > summary.length && (
            <span className="text-[10px] text-slate-500">+{count - summary.length} more assignments</span>
          )}
        </div>
      )
    }
    return (
      <div className="text-xs text-slate-300">
        Reserve <strong className="text-slate-100">Bed {String(p.bed_id ?? 'Unknown')}</strong>
        {typeof p.patient_token === 'string' && p.patient_token !== 'UNKNOWN' && (
          <> · Patient <span className="font-mono text-slate-400">{String(p.patient_token).slice(0, 8)}</span></>
        )}
      </div>
    )
  }

  if (a.action_type === 'icu_admission_request') {
    return (
      <div className="space-y-0.5">
        <div className="text-xs text-slate-300">
          Patient <span className="font-mono text-slate-400">{String(p.patient_token ?? 'Unknown').slice(0, 8)}</span>
          {p.rank != null && <span className="ml-2 text-slate-500 text-[10px]">Rank #{String(p.rank)}</span>}
        </div>
        {p.reason != null && (
          <div className="text-[11px] text-slate-400 leading-snug">{String(p.reason)}</div>
        )}
        {Boolean(p.ventilator_dependent) && (
          <div className="text-[10px] text-amber-400 font-medium">⚠ Ventilator dependent</div>
        )}
      </div>
    )
  }

  if (a.action_type === 'icu_transfer_recommendations') {
    const escalation = (p.escalation_candidates as unknown[] | undefined) ?? []
    const stepDown   = (p.step_down_candidates  as unknown[] | undefined) ?? []
    return (
      <div className="space-y-1">
        {typeof p.summary === 'string' && (
          <div className="text-xs text-slate-300 leading-snug">{p.summary}</div>
        )}
        <div className="flex gap-2">
          {escalation.length > 0 && (
            <span className="px-2 py-0.5 rounded-full bg-red-500/10 border border-red-500/20 text-[10px] text-red-300 font-medium">
              ↑ {escalation.length} escalation{escalation.length > 1 ? 's' : ''}
            </span>
          )}
          {stepDown.length > 0 && (
            <span className="px-2 py-0.5 rounded-full bg-teal-500/10 border border-teal-500/20 text-[10px] text-teal-300 font-medium">
              ↓ {stepDown.length} step-down{stepDown.length > 1 ? 's' : ''}
            </span>
          )}
        </div>
      </div>
    )
  }

  if (a.action_type === 'mark_discharge_ready') {
    const count = (p.ready_count as number | undefined) ?? (p.ready_ids as unknown[] | undefined)?.length ?? 0
    return (
      <div className="space-y-1">
        <div className="text-xs text-slate-300">
          <strong className="text-teal-400">{count}</strong> patient{count !== 1 ? 's' : ''} cleared for discharge
        </div>
        {Array.isArray(p.ready_ids) && p.ready_ids.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {(p.ready_ids as string[]).slice(0, 4).map((id) => (
              <span key={id} className="px-1.5 py-0.5 rounded-full bg-teal-500/15 border border-teal-500/25 text-[10px] text-teal-300 font-mono">
                {String(id).slice(0, 8)}
              </span>
            ))}
            {p.ready_ids.length > 4 && (
              <span className="text-[10px] text-slate-500 py-0.5">+{p.ready_ids.length - 4} more</span>
            )}
          </div>
        )}
      </div>
    )
  }

  if (a.action_type === 'ambulance_dispatch') {
    const asgn = (p.assignment ?? {}) as Record<string, unknown>
    return (
      <div className="space-y-0.5">
        <div className="text-xs text-sky-300 font-medium">{String(p.emergency_type ?? 'Emergency')}</div>
        {asgn.assigned_vehicle_no != null && (
          <div className="text-[11px] text-slate-400">
            Vehicle <strong className="text-slate-200">{String(asgn.assigned_vehicle_no)}</strong>
            {asgn.eta_mins != null && <> · ETA <strong className="text-slate-200">{String(asgn.eta_mins)} min</strong></>}
          </div>
        )}
        {typeof asgn.summary === 'string' && (
          <div className="text-[10px] text-slate-500 leading-snug">{asgn.summary}</div>
        )}
      </div>
    )
  }

  if (a.action_type === 'staff_reallocation') {
    const count = (p.recommendations as unknown[] | undefined)?.length ?? 0
    return (
      <div className="space-y-0.5">
        <div className="text-xs text-slate-300">
          <strong className="text-purple-400">{count}</strong> nurse reallocation{count !== 1 ? 's' : ''} recommended
        </div>
        {typeof p.summary === 'string' && (
          <div className="text-[11px] text-slate-400 leading-snug">{p.summary}</div>
        )}
        {Array.isArray(p.high_pressure_wards) && p.high_pressure_wards.length > 0 && (
          <div className="text-[10px] text-amber-400">
            High pressure: {(p.high_pressure_wards as string[]).join(', ')}
          </div>
        )}
      </div>
    )
  }

  // Generic fallback
  const keys = Object.keys(p).filter((k) => k !== '_idem').slice(0, 3)
  return (
    <div className="space-y-0.5">
      {keys.map((k) => {
        const v = p[k]
        const display = typeof v === 'object' ? JSON.stringify(v).slice(0, 60) : String(v).slice(0, 80)
        return (
          <div key={k} className="flex gap-2 text-[11px]">
            <span className="text-slate-500 capitalize shrink-0">{k.replace(/_/g, ' ')}:</span>
            <span className="text-slate-300 truncate">{display}</span>
          </div>
        )
      })}
    </div>
  )
}

// Compact left-to-right agent-chip chain for an approval's session pipeline. Agents are
// ordered by execution wave (computeWaves) and the agent that raised THIS approval is
// flagged so an approver sees where in the flow the decision sits.
function FlowChain({ session, raisedByAgentId }: { session: SessionDetail; raisedByAgentId: string }) {
  const pipeline = session.pipeline ?? session.pipeline_snapshot
  if (!pipeline || !pipeline.agents?.length) {
    return <span className="text-xs text-slate-600 italic">Flow unavailable for this run.</span>
  }
  const byId = new Map(pipeline.agents.map((a) => [a.id, a]))
  const ordered = computeWaves(pipeline).flat().map((id) => byId.get(id)).filter(Boolean) as typeof pipeline.agents
  // Include any agents computeWaves dropped (e.g. disconnected), preserving array order.
  for (const a of pipeline.agents) if (!ordered.some((o) => o.id === a.id)) ordered.push(a)

  return (
    <div className="flex flex-wrap items-center gap-x-1 gap-y-1.5">
      {ordered.map((a, i) => {
        const raised = a.id === raisedByAgentId
        const color = a.color || '#94a3b8'
        return (
          <div key={a.id} className="flex items-center gap-1">
            <span
              className="inline-flex items-center gap-1.5 px-2 py-1 rounded-lg text-[11px] font-semibold border"
              style={raised
                ? { background: color + '25', color, borderColor: color + '90', boxShadow: `0 0 0 1px ${color}55` }
                : { background: color + '12', color, borderColor: color + '35' }}
              title={a.role || a.label}
            >
              <span className="w-1.5 h-1.5 rounded-full flex-shrink-0" style={{ background: color }} />
              {a.label}
              {raised && <Flag size={10} className="flex-shrink-0" />}
            </span>
            {i < ordered.length - 1 && <ChevronRight size={12} className="text-slate-600 flex-shrink-0" />}
          </div>
        )
      })}
    </div>
  )
}

export function ApprovalsPage() {
  const [approvals, setApprovals]         = useState<AllPendingApproval[]>([])
  const [loading, setLoading]             = useState(true)
  const [deciding, setDeciding]           = useState<Record<string, 'approving' | 'rejecting'>>({})
  const [fleetPickerOpen, setFleetPicker] = useState<Record<string, boolean>>({})
  // Which approval's context panel (query + flow) is expanded, and a per-session cache of
  // the fetched detail so switching between rows of the same session doesn't refetch.
  const [expandedId, setExpandedId]       = useState<string | null>(null)
  const [sessionCache, setSessionCache]   = useState<Record<string, SessionDetail | 'loading' | 'error'>>({})

  const refresh = useCallback(async () => {
    const data = await fetchAllPendingApprovals()
    setApprovals(data)
    setLoading(false)
  }, [])

  // Toggle the context panel; lazily fetch the session (goal + pipeline) on first expand.
  function toggleDetails(a: AllPendingApproval) {
    if (expandedId === a.id) { setExpandedId(null); return }
    setExpandedId(a.id)
    if (a.session_id && !sessionCache[a.session_id]) {
      setSessionCache((prev) => ({ ...prev, [a.session_id]: 'loading' }))
      getSession(a.session_id)
        .then((s) => setSessionCache((prev) => ({ ...prev, [a.session_id]: s })))
        .catch(() => setSessionCache((prev) => ({ ...prev, [a.session_id]: 'error' })))
    }
  }

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 5000)
    return () => clearInterval(id)
  }, [refresh])

  async function decide(approvalId: string, decision: 'approved' | 'rejected', overrideVehicleNo?: string) {
    setDeciding((prev) => ({ ...prev, [approvalId]: decision === 'approved' ? 'approving' : 'rejecting' }))
    try {
      await decideApproval(approvalId, decision, overrideVehicleNo)
      setApprovals((prev) => prev.filter((a) => a.id !== approvalId))
      setFleetPicker((prev) => { const next = { ...prev }; delete next[approvalId]; return next })
    } catch (err) {
      console.error('[ApprovalsPage] decide failed', err)
    } finally {
      setDeciding((prev) => { const next = { ...prev }; delete next[approvalId]; return next })
    }
  }

  return (
    <div className="flex-1 overflow-y-auto bg-[var(--bg-base)] p-6">
      <div className="max-w-7xl mx-auto">

        {/* Header */}
        <div className="flex items-center justify-between mb-5">
          <div>
            <h1 className="text-xl font-bold text-slate-100 flex items-center gap-2.5">
              <ShieldAlert size={20} className="text-amber-400" />
              Pending Approvals
            </h1>
            <p className="text-sm text-slate-500 mt-0.5">
              Review and authorize AI-recommended clinical actions
            </p>
          </div>
          <button
            onClick={refresh}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs text-slate-400 border border-[var(--border-a)] hover:bg-[var(--bg-raised)] transition-colors"
          >
            <RefreshCw size={12} />
            Refresh
          </button>
        </div>

        {/* States */}
        {loading ? (
          <div className="flex items-center justify-center py-20 text-slate-500">
            <Loader2 size={20} className="animate-spin mr-2" />
            Loading…
          </div>
        ) : approvals.length === 0 ? (
          <div className="text-center py-20">
            <div className="text-4xl mb-3">✅</div>
            <div className="text-slate-400 font-medium">No pending approvals</div>
            <div className="text-slate-600 text-sm mt-1">
              All caught up — new approvals appear here automatically.
            </div>
          </div>
        ) : (

          /* Table */
          <div className="rounded-xl border border-[var(--border)] overflow-hidden">
            {/* Column headers */}
            <div className="grid grid-cols-[190px_1fr_110px_84px_290px] bg-[var(--bg-surface)] border-b border-[var(--border)] px-4 py-2.5">
              {['Type', 'Description', 'User', 'Waiting', 'Actions'].map((col) => (
                <div key={col} className="text-[10px] font-semibold text-slate-500 uppercase tracking-wider">
                  {col}
                </div>
              ))}
            </div>

            {/* Rows */}
            <div className="divide-y divide-[var(--border)]">
              {approvals.map((a) => {
                const title  = ACTION_TITLES[a.action_type] ?? a.action_type.replace(/_/g, ' ')
                const colors = ACTION_COLORS[a.action_type] ?? { bg: 'bg-slate-500/15', text: 'text-slate-300', border: 'border-slate-500/30' }
                const isBusy = !!deciding[a.id]

                const isAmbulance = a.action_type === 'ambulance_dispatch'
                const fleetOpen   = fleetPickerOpen[a.id] ?? false
                const fleet       = isAmbulance
                  ? ((a.payload.available_ambulances as Record<string, unknown>[] | undefined) ?? [])
                  : []

                return (
                  <div key={a.id}>
                    {/* Main row */}
                    <div
                      className="grid grid-cols-[190px_1fr_110px_84px_290px] items-start px-4 py-3.5 bg-[var(--bg-surface)] hover:bg-[var(--bg-raised)] transition-colors"
                      style={{ borderLeft: `3px solid ${colors.accent}` }}
                    >
                      {/* Type + escalation badge + context toggle */}
                      <div className="flex flex-col gap-1.5 pt-0.5">
                        <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-[11px] font-semibold w-fit ${colors.bg} ${colors.text} ${colors.border}`}>
                          <ShieldAlert size={11} />
                          {title}
                        </span>
                        {a.escalation_level > 0 && (
                          <span className="inline-flex items-center px-2 py-0.5 rounded-full bg-red-500/15 border border-red-500/25 text-[10px] text-red-400 font-bold w-fit">
                            Escalation L{a.escalation_level}
                          </span>
                        )}
                        <button
                          onClick={() => toggleDetails(a)}
                          className="inline-flex items-center gap-1 text-[10px] font-semibold text-slate-500 hover:text-slate-300 transition-colors w-fit"
                          title="Show the query and agent flow behind this approval"
                        >
                          {expandedId === a.id ? <ChevronUp size={11} /> : <ChevronDown size={11} />}
                          Query &amp; flow
                        </button>
                      </div>

                      {/* Description */}
                      <div className="pr-4">
                        <Description a={a} />
                      </div>

                      {/* User */}
                      <div className="pt-0.5">
                        <span className="text-[11px] text-slate-400 truncate block max-w-[110px]">
                          {a.user_display_name ?? '—'}
                        </span>
                      </div>

                      {/* Waiting */}
                      <div className="flex items-center gap-1 text-[11px] text-slate-500 pt-0.5">
                        <Clock size={10} className="flex-shrink-0" />
                        {timeAgo(a.created_at)}
                      </div>

                      {/* Actions — Approve, the optional ambulance-fleet toggle, and Decline
                          all stay on one line (no wrap); the column is sized to fit them. */}
                      <div className="flex flex-nowrap items-center gap-2 pt-0.5">
                        <button
                          onClick={() => decide(a.id, 'approved')}
                          disabled={isBusy}
                          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-teal-600 hover:bg-teal-500 disabled:opacity-50 disabled:cursor-not-allowed text-white text-xs font-semibold transition-colors"
                        >
                          {deciding[a.id] === 'approving'
                            ? <Loader2 size={11} className="animate-spin" />
                            : <CheckCircle2 size={11} />
                          }
                          Approve
                        </button>

                        {/* Ambulance-only: pick a different unit */}
                        {isAmbulance && fleet.length > 1 && (
                          <button
                            onClick={() => setFleetPicker((p) => ({ ...p, [a.id]: !p[a.id] }))}
                            disabled={isBusy}
                            title="Choose a different vehicle"
                            aria-label="Choose a different vehicle"
                            className="flex items-center gap-1 px-2 py-1.5 rounded-lg border border-[var(--border-a)] bg-[var(--bg-raised)] hover:bg-[var(--bg-hover)] disabled:opacity-50 text-slate-400 transition-colors"
                          >
                            <Truck size={13} />
                            {fleetOpen ? <ChevronUp size={10} /> : <ChevronDown size={10} />}
                          </button>
                        )}

                        <button
                          onClick={() => decide(a.id, 'rejected')}
                          disabled={isBusy}
                          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-[var(--bg-raised)] hover:bg-[var(--bg-hover)] disabled:opacity-50 disabled:cursor-not-allowed border border-[var(--border-a)] text-slate-400 text-xs font-semibold transition-colors"
                        >
                          {deciding[a.id] === 'rejecting'
                            ? <Loader2 size={11} className="animate-spin" />
                            : <XCircle size={11} />
                          }
                          Decline
                        </button>
                      </div>
                    </div>

                    {/* Context panel — the query + agent flow behind this approval (lazy) */}
                    {expandedId === a.id && (() => {
                      const s = a.session_id ? sessionCache[a.session_id] : undefined
                      return (
                        <div className="px-4 pb-4 pt-2 bg-[var(--bg-raised)] border-t border-[var(--border-a)] space-y-3" style={{ borderLeft: `3px solid ${colors.accent}` }}>
                          {s === 'loading' || s === undefined ? (
                            <div className="flex items-center gap-2 text-xs text-slate-500 py-2">
                              <Loader2 size={13} className="animate-spin" /> Loading context…
                            </div>
                          ) : s === 'error' ? (
                            <div className="text-xs text-slate-500 py-2">Couldn't load this run's context.</div>
                          ) : (
                            <>
                              {/* Query */}
                              <div>
                                <div className="text-[10px] font-bold text-slate-500 uppercase tracking-wider mb-1 flex items-center gap-1.5">
                                  <MessageSquare size={10} /> Query
                                </div>
                                <p className="text-xs text-slate-300 leading-relaxed">{s.goal}</p>
                                {s.constraints?.trim() && (
                                  <p className="text-[11px] text-amber-400/90 leading-snug mt-1">
                                    <span className="font-semibold">Constraints:</span> {s.constraints}
                                  </p>
                                )}
                              </div>

                              {/* Flow */}
                              <div>
                                <div className="text-[10px] font-bold text-slate-500 uppercase tracking-wider mb-1.5 flex items-center gap-1.5">
                                  <Workflow size={10} /> Flow
                                </div>
                                <FlowChain session={s} raisedByAgentId={a.agent_id} />
                              </div>
                            </>
                          )}
                        </div>
                      )
                    })()}

                    {/* Fleet picker panel — ambulance only */}
                    {isAmbulance && fleetOpen && (
                      <div className="px-4 pb-3 pt-2 bg-[var(--bg-raised)] border-t border-[var(--border-a)]" style={{ borderLeft: `3px solid ${colors.accent}` }}>
                        <div className="text-[10px] font-bold text-slate-500 uppercase tracking-wider mb-2 flex items-center gap-1.5">
                          <Truck size={10} />
                          Available Fleet — select a unit to dispatch instead
                        </div>
                        <div className="overflow-x-auto">
                          <table className="w-full text-[11px]">
                            <thead>
                              <tr className="text-[10px] text-slate-500 uppercase tracking-wider border-b border-[var(--border)]">
                                <th className="text-left pb-1.5 pr-3 font-semibold">Vehicle</th>
                                <th className="text-left pb-1.5 pr-3 font-semibold">Type</th>
                                <th className="text-left pb-1.5 pr-3 font-semibold">Driver</th>
                                <th className="text-left pb-1.5 pr-3 font-semibold">Location</th>
                                <th className="text-left pb-1.5 pr-3 font-semibold">ETA</th>
                                <th className="text-left pb-1.5 pr-3 font-semibold">Fuel</th>
                                <th className="pb-1.5" />
                              </tr>
                            </thead>
                            <tbody className="divide-y divide-[var(--border)]">
                              {fleet.map((unit) => {
                                const vno = String(unit.vehicle_no ?? '')
                                const isAssigned = vno === String((a.payload.assignment as Record<string, unknown>)?.assigned_vehicle_no ?? '')
                                return (
                                  <tr key={vno} className={`transition-colors ${isAssigned ? 'bg-teal-500/10' : 'hover:bg-[var(--bg-hover)]'}`}>
                                    <td className="py-1.5 pr-3 font-mono font-semibold text-slate-200">
                                      {vno}
                                      {isAssigned && <span className="ml-1.5 text-[9px] text-teal-400 font-bold uppercase">AI pick</span>}
                                    </td>
                                    <td className="py-1.5 pr-3">
                                      <span className={`px-1.5 py-0.5 rounded text-[9px] font-bold ${unit.vehicle_type === 'ALS' ? 'bg-red-500/15 text-red-400' : 'bg-blue-500/15 text-blue-400'}`}>
                                        {String(unit.vehicle_type ?? '')}
                                      </span>
                                    </td>
                                    <td className="py-1.5 pr-3 text-slate-300">{String(unit.driver_name ?? '—')}</td>
                                    <td className="py-1.5 pr-3 text-slate-400">{String(unit.current_location ?? '—')}</td>
                                    <td className="py-1.5 pr-3 text-slate-300">{unit.eta_mins != null ? `${unit.eta_mins}m` : '—'}</td>
                                    <td className="py-1.5 pr-3">
                                      <span className="flex items-center gap-1 text-slate-400">
                                        <Fuel size={9} />
                                        {unit.fuel_level != null ? `${unit.fuel_level}%` : '—'}
                                      </span>
                                    </td>
                                    <td className="py-1.5">
                                      {isAssigned ? (
                                        <button
                                          onClick={() => decide(a.id, 'approved')}
                                          disabled={isBusy}
                                          className="flex items-center gap-1 px-2 py-1 rounded bg-teal-600 hover:bg-teal-500 text-white text-[10px] font-semibold disabled:opacity-50 transition-colors"
                                        >
                                          <Check size={9} /> Dispatch
                                        </button>
                                      ) : (
                                        <button
                                          onClick={() => decide(a.id, 'approved', vno)}
                                          disabled={isBusy}
                                          className="flex items-center gap-1 px-2 py-1 rounded border border-[var(--border-a)] bg-[var(--bg-surface)] hover:bg-teal-600 hover:text-white hover:border-teal-600 text-slate-400 text-[10px] font-semibold disabled:opacity-50 transition-colors"
                                        >
                                          Select
                                        </button>
                                      )}
                                    </td>
                                  </tr>
                                )
                              })}
                            </tbody>
                          </table>
                        </div>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>

            {/* Footer count */}
            <div className="px-4 py-2.5 bg-[var(--bg-surface)] border-t border-[var(--border)] text-[11px] text-slate-600">
              {approvals.length} pending approval{approvals.length !== 1 ? 's' : ''} · auto-refreshes every 5s
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
