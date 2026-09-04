import { Handle, Position, type NodeProps } from '@xyflow/react'
import { useTheme } from 'next-themes'

export type TaskCondStatus = 'passed' | 'skipped' | 'failed'

export type TaskNodeData = {
  label:       string
  index:       number
  color:       string
  condition?:  string
  condStatus?: TaskCondStatus
}

const STATUS_DARK: Record<TaskCondStatus, { border: string; bg: string; dot: string; opacity: number }> = {
  passed:  { border: '#14b8a6', bg: '#07211d', dot: '#14b8a6', opacity: 1 },
  skipped: { border: '#334155', bg: '#0d1729', dot: '#64748b', opacity: 0.45 },
  failed:  { border: '#dc2626', bg: '#250d0d', dot: '#dc2626', opacity: 1 },
}

const STATUS_LIGHT: Record<TaskCondStatus, { border: string; bg: string; dot: string; opacity: number }> = {
  passed:  { border: '#14b8a6', bg: '#f0fdfa', dot: '#0d9488', opacity: 1 },
  skipped: { border: '#cbd5e1', bg: '#f8fafc', dot: '#94a3b8', opacity: 0.55 },
  failed:  { border: '#dc2626', bg: '#fff1f2', dot: '#dc2626', opacity: 1 },
}

const STATUS_GLYPH: Record<TaskCondStatus, string> = { passed: '✓', skipped: '⊘', failed: '✕' }

// Matches TaskFlowCanvas's SELECTED_COLOR — a clicked node should read as
// visibly distinct before the user presses Delete, same as a clicked edge.
const SELECTED_COLOR = '#facc15'

export function TaskNode({ data, selected }: NodeProps) {
  const d = data as TaskNodeData
  const { theme } = useTheme()
  const isLight = theme === 'light'

  const STATUS_STYLE = isLight ? STATUS_LIGHT : STATUS_DARK
  const s = d.condStatus ? STATUS_STYLE[d.condStatus] : null

  const defaultBorder = isLight ? '#bfdbfe' : '#1e293b'
  const defaultBg     = isLight ? '#eff6ff' : '#0d1729'
  const labelColor    = isLight ? '#1e293b' : '#94a3b8'
  const handleBg      = isLight ? '#bfdbfe' : '#334155'
  const handleBorder  = isLight ? '#93c5fd' : '#1e293b'
  const condBg        = isLight ? 'rgba(37,99,235,0.07)' : 'rgba(148,163,184,0.08)'

  return (
    <div style={{
      width: 172,
      minHeight: 56,
      borderRadius: 10,
      border: `1.5px solid ${selected ? SELECTED_COLOR : (s?.border ?? defaultBorder)}`,
      background: s?.bg ?? defaultBg,
      opacity: s?.opacity ?? 1,
      display: 'flex',
      flexDirection: 'column',
      gap: 4,
      padding: '8px 12px',
      boxShadow: selected
        ? `0 0 0 2px ${SELECTED_COLOR}55, 0 2px 8px rgba(0,0,0,0.35)`
        : (isLight ? '0 2px 8px rgba(37,99,235,0.10)' : '0 2px 8px rgba(0,0,0,0.35)'),
      transition: 'background 0.3s, border-color 0.3s',
    }}>
      <Handle
        type="target"
        position={Position.Left}
        style={{ background: handleBg, border: `2px solid ${handleBorder}`, width: 8, height: 8 }}
      />

      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{
          fontSize: 13,
          fontWeight: 700,
          color: d.color,
          opacity: 0.8,
          flexShrink: 0,
          fontFamily: 'monospace',
          lineHeight: 1,
        }}>
          {d.index + 1}.
        </span>

        <span style={{
          fontSize: 13,
          color: labelColor,
          lineHeight: 1.45,
          fontFamily: 'system-ui, sans-serif',
          flex: 1,
          minWidth: 0,
          overflowWrap: 'anywhere',
        }}>
          {d.label}
        </span>

        {d.condStatus && (
          <span style={{
            flexShrink: 0,
            fontSize: 13,
            fontWeight: 700,
            color: s!.dot,
            lineHeight: 1,
          }}>
            {STATUS_GLYPH[d.condStatus]}
          </span>
        )}
      </div>

      {d.condition && (
        <div style={{
          fontSize: 14,
          fontFamily: 'monospace',
          color: s?.dot ?? (isLight ? '#334155' : '#475569'),
          background: condBg,
          borderRadius: 4,
          padding: '1px 5px',
          alignSelf: 'flex-start',
          maxWidth: '100%',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}>
          if {d.condition}
        </div>
      )}

      <Handle
        type="source"
        position={Position.Right}
        style={{ background: handleBg, border: `2px solid ${handleBorder}`, width: 8, height: 8 }}
      />
    </div>
  )
}
