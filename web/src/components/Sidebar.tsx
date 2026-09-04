import { useEffect, useRef, useState } from 'react'
import {
  AlertTriangle,
  Loader2,
  Play,
  Plus,
  RotateCcw,
  Send,
  Zap,
} from 'lucide-react'
import { useStore } from '../store'
import type { SidebarTurn as Turn, SidebarAgentChip as AgentChip } from '../store'
import { AGENT_MAP } from '../data/agents'
import type { BackendPipeline } from '../services/api'

// Resolve a node's chip info with a backend fallback -- mirrors AgentNode.tsx's own
// identity resolution. The static AGENT_MAP catalog can lag behind agents the backend
// already knows about (e.g. patient_verification_agent has no static entry); without
// this fallback such an agent is silently dropped from every chip list instead of
// showing up with a generic icon/color.
function resolveAgentChip(node: { id: string; data: unknown }, backendPipeline: BackendPipeline | null): AgentChip | null {
  const agentId = (node.data as { agentId: string }).agentId
  const staticAgent = AGENT_MAP[agentId]
  const backendAgent = backendPipeline?.agents.find((a) => a.id === node.id) ?? null
  if (!staticAgent && !backendAgent) return null
  return {
    id: agentId,
    label: staticAgent?.label ?? backendAgent?.label ?? agentId,
    emoji: staticAgent?.emoji ?? '💳',
    color: staticAgent?.color ?? backendAgent?.color ?? '#94a3b8',
  }
}

// ── Component ─────────────────────────────────────────────────────────────────

