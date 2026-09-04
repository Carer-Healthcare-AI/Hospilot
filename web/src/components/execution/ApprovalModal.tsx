import { useEffect, useState } from 'react'
import { ShieldAlert, CheckCircle2, XCircle, Clock, Layers, ChevronRight, Minus } from 'lucide-react'
import { useStore } from '../../store'
import { AGENT_MAP } from '../../data/agents'
import { fetchPendingApprovals } from '../../services/api'

function RichText({ text }: { text: string }) {
  const paragraphs = (text ?? '').split('\n\n')
  return (
    <div className="space-y-2">
      {paragraphs.map((para, pi) => {
        const lines = para.split('\n')
        return (
          <div key={pi} className="space-y-0.5">
            {lines.map((line, li) => {
              const isBullet = line.startsWith('•')
              const parts = line.replace(/^\•\s*/, '').split(/\*\*(.+?)\*\*/g)
              return (
                <p key={li} className={isBullet ? 'pl-3 flex gap-1.5' : ''}>
                  {isBullet && <span className="text-teal-400 flex-shrink-0">•</span>}
                  <span>
                    {parts.map((part, idx) =>
                      idx % 2 === 1
                        ? <strong key={idx} className="text-slate-100 font-semibold">{part}</strong>
                        : <span key={idx}>{part}</span>
                    )}
                  </span>
                </p>
              )
            })}
          </div>
        )
      })}
    </div>
  )
}

