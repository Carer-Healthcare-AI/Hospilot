import { memo } from 'react'
import { Handle, Position, type NodeProps } from '@xyflow/react'
import { useStore } from '../../store'
import { useTheme } from 'next-themes'

export const DecisionNode = memo(function DecisionNode({ data, id }: NodeProps) {
  const nodeStates = useStore((s) => s.nodeStates)
  const status = nodeStates[id]?.status ?? 'idle'
  const { theme } = useTheme()
  const isLight = theme === 'light'

  const borderColor = status === 'complete' ? '#14b8a6'
    : status === 'skipped'                  ? (isLight ? '#cbd5e1' : '#1e293b')
    : (isLight ? '#3b82f6' : '#2563eb')

  const glowColor = status === 'complete' ? (isLight ? 'rgba(20,184,166,0.2)' : 'rgba(20,184,166,0.25)')
    : status === 'skipped'                ? 'transparent'
    : (isLight ? 'rgba(59,130,246,0.15)' : 'rgba(37,99,235,0.25)')

  const bgColor = status === 'complete'
    ? (isLight ? '#f0fdfa' : '#080f1e')
    : status === 'skipped'
    ? (isLight ? '#f8fafc' : '#080f1e')
    : (isLight ? '#eff6ff' : '#080f1e')

  const textColor = status === 'skipped'
    ? (isLight ? '#94a3b8' : '#475569')
    : (isLight ? '#1d4ed8' : '#93c5fd')

  const opacity = status === 'skipped' ? 0.4 : 1
  const question = (data as { question?: string }).question ?? ''

  return (
    <div style={{ width: 260, height: 260, position: 'relative', opacity }}>
      <Handle
        type="target"
        position={Position.Left}
        style={{ background: 'transparent', border: 'none', width: 8, height: 8 }}
      />
      <Handle
        id="yes"
        type="source"
        position={Position.Top}
        style={{ background: 'transparent', border: 'none', width: 8, height: 8 }}
      />
      <Handle
        id="no"
        type="source"
        position={Position.Bottom}
        style={{ background: 'transparent', border: 'none', width: 8, height: 8 }}
      />

      {/* Diamond body */}
      <div style={{
        position: 'absolute',
        top: '50%', left: '50%',
        transform: 'translate(-50%, -50%) rotate(45deg)',
        width: 184, height: 184,
        background: bgColor,
        border: `2px solid ${borderColor}`,
        borderRadius: 10,
        boxShadow: `0 0 24px ${glowColor}`,
        transition: 'border-color 0.3s, box-shadow 0.3s, background 0.3s',
      }} />

      {/* Question label */}
      <div style={{
        position: 'absolute',
        top: '50%', left: '50%',
        transform: 'translate(-50%, -50%)',
        width: 210,
        textAlign: 'center',
        pointerEvents: 'none',
        userSelect: 'none',
      }}>
        <span style={{
          fontSize: 15,
          color: textColor,
          fontFamily: 'monospace',
          fontWeight: 600,
          lineHeight: 1.4,
          display: 'block',
          transition: 'color 0.3s',
        }}>
          {question}?
        </span>
      </div>
    </div>
  )
})
