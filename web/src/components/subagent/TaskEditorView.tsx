import { useState, useEffect, useCallback, useRef } from 'react'
import {
  DndContext,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from '@dnd-kit/core'
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import {
  ArrowLeft,
  GripVertical,
  Loader2,
  Pencil,
  RefreshCw,
  Send,
  Trash2,
} from 'lucide-react'
import type { SubAgentDef, TaskDef, TaskEdgeDef } from '../../data/agents'
import type { TaskConditionState } from '../../store'
import { useStore } from '../../store'
import { TaskFlowCanvas } from './TaskFlowCanvas'

// ─── Task generation simulator ────────────────────────────────────────────────


// ─── Sortable task row ────────────────────────────────────────────────────────

function SortableEditorTask({
  task,
  index,
  color,
  active,
  isEditing,
  editingLabel,
  onStartEdit,
  onEditChange,
  onCommitEdit,
  onCancelEdit,
  onRemove,
}: {
  task: TaskDef
  index: number
  color: string
  active: boolean
  isEditing: boolean
  editingLabel: string
  onStartEdit: () => void
  onEditChange: (v: string) => void
  onCommitEdit: () => void
  onCancelEdit: () => void
  onRemove: () => void
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: task.id,
  })

  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition, opacity: isDragging ? 0.4 : 1 }}
      className="flex items-center gap-1.5 bg-[var(--bg-surface)] border border-[var(--border)] rounded-xl px-3 py-2.5 group"
    >
      <div
        {...attributes}
        {...listeners}
        className="flex-shrink-0 cursor-grab active:cursor-grabbing text-slate-700 hover:text-slate-400 touch-none"
      >
        <GripVertical size={13} />
      </div>
      <span
        className="text-xs font-bold w-3.5 flex-shrink-0 text-right"
        style={{ color: active ? color + 'aa' : '#475569' }}
      >
        {index + 1}.
      </span>
      {isEditing ? (
        <input
          autoFocus
          value={editingLabel}
          onChange={(e) => onEditChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') onCommitEdit()
            if (e.key === 'Escape') onCancelEdit()
          }}
          onBlur={onCommitEdit}
          className="flex-1 bg-[var(--bg-base)] border border-blue-500/50 rounded-lg px-2 py-0.5 text-xs text-slate-200 focus:outline-none"
        />
      ) : (
        <span className="flex-1 min-w-0 break-words text-xs text-slate-300 leading-tight">{task.label}</span>
      )}
      {!isEditing && (
        <div className="flex items-center gap-1.5 flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity">
          <button
            onClick={onStartEdit}
            className="text-slate-600 hover:text-slate-300 transition-colors"
            title="Edit task"
          >
            <Pencil size={11} />
          </button>
          <button
            onClick={onRemove}
            className="text-slate-600 hover:text-red-400 transition-colors"
            title="Delete task"
          >
            <Trash2 size={11} />
          </button>
        </div>
      )}
    </div>
  )
}

// ─── Main editor ──────────────────────────────────────────────────────────────

interface TaskEditorViewProps {
  subAgent: SubAgentDef
  agentLabel: string
  agentColor: string
  tasks: TaskDef[]
  taskEdges?: TaskEdgeDef[]
  taskConditions?: Record<string, TaskConditionState>
  onTasksChange: (tasks: TaskDef[]) => void
  onEdgesChange?: (edges: TaskEdgeDef[]) => void
  onBack: () => void
  onReorchestrate?: (feedback: string) => Promise<void>
  reorchestrateLoading?: boolean
}

