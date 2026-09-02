import { memo } from 'react'
import { Handle, Position, type NodeProps, useReactFlow } from '@xyflow/react'
import { Loader2, CheckCircle, Clock, Workflow, X } from 'lucide-react'
import { useStore, type NodeStatus } from '../../store'
import { AGENT_MAP } from '../../data/agents'
import clsx from 'clsx'

export const AgentNode = memo(function AgentNode({ data, id }: NodeProps) {
  const nodeStates = useStore((s) => s.nodeStates)
  const openSubAgent = useStore((s) => s.openSubAgent)
  const executionStatus = useStore((s) => s.executionStatus)
  const backendPipeline = useStore((s) => s.backendPipeline)
  const { deleteElements } = useReactFlow()

  const agentId  = (data as { agentId: string }).agentId
  const taskType = (data as { taskType?: string }).taskType
  // Optional overrides -- used only by the checkpoint editor, where "locked as of
  // the SELECTED checkpoint" and "editable" don't match live execution state (you
  // can rewind to an earlier checkpoint than the flow's real progress, so a truly-
  // complete agent may need to render as pending again). Undefined everywhere else
  // (the main canvas), so behavior there is unchanged.
  const statusOverride = (data as { statusOverride?: NodeStatus }).statusOverride
  const editableOverride = (data as { editable?: boolean }).editable
  const disableClick = (data as { disableClick?: boolean }).disableClick
  const staticAgent  = AGENT_MAP[agentId]
  const backendAgent = backendPipeline?.agents.find((a) => a.id === id) ?? null

  // Identity, with backend fallback for agents lacking a static catalog entry.
  if (!staticAgent) console.log('[AgentNode] no static entry for agentId:', agentId, 'node id:', id)
  const label       = staticAgent?.label ?? backendAgent?.label ?? agentId
  const emoji       = staticAgent?.emoji ?? '💳'
  const color       = staticAgent?.color ?? backendAgent?.color ?? '#94a3b8'
  const description = backendAgent?.role ?? staticAgent?.description ?? ''
  const nodeState = nodeStates[id]
  const status = statusOverride ?? nodeState?.status ?? 'idle'

  const isRunning = status === 'running'
  const isComplete = status === 'complete'
  const isWaiting = status === 'waiting'
  const isExecuting = executionStatus !== 'idle'
  // Interactive (hover highlight + remove button) when editableOverride is set;
  // otherwise the original rule (only pre-execution, on the main canvas).
  const isInteractive = editableOverride ?? !isExecuting
  const isLocked = editableOverride === false

  const borderColor = isRunning
    ? '#3b82f6'
    : isWaiting
    ? '#f59e0b'
    : isComplete
    ? '#14b8a6'
    : 'var(--border-a)'

  const allSubAgents: { id: string; label: string; role?: string; active: boolean }[] =
    (backendAgent?.sub_agents ?? []).length > 0
      ? (backendAgent?.sub_agents ?? []).map((sa) => ({ id: sa.id, label: sa.label, role: sa.role, active: true }))
      : (staticAgent?.subAgents ?? []).map((sa) => ({ ...sa, role: undefined }))
  const activeSubAgents = (() => {
    const active = allSubAgents.filter((sa) => sa.active)
    if (agentId !== 'bed') return active
    let filtered = active
    if (id === 'bed_prediction_agent') filtered = allSubAgents.filter((sa) => sa.id.startsWith('sa_bed_pred'))
    else if (taskType === 'bed_reservation') filtered = active.filter((sa) => sa.id !== 'sa_bed_prediction')
    else if (taskType === 'availability_check') filtered = allSubAgents.filter((sa) => ['sa_bed_availability', 'sa_bed_ranking'].includes(sa.id))
    else filtered = active.filter((sa) => sa.id !== 'sa_bed_prediction')
    // Never blank the tag row because of an id mismatch — the backend can emit
    // sub-agent ids the static hardcoded patterns don't know (e.g. a mission-
    // generated "Bed Turnover Forecast"). Mirror SubAgentView.filterSubAgents so
    // the canvas card and the sub-agent detail screen never disagree.
    return filtered.length > 0 ? filtered : active
  })()

  function handleClick() {
    if (disableClick) return
    openSubAgent(id)
  }

  function handleRemove(e: React.MouseEvent) {
    e.stopPropagation()
    deleteElements({ nodes: [{ id }] })
  }

  return (
    <div
      className={clsx(
        'relative rounded-xl border-2 select-none transition-all duration-200 bg-[var(--bg-surface)]',
        'w-[300px]',
        disableClick ? 'cursor-default' : 'cursor-pointer',
        isRunning && 'node-running',
        isInteractive && !disableClick && 'hover:border-blue-500/60',
        isLocked && 'opacity-50'
      )}
      style={{ borderColor }}
      onClick={handleClick}
    >
      <Handle
        type="target"
        position={Position.Left}
        style={{ background: 'var(--border-s)', border: '2px solid var(--bg-surface)', width: 10, height: 10 }}
      />
      <Handle
        type="source"
        position={Position.Right}
        style={{ background: 'var(--border-s)', border: '2px solid var(--bg-surface)', width: 10, height: 10 }}
      />

      <div className="p-4">
        {/* Header row */}
        <div className="flex items-start justify-between gap-2 mb-2">
          <div className="flex items-center gap-2.5 min-w-0">
            <span className="text-2xl leading-none flex-shrink-0">{emoji}</span>
            <span
              className="text-lg font-bold leading-tight truncate"
              style={{ color }}
            >
              {label}
            </span>
          </div>
          <div className="flex items-center gap-1.5 flex-shrink-0">
            <div className="rounded-md p-1" style={{ background: color + '20', border: `1px solid ${color}40` }}>
              <Workflow size={12} style={{ color }} />
            </div>
            {isInteractive && (
              <button
                onMouseDown={handleRemove}
                className="nodrag rounded-md p-1 text-slate-600 hover:text-red-400 hover:bg-red-500/10 transition-colors"
                title="Remove agent"
              >
                <X size={14} />
              </button>
            )}
          </div>
        </div>

        {/* Description */}
        {description && (
          <p className="text-base text-slate-400 leading-snug mb-3 line-clamp-2 cursor-default" title={description}>
            {description}
          </p>
        )}

        {/* Status */}
        <div className="flex items-center gap-2 mb-3">
          {isRunning && (
            <>
              <Loader2 size={12} className="text-blue-400 animate-spin" />
              <span className="text-sm text-blue-400 font-medium">Running</span>
            </>
          )}
          {isComplete && (
            <>
              <CheckCircle size={12} className="text-teal-400" />
              <span className="text-sm text-teal-400 font-medium">Complete</span>
            </>
          )}
          {isWaiting && (
            <>
              <Clock size={12} className="text-amber-400" />
              <span className="text-sm text-amber-400 font-medium">Awaiting Approval</span>
            </>
          )}
          {!isRunning && !isComplete && !isWaiting && (
            <>
              <span className="w-2 h-2 rounded-full bg-slate-600" />
              <span className="text-xs text-slate-600 font-medium flex items-center gap-1">
                Queued <Clock size={11} className="text-slate-600" />
              </span>
            </>
          )}
        </div>

        {/* Sub-agent tags */}
        {activeSubAgents.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {activeSubAgents.slice(0, 4).map((sa) => (
              <span
                key={sa.id}
                title={sa.role || sa.label}
                className="text-sm font-medium px-2 py-1 rounded-md cursor-default"
                style={{
                  background: color + '25',
                  color,
                  border: `1px solid ${color}40`,
                }}
              >
                {sa.label}
              </span>
            ))}
            {activeSubAgents.length > 4 && (
              <span className="text-sm text-slate-600 px-1.5 py-1">+{activeSubAgents.length - 4}</span>
            )}
          </div>
        )}

        {/* Running progress bar */}
        {isRunning && (
          <div className="mt-2 h-0.5 rounded-full bg-[var(--bg-raised)] overflow-hidden">
            <div className="h-full bg-blue-500 animate-pulse w-2/3" />
          </div>
        )}
        {isComplete && (
          <div className="mt-2 h-0.5 rounded-full bg-teal-500 w-full" />
        )}
      </div>
    </div>
  )
})
