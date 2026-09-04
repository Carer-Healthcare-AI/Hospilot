import { useEffect, useRef, useState, useCallback, useMemo } from 'react'
import {
  Loader2, Pencil, Check, X, Zap, Workflow as WorkflowIcon,
  CheckCircle2, XCircle, Clock, Ban, ArrowUpRight, PauseCircle, Bell, ArrowLeft,
} from 'lucide-react'
import { useStore } from '../store'
import {
  listSessions, renameSession, fetchPausedQueue, fetchAllPendingApprovals,
  type SessionSummary, type AllPendingApproval,
} from '../services/api'

// Elapsed time since an approval was created, for the "waiting Xm" line in the popover.
// Same unit-letter style as formatDuration below, just always counting up from a fixed
// point (an open approval has no end time).
function formatWaitTime(iso: string) {
  const secs = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000))
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m`
  return `${secs}s`
}

// Every workflow the user has launched. Polls /api/sessions so all concurrent runs
// stay live without a WebSocket each; clicking a row (or its Action button) re-attaches
// the canvas (loadSession) to that workflow. Names are editable inline (default "New
// Workflow").
const STATUS_META: Record<string, { label: string; color: string; pulse: boolean; Icon: typeof Loader2 }> = {
  pending:          { label: 'Queued',    color: '#94a3b8', pulse: false, Icon: Clock },
  running:          { label: 'Running',   color: '#3b82f6', pulse: true,  Icon: Loader2 },
  complete_pending: { label: 'Finishing', color: '#14b8a6', pulse: true,  Icon: Loader2 },
  submitted:        { label: 'Completed', color: '#10b981', pulse: false, Icon: CheckCircle2 },
  completed:        { label: 'Completed', color: '#10b981', pulse: false, Icon: CheckCircle2 },
  failed:           { label: 'Failed',    color: '#ef4444', pulse: false, Icon: XCircle },
  cancelled:        { label: 'Cancelled', color: '#94a3b8', pulse: false, Icon: Ban },
}

// Pausing never touches the DB status column (still "running" underneath — pausing
// is tracked only in Redis), so this overrides the row's displayed status when its
// session_id shows up in the paused queue with kind "user_paused" (see refresh()).
const PAUSED_META = { label: 'Paused', color: '#f59e0b', pulse: false, Icon: PauseCircle }

function displayName(s: SessionSummary) {
  return s.name?.trim() || 'New Workflow'
}

// Absolute clock time rather than "3h ago" — relative time reads fine at a glance but
// gets ambiguous once a few workflows are sitting at similar ages; a real timestamp
// doesn't need re-reading every time it ticks over either. Full precision still lives
// in the title tooltip.
function formatStarted(iso: string) {
  return new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

const ACTIVE = new Set(['pending', 'running', 'complete_pending'])

// Elapsed wall-clock time for the run: (now, or updated_at once finished) − created_at.
// Spelled out with unit letters (m/s/h) rather than a bare "27:01" clock format, which
// reads ambiguously as either minutes:seconds or hours:minutes.
function formatDuration(s: SessionSummary) {
  const start = new Date(s.created_at).getTime()
  const end = ACTIVE.has(s.status) ? Date.now() : new Date(s.updated_at).getTime()
  const secs = Math.max(0, Math.floor((end - start) / 1000))
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  const sec = secs % 60
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`
  if (m > 0) return `${m}m ${String(sec).padStart(2, '0')}s`
  return `${sec}s`
}

