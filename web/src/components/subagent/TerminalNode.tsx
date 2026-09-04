import { Handle, Position, type NodeProps } from '@xyflow/react'
import { useTheme } from 'next-themes'
import { Ban } from 'lucide-react'

export function TerminalNode({ data }: NodeProps) {
  const d = data as { label: string }
  const { theme } = useTheme()
  const isLight = theme === 'light'

  return (
    <div className="flex flex-col items-center justify-center w-[180px] h-[80px] rounded-xl border border-slate-700/60 bg-slate-900/80">
      <Handle
        type="target"
        position={Position.Left}
        style={{
          background: isLight ? '#bfdbfe' : '#334155',
          border: `2px solid ${isLight ? '#93c5fd' : '#1e293b'}`,
          width: 8,
          height: 8,
        }}
      />
      <Ban size={18} className="text-slate-600 mb-1" />
      <span className="text-[12px] text-slate-600 font-medium text-center leading-tight px-3">
        {d.label}
      </span>
    </div>
  )
}