export function ApprovalModal() {
  const pendingApprovals = useStore((s) => s.pendingApprovals)
  const approveGate = useStore((s) => s.approveGate)
  const rejectGate = useStore((s) => s.rejectGate)
  const focusApproval = useStore((s) => s.focusApproval)
  const approvalMinimized = useStore((s) => s.approvalMinimized)
  const setApprovalMinimized = useStore((s) => s.setApprovalMinimized)
  const dismissExternalApproval = useStore((s) => s.dismissExternalApproval)
  const currentUser = useStore((s) => s.currentUser)
  const isApprover = currentUser?.role === 'approver'
  const sessionId = useStore((s) => s.sessionId)
  const nodes = useStore((s) => s.nodes)

  // "+N more" stack expander — lets the reviewer see every queued approval and jump
  // to any of them (focusApproval brings it to the front, which approve/reject act on).
  const [stackOpen, setStackOpen] = useState(false)

  const current = pendingApprovals[0]

  // Resolve the display agent (emoji + label + color) for any gate by its node id.
  function agentFor(agentId: string | undefined) {
    const resolved = (nodes.find((n) => n.id === agentId)?.data as { agentId: string })?.agentId ?? agentId
    return resolved ? AGENT_MAP[resolved] : undefined
  }

  // Poll for external decisions (e.g. doctor approved on /approvals page)
  useEffect(() => {
    if (!current?.approvalId || !sessionId) return
    const approvalId = current.approvalId
    const id = setInterval(async () => {
      const pending = await fetchPendingApprovals(sessionId)
      if (!pending.some((a) => a.id === approvalId)) {
        dismissExternalApproval(approvalId)
      }
    }, 5000)
    return () => clearInterval(id)
  }, [current?.approvalId, sessionId, dismissExternalApproval])

  if (!current) return null
  // Collapsed to the canvas "reopen" pill — the parked session keeps running server-side
  // while the user works on something else. PipelineCanvas renders the pill that restores it.
  if (approvalMinimized) return null

  const queued = pendingApprovals.length - 1
  const agent = agentFor(current.agentId)

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm">
      <div className="bg-[var(--bg-surface)] border border-amber-500/40 rounded-2xl p-6 max-w-md w-full mx-4 shadow-2xl max-h-[85vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center gap-3 mb-4">
          <div className="w-10 h-10 rounded-xl bg-amber-500/10 border border-amber-500/30 flex items-center justify-center">
            <ShieldAlert size={20} className="text-amber-400" />
          </div>
          <div className="flex-1">
            <div className="text-xs text-amber-400 font-semibold uppercase tracking-wider">Human Approval Required</div>
            <div className="text-sm font-bold text-slate-100 mt-0.5">{current.title}</div>
          </div>
          {queued > 0 && (
            <div className="relative flex-shrink-0">
              <button
                onClick={() => setStackOpen((v) => !v)}
                title="View all pending approvals"
                className={`flex items-center gap-1 text-xs font-semibold px-2 py-1 rounded-full border transition-colors ${
                  stackOpen
                    ? 'bg-amber-500/30 text-amber-300 border-amber-500/50'
                    : 'bg-amber-500/20 text-amber-400 border-amber-500/30 hover:bg-amber-500/30'
                }`}
              >
                <Layers size={11} />
                +{queued} more
              </button>

              {/* Stack list — every pending approval; click one to bring it to the front. */}
              {stackOpen && (
                <div className="absolute right-0 top-full mt-1.5 w-64 max-h-64 overflow-y-auto rounded-xl border border-amber-500/30 bg-[var(--bg-surface)] shadow-2xl z-10 p-1.5">
                  <div className="text-[10px] font-bold text-slate-500 uppercase tracking-wider px-2 py-1">
                    {pendingApprovals.length} pending
                  </div>
                  {pendingApprovals.map((gate, i) => {
                    const a = agentFor(gate.agentId)
                    const isCurrent = i === 0
                    return (
                      <button
                        key={gate.approvalId ?? `${gate.agentId}-${i}`}
                        onClick={() => {
                          if (gate.approvalId) focusApproval(gate.approvalId)
                          setStackOpen(false)
                        }}
                        disabled={isCurrent || !gate.approvalId}
                        className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-left transition-colors ${
                          isCurrent ? 'bg-amber-500/10 cursor-default' : 'hover:bg-[var(--bg-hover)]'
                        }`}
                      >
                        {a && <span className="text-sm flex-shrink-0">{a.emoji}</span>}
                        <div className="min-w-0 flex-1">
                          <div className="text-xs font-medium text-slate-200 truncate leading-tight">{gate.title}</div>
                          {a && <div className="text-[10px] leading-tight truncate" style={{ color: a.color }}>{a.label}</div>}
                        </div>
                        {isCurrent ? (
                          <span className="text-[9px] font-bold uppercase text-amber-400 flex-shrink-0">Viewing</span>
                        ) : (
                          <ChevronRight size={13} className="text-slate-500 flex-shrink-0" />
                        )}
                      </button>
                    )
                  })}
                </div>
              )}
            </div>
          )}

          {/* Minimize — park the approval as a floating canvas pill so the user can keep
              working (e.g. start another query) while it awaits a decision. */}
          <button
            onClick={() => setApprovalMinimized(true)}
            title="Minimize — keep working while this awaits approval"
            aria-label="Minimize approval"
            className="flex-shrink-0 w-7 h-7 flex items-center justify-center rounded-lg text-slate-500 hover:text-slate-200 hover:bg-[var(--bg-raised)] transition-colors"
          >
            <Minus size={16} />
          </button>
        </div>

        {/* Agent badge */}
        {agent && (
          <div className="flex items-center gap-2 mb-3 px-3 py-1.5 rounded-lg bg-[var(--bg-raised)] border border-[var(--border-a)] w-fit">
            <span className="text-sm">{agent.emoji}</span>
            <span className="text-xs font-medium" style={{ color: agent.color }}>{agent.label}</span>
          </div>
        )}

        {/* Recommendation */}
        <div className="bg-[var(--bg-base)] border border-[var(--border)] rounded-xl p-4 mb-5 flex-1 min-h-0 overflow-y-auto">
          <div className="text-[10px] font-semibold text-slate-500 uppercase tracking-wider mb-2">AI Recommendation</div>
          <div className="text-sm text-slate-300 leading-relaxed">
            <RichText text={current.recommendation} />
          </div>
        </div>

        {/* Actions */}
        {isApprover ? (
          <div className="flex gap-3 flex-shrink-0">
            <button
              onClick={approveGate}
              className="flex-1 flex items-center justify-center gap-2 py-2.5 rounded-xl bg-teal-600 hover:bg-teal-500 text-white text-sm font-semibold transition-colors"
            >
              <CheckCircle2 size={16} />
              {current.action}
            </button>
            <button
              onClick={rejectGate}
              className="flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl bg-[var(--bg-raised)] hover:bg-[var(--bg-hover)] border border-[var(--border-a)] text-slate-400 text-sm font-semibold transition-colors"
            >
              <XCircle size={16} />
              Reject
            </button>
          </div>
        ) : (
          <div className="flex items-center gap-3 py-3 px-4 rounded-xl bg-amber-500/10 border border-amber-500/30 flex-shrink-0">
            <Clock size={15} className="text-amber-400 flex-shrink-0 animate-pulse" />
            <div>
              <div className="text-xs font-semibold text-amber-400">Waiting for approver</div>
              <div className="text-[11px] text-slate-500 mt-0.5">An approver must review this before execution continues.</div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