export function Sidebar() {
  const promptText       = useStore((s) => s.promptText)
  const constraintText   = useStore((s) => s.constraintText)
  const setPrompt        = useStore((s) => s.setPrompt)
  const setConstraint    = useStore((s) => s.setConstraint)
  const executionStatus  = useStore((s) => s.executionStatus)
  const pipelineGenerated = useStore((s) => s.pipelineGenerated)
  const pipelineLoading  = useStore((s) => s.pipelineLoading)
  const planningStage    = useStore((s) => s.planningStage)
  const generatePipeline = useStore((s) => s.generatePipeline)
  const resetExecution   = useStore((s) => s.resetExecution)
  const reorchestrateWithFeedback = useStore((s) => s.reorchestrateWithFeedback)
  const reorchestrateLoading = useStore((s) => s.reorchestrateLoading)
  const nodes            = useStore((s) => s.nodes)
  const backendPipeline  = useStore((s) => s.backendPipeline)
  const sessionId        = useStore((s) => s.sessionId)
  const sessionLoadKey   = useStore((s) => s.sessionLoadKey)

  const isRunning   = executionStatus === 'running' || executionStatus === 'waiting_approval'
  const isSubmitted = executionStatus === 'submitted'

  // ── Conversation thread state ─────────────────────────────────────────────
  // Lives in the store (not local useState) -- App.tsx unmounts <Sidebar/> whenever
  // the sub-agent drill-down view is open, and local state would reset to [] on
  // remount, dropping `inConversation` to false and silently swapping the "Modify
  // pipeline" follow-up view back to the blank Mission Brief setup form.
  const turns = useStore((s) => s.sidebarTurns)
  const setTurns = useStore((s) => s.setSidebarTurns)
  const [followUpText, setFollowUpText] = useState('')

  // Tracks whether we are waiting for the next pipelineLoading→false to add a system turn
  const pendingSystemTurn = useRef<{ isUpdate: boolean } | null>(null)
  const prevLoading = useRef(false)
  const threadEndRef = useRef<HTMLDivElement>(null)

  // Reset thread when pipeline is reset externally (e.g. new workflow).
  useEffect(() => {
    if (!pipelineGenerated && !pipelineLoading && turns.length > 0) {
      setTurns([])
      setFollowUpText('')
    }
  }, [pipelineGenerated, pipelineLoading]) // eslint-disable-line

  // Mirror the thread to localStorage per-session. The backend only persists the
  // original goal (session.goal) -- follow-up "Modify pipeline" prompts live only in
  // this client-side turns array, so without this a reload/reconnect would silently
  // lose every modify-prompt and fall back to a bare "Mission: {original goal}" card.
  // Restored below by the sessionLoadKey effect.
  useEffect(() => {
    if (!sessionId || turns.length === 0) return
    try {
      localStorage.setItem(`hospilot_turns_${sessionId}`, JSON.stringify(turns))
    } catch {
      // storage full/unavailable -- just means this reload won't restore the thread
    }
  }, [turns, sessionId])

  // Reset thread when a previous session is loaded from the drawer
  // sessionLoadKey increments on every successful loadSession(), which pipelineGenerated alone can't detect
  useEffect(() => {
    if (sessionLoadKey === 0) return

    // Prefer the locally-persisted thread (full modify-prompt history) over the
    // bare single-turn reconstruction below.
    const stored = sessionId ? localStorage.getItem(`hospilot_turns_${sessionId}`) : null
    if (stored) {
      try {
        const parsed = JSON.parse(stored) as Turn[]
        if (Array.isArray(parsed) && parsed.length > 0) {
          setTurns(parsed)
          setFollowUpText('')
          return
        }
      } catch {
        // corrupt entry -- fall through to reconstruction
      }
    }

    if (pipelineGenerated && !isSubmitted) {
      // An observer client picked up a session with a plan already generated elsewhere
      // (e.g. the widget started it, this iframe just loaded it). Seed a turn so the
      // conversation view — and its "Modify pipeline" follow-up box — renders instead
      // of falling back to the blank Mission Brief setup form.
      const seen = new Set<string>()
      const agents: AgentChip[] = nodes
        .map((n) => resolveAgentChip(n, backendPipeline))
        .filter((a): a is AgentChip => a !== null && !seen.has(a.id) && (seen.add(a.id), true))
      setTurns([
        { id: 'loaded-user', role: 'user', text: promptText, constraint: constraintText || undefined },
        { id: 'loaded-sys', role: 'system', text: `Workflow ready — ${agents.length} agent${agents.length === 1 ? '' : 's'} coordinated.`, agents },
      ])
    } else {
      setTurns([])
    }
    setFollowUpText('')
  }, [sessionLoadKey]) // eslint-disable-line

  // Add system turn when loading finishes
  useEffect(() => {
    if (prevLoading.current && !pipelineLoading && pipelineGenerated && pendingSystemTurn.current) {
      const isUpdate = pendingSystemTurn.current.isUpdate
      pendingSystemTurn.current = null

      const seen = new Set<string>()
      const agents: AgentChip[] = nodes
        .map((n) => resolveAgentChip(n, backendPipeline))
        .filter((a): a is AgentChip => a !== null && !seen.has(a.id) && (seen.add(a.id), true))

      setTurns((prev) => [
        ...prev,
        {
          id: `sys-${Date.now()}`,
          role: 'system',
          text: isUpdate ? 'Workflow updated.' : `Workflow ready — ${agents.length} agent${agents.length === 1 ? '' : 's'} coordinated.`,
          agents,
          isUpdate,
        },
      ])
    }
    prevLoading.current = pipelineLoading
  }, [pipelineLoading, pipelineGenerated, nodes])

  // Scroll thread to bottom on new turn
  useEffect(() => {
    threadEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [turns])

  function handleRunMission() {
    if (!promptText.trim()) return
    setTurns([{ id: `u-${Date.now()}`, role: 'user', text: promptText, constraint: constraintText || undefined }])
    pendingSystemTurn.current = { isUpdate: false }
    generatePipeline()
  }

  async function handleFollowUp() {
    const text = followUpText.trim()
    if (!text || isRunning || reorchestrateLoading) return
    setFollowUpText('')
    setTurns((prev) => [...prev, { id: `u-${Date.now()}`, role: 'user', text, constraint: constraintText || undefined }])

    await reorchestrateWithFeedback(text)

    const seen = new Set<string>()
    const { nodes: updatedNodes, backendPipeline: updatedPipeline } = useStore.getState()
    const agents: AgentChip[] = updatedNodes
      .map((n) => resolveAgentChip(n, updatedPipeline))
      .filter((a): a is AgentChip => a !== null && !seen.has(a.id) && (seen.add(a.id), true))
    setTurns((prev) => [...prev, { id: `sys-${Date.now()}`, role: 'system', text: 'Workflow updated.', agents, isUpdate: true }])
  }

  function handleNewWorkflow() {
    resetExecution()
    setTurns([])
    setFollowUpText('')
  }

  // Are we in conversation mode (thread has started)?
  const inConversation = turns.length > 0 || pipelineLoading

  return (
    <aside className="w-64 xl:w-72 2xl:w-80 flex-shrink-0 bg-[var(--bg-base)] border-r border-[var(--border)] flex flex-col overflow-hidden">

      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <div className="px-4 pt-4 pb-2 flex items-center justify-between flex-shrink-0">
        <div className="flex items-center gap-2">
          <div className="text-xs font-bold text-slate-500 uppercase tracking-widest">Mission Brief</div>
        </div>
        <button
          onClick={handleNewWorkflow}
          className="flex items-center gap-1 px-2 py-1 rounded-lg text-xs text-slate-500 hover:text-slate-300 hover:bg-[var(--bg-surface)] transition-colors"
          title="New workflow"
        >
          <Plus size={10} />
          New
        </button>
      </div>

      {/* ── SETUP FORM — before first run ────────────────────────────────── */}
      {!inConversation && !isSubmitted && (
        <div className="flex-1 overflow-y-auto">
          <div className="px-4 py-3 flex flex-col gap-1.5">
            <label className="text-xs font-semibold text-slate-500 uppercase tracking-wider">What do you need?</label>
            <textarea
              value={promptText}
              onChange={(e) => setPrompt(e.target.value)}
              rows={5}
              className="w-full bg-[var(--bg-surface)] border border-[var(--border-a)] rounded-lg px-3 py-2 text-xs text-slate-300 resize-none placeholder-slate-700 focus:outline-none focus:border-blue-500 leading-relaxed"
              placeholder="Describe the situation and what you need the AI to coordinate..."
            />
          </div>

          <div className="px-4 pb-3 flex flex-col gap-1.5">
            <label className="flex items-center gap-1.5 text-xs font-semibold text-slate-500 uppercase tracking-wider">
              <AlertTriangle size={10} className="text-amber-500" />
              Constraints
            </label>
            <textarea
              value={constraintText}
              onChange={(e) => setConstraint(e.target.value)}
              rows={3}
              className="w-full bg-[var(--bg-surface)] border border-amber-900/40 rounded-lg px-3 py-2 text-xs text-slate-300 resize-none placeholder-slate-700 focus:outline-none focus:border-amber-500 leading-relaxed"
              placeholder="Safety rules and approval requirements..."
            />
          </div>
        </div>
      )}

      {/* ── LOADED SESSION VIEW — previous session restored, no live thread ── */}
      {!inConversation && isSubmitted && (
        <div className="flex-1 overflow-y-auto px-3 py-3 flex flex-col gap-3">
          {/* Mission goal read-only card */}
          <div className="w-full bg-blue-600/10 border border-blue-600/25 rounded-xl px-3 py-2.5">
            <div className="text-[11px] font-bold text-blue-400/70 uppercase tracking-widest mb-1">Mission</div>
            <p className="text-xs text-blue-100 leading-relaxed">{promptText}</p>
          </div>

          {/* Agent chips */}
          {(() => {
            const seen = new Set<string>()
            const chips = nodes
              .map((n) => resolveAgentChip(n, backendPipeline))
              .filter((a): a is AgentChip => a !== null && !seen.has(a.id) && (seen.add(a.id), true))
            return chips.length > 0 ? (
              <div className="w-full bg-[var(--bg-surface)] border border-[var(--border-a)] rounded-xl px-3 py-2.5">
                <div className="text-[11px] font-bold text-slate-500 uppercase tracking-widest mb-2">Agents Coordinated</div>
                <div className="flex flex-wrap gap-1">
                  {chips.map((a) => (
                    <span
                      key={a.id}
                      className="text-[11px] px-1.5 py-0.5 rounded font-medium"
                      style={{ background: a.color + '20', color: a.color, border: `1px solid ${a.color}30` }}
                    >
                      {a.emoji} {a.label}
                    </span>
                  ))}
                </div>
              </div>
            ) : null
          })()}
        </div>
      )}

      {/* ── CONVERSATION THREAD ───────────────────────────────────────────── */}
      {inConversation && (
        <div className="flex-1 overflow-y-auto px-3 py-3 flex flex-col gap-3">
          {turns.map((turn) => (
            <div key={turn.id} className="flex flex-col gap-1.5">
              {turn.role === 'user' ? (
                /* User card — prompt + optional constraints */
                <div className="w-full bg-blue-600/10 border border-blue-600/25 rounded-xl px-3 py-2.5">
                  <div className="text-[11px] font-bold text-blue-400/70 uppercase tracking-widest mb-1">Prompt</div>
                  <p className="text-xs text-blue-100 leading-relaxed">{turn.text}</p>
                  {turn.constraint && (
                    <div className="mt-2 pt-2 border-t border-blue-600/15">
                      <div className="flex items-center gap-1 text-[11px] font-bold text-amber-500/70 uppercase tracking-widest mb-1">
                        <AlertTriangle size={8} />
                        Constraints
                      </div>
                      <p className="text-xs text-slate-400 leading-relaxed">{turn.constraint}</p>
                    </div>
                  )}
                </div>
              ) : (
                /* System card */
                <div className="w-full bg-[var(--bg-surface)] border border-[var(--border-a)] rounded-xl px-3 py-2.5">
                  <div className="flex items-center gap-1.5 mb-1.5">
                    <div className="w-3.5 h-3.5 rounded bg-blue-600 flex items-center justify-center flex-shrink-0">
                      <Zap size={8} className="text-white" />
                    </div>
                    <span className="text-xs font-semibold text-slate-400">Hospilot</span>
                  </div>
                  <p className="text-xs text-slate-300 leading-relaxed mb-2">{turn.text}</p>
                  {turn.agents && turn.agents.length > 0 && (
                    <div className="flex flex-wrap gap-1">
                      {turn.agents.map((a) => (
                        <span
                          key={a.id}
                          className="text-[11px] px-1.5 py-0.5 rounded font-medium"
                          style={{ background: a.color + '20', color: a.color, border: `1px solid ${a.color}30` }}
                        >
                          {a.emoji} {a.label}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          ))}

          {/* Loading shimmer */}
          {pipelineLoading && (
            <div className="w-full bg-[var(--bg-surface)] border border-[var(--border-a)] rounded-xl px-3 py-2.5 flex items-center gap-2">
              <div className="w-3.5 h-3.5 rounded bg-blue-600 flex items-center justify-center flex-shrink-0">
                <Zap size={8} className="text-white" />
              </div>
              <span className="text-xs font-semibold text-slate-400">Hospilot</span>
              <div className="flex gap-1 ml-1">
                {[0, 1, 2].map((i) => (
                  <span key={i} className="w-1 h-1 rounded-full bg-slate-500" style={{ animation: `pulse 1s ${i * 0.2}s infinite` }} />
                ))}
              </div>
            </div>
          )}

          <div ref={threadEndRef} />
        </div>
      )}

      {/* ── BOTTOM AREA ──────────────────────────────────────────────────── */}
      <div className="flex-shrink-0 border-t border-[var(--border)]">

        {/* Setup mode — Run Mission */}
        {!inConversation && !isSubmitted && (
          <div className="px-4 py-4">
            {pipelineLoading ? (
              <div className="w-full flex items-center justify-center gap-2 py-3 rounded-xl bg-blue-900/40 border border-blue-700/50 text-blue-300 text-sm font-semibold cursor-not-allowed">
                <Loader2 size={15} className="animate-spin" />
                {planningStage ?? 'Coordinating agents…'}
              </div>
            ) : (
              <button
                onClick={handleRunMission}
                disabled={!promptText.trim()}
                className="w-full flex items-center justify-center gap-2 py-3 rounded-xl bg-blue-600 hover:bg-blue-500 disabled:opacity-40 disabled:cursor-not-allowed text-white text-sm font-semibold transition-colors shadow-lg shadow-blue-900/40"
              >
                <Play size={15} />
                Run Mission
              </button>
            )}
          </div>
        )}

        {/* Conversation mode — dual input (prompt + constraints). */}
        {inConversation && (
          <div className="flex flex-col">
            {!isSubmitted && (
              <div className="px-3 pt-3 pb-2 flex flex-col gap-2">
                {/* Prompt textarea */}
                <div className="flex flex-col gap-1">
                  <label className="text-[11px] font-semibold text-slate-600 uppercase tracking-wider">Modify pipeline</label>
                  <textarea
                    value={followUpText}
                    onChange={(e) => setFollowUpText(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleFollowUp() }
                    }}
                    disabled={isRunning || pipelineLoading}
                    placeholder="Describe changes to make…"
                    rows={3}
                    className="w-full bg-[var(--bg-surface)] border border-[var(--border-a)] rounded-xl px-3 py-2 text-xs text-slate-300 resize-none placeholder-slate-700 focus:outline-none focus:border-blue-500 disabled:opacity-50 transition-colors leading-relaxed"
                  />
                </div>

                {/* Constraints textarea */}
                <div className="flex flex-col gap-1">
                  <label className="flex items-center gap-1 text-[11px] font-semibold text-slate-600 uppercase tracking-wider">
                    <AlertTriangle size={8} className="text-amber-500" />
                    Constraints
                  </label>
                  <textarea
                    value={constraintText}
                    onChange={(e) => setConstraint(e.target.value)}
                    disabled={isRunning || pipelineLoading}
                    rows={2}
                    className="w-full bg-[var(--bg-surface)] border border-amber-900/40 rounded-xl px-3 py-2 text-xs text-slate-300 resize-none placeholder-slate-700 focus:outline-none focus:border-amber-500 disabled:opacity-50 transition-colors leading-relaxed"
                    placeholder="Safety rules and approval requirements…"
                  />
                </div>

                {/* Send button */}
                <button
                  onClick={handleFollowUp}
                  disabled={!followUpText.trim() || isRunning || pipelineLoading || reorchestrateLoading}
                  className="w-full flex items-center justify-center gap-2 py-2 rounded-xl bg-blue-600 hover:bg-blue-500 disabled:opacity-30 disabled:cursor-not-allowed transition-colors text-white text-xs font-semibold"
                >
                  <Send size={12} />
                  Send
                </button>

                {reorchestrateLoading && (
                  <div className="flex items-center gap-2 px-1 py-1 text-xs text-blue-400">
                    <Loader2 size={11} className="animate-spin flex-shrink-0" />
                    <span>Re-orchestrating pipeline…</span>
                  </div>
                )}
              </div>
            )}

            {/* Re-orchestrate / submitted actions */}
            {isSubmitted && (
              <div className="px-3 pb-3 flex flex-col gap-1.5">
                <button onClick={handleNewWorkflow} className="w-full flex items-center justify-center gap-2 py-2 rounded-xl bg-[var(--bg-raised)] hover:bg-[var(--bg-hover)] border border-[var(--border-a)] text-slate-500 text-xs transition-colors">
                  <RotateCcw size={12} />
                  New Mission
                </button>
              </div>
            )}
          </div>
        )}

        {/* Loaded session — New Mission only (no live conversation thread) */}
        {!inConversation && isSubmitted && (
          <div className="px-3 pb-3 pt-3 flex flex-col gap-1.5">
            <button
              onClick={handleNewWorkflow}
              className="w-full flex items-center justify-center gap-2 py-2 rounded-xl bg-[var(--bg-raised)] hover:bg-[var(--bg-hover)] border border-[var(--border-a)] text-slate-500 text-xs transition-colors"
            >
              <RotateCcw size={12} />
              New Mission
            </button>
          </div>
        )}
      </div>
    </aside>
  )
}
