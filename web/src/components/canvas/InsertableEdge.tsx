import { BaseEdge, EdgeLabelRenderer, getSmoothStepPath, type EdgeProps } from '@xyflow/react'
import { Plus } from 'lucide-react'

type InsertEdgeData = {
  isEditing?: boolean
  onInsert?: (id: string, screenX: number, screenY: number) => void
  condition?: string | null
  condition_label?: string | null
  isSkipped?: boolean
  isDecisionBranch?: 'yes' | 'no' | null
}

// Positive-outcome conditions → YES branch; negated conditions → NO branch
const CONDITION_POLARITY: Record<string, 'yes' | 'no'> = {
  icu_full:                 'yes',
  icu_not_full:             'no',
  er_critical_patients:     'yes',
  no_er_critical_patients:  'no',
  has_stepdown_candidates:  'yes',
  has_discharge_candidates: 'yes',
  beds_available:           'yes',
  no_beds_available:        'no',
}

export function InsertableEdge({
  id, sourceX, sourceY, targetX, targetY,
  sourcePosition, targetPosition, style, markerEnd, data,
}: EdgeProps) {
  const [edgePath, labelX, labelY] = getSmoothStepPath({
    sourceX, sourceY, sourcePosition,
    targetX, targetY, targetPosition,
  })
  const d = data as InsertEdgeData | undefined
  const hasCondition    = !!d?.condition
  const condLabel       = d?.condition_label ?? d?.condition ?? ''
  const isSkipped       = !!d?.isSkipped
  const isDecisionBranch = d?.isDecisionBranch ?? null
  const polarity        = d?.condition ? (CONDITION_POLARITY[d.condition] ?? 'yes') : null

  // YES/NO badge sits halfway between the diamond midpoint and the target
  const badgeX = labelX + (targetX - labelX) * 0.5
  const badgeY = labelY + (targetY - labelY) * 0.5

  // ── Decision branch edge: YES or NO pill at midpoint, no diamond ──────────
  if (isDecisionBranch) {
    const isYes = isDecisionBranch === 'yes'
    return (
      <>
        <BaseEdge id={id} path={edgePath} style={style} markerEnd={markerEnd} />
        <EdgeLabelRenderer>
          <div
            className="nodrag nopan"
            style={{
              position: 'absolute',
              transform: `translate(-50%,-50%) translate(${labelX}px,${labelY}px)`,
              pointerEvents: 'none',
            }}
          >
            {isSkipped ? (
              <div style={{
                width: 24, height: 24, borderRadius: '50%',
                background: '#0f172a', border: '1.5px solid #1e293b',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                opacity: 0.6,
              }}>
                <span style={{ color: '#374151', fontSize: 11, lineHeight: 1 }}>✕</span>
              </div>
            ) : (
              <div style={{
                padding: '3px 10px',
                borderRadius: 999,
                background: isYes ? 'rgba(20,83,45,0.9)' : 'rgba(127,29,29,0.6)',
                border: `1.5px solid ${isYes ? '#16a34a' : '#ef4444'}`,
                color: isYes ? '#4ade80' : '#f87171',
                fontSize: 10,
                fontFamily: 'monospace',
                fontWeight: 700,
                letterSpacing: '0.08em',
                boxShadow: isYes ? '0 0 8px rgba(22,163,74,0.3)' : '0 0 8px rgba(239,68,68,0.25)',
              }}>
                {isYes ? 'YES' : 'NO'}
              </div>
            )}
          </div>
        </EdgeLabelRenderer>
      </>
    )
  }

  return (
    <>
      <BaseEdge id={id} path={edgePath} style={style} markerEnd={markerEnd} />
      <EdgeLabelRenderer>
        {/* YES / NO outcome badge on standalone conditional edges */}
        {hasCondition && !isSkipped && polarity && (
          <div
            className="nodrag nopan"
            style={{
              position: 'absolute',
              transform: `translate(-50%,-50%) translate(${badgeX}px,${badgeY}px)`,
              pointerEvents: 'none',
            }}
          >
            <div style={{
              padding: '2px 8px',
              borderRadius: 999,
              background: polarity === 'yes' ? 'rgba(20,83,45,0.85)' : 'rgba(127,29,29,0.6)',
              border: `1px solid ${polarity === 'yes' ? '#16a34a' : '#ef4444'}`,
              color: polarity === 'yes' ? '#4ade80' : '#f87171',
              fontSize: 9,
              fontFamily: 'monospace',
              fontWeight: 700,
              letterSpacing: '0.08em',
              backdropFilter: 'blur(4px)',
            }}>
              {polarity === 'yes' ? 'YES' : 'NO'}
            </div>
          </div>
        )}

        <div
          className="nodrag nopan"
          style={{
            position: 'absolute',
            transform: `translate(-50%,-50%) translate(${labelX}px,${labelY}px)`,
            pointerEvents: hasCondition ? 'none' : (d?.isEditing ? 'all' : 'none'),
          }}
        >
          {hasCondition ? (
            isSkipped ? (
              /* Stop indicator — replaces diamond when branch was skipped */
              <div style={{
                width: 28, height: 28,
                borderRadius: '50%',
                background: '#0f172a',
                border: '1.5px solid #374151',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                opacity: 0.75,
              }}>
                <span style={{ color: '#4b5563', fontSize: 13, lineHeight: 1, userSelect: 'none' }}>✕</span>
              </div>
            ) : (
              /* Diamond decision node */
              <div style={{ position: 'relative', width: 180, height: 180 }}>
                <div style={{
                  position: 'absolute',
                  top: '50%', left: '50%',
                  transform: 'translate(-50%, -50%) rotate(45deg)',
                  width: 124, height: 124,
                  background: '#080f1e',
                  border: '2px solid #2563eb',
                  borderRadius: 8,
                  boxShadow: '0 0 18px rgba(37,99,235,0.25)',
                }} />
                <div style={{
                  position: 'absolute',
                  top: '50%', left: '50%',
                  transform: 'translate(-50%, -50%)',
                  width: 148,
                  textAlign: 'center',
                }}>
                  <span style={{
                    fontSize: 11,
                    color: '#93c5fd',
                    fontFamily: 'monospace',
                    lineHeight: 1.4,
                    display: 'block',
                    fontWeight: 500,
                  }}>
                    {condLabel}?
                  </span>
                </div>
              </div>
            )
          ) : d?.isEditing ? (
            <button
              onClick={(e) => {
                e.stopPropagation()
                const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
                d.onInsert?.(id, rect.left + rect.width / 2, rect.top + rect.height / 2)
              }}
              className="w-6 h-6 rounded-full bg-[var(--bg-raised)] hover:bg-blue-600 border border-[var(--border-s)] hover:border-blue-400 flex items-center justify-center text-slate-500 hover:text-white opacity-30 hover:opacity-100 transition-all shadow-md"
              title="Insert agent"
            >
              <Plus size={10} strokeWidth={2.5} />
            </button>
          ) : null}
        </div>
      </EdgeLabelRenderer>
    </>
  )
}