export function WorkflowsPage() {
  const loadSession = useStore((s) => s.loadSession)
  const setActiveView = useStore((s) => s.setActiveView)

  const [rows, setRows] = useState<SessionSummary[]>([])
  const [pausedIds, setPausedIds] = useState<Set<string>>(new Set())
  const [approvals, setApprovals] = useState<AllPendingApproval[]>([])
  const [loaded, setLoaded] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  // Which row's pending-approvals popover is open, anchored at the bell icon's
  // position (fixed positioning so it isn't clipped by the table's scroll containers).
  const [openApprovalsFor, setOpenApprovalsFor] = useState<{ sessionId: string; x: number; y: number } | null>(null)
  const inFlight = useRef(false)

  const refresh = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    try {
      const [{ sessions }, pausedResult, pendingApprovals] = await Promise.all([
        listSessions(50),
        // Cheap, Redis-backed — safe to poll alongside the session list every tick.
        fetchPausedQueue().catch((err) => {
          console.error('[WorkflowsPage] fetchPausedQueue failed', err)
          return null
        }),
        fetchAllPendingApprovals(),   // already returns [] on any failure
      ])
      setRows(sessions)
      setApprovals(pendingApprovals)
      if (pausedResult) {
        setPausedIds(new Set(
          pausedResult.flows.filter((f) => f.kind === 'user_paused').map((f) => f.session_id),
        ))
      }
    } catch (err) {
      console.error('[WorkflowsPage] list failed', err)
    } finally {
      inFlight.current = false
      setLoaded(true)
    }
  }, [])

  // Poll while the page is open so concurrent runs (and their pending approvals) update
  // live without leaving the table.
  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 3000)
    return () => clearInterval(t)
  }, [refresh])

  const approvalsBySession = useMemo(() => {
    const map = new Map<string, AllPendingApproval[]>()
    for (const a of approvals) {
      const list = map.get(a.session_id)
      if (list) list.push(a)
      else map.set(a.session_id, [a])
    }
    return map
  }, [approvals])

  // Close the popover on any click elsewhere (including scrolling/re-render churn from
  // the poll) rather than wiring per-row outside-click refs.
  useEffect(() => {
    if (!openApprovalsFor) return
    const close = () => setOpenApprovalsFor(null)
    document.addEventListener('click', close)
    return () => document.removeEventListener('click', close)
  }, [openApprovalsFor])

  function openWorkflow(id: string) {
    if (editingId) return
    loadSession(id)
    setActiveView('orchestrator')
  }

  function startEdit(e: React.MouseEvent, s: SessionSummary) {
    e.stopPropagation()
    setEditingId(s.id)
    setDraft(s.name ?? '')
  }

  async function commitEdit(id: string) {
    const name = draft
    setEditingId(null)
    // Optimistic: reflect immediately, then persist.
    setRows((prev) => prev.map((r) => (r.id === id ? { ...r, name: name.trim() || null } : r)))
    try {
      await renameSession(id, name)
    } catch (err) {
      console.error('[WorkflowsPage] rename failed', err)
      refresh()   // roll back to server truth
    }
  }

  return (
    <div className="flex flex-1 flex-col overflow-hidden bg-[var(--bg-base)]">
      {/* Header */}
      <div className="px-6 py-4 border-b border-[var(--border)] flex items-center gap-3 flex-shrink-0">
        <button
          onClick={() => setActiveView('orchestrator')}
          title="Back to Orchestrator"
          aria-label="Back to Orchestrator"
          className="w-9 h-9 rounded-xl border border-[var(--border-a)] bg-[var(--bg-raised)] text-slate-400 hover:bg-[var(--bg-hover)] hover:text-slate-200 flex items-center justify-center flex-shrink-0 transition-colors"
        >
          <ArrowLeft size={16} />
        </button>
        <div className="w-9 h-9 rounded-xl bg-blue-500/15 border border-blue-500/30 flex items-center justify-center flex-shrink-0">
          <WorkflowIcon size={17} className="text-blue-400" />
        </div>
        <div className="min-w-0">
          <div className="text-lg font-bold text-slate-100 leading-tight">Workflows</div>
          <div className="text-xs text-slate-500">Every mission you've run — open one to watch its canvas</div>
        </div>

        <div className="ml-auto flex items-center gap-3 flex-shrink-0">
          <span className="flex items-center gap-1.5 text-[10px] font-semibold text-emerald-400">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
            Live
          </span>
          <span className="text-[11px] font-bold px-2.5 py-1 rounded-full bg-[var(--bg-raised)] border border-[var(--border-a)] text-slate-400">
            {rows.length} workflow{rows.length === 1 ? '' : 's'}
          </span>
        </div>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-auto px-6 py-4">
        <div className="rounded-2xl border border-[var(--border-a)] bg-[var(--bg-surface)] overflow-hidden">
          {!loaded ? (
            <div className="flex items-center gap-2 text-slate-500 text-sm py-14 justify-center">
              <Loader2 size={16} className="animate-spin" /> Loading workflows…
            </div>
          ) : rows.length === 0 ? (
            <div className="flex flex-col items-center justify-center text-center gap-3 py-16 text-slate-600">
              <div className="w-14 h-14 rounded-2xl bg-[var(--bg-raised)] border border-[var(--border-a)] flex items-center justify-center">
                <Zap size={22} className="text-slate-500" />
              </div>
              <div className="text-sm font-semibold text-slate-400">No workflows yet</div>
              <div className="text-xs text-slate-600 max-w-xs">
                Run a mission from the Orchestrator — it'll show up here and you can reopen it anytime.
              </div>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-sm">
                <thead className="bg-[var(--bg-raised)]">
                  <tr className="text-[10px] font-bold text-slate-500 uppercase tracking-wider">
                    <th className="text-left px-4 py-3">Name</th>
                    <th className="text-left px-4 py-3">Goal</th>
                    <th className="text-left px-4 py-3 whitespace-nowrap">Status</th>
                    <th className="text-left px-4 py-3 whitespace-nowrap">Started</th>
                    <th className="text-left px-4 py-3 whitespace-nowrap">Duration</th>
                    <th className="text-right px-4 py-3 whitespace-nowrap">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--border)]">
                  {rows.map((s) => {
                    const st = pausedIds.has(s.id) ? PAUSED_META : (STATUS_META[s.status] ?? STATUS_META.pending)
                    const StatusIcon = st.Icon
                    const isEditing = editingId === s.id
                    return (
                      <tr
                        key={s.id}
                        onClick={() => openWorkflow(s.id)}
                        className="hover:bg-[var(--bg-raised)] cursor-pointer transition-colors"
                      >
                        {/* Name (editable) — left accent bar shows the row's status at a glance */}
                        <td className="px-4 py-3 align-top" style={{ boxShadow: `inset 3px 0 0 ${st.color}` }}>
                          {isEditing ? (
                            <div className="flex items-center gap-1.5" onClick={(e) => e.stopPropagation()}>
                              <input
                                value={draft}
                                autoFocus
                                onChange={(e) => setDraft(e.target.value)}
                                onKeyDown={(e) => {
                                  if (e.key === 'Enter') commitEdit(s.id)
                                  if (e.key === 'Escape') setEditingId(null)
                                }}
                                placeholder="New Workflow"
                                className="w-44 bg-[var(--bg-base)] border border-blue-500/60 rounded-md px-2 py-1 text-xs text-slate-200 focus:outline-none"
                              />
                              <button onClick={() => commitEdit(s.id)} className="text-emerald-400 hover:text-emerald-300" title="Save">
                                <Check size={14} />
                              </button>
                              <button onClick={() => setEditingId(null)} className="text-slate-500 hover:text-slate-300" title="Cancel">
                                <X size={14} />
                              </button>
                            </div>
                          ) : (
                            <div className="flex items-center gap-1.5 group/name">
                              <span className={`font-semibold truncate max-w-[220px] ${s.name?.trim() ? 'text-slate-200' : 'text-slate-500 italic'}`}>
                                {displayName(s)}
                              </span>
                              <button
                                onClick={(e) => startEdit(e, s)}
                                className="text-slate-600 hover:text-slate-300 opacity-0 group-hover/name:opacity-100 transition-opacity flex-shrink-0"
                                title="Rename"
                              >
                                <Pencil size={12} />
                              </button>
                            </div>
                          )}
                        </td>

                        {/* Goal — clamped to 3 lines. Forced via inline style rather than
                            relying solely on the `line-clamp-3` utility class, which wasn't
                            actually capping the line count in practice. */}
                        <td className="px-4 py-3 align-top">
                          <span
                            className="text-slate-400 leading-snug max-w-md block"
                            style={{
                              display: '-webkit-box',
                              WebkitLineClamp: 3,
                              WebkitBoxOrient: 'vertical',
                              overflow: 'hidden',
                            }}
                            title={s.goal}
                          >
                            {s.goal}
                          </span>
                        </td>

                        {/* Status — a bell badge shows up alongside it when this session has
                            open approvals, so you can see what a run is stuck on without
                            leaving the table. */}
                        <td className="px-4 py-3 align-top whitespace-nowrap">
                          <div className="flex items-center gap-1.5">
                            <span
                              className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-semibold"
                              style={{ background: st.color + '1a', color: st.color, border: `1px solid ${st.color}45` }}
                            >
                              <StatusIcon size={12} className={st.pulse ? 'animate-spin' : ''} />
                              {st.label}
                            </span>
                            {(() => {
                              const pending = approvalsBySession.get(s.id)
                              if (!pending?.length) return null
                              return (
                                <button
                                  onClick={(e) => {
                                    e.stopPropagation()
                                    const r = e.currentTarget.getBoundingClientRect()
                                    setOpenApprovalsFor((prev) =>
                                      prev?.sessionId === s.id ? null : { sessionId: s.id, x: r.left, y: r.bottom + 4 }
                                    )
                                  }}
                                  title={`${pending.length} pending approval${pending.length === 1 ? '' : 's'}`}
                                  className="relative inline-flex items-center justify-center w-6 h-6 rounded-full border border-amber-500/40 bg-amber-500/10 text-amber-400 hover:bg-amber-500/20 transition-colors flex-shrink-0"
                                >
                                  <Bell size={11} />
                                  <span className="absolute -top-1 -right-1 min-w-[14px] h-[14px] px-0.5 rounded-full bg-amber-500 text-black text-[9px] font-bold flex items-center justify-center leading-none">
                                    {pending.length}
                                  </span>
                                </button>
                              )
                            })()}
                          </div>
                        </td>

                        {/* Started */}
                        <td className="px-4 py-3 align-top whitespace-nowrap">
                          <span className="text-slate-400 text-xs" title={new Date(s.created_at).toLocaleString()}>
                            {formatStarted(s.created_at)}
                          </span>
                        </td>

                        {/* Duration */}
                        <td className="px-4 py-3 align-top whitespace-nowrap">
                          <span className="text-slate-400 text-xs font-mono">{formatDuration(s)}</span>
                        </td>

                        {/* Action */}
                        <td className="px-4 py-3 align-top whitespace-nowrap text-right">
                          <button
                            onClick={(e) => { e.stopPropagation(); openWorkflow(s.id) }}
                            title="View workflow"
                            aria-label="View workflow"
                            className="inline-flex items-center justify-center w-7 h-7 rounded-lg border border-blue-500/40 bg-blue-500/10 text-blue-300 hover:bg-blue-500/20 hover:border-blue-500/60 transition-colors"
                          >
                            <ArrowUpRight size={14} />
                          </button>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      {/* Pending-approvals popover — fixed positioning so the table's own scroll
          containers (overflow-x-auto / overflow-auto) can't clip it. */}
      {openApprovalsFor && (
        <div
          onClick={(e) => e.stopPropagation()}
          className="fixed z-[100] w-72 bg-[var(--bg-surface)] border border-[var(--border-a)] rounded-xl shadow-2xl p-3"
          style={{ top: openApprovalsFor.y, left: openApprovalsFor.x }}
        >
          <div className="text-[10px] font-bold text-slate-500 uppercase tracking-wider mb-2">
            Pending Approvals
          </div>
          <div className="flex flex-col gap-2 max-h-64 overflow-y-auto">
            {(approvalsBySession.get(openApprovalsFor.sessionId) ?? []).map((a) => (
              <div key={a.id} className="text-xs border-b border-[var(--border)] pb-2 last:border-0 last:pb-0">
                <div className="text-slate-200 font-semibold capitalize">{a.action_type.replace(/_/g, ' ')}</div>
                <div className="text-slate-500">
                  {a.agent_id.replace(/_/g, ' ')} · waiting {formatWaitTime(a.created_at)}
                  {a.escalation_level > 0 && ` · escalated ×${a.escalation_level}`}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