export function TaskEditorView({
  subAgent,
  agentLabel,
  agentColor,
  tasks,
  taskEdges,
  taskConditions,
  onTasksChange,
  onEdgesChange,
  onBack,
  onReorchestrate,
  reorchestrateLoading,
}: TaskEditorViewProps) {
  const promptText = useStore((s) => s.promptText)
  const [missionExpanded, setMissionExpanded] = useState(false)
  const [localTasks, setLocalTasks] = useState<TaskDef[]>(tasks)
  const [localTaskEdges, setLocalTaskEdges] = useState<TaskEdgeDef[] | undefined>(taskEdges)

  // Sync when reorchestration updates the tasks/edges props externally
  useEffect(() => { setLocalTasks(tasks) }, [tasks]) // eslint-disable-line
  useEffect(() => { setLocalTaskEdges(taskEdges) }, [taskEdges]) // eslint-disable-line
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editingLabel, setEditingLabel] = useState('')
  const [modifyText, setModifyText] = useState('')
  const [modifyMessages, setModifyMessages] = useState<Array<{ role: 'user' | 'system'; text: string }>>([])
  const modifyBottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    modifyBottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [modifyMessages])

  async function sendModify() {
    const text = modifyText.trim()
    if (!text || reorchestrateLoading || !onReorchestrate) return
    setModifyText('')
    setModifyMessages((prev) => [...prev, { role: 'user', text }])
    await onReorchestrate(text)
    setModifyMessages((prev) => [...prev, { role: 'system', text: 'Re-orchestrated' }])
  }
  const updateTasks = useCallback(
    (updated: TaskDef[]) => {
      setLocalTasks(updated)
      onTasksChange(updated)
    },
    [onTasksChange],
  )

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  )

  function handleDragEnd(event: DragEndEvent) {
    const { active, over } = event
    if (!over || active.id === over.id) return
    const oldIndex = localTasks.findIndex((t) => t.id === active.id)
    const newIndex = localTasks.findIndex((t) => t.id === over.id)
    updateTasks(arrayMove(localTasks, oldIndex, newIndex))
  }

  function startEdit(task: TaskDef) {
    setEditingId(task.id)
    setEditingLabel(task.label)
  }

  function commitEdit() {
    if (!editingId) return
    const label = editingLabel.trim()
    if (!label) { setEditingId(null); return }
    updateTasks(localTasks.map((t) => (t.id === editingId ? { ...t, label } : t)))
    setEditingId(null)
  }

  function removeTask(id: string) {
    updateTasks(localTasks.filter((t) => t.id !== id))
  }

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-3 px-5 py-2.5 border-b border-[var(--border)] bg-[var(--bg-base)] flex-shrink-0">
        <button
          onClick={onBack}
          className="flex items-center gap-1.5 text-xs text-slate-400 hover:text-slate-200 transition-colors"
        >
          <ArrowLeft size={14} />
          Back to {agentLabel}
        </button>
        <div className="w-px h-4 bg-[var(--border-a)]" />
        <span
          className="flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold border flex-shrink-0"
          style={{ color: agentColor, borderColor: agentColor + '50', background: agentColor + '15' }}
        >
          <span className="w-1 h-1 rounded-full" style={{ background: agentColor }} />
          {subAgent.active ? 'Active' : 'Inactive'}
        </span>
        <div>
          <div className="text-sm font-bold text-slate-100 leading-tight">{subAgent.label}</div>
          <div className="text-[10px] text-slate-500 leading-tight">{subAgent.description}</div>
        </div>
      </div>

      {/* Body */}
      <div className="flex flex-1 overflow-hidden">

        {/* Left sidebar */}
        <div className="w-64 flex-shrink-0 border-r border-[var(--border)] flex flex-col overflow-hidden">
          {promptText && (
            <div className="px-4 pt-3 pb-2.5 border-b border-[var(--border)] flex-shrink-0">
              <div className="text-[10px] font-bold text-slate-600 uppercase tracking-widest mb-1">Mission</div>
              <p className={`text-xs text-slate-400 leading-snug ${missionExpanded ? '' : 'line-clamp-3'}`}>{promptText}</p>
              <button
                onClick={() => setMissionExpanded((v) => !v)}
                className="mt-1 text-[10px] text-slate-600 hover:text-slate-400 transition-colors"
              >
                {missionExpanded ? 'show less' : 'show more'}
              </button>
            </div>
          )}
          <div className="px-4 pt-3 pb-2 flex items-center justify-between flex-shrink-0">
            <span className="text-xs font-bold text-slate-500 uppercase tracking-widest">Tasks</span>
            <span className="text-xs font-semibold px-1.5 py-0.5 rounded bg-teal-900/40 text-teal-400 border border-teal-700/40">
              {localTasks.length} active
            </span>
          </div>

          <div className="flex-1 overflow-y-auto px-2 pb-2">
            <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
              <SortableContext items={localTasks.map((t) => t.id)} strategy={verticalListSortingStrategy}>
                <div className="flex flex-col gap-0.5">
                  {localTasks.map((task, i) => (
                    <SortableEditorTask
                      key={task.id}
                      task={task}
                      index={i}
                      color={agentColor}
                      active={subAgent.active}
                      isEditing={editingId === task.id}
                      editingLabel={editingLabel}
                      onStartEdit={() => startEdit(task)}
                      onEditChange={setEditingLabel}
                      onCommitEdit={commitEdit}
                      onCancelEdit={() => setEditingId(null)}
                      onRemove={() => removeTask(task.id)}
                    />
                  ))}
                </div>
              </SortableContext>
            </DndContext>

          </div>

          {onReorchestrate && (
            <div className="border-t border-[var(--border)] px-3 py-3 flex flex-col gap-2 flex-shrink-0">
              <label className="text-[11px] font-semibold text-slate-600 uppercase tracking-wider flex items-center gap-1.5">
                <RefreshCw size={10} />
                Modify tasks
              </label>

              {/* Prompt history */}
              {modifyMessages.length > 0 && (
                <div className="flex flex-col gap-1.5 max-h-40 overflow-y-auto pr-0.5">
                  {modifyMessages.map((msg, i) => (
                    <div key={i} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                      <span className={`
                        max-w-[85%] px-2.5 py-1.5 rounded-xl text-xs leading-snug
                        ${msg.role === 'user'
                          ? 'bg-blue-600 text-white rounded-br-none'
                          : 'bg-[var(--bg-surface)] border border-[var(--border-a)] text-slate-400 rounded-bl-none'
                        }
                      `}>
                        {msg.text}
                      </span>
                    </div>
                  ))}
                  <div ref={modifyBottomRef} />
                </div>
              )}

              <textarea
                value={modifyText}
                onChange={(e) => setModifyText(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendModify() } }}
                disabled={reorchestrateLoading}
                placeholder="Describe changes to task selection…"
                rows={2}
                className="w-full bg-[var(--bg-surface)] border border-[var(--border-a)] rounded-xl px-3 py-2 text-xs text-slate-300 resize-none placeholder-slate-700 focus:outline-none focus:border-blue-500 disabled:opacity-50 transition-colors leading-relaxed"
              />
              <button
                onClick={sendModify}
                disabled={!modifyText.trim() || reorchestrateLoading}
                className="w-full flex items-center justify-center gap-2 py-1.5 rounded-xl bg-blue-600 hover:bg-blue-500 disabled:opacity-30 disabled:cursor-not-allowed transition-colors text-white text-xs font-semibold"
              >
                {reorchestrateLoading ? <Loader2 size={11} className="animate-spin" /> : <Send size={11} />}
                Send
              </button>
            </div>
          )}
        </div>

        {/* Canvas — task flow */}
        <div className="flex-1 flex flex-col overflow-hidden">
          <div className="px-6 py-3 border-b border-[var(--border)] flex-shrink-0">
            <span className="text-[10px] font-bold text-slate-500 uppercase tracking-widest">Flow</span>
          </div>

          <div className="flex-1 overflow-hidden relative">
            {/* Re-orchestrate button — top left of canvas */}
            {onReorchestrate && (
              <div className="absolute top-4 left-4 z-10">
                <button
                  onClick={async () => {
                    if (reorchestrateLoading) return
                    await onReorchestrate('')
                    setModifyMessages((prev) => [...prev, { role: 'system', text: 'Re-orchestrated' }])
                  }}
                  disabled={reorchestrateLoading}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-[var(--bg-raised)] border border-[var(--border-a)] text-slate-400 text-xs hover:bg-[var(--bg-hover)] hover:text-slate-300 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                >
                  {reorchestrateLoading
                    ? <Loader2 size={12} className="animate-spin" />
                    : <RefreshCw size={12} />
                  }
                  Re-orchestrate
                </button>
              </div>
            )}

            {reorchestrateLoading && (
              <div className="absolute inset-0 z-20 flex items-center justify-center bg-black/40 backdrop-blur-sm pointer-events-none">
                <div className="flex items-center gap-3 px-5 py-3 rounded-2xl bg-[var(--bg-overlay-base)] border border-blue-500/40 shadow-2xl">
                  <Loader2 size={14} className="text-blue-400 animate-spin flex-shrink-0" />
                  <span className="text-xs text-blue-300 font-semibold">Re-orchestrating tasks…</span>
                </div>
              </div>
            )}
            <TaskFlowCanvas
              tasks={localTasks}
              taskEdges={localTaskEdges ?? subAgent.taskEdges}
              taskConditions={taskConditions}
              agentColor={agentColor}
              onEdgesUpdate={(edges) => {
                setLocalTaskEdges(edges)
                onEdgesChange?.(edges)
              }}
              onTasksDelete={(deletedIds) => updateTasks(localTasks.filter((t) => !deletedIds.includes(t.id)))}
            />
          </div>
        </div>
      </div>
    </div>
  )
}
