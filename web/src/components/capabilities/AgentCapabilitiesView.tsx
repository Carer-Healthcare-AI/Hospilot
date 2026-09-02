import { useState, useMemo } from 'react'
import { Search, ChevronDown, ChevronRight, Zap, Loader2, CheckCircle, ArrowLeft } from 'lucide-react'
import { useStore } from '../../store'
import { AGENT_META } from '../../data/capabilities'

// Strip _agent / _prediction suffix to match static meta keys (er, icu, bed, …)
function metaId(id: string) {
  return id.replace(/_prediction_agent$/, '').replace(/_agent$/, '')
}

export function AgentCapabilitiesView() {
  const agentRegistry = useStore((s) => s.agentRegistry)
  const agentRegistryLoaded = useStore((s) => s.agentRegistryLoaded)
  const setActiveView = useStore((s) => s.setActiveView)

  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [expandedSubs, setExpandedSubs] = useState<Set<string>>(new Set())

  const effectiveId = selectedAgentId ?? agentRegistry[0]?.id ?? null
  const selectedAgent = agentRegistry.find((a) => a.id === effectiveId) ?? null
  const meta = selectedAgent ? AGENT_META[metaId(selectedAgent.id)] : null

  const allSubagents = selectedAgent?.subagents ?? []

  const totalTasks = useMemo(
    () => allSubagents.reduce((sum, sa) => sum + sa.tasks.length, 0),
    [allSubagents],
  )

  // Real per-agent profile derived from the registry — recomputes on every agent
  // switch, so the right panel is genuine data (not the old static insights/coverage).
  const agentProfile = useMemo(() => {
    const outputs = new Set<string>()
    const capabilities = new Set<string>()
    let prefetchCount = 0
    const dist = allSubagents
      .map((sa) => {
        sa.tasks.forEach((t) => (t.outputs ?? []).forEach((o) => o && outputs.add(o)))
        ;(sa.capabilities ?? []).forEach((c) => c && capabilities.add(c))
        if (sa.is_prefetch_eligible) prefetchCount += 1
        return { id: sa.id, label: sa.label, count: sa.tasks.length }
      })
      .sort((a, b) => b.count - a.count)
    const maxCount = dist.reduce((m, d) => Math.max(m, d.count), 0)
    return {
      outputs: [...outputs],
      capabilities: [...capabilities],
      prefetchCount,
      dist,
      maxCount,
    }
  }, [allSubagents])

  function toggleSub(id: string) {
    setExpandedSubs((prev) => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }

  if (!agentRegistryLoaded) {
    return (
      <div className="flex flex-1 items-center justify-center bg-[var(--bg-base)]">
        <div className="flex items-center gap-2 text-slate-500 text-sm">
          <Loader2 size={16} className="animate-spin" />
          Loading agent registry…
        </div>
      </div>
    )
  }

  if (!selectedAgent) {
    return (
      <div className="flex flex-1 items-center justify-center bg-[var(--bg-base)]">
        <p className="text-slate-600 text-sm">No agents in registry.</p>
      </div>
    )
  }

  return (
    <div className="flex flex-1 overflow-hidden bg-[var(--bg-base)]">

      {/* ── Left: agent list ───────────────────────────────────────────────── */}
      <div className="w-56 xl:w-64 2xl:w-72 flex-shrink-0 border-r border-[var(--border)] flex flex-col overflow-hidden">
        <div className="px-4 pt-4 pb-3 border-b border-[var(--border)]">
          <button
            onClick={() => setActiveView('orchestrator')}
            className="flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-300 transition-colors mb-3"
          >
            <ArrowLeft size={12} />
            Back to Orchestrator
          </button>
          <div className="flex items-center gap-1.5 mb-2.5">
            <CheckCircle size={14} className="text-blue-400 flex-shrink-0" />
            <span className="text-sm font-bold text-slate-100">Agent Capabilities</span>
          </div>
          <div className="text-[10px] font-bold text-slate-500 uppercase tracking-widest mb-2">
            Agents ({agentRegistry.length})
          </div>
          <div className="flex items-center gap-2 bg-[var(--bg-surface)] border border-[var(--border-a)] rounded-lg px-3 py-1.5">
            <Search size={12} className="text-slate-600" />
            <input
              placeholder="Search agents..."
              className="flex-1 bg-transparent text-xs text-slate-300 placeholder-slate-700 focus:outline-none"
            />
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-2 py-2">
          {agentRegistry.map((agent) => {
            const m = AGENT_META[metaId(agent.id)]
            const subCount = agent.subagents.length
            const taskCount = agent.subagents.reduce((s, sa) => s + sa.tasks.length, 0)
            const isSelected = agent.id === effectiveId
            return (
              <button
                key={agent.id}
                onClick={() => { setSelectedAgentId(agent.id); setSearch('') }}
                className="w-full text-left px-3 py-2.5 rounded-xl mb-1 transition-all"
                style={{
                  background: isSelected
                    ? `linear-gradient(135deg, ${agent.color}35 0%, ${agent.color}14 100%)`
                    : `linear-gradient(135deg, ${agent.color}18 0%, ${agent.color}06 100%)`,
                  boxShadow: isSelected
                    ? `inset 3px 0 0 ${agent.color}, 0 0 0 1px ${agent.color}50`
                    : `inset 3px 0 0 ${agent.color}70`,
                }}
              >
                <div className="flex items-center justify-between mb-1">
                  <div className="flex items-center gap-2 min-w-0">
                    <div
                      className="w-8 h-8 rounded-lg flex items-center justify-center text-base flex-shrink-0"
                      style={{ background: agent.color + '20', border: `1px solid ${agent.color}40` }}
                    >
                      {agent.emoji}
                    </div>
                    <div className="min-w-0">
                      <div className="text-xs font-semibold text-slate-200 truncate">{agent.label}</div>
                      <div className="text-[9px] text-slate-600">{m?.category ?? ''}</div>
                    </div>
                  </div>
                  <span
                    className="text-xs font-bold flex-shrink-0 ml-1"
                    style={{ color: (m?.successRate ?? 90) >= 95 ? '#14b8a6' : (m?.successRate ?? 90) >= 90 ? '#f97316' : '#ef4444' }}
                  >
                    {m?.successRate ?? '—'}%
                  </span>
                </div>
                <div className="flex items-center gap-2 text-[9px] text-slate-600">
                  <span>{subCount} sub-agents</span>
                  <span>·</span>
                  <span>{taskCount} tasks</span>
                </div>
              </button>
            )
          })}
        </div>
      </div>

      {/* ── Center: subagents + tasks ──────────────────────────────────────── */}
      <div className="flex-1 min-w-0 flex flex-col overflow-hidden">

        {/* Agent header */}
        <div
          className="px-6 py-4 border-b border-[var(--border)] flex items-start justify-between flex-shrink-0"
          style={{ borderTop: `3px solid ${selectedAgent.color}`, background: selectedAgent.color + '08' }}
        >
          <div className="flex items-center gap-3 min-w-0">
            <div
              className="w-12 h-12 rounded-xl flex items-center justify-center text-2xl shadow-sm flex-shrink-0"
              style={{ background: selectedAgent.color + '25', border: `2px solid ${selectedAgent.color}50` }}
            >
              {selectedAgent.emoji}
            </div>
            <div className="min-w-0">
              <div className="text-lg font-bold text-slate-100 truncate">{selectedAgent.label}</div>
              <div className="text-xs text-slate-500 line-clamp-2">{selectedAgent.description}</div>
            </div>
          </div>
        </div>

        {/* Stats row */}
        <div className="flex gap-2 px-4 py-3 border-b border-[var(--border)] flex-shrink-0">
          {[
            { label: 'Success Rate', value: `${meta?.successRate ?? '—'}%`, color: '#14b8a6' },
            { label: 'Avg Response', value: meta?.avgResponse ?? '—', color: '#3b82f6' },
            { label: 'Sub-agents', value: allSubagents.length, color: selectedAgent.color },
            { label: 'Total Tasks', value: totalTasks, color: '#64748b' },
          ].map((s, i) => (
            <div
              key={i}
              className="flex-1 px-3 py-2.5 rounded-xl relative overflow-hidden"
              style={{
                background: `linear-gradient(135deg, ${s.color}40 0%, ${s.color}18 100%)`,
                border: `1px solid ${s.color}35`,
              }}
            >
              <div className="absolute bottom-0 left-0 right-0 h-0.5" style={{ background: s.color + '90' }} />
              <div className="text-[10px] text-slate-500 uppercase tracking-wider mb-0.5">{s.label}</div>
              <div className="text-lg font-bold" style={{ color: s.color }}>{s.value}</div>
            </div>
          ))}
        </div>

        {/* Search bar */}
        <div className="flex items-center gap-3 px-6 py-3 border-b border-[var(--border)] flex-shrink-0">
          <div className="flex items-center gap-2 bg-[var(--bg-surface)] border border-[var(--border-a)] rounded-lg px-3 py-1.5 flex-1 max-w-xs">
            <Search size={12} className="text-slate-600" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search tasks..."
              className="flex-1 bg-transparent text-xs text-slate-300 placeholder-slate-700 focus:outline-none"
            />
          </div>
          <div className="ml-auto text-[10px] text-slate-600">
            {allSubagents.length} sub-agents · {totalTasks} tasks
          </div>
        </div>

        {/* Sub-agent accordion */}
        <div className="flex-1 overflow-y-auto px-6 py-3">
          {allSubagents.map((sa) => {
            const filteredTasks = sa.tasks.filter(
              (t) => !search || t.label.toLowerCase().includes(search.toLowerCase()),
            )
            if (filteredTasks.length === 0 && search) return null
            const isExpanded = expandedSubs.has(sa.id)

            return (
              <div
                key={sa.id}
                className="mb-3 rounded-xl overflow-hidden transition-all"
                style={isExpanded ? {
                  background: selectedAgent.color + '0a',
                  boxShadow: `inset 0 0 0 1px ${selectedAgent.color}30`,
                } : undefined}
              >
                <div
                  onClick={() => toggleSub(sa.id)}
                  className="flex items-center gap-3 py-2 px-2 cursor-pointer hover:bg-[var(--bg-surface)] rounded-lg transition-colors"
                >
                  {isExpanded
                    ? <ChevronDown size={13} className="text-slate-500 flex-shrink-0" />
                    : <ChevronRight size={13} className="text-slate-500 flex-shrink-0" />}
                  <span
                    className="text-xs font-bold px-2 py-0.5 rounded-md flex-shrink-0"
                    style={{
                      background: selectedAgent.color + '25',
                      color: selectedAgent.color,
                      border: `1px solid ${selectedAgent.color}40`,
                    }}
                  >
                    {sa.label}
                  </span>
                  {sa.is_prefetch_eligible && (
                    <span
                      className="flex items-center gap-1 text-[10px] font-semibold px-1.5 py-0.5 rounded-md flex-shrink-0 bg-amber-500/15 text-amber-400 border border-amber-500/30"
                      title="This sub-agent's data is prefetched ahead of time, so it can respond instantly instead of fetching on demand."
                    >
                      <Zap size={10} />
                      Prefetch
                    </span>
                  )}
                  <span className="text-xs text-slate-500 flex-1 truncate">{sa.description}</span>
                  <span className="text-[9px] text-slate-600 flex-shrink-0">{sa.tasks.length} tasks</span>
                </div>

                {isExpanded && (
                  <div className="ml-6 flex flex-col gap-2 mt-1 pb-2">
                    {filteredTasks.length === 0 ? (
                      <p className="text-[10px] text-slate-600 italic px-3 py-2">No tasks defined</p>
                    ) : filteredTasks.map((task) => (
                      <div
                        key={task.id}
                        className="relative px-3 py-2.5 rounded-xl"
                        style={{
                          background: `linear-gradient(135deg, ${selectedAgent.color}22 0%, ${selectedAgent.color}0c 100%)`,
                          border: `1px solid ${selectedAgent.color}35`,
                          borderLeft: `3px solid ${selectedAgent.color}`,
                        }}
                      >
                        <div className="text-[12px] font-semibold text-slate-200 mb-1 pr-4">{task.label}</div>
                        {task.outputs.length > 0 && (
                          <div className="flex flex-wrap gap-1">
                            {task.outputs.map((out) => (
                              <span
                                key={out}
                                className="text-[9px] px-1.5 py-0.5 rounded bg-[var(--bg-raised)] text-slate-500 border border-[var(--border-a)]"
                              >
                                {out}
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </div>

      {/* ── Right: per-agent profile (real registry data) ─────────────────── */}
      <div className="w-56 xl:w-64 2xl:w-72 flex-shrink-0 border-l border-[var(--border-a)] flex flex-col overflow-y-auto bg-[var(--bg-raised)]">

        {/* At a glance */}
        <div className="px-4 py-4 border-b border-[var(--border)]">
          <div className="flex items-center gap-2 mb-3">
            <Zap size={13} style={{ color: selectedAgent.color }} />
            <span className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">At a Glance</span>
          </div>
          <div className="grid grid-cols-2 gap-2">
            {[
              { label: 'Sub-agents', value: allSubagents.length,
                hint: 'How many sub-agents make up this agent.' },
              { label: 'Tasks', value: totalTasks,
                hint: 'Total tasks defined across all of this agent\'s sub-agents.' },
              { label: 'Data outputs', value: agentProfile.outputs.length,
                hint: 'Distinct data fields this agent\'s tasks produce (e.g. bed_count, occupancy_rate) — see "Produces" below.' },
              { label: 'Prefetch-ready', value: `${agentProfile.prefetchCount}/${allSubagents.length}`,
                hint: 'Sub-agents whose data is fetched ahead of time so they can respond instantly, out of the total for this agent.' },
            ].map((s) => (
              <div key={s.label} className="rounded-lg px-2.5 py-2 bg-[var(--bg-surface)] border border-[var(--border-a)]" title={s.hint}>
                <div className="text-base font-bold text-slate-200 leading-none">{s.value}</div>
                <div className="text-[9px] text-slate-500 uppercase tracking-wider mt-1">{s.label}</div>
              </div>
            ))}
          </div>
        </div>

        {/* Task distribution across sub-agents */}
        {agentProfile.dist.length > 0 && (
          <div className="px-4 py-4 border-b border-[var(--border)]">
            <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-3">
              Task Distribution
            </div>
            <div className="flex flex-col gap-2.5">
              {agentProfile.dist.map((d) => (
                <div key={d.id}>
                  <div className="flex items-center justify-between mb-1 gap-2">
                    <span className="text-[10px] text-slate-400 truncate">{d.label}</span>
                    <span className="text-[10px] font-bold text-slate-300 flex-shrink-0">{d.count}</span>
                  </div>
                  <div className="h-1.5 rounded-full bg-[var(--border)] overflow-hidden">
                    <div
                      className="h-full rounded-full transition-all"
                      style={{
                        width: `${agentProfile.maxCount ? Math.max((d.count / agentProfile.maxCount) * 100, d.count ? 6 : 0) : 0}%`,
                        background: selectedAgent.color,
                      }}
                    />
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Capabilities declared by this agent's sub-agents */}
        {agentProfile.capabilities.length > 0 && (
          <div className="px-4 py-4 border-b border-[var(--border)]">
            <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-2.5">
              Capabilities
            </div>
            <div className="flex flex-wrap gap-1.5">
              {agentProfile.capabilities.map((c) => (
                <span
                  key={c}
                  className="text-[9px] px-2 py-0.5 rounded-full border"
                  style={{
                    background: selectedAgent.color + '18',
                    color: selectedAgent.color,
                    borderColor: selectedAgent.color + '40',
                  }}
                >
                  {c}
                </span>
              ))}
            </div>
          </div>
        )}

        {/* Data this agent produces (union of task outputs) */}
        {agentProfile.outputs.length > 0 && (
          <div className="px-4 py-4">
            <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-2.5">
              Produces
            </div>
            <div className="flex flex-wrap gap-1.5">
              {agentProfile.outputs.slice(0, 24).map((o) => (
                <span
                  key={o}
                  className="text-[9px] px-1.5 py-0.5 rounded bg-[var(--bg-surface)] text-slate-500 border border-[var(--border-a)]"
                >
                  {o}
                </span>
              ))}
              {agentProfile.outputs.length > 24 && (
                <span className="text-[9px] px-1.5 py-0.5 text-slate-600">
                  +{agentProfile.outputs.length - 24} more
                </span>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
