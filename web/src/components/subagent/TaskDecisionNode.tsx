import { Handle, Position, type NodeProps } from '@xyflow/react'
import { useTheme } from 'next-themes'

export function TaskDecisionNode({ data }: NodeProps) {
  const question = (data as { question?: string }).question ?? ''
  const { theme } = useTheme()
  const isLight = theme === 'light'

  const bgColor     = isLight ? '#eff6ff' : '#080f1e'
  const borderColor = isLight ? '#3b82f6' : '#2563eb'
  const glow        = isLight ? 'rgba(59,130,246,0.15)' : 'rgba(37,99,235,0.3)'
  const textColor   = isLight ? '#1d4ed8' : '#93c5fd'
  const handleBg    = isLight ? '#bfdbfe' : '#334155'

  return (
    <div style={{ width: 90, height: 90, position: 'relative' }}>
      <Handle
        type="target"
        position={Position.Left}
        style={{ background: handleBg, border: `2px solid ${isLight ? '#93c5fd' : '#0d1729'}`, width: 7, height: 7 }}
      />
      <Handle
        id="yes"
        type="source"
        position={Position.Top}
        style={{ background: '#16a34a', border: `2px solid ${isLight ? '#bbf7d0' : '#14532d'}`, width: 7, height: 7 }}
      />
      <Handle
        id="no"
        type="source"
        position={Position.Right}
        style={{ background: '#dc2626', border: `2px solid ${isLight ? '#fecaca' : '#7f1d1d'}`, width: 7, height: 7 }}
      />

      {/* Diamond body */}
      <div style={{
        position: 'absolute',
        top: '50%', left: '50%',
        transform: 'translate(-50%, -50%) rotate(45deg)',
        width: 62, height: 62,
        background: bgColor,
        border: `2px solid ${borderColor}`,
        borderRadius: 6,
        boxShadow: `0 0 12px ${glow}`,
        transition: 'background 0.3s, border-color 0.3s',
      }} />

      <div style={{
        position: 'absolute',
        top: '50%', left: '50%',
        transform: 'translate(-50%, -50%)',
        width: 66,
        height: 60,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        overflow: 'hidden',
        textAlign: 'center',
        pointerEvents: 'none',
        userSelect: 'none',
      }}>
        <span title={question} style={{
          fontSize: 10,
          color: textColor,
          fontFamily: 'monospace',
          fontWeight: 600,
          lineHeight: 1.3,
          display: '-webkit-box',
          WebkitLineClamp: 4,
          WebkitBoxOrient: 'vertical',
          overflow: 'hidden',
          pointerEvents: 'auto',
        }}>
          {question}
        </span>
      </div>
    </div>
  )
}
