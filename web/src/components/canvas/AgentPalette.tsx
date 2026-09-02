import { X } from 'lucide-react'
import { AGENTS } from '../../data/agents'
import { useStore } from '../../store'
import type { Node } from '@xyflow/react'

interface Props {
  onClose: () => void
}

export function AgentPalette({ onClose }: Props) {
  const nodes = useStore((s) => s.nodes)
  const setNodes = useStore((s) => s.setNodes)

  function addAgent(agentId: string) {
    const newId = `${agentId}-${Date.now()}`
    const newNode: Node = {
      id: newId,
      type: 'agentNode',
      position: { x: 100 + nodes.length * 20, y: 100 + nodes.length * 20 },
      data: { agentId, nodeId: newId },
    }
    setNodes([...nodes, newNode])
    onClose()
  }

  function onDragStart(event: React.DragEvent, agentId: string) {
    event.dataTransfer.setData('agentId', agentId)
    event.dataTransfer.effectAllowed = 'move'
  }

  const usedIds = new Set(nodes.map((n) => (n.data as { agentId: string }).agentId))

  return (
    <div className="absolute top-14 right-4 z-20 bg-[var(--bg-surface)] border border-[var(--border-a)] rounded-xl p-3 w-60 shadow-2xl">
      <div className="flex items-center justify-between mb-2">
        <div>
          <span className="text-xs font-semibold text-slate-300">Add Agent</span>
          <div className="text-[9px] text-slate-600 mt-0.5">Drag onto canvas or click to add</div>
        </div>
        <button onClick={onClose} className="text-slate-500 hover:text-slate-300">
          <X size={14} />
        </button>
      </div>
      <div className="flex flex-col gap-0.5 max-h-80 overflow-y-auto">
        {AGENTS.map((agent) => (
          <div
            key={agent.id}
            draggable
            onDragStart={(e) => onDragStart(e, agent.id)}
            onClick={() => addAgent(agent.id)}
            className={`flex items-center gap-2 px-2 py-1.5 rounded-lg cursor-grab active:cursor-grabbing hover:bg-[var(--bg-hover)] transition-colors text-xs select-none ${
              usedIds.has(agent.id) ? 'opacity-40' : ''
            }`}
          >
            <span className="text-sm flex-shrink-0">{agent.emoji}</span>
            <div className="min-w-0">
              <div className="truncate font-medium" style={{ color: agent.color }}>{agent.label}</div>
              <div className="text-[9px] text-slate-600 truncate">{agent.description}</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
