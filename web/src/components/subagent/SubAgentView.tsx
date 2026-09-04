import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import {
	ReactFlow,
	Background,
	BackgroundVariant,
	Controls,
	MarkerType,
	useNodesState,
	useEdgesState,
	addEdge,
	Handle,
	Position,
	type Connection,
	type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
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
	ChevronRight,
	GripVertical,
	Loader2,
	Plus,
	RefreshCw,
	Send,
	Workflow,
	X,
} from 'lucide-react'

import { useStore, type SubAgentOverride } from '../../store'
import { AGENT_MAP, type AgentDef, type SubAgentDef, type TaskDef, type TaskEdgeDef } from '../../data/agents'
import type { RegistrySubAgent } from '../../services/api'
import { TaskEditorView } from './TaskEditorView'
import { computeLayout } from '../../lib/layout'
import { DecisionNode } from '../canvas/DecisionNode'
import { TerminalNode } from './TerminalNode'

// ─── Context ──────────────────────────────────────────────────────────────────

interface SubAgentContextValue {
	taskOverrides: Record<string, TaskDef[]>
	selectedTasksBySubagent: Record<string, string[]>
	registryTasksBySubagent: Record<string, TaskDef[]>
	onTasksChange: (subAgentId: string, tasks: TaskDef[]) => void
	onRemoveSubAgent: (subAgentId: string) => void
	onOpenTaskEditor: (subAgentId: string) => void
}

const SubAgentContext = createContext<SubAgentContextValue>({
	taskOverrides: {},
	selectedTasksBySubagent: {},
	registryTasksBySubagent: {},
	onTasksChange: () => { },
	onRemoveSubAgent: () => { },
	onOpenTaskEditor: () => { },
})

// Render a planner task gate as a short, readable string. The planner sends a typed
// { symbol, op, value }; mirror its _condition_to_string so the canvas shows the same
// text. Passes a string condition through; returns undefined when there is none.
function conditionToString(cond: unknown): string | undefined {
	if (cond == null) return undefined
	if (typeof cond === 'string') return cond || undefined
	if (typeof cond !== 'object') return undefined
	const c = cond as { symbol?: string; op?: string; value?: unknown }
	const field = (c.symbol ?? '').split('.').pop() || c.symbol || ''
	const op = c.op ?? ''
	const val = c.value
	if (val == null) return op === '!=' ? `${field} set` : `${field} missing`
	if (typeof val === 'boolean') {
		const positive = op === '==' ? val : op === '!=' ? !val : val
		return positive ? field : `not ${field}`
	}
	return `${field} ${op} ${val}`.trim()
}

// ─── Task item (canvas node) ─────────────────────────────────────────────────

function TaskItem({
	task,
	index,
	color,
	active,
}: {
	task: TaskDef
	index: number
	color: string
	active: boolean
}) {
	return (
		<div className="flex items-start gap-1.5 mb-1">
			<span
				className="text-xs font-bold flex-shrink-0 mt-px w-3.5 text-right"
				style={{ color: active ? color + 'aa' : '#475569' }}
			>
				{index + 1}.
			</span>
			<span className="flex-1 min-w-0 text-xs leading-tight break-words text-slate-400">
				{task.label}
			</span>
		</div>
	)
}

// ─── Sortable sidebar item ────────────────────────────────────────────────────

function SortableSidebarItem({
	sa,
	index,
	agentColor,
	taskCount,
	onOpen,
	onRemove,
}: {
	sa: SubAgentDef
	index: number
	agentColor: string
	taskCount: number
	onOpen: () => void
	onRemove: () => void
}) {
	const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
		id: sa.id,
	})

	return (
		<div
			ref={setNodeRef}
			style={{ transform: CSS.Transform.toString(transform), transition, opacity: isDragging ? 0.5 : 1 }}
			className="rounded-lg group/sidebar"
		>
			<div className="flex items-center hover:bg-[var(--bg-surface)] transition-colors rounded-lg">
				{/* Drag handle */}
				<div
					{...attributes}
					{...listeners}
					className="pl-2 py-2 cursor-grab active:cursor-grabbing text-slate-700 hover:text-slate-500 touch-none flex-shrink-0"
				>
					<GripVertical size={10} />
				</div>

				{/* Main row — click opens task editor */}
				<button
					onClick={onOpen}
					className="flex-1 flex items-center gap-2 px-2 py-2 text-left min-w-0"
				>
					<span
						className="w-5 h-5 rounded-full flex items-center justify-center text-xs font-bold text-white flex-shrink-0"
						style={{ background: sa.active ? agentColor : '#334155' }}
					>
						{index + 1}
					</span>
					<div className="flex-1 min-w-0">
						<div className={`text-xs font-semibold leading-tight ${sa.active ? 'text-slate-200' : 'text-slate-500'}`}>
							{sa.label}
						</div>
						<div title={sa.description} className="text-xs text-slate-600 leading-tight truncate">{sa.description}</div>
					</div>
					<div className="flex items-center gap-1.5 flex-shrink-0">
						<span className="text-xs text-slate-600">{taskCount} tasks</span>
						<ChevronRight size={11} className="text-slate-700 group-hover/sidebar:text-slate-400 transition-colors" />
					</div>
				</button>

				{/* Remove button */}
				<button
					onClick={onRemove}
					className="pr-2 py-2 flex-shrink-0 text-slate-700 hover:text-red-400 transition-colors opacity-0 group-hover/sidebar:opacity-100"
					title="Remove sub-agent"
				>
					<X size={11} />
				</button>
			</div>
		</div>
	)
}

// ─── Sub-agent canvas node ────────────────────────────────────────────────────

function SubAgentCanvasNode({ data }: NodeProps) {
	const d = data as { sub: SubAgentDef; color: string; index: number }
	const { taskOverrides, selectedTasksBySubagent, registryTasksBySubagent, onRemoveSubAgent } = useContext(SubAgentContext)
	const rawTasks = taskOverrides[d.sub.id] ?? d.sub.tasks ?? []
	const selectedIds = selectedTasksBySubagent[d.sub.id]
	const tasks = selectedIds === undefined
		? rawTasks
		: selectedIds.length === 0
			? []
			: (() => {
				const taskById = new Map<string, TaskDef>(
					[...(registryTasksBySubagent[d.sub.id] ?? []), ...rawTasks].map((t) => [t.id, t])
				)
				return selectedIds.flatMap((id) => { const t = taskById.get(id); return t ? [t] : [] })
			})()

	return (
		<div
			className="group rounded-xl border-2 bg-[var(--bg-surface)] w-[220px]"
			style={{ borderColor: d.sub.active ? d.color + '60' : 'var(--border-a)' }}
		>
			<Handle
				type="target"
				position={Position.Left}
				style={{ background: 'var(--border-s)', border: '2px solid var(--bg-surface)', width: 8, height: 8 }}
			/>
			<Handle
				type="source"
				position={Position.Right}
				style={{ background: 'var(--border-s)', border: '2px solid var(--bg-surface)', width: 8, height: 8 }}
			/>

			<div className="p-3">
				{/* Header */}
				<div className="flex items-center gap-2 mb-1.5">
					<span
						className="w-5 h-5 rounded-full flex items-center justify-center text-sm font-bold text-white flex-shrink-0"
						style={{ background: d.sub.active ? d.color : '#334155' }}
					>
						{d.index + 1}
					</span>
					<span className="text-base font-bold text-slate-100 leading-tight flex-1">{d.sub.label}</span>
					<div className="flex-shrink-0 rounded-md p-0.5" style={{ background: d.color + '20', border: `1px solid ${d.color}40` }}>
						<Workflow size={10} style={{ color: d.color }} />
					</div>
					<button
						onMouseDown={(e) => { e.stopPropagation(); onRemoveSubAgent(d.sub.id) }}
						className="nodrag flex-shrink-0 rounded p-0.5 text-slate-500 hover:text-red-400 hover:bg-red-500/10 transition-colors"
						title="Remove sub-agent"
					>
						<X size={14} />
					</button>
				</div>
				<p className="text-sm text-slate-500 leading-tight mb-2">{d.sub.description}</p>

				{/* Task sequence */}
				<div
					className="pt-2 border-t border-[var(--border)] nodrag"
					onMouseDown={(e) => e.stopPropagation()}
					onClick={(e) => e.stopPropagation()}
				>
					<div className="flex items-center justify-between mb-1.5">
						<div className="text-sm font-bold text-slate-700 uppercase tracking-widest">
							Task Sequence
						</div>
					</div>

					<div>
						{tasks.map((task, ti) => (
							<TaskItem
								key={task.id}
								task={task}
								index={ti}
								color={d.color}
								active={d.sub.active}
							/>
						))}
					</div>

					{tasks.length === 0 && (
						<p className="text-xs text-slate-700 italic mb-1">No tasks yet</p>
					)}
				</div>
			</div>
		</div>
	)
}

const subNodeTypes = { subAgentNode: SubAgentCanvasNode, decisionNode: DecisionNode, terminalNode: TerminalNode }

// ─── Main view ────────────────────────────────────────────────────────────────

export function SubAgentView() {
	const subAgentNodeId = useStore((s) => s.subAgentNodeId)
	const closeSubAgent = useStore((s) => s.closeSubAgent)
	const storeNodes = useStore((s) => s.nodes)
	const executionStatus = useStore((s) => s.executionStatus)
	const isEditing = executionStatus === 'idle'
	const saveAgentOverride = useStore((s) => s.saveAgentOverride)
	const backendPipeline = useStore((s) => s.backendPipeline)
	const livePlans = useStore((s) => s.livePlans)
	const taskConditions = useStore((s) => s.taskConditions)
	const agentRegistry = useStore((s) => s.agentRegistry)
	const reorchestrateWithFeedback = useStore((s) => s.reorchestrateWithFeedback)
	const reorchestrateLoading = useStore((s) => s.reorchestrateLoading)
	const selectedSubagentsByAgent = useStore((s) => s.selectedSubagentsByAgent)
	const selectedTasksBySubagent = useStore((s) => s.selectedTasksBySubagent)
	const reorchestratedEdgesByAgent = useStore((s) => s.reorchestratedEdgesByAgent)
	const promptText = useStore((s) => s.promptText)

	// Strip implementation-detail words and planner-instruction suffixes from task labels
	// before showing them. Catalog labels embed planner hints like
	// "… — condition: ta_x.field > 0" / "… — always include" / "… — ONLY when …" that
	// must never reach the UI.
	function cleanTaskLabel(label: string): string {
		return label
			.replace(/\s*[—–-]\s+(condition:|always\b|only when\b|fallback\b|emergency\b).*$/i, '')
			.replace(/\s+(from|in|to|via|across)\s+Redis/gi, '')
			.replace(/\s+with\s+Claude(\s+AI)?/gi, '')
			.replace(/\s+(in|to|from)\s+Hasura/gi, '')
			.replace(/\s*\(\s*Claude\s*\)/gi, '')
			.replace(/\s{2,}/g, ' ')
			.trim()
	}

	// Build sub-agent id → TaskDef[] from the backend pipeline response for this node
	const backendTasksMap = useMemo<Record<string, TaskDef[]>>(() => {
		if (!backendPipeline || !subAgentNodeId) return {}
		const backendAgent = backendPipeline.agents.find((a) => a.id === subAgentNodeId)
		if (!backendAgent?.sub_agents?.length) return {}
		return Object.fromEntries(
			backendAgent.sub_agents.map((sa) => [
				sa.id,
				(sa.tasks ?? []).map((t, i) =>
					typeof t === 'string'
						? { id: `bt-${sa.id}-${i}`, label: cleanTaskLabel(t) }
						: { id: t.id, label: cleanTaskLabel(t.label), condition: conditionToString(t.condition) }
				),
			])
		)
	}, [backendPipeline, subAgentNodeId])

	// Build sub-agent id → TaskEdgeDef[] from task_edges in the backend response
	const backendEdgesMap = useMemo<Record<string, TaskEdgeDef[]>>(() => {
		if (!backendPipeline || !subAgentNodeId) return {}
		const backendAgent = backendPipeline.agents.find((a) => a.id === subAgentNodeId)
		if (!backendAgent?.sub_agents?.length) return {}
		return Object.fromEntries(
			backendAgent.sub_agents
				.filter((sa) => sa.task_edges?.length)
				.map((sa) => [sa.id, sa.task_edges!])
		)
	}, [backendPipeline, subAgentNodeId])

	// Registry task lookup: subAgentId → TaskDef[] for the current agent node.
	// Used as a fallback when selected_tasks from reorchestration includes IDs that were
	// not in the original pipeline (i.e. newly added tasks).
	const registryTasksBySubagent = useMemo<Record<string, TaskDef[]>>(() => {
		if (!subAgentNodeId) return {}
		const baseId = subAgentNodeId.split(':')[0]
		const regAgent = agentRegistry.find((a) => a.id === baseId || a.id === subAgentNodeId)
		if (!regAgent) return {}
		return Object.fromEntries(
			regAgent.subagents.map((sa) => [
				sa.id,
				(sa.tasks ?? []).map((t) => ({ id: t.id, label: cleanTaskLabel(t.label) })),
			])
		)
	}, [agentRegistry, subAgentNodeId])

	// Live plans streamed during execution (agent_plan) — take precedence over both the
	// static definitions and the planning-time backend tasks. agent_plan carries no task
	// labels, so resolve them by id from the createSession tasks (nice goal-specific
	// labels); fall back to the humanised id the WS handler produced.
	const liveTasksMap = useMemo<Record<string, TaskDef[]>>(() => {
		const out: Record<string, TaskDef[]> = {}
		for (const [saId, tasks] of Object.entries(livePlans)) {
			const labelById = new Map((backendTasksMap[saId] ?? []).map((t) => [t.id, t.label]))
			out[saId] = tasks.map((t) => ({ ...t, label: labelById.get(t.id) ?? cleanTaskLabel(t.label) }))
		}
		return out
	}, [livePlans, backendTasksMap])

	const agentId = subAgentNodeId
		? (storeNodes.find((n) => n.id === subAgentNodeId)?.data as { agentId: string })?.agentId
		: null
	const taskType = subAgentNodeId
		? (storeNodes.find((n) => n.id === subAgentNodeId)?.data as { taskType?: string })?.taskType
		: null

	// Backend pipeline agent for this node (the node id IS the backend agent id).
	const backendAgent = backendPipeline && subAgentNodeId
		? backendPipeline.agents.find((a) => a.id === subAgentNodeId) ?? null
		: null

	const staticAgent = agentId ? AGENT_MAP[agentId] : null

	// Agent identity. The static catalog carries the emoji + inter-sub-agent edges, so
	// prefer it; fall back to the backend's own label/color when no static def matches
	// (e.g. billing_agent, whose static catalog entry was stale and removed).
	// Description always prefers the backend's mission-specific role over the static
	// catalog's generic blurb — matches AgentNode.tsx's canvas-card precedence so the
	// two views never disagree on what this agent is doing for THIS run.
	const agent: AgentDef | null = staticAgent
		? { ...staticAgent, description: backendAgent?.role ?? staticAgent.description }
		: (backendAgent
			? {
				id: backendAgent.id,
				label: backendAgent.label,
				emoji: '💳',
				color: backendAgent.color ?? '#94a3b8',
				description: backendAgent.role ?? '',
				subAgents: [],
			}
			: null)

	// For the bed agent, filter sub-agents based on task_type from the backend pipeline:
	//   bed_prediction_agent flow  → show only 'sa_bed_prediction'
	//   bed_agent reservation flow → show only the three reservation sub-agents
	//   bed_agent availability     → show only the two availability sub-agents (no reservation)
	//   anything else              → show all
	function filterSubAgents(subs: SubAgentDef[]): SubAgentDef[] {
		if (agentId !== 'bed') return subs
		const backendId = subAgentNodeId ?? ''
		let filtered = subs
		if (backendId === 'bed_prediction_agent') {
			// Backend bed_prediction_agent uses sa_bed_pred_* ids (the static catalog used sa_bed_prediction).
			filtered = subs.filter((s) => s.id.startsWith('sa_bed_pred'))
		} else if (taskType === 'bed_reservation') {
			filtered = subs.filter((s) => s.id !== 'sa_bed_prediction')
		} else if (taskType === 'availability_check') {
			filtered = subs.filter((s) => ['sa_bed_availability', 'sa_bed_ranking'].includes(s.id))
		}
		// Never blank the panel because of an id mismatch — fall back to the full list.
		return filtered.length > 0 ? filtered : subs
	}

	// The sub-agent list comes from the backend pipeline — the source of truth for what
	// actually runs — enriched with static metadata (capabilities/edges/terminal) where
	// IDs match. Falls back to the static catalog only when the backend has no sub-agents
	// for this node (e.g. simulation mode with no live session).
	const resolvedSubAgents = useMemo<SubAgentDef[]>(() => {
		if (backendAgent?.sub_agents?.length) {
			const staticById = new Map((staticAgent?.subAgents ?? []).map((sa) => [sa.id, sa]))
			const baseId = subAgentNodeId?.split(':')[0] ?? ''
			const registryById = new Map(
				(agentRegistry.find((a) => a.id === baseId || a.id === subAgentNodeId)?.subagents ?? []).map((sa) => [sa.id, sa])
			)
			const list = backendAgent.sub_agents.map((bsa): SubAgentDef => {
				const st = staticById.get(bsa.id)
				const reg = registryById.get(bsa.id)
				return {
					id: bsa.id,
					label: reg?.label ?? bsa.label ?? st?.label,
					description: bsa.subgoal ?? reg?.description ?? bsa.role ?? st?.description ?? '',
					active: true,
					capabilities: st?.capabilities ?? [],
					tasks: st?.tasks,
					taskEdges: st?.taskEdges,
					terminal: st?.terminal,
				}
			})
			return filterSubAgents(list)
		}
		return filterSubAgents(staticAgent?.subAgents ?? [])
	}, [backendAgent, staticAgent]) // eslint-disable-line

	const [missionExpanded, setMissionExpanded] = useState(false)

	// ── Modify sub-agents state (declared early — used in the subAgentNodeId effect below) ─
	const [modifyText, setModifyText] = useState('')
	const [modifyMessages, setModifyMessages] = useState<Array<{ role: 'user' | 'system'; text: string }>>([])
	const modifyBottomRef = useRef<HTMLDivElement>(null)

	useEffect(() => {
		modifyBottomRef.current?.scrollIntoView({ behavior: 'smooth' })
	}, [modifyMessages])

	// ── Unified ordered sub-agent list ───────────────────────────────────────
	const [orderedSubAgents, setOrderedSubAgents] = useState<SubAgentDef[]>([])
	const [removedIds, setRemovedIds] = useState<Set<string>>(new Set())
	// IDs added manually via "Add Sub-agent" — exempt from reorchestration selection filter
	const [manuallyAddedIds, setManuallyAddedIds] = useState<Set<string>>(new Set())

	// Re-initialise whenever we open a different agent node
	useEffect(() => {
		setOrderedSubAgents(resolvedSubAgents)
		setRemovedIds(new Set())
		setManuallyAddedIds(new Set())
		setTaskOverrides(backendTasksMap)
		setTaskEdgeOverrides(backendEdgesMap)
		setTaskEditorId(null)
		setModifyMessages([])
		setMissionExpanded(false)
	}, [subAgentNodeId]) // eslint-disable-line

	// visibleSubAgents: apply manual removals first, then clamp to the reorchestrated
	// selection if one exists. The selection lives in the store (not local state) so it
	// survives navigation without any sync effect.
	const reorchestratedSelection = subAgentNodeId ? (selectedSubagentsByAgent[subAgentNodeId] ?? null) : null

	const visibleSubAgents = useMemo(() => {
		const afterRemoval = orderedSubAgents.filter((sa) => !removedIds.has(sa.id))
		if (!reorchestratedSelection) return afterRemoval
		const positionMap = new Map(reorchestratedSelection.map((id, i) => [id, i]))

		// Newly selected IDs absent from orderedSubAgents — look them up from registry/static
		const existingIds = new Set(orderedSubAgents.map((sa) => sa.id))
		const baseId = subAgentNodeId?.split(':')[0] ?? ''
		const regAgent = agentRegistry.find((a) => a.id === baseId || a.id === subAgentNodeId)
		const registryById = new Map((regAgent?.subagents ?? []).map((sa) => [sa.id, sa]))
		const staticById = new Map((staticAgent?.subAgents ?? []).map((sa) => [sa.id, sa]))
		const newSubs: SubAgentDef[] = reorchestratedSelection
			.filter((id) => !existingIds.has(id))
			.map((id) => {
				const reg = registryById.get(id)
				const st = staticById.get(id)
				if (!reg && !st) return null
				return {
					id,
					label: reg?.label ?? st?.label ?? id,
					description: reg?.description ?? st?.description ?? '',
					active: true,
					capabilities: st?.capabilities ?? [],
					tasks: st?.tasks,
					taskEdges: st?.taskEdges,
					terminal: st?.terminal,
				}
			})
			.filter((s) => s !== null) as SubAgentDef[]

		return [...afterRemoval, ...newSubs]
			.filter((sa) => sa.terminal || positionMap.has(sa.id) || manuallyAddedIds.has(sa.id))
			.sort((a, b) => {
				if (a.terminal && b.terminal) return 0
				if (a.terminal) return 1
				if (b.terminal) return -1
				return (positionMap.get(a.id) ?? Infinity) - (positionMap.get(b.id) ?? Infinity)
			})
	}, [orderedSubAgents, removedIds, reorchestratedSelection, agentRegistry, staticAgent, subAgentNodeId, manuallyAddedIds])

	const sidebarSubAgents = useMemo(
		() => visibleSubAgents.filter((sa) => !sa.terminal),
		[visibleSubAgents],
	)

	const activeCount = sidebarSubAgents.filter((s) => s.active).length

	// Registry subagents for this agent that aren't currently in the pipeline
	const remainingRegistrySubAgents = useMemo<RegistrySubAgent[]>(() => {
		if (!subAgentNodeId) return []
		const baseId = subAgentNodeId.split(':')[0]
		const registryAgent = agentRegistry.find((a) => a.id === baseId || a.id === subAgentNodeId)
		if (!registryAgent) return []
		const activeIds = new Set(visibleSubAgents.map((sa) => sa.id))
		return registryAgent.subagents.filter((sa) => !activeIds.has(sa.id))
	}, [agentRegistry, subAgentNodeId, visibleSubAgents])

	// ── Task overrides ────────────────────────────────────────────────────────
	const [taskOverrides, setTaskOverrides] = useState<Record<string, TaskDef[]>>({})
	const [taskEdgeOverrides, setTaskEdgeOverrides] = useState<Record<string, TaskEdgeDef[]>>({})

	// Re-seed from the backend pipeline whenever it changes (e.g. a task-level
	// reorchestration merged new tasks/conditions in). The [subAgentNodeId] seed
	// effect above only fires on node open, so without this the editor keeps showing
	// the pre-reorchestration snapshot and a freshly-added condition never appears.
	useEffect(() => {
		setTaskOverrides(backendTasksMap)
		setTaskEdgeOverrides(backendEdgesMap)
	}, [backendTasksMap, backendEdgesMap])

	const handleTasksChange = useCallback((subAgentId: string, tasks: TaskDef[]) => {
		setTaskOverrides((prev) => ({ ...prev, [subAgentId]: tasks }))
		setOrderedSubAgents((prev) => prev.map((sa) => sa.id === subAgentId ? { ...sa, tasks } : sa))
	}, [])

	// Sync current sub-agent configuration into the global store so confirmAndExecute
	// can pass overrides to the backend.
	useEffect(() => {
		if (!subAgentNodeId) return
		const overrides: SubAgentOverride[] = sidebarSubAgents.map((sa) => ({
			id: sa.id,
			label: sa.label,
			active: sa.active,
			tasks: (taskOverrides[sa.id] ?? sa.tasks ?? []).map((t) => t.label),
		}))
		saveAgentOverride(subAgentNodeId, overrides)
	}, [visibleSubAgents, taskOverrides]) // eslint-disable-line

	const getEffectiveTasks = useCallback(
		(sa: SubAgentDef) => {
			const tasks = liveTasksMap[sa.id] ?? taskOverrides[sa.id] ?? sa.tasks ?? []
			const selectedIds = selectedTasksBySubagent[sa.id]
			if (selectedIds === undefined) return tasks
			if (selectedIds.length === 0) return []
			// Build a lookup from current tasks, augmented by registry for any IDs
			// that weren't in the original pipeline (added by reorchestration).
			const taskById = new Map<string, TaskDef>(
				[...(registryTasksBySubagent[sa.id] ?? []), ...tasks].map((t) => [t.id, t])
			)
			return selectedIds.flatMap((id) => {
				const t = taskById.get(id)
				return t ? [t] : []
			})
		},
		[liveTasksMap, taskOverrides, selectedTasksBySubagent, registryTasksBySubagent],
	)

	// ── Sub-agent operations ──────────────────────────────────────────────────
	const handleRemoveSubAgent = useCallback((subAgentId: string) => {
		setRemovedIds((prev) => new Set([...prev, subAgentId]))
	}, [])

	const sidebarSensors = useSensors(
		useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
		useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
	)

	function handleSubAgentReorder(event: DragEndEvent) {
		const { active, over } = event
		if (!over || active.id === over.id) return
		// Reorder within the full ordered list (not just visible) to preserve removed positions
		const oldIndex = orderedSubAgents.findIndex((sa) => sa.id === active.id)
		const newIndex = orderedSubAgents.findIndex((sa) => sa.id === over.id)
		setOrderedSubAgents((prev) => arrayMove(prev, oldIndex, newIndex))
	}

	// ── Task editor navigation ────────────────────────────────────────────────
	const [taskEditorId, setTaskEditorId] = useState<string | null>(null)

	// ── Catalog / add state ───────────────────────────────────────────────────
	const [showCatalog, setShowCatalog] = useState(false)

	function addFromRegistry(rsa: RegistrySubAgent) {
		const existingIndex = orderedSubAgents.findIndex((sa) => sa.id === rsa.id)
		if (existingIndex >= 0) {
			// Was removed — restore it
			setRemovedIds((prev) => { const next = new Set(prev); next.delete(rsa.id); return next })
		} else {
			const newSa: SubAgentDef = {
				id: rsa.id,
				label: rsa.label,
				description: rsa.description,
				active: true,
				capabilities: rsa.capabilities,
				tasks: rsa.tasks.map((t) => ({ id: t.id, label: t.label })),
			}
			setOrderedSubAgents((prev) => [...prev, newSa])
		}
		// Mark as manually added so it isn't filtered out by a prior reorchestration selection
		setManuallyAddedIds((prev) => new Set([...prev, rsa.id]))
		setShowCatalog(false)
	}

	// ── Modify sub-agents ────────────────────────────────────────────────────
	async function sendModify() {
		const text = modifyText.trim()
		if (!text || reorchestrateLoading || !subAgentNodeId) return
		setModifyText('')
		setModifyMessages((prev) => [...prev, { role: 'user', text }])
		await reorchestrateWithFeedback(text, subAgentNodeId)
		setModifyMessages((prev) => [...prev, { role: 'system', text: 'Re-orchestrated' }])
	}

	// ── Canvas ────────────────────────────────────────────────────────────────
	const { initialNodes, initialEdges } = useMemo(() => {
		const visibleIds = new Set(visibleSubAgents.map((sa) => sa.id))

		const reorchestratedEdges = subAgentNodeId ? (reorchestratedEdgesByAgent[subAgentNodeId] ?? null) : null
		const sourceEdges = reorchestratedEdges ?? backendAgent?.sub_agent_edges ?? []
		const agentEdges = sourceEdges.filter((e) => visibleIds.has(e.source) && visibleIds.has(e.target))
		const edgeDefs = agentEdges.length > 0
			? agentEdges
			: visibleSubAgents.slice(0, -1).map((sa, i) => ({
				source: sa.id,
				target: visibleSubAgents[i + 1].id,
			}))

		const canvasSubAgents = visibleSubAgents

		const nodeDefs = canvasSubAgents.map((sa) => ({ id: sa.id, agentId: sa.id }))
		const layout = computeLayout(nodeDefs, edgeDefs)

		const nodes = layout.nodes.map((n) => {
			if (n.type === 'decisionNode') return n
			if (n.type === 'terminalNode') return n  // layout-injected stop node — already fully formed
			const sa = canvasSubAgents.find((s) => s.id === n.id)!
			if (sa?.terminal) return { ...n, type: 'terminalNode', data: { label: sa.label } }
			const index = visibleSubAgents.filter((s) => !s.terminal).indexOf(sa)
			return { ...n, type: 'subAgentNode', data: { sub: sa, color: agent?.color ?? '#94a3b8', index } }
		})

		const edges = layout.edges.map((e) => {
			const branch = e.data?.isDecisionBranch as string | null
			const branchColor = branch === 'yes' ? '#16a34a' : branch === 'no' ? '#dc2626' : null
			// Non-branch edges may still carry a gate (e.g. "icu_available > 0"); computeLayout
			// stamps it onto e.data.condition_label. Surface it as the edge label.
			const condLabel = !branch ? (e.data?.condition_label as string | null) : null
			const label = branch ? (branch === 'yes' ? 'YES' : 'NO') : (condLabel ?? undefined)
			return {
				...e,
				type: 'smoothstep',
				label,
				labelStyle: branch
					? { fill: branch === 'yes' ? '#4ade80' : '#f87171', fontWeight: 700, fontSize: 14 }
					: condLabel
						? { fill: '#94a3b8', fontWeight: 600, fontSize: 11 }
						: undefined,
				labelBgStyle: (branch || condLabel) ? { fill: '#0f172a', fillOpacity: 0.85 } : undefined,
				labelBgPadding: (branch || condLabel) ? [4, 2] as [number, number] : undefined,
				style: condLabel
					? { stroke: '#475569', strokeWidth: 1.5, strokeDasharray: '5,4' }
					: { stroke: branchColor ?? '#2d4a7a', strokeWidth: 1.5, strokeDasharray: '6,3' },
				markerEnd: { type: MarkerType.ArrowClosed, color: branchColor ?? '#2d4a7a', width: 20, height: 20 },
			}
		})

		return { initialNodes: nodes, initialEdges: edges }
	}, [visibleSubAgents, backendAgent, reorchestratedEdgesByAgent, subAgentNodeId]) // eslint-disable-line

	const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes)
	const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges)
	const rfInstanceRef = useRef<{ fitView: (opts?: { padding?: number }) => void } | null>(null)

	useEffect(() => {
		setNodes(initialNodes)
		setEdges(initialEdges)
		setTimeout(() => {
			requestAnimationFrame(() => rfInstanceRef.current?.fitView({ padding: 0.4 }))
		}, 30)
	}, [visibleSubAgents, backendAgent, reorchestratedEdgesByAgent, subAgentNodeId]) // eslint-disable-line

	const onConnect = useCallback(
		(params: Connection) =>
			setEdges((eds) =>
				addEdge({ ...params, type: 'smoothstep', style: { stroke: '#2d4a7a', strokeWidth: 1.5, strokeDasharray: '6,3' }, markerEnd: { type: MarkerType.ArrowClosed, color: '#2d4a7a', width: 20, height: 20 } }, eds)
			),
		[setEdges],
	)

	// ── Context value ─────────────────────────────────────────────────────────
	const handleOpenTaskEditor = useCallback((id: string) => setTaskEditorId(id), [])

	const contextValue = useMemo<SubAgentContextValue>(
		() => ({
			taskOverrides,
			selectedTasksBySubagent,
			registryTasksBySubagent,
			onTasksChange: handleTasksChange,
			onRemoveSubAgent: handleRemoveSubAgent,
			onOpenTaskEditor: handleOpenTaskEditor,
		}),
		[taskOverrides, selectedTasksBySubagent, registryTasksBySubagent, handleTasksChange, handleRemoveSubAgent, handleOpenTaskEditor],
	)

	if (!subAgentNodeId || !agent) return null

	// ── Task editor screen ────────────────────────────────────────────────────
	const taskEditorSA = taskEditorId ? sidebarSubAgents.find((sa) => sa.id === taskEditorId) : null
	if (taskEditorSA) {
		return (
			<div className="flex-1 flex flex-col overflow-hidden bg-[var(--bg-base)]">
				<TaskEditorView
					subAgent={taskEditorSA}
					agentLabel={agent.label}
					agentColor={agent.color}
					tasks={getEffectiveTasks(taskEditorSA)}
					taskEdges={
						liveTasksMap[taskEditorSA.id]
							? undefined  // live plan IDs won't match static edges → linear layout
							: taskEdgeOverrides[taskEditorSA.id] ?? backendEdgesMap[taskEditorSA.id] ?? taskEditorSA.taskEdges
					}
					taskConditions={taskConditions}
					onTasksChange={(tasks) => handleTasksChange(taskEditorSA.id, tasks)}
					onEdgesChange={(edges) => setTaskEdgeOverrides((prev) => ({ ...prev, [taskEditorSA.id]: edges }))}
					onBack={() => setTaskEditorId(null)}
					onReorchestrate={(feedback) => reorchestrateWithFeedback(feedback, subAgentNodeId, taskEditorSA.id)}
					reorchestrateLoading={reorchestrateLoading}
				/>
			</div>
		)
	}

	return (
		<SubAgentContext.Provider value={contextValue}>
			<div className="flex-1 flex flex-col overflow-hidden bg-[var(--bg-base)]">
				{/* Header */}
				<div className="flex items-center justify-between px-5 py-2.5 border-b border-[var(--border)] bg-[var(--bg-surface)] flex-shrink-0">
					<div className="flex items-center gap-3">
						<button
							onClick={closeSubAgent}
							className="flex items-center gap-1.5 text-xs text-slate-400 hover:text-slate-200 transition-colors"
						>
							<ArrowLeft size={14} />
							Back to Pipeline
						</button>
						<div className="w-px h-4 bg-[var(--border-a)]" />
						<span className="text-base">{agent.emoji}</span>
						<div>
							<div className="text-base font-bold text-slate-100 leading-tight">{agent.label}</div>
							<div className="text-sm text-slate-600 leading-tight">{agent.description}</div>
						</div>
					</div>
				</div>

				<div className="flex flex-1 overflow-hidden">
					{/* Left sidebar */}
					<div className="w-56 xl:w-64 2xl:w-72 flex-shrink-0 border-r border-[var(--border)] flex flex-col overflow-hidden">
						<div className="flex-1 overflow-y-auto">
							{promptText && (
							<div className="px-4 pt-3 pb-2.5 border-b border-[var(--border)]">
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
						<div className="px-4 pt-3 pb-2 flex items-center justify-between">
							<span className="text-xs font-bold text-slate-500 uppercase tracking-widest">Sub-Agents</span>
							<span className="text-xs font-semibold px-1.5 py-0.5 rounded bg-teal-900/40 text-teal-400 border border-teal-700/40">
								{activeCount} active
							</span>
						</div>

							{/* Sortable sub-agent list */}
							<div className="flex flex-col gap-0.5 px-2 pb-2">
								<DndContext
									sensors={sidebarSensors}
									collisionDetection={closestCenter}
									onDragEnd={handleSubAgentReorder}
								>
									<SortableContext
										items={sidebarSubAgents.map((sa) => sa.id)}
										strategy={verticalListSortingStrategy}
									>
										{sidebarSubAgents.map((sa, i) => (
											<SortableSidebarItem
												key={sa.id}
												sa={sa}
												index={i}
												agentColor={agent.color}
												taskCount={getEffectiveTasks(sa).length}
												onOpen={() => setTaskEditorId(sa.id)}
												onRemove={() => handleRemoveSubAgent(sa.id)}
											/>
										))}
									</SortableContext>
								</DndContext>
							</div>

							{/* Add sub-agent */}
							<div className="px-3 pb-3">
								<button
									onClick={() => setShowCatalog(!showCatalog)}
									className="flex items-center gap-1.5 text-xs text-teal-500 hover:text-teal-400 transition-colors"
								>
									<Plus size={12} />
									Add Sub-agent
								</button>

								{showCatalog && (
									<div className="mt-2 bg-[var(--bg-surface)] border border-[var(--border-a)] rounded-xl p-3">
										<div className="flex items-center justify-between mb-2">
											<span className="text-xs font-semibold text-slate-400">Add Sub-agent</span>
											<button onClick={() => setShowCatalog(false)} className="text-slate-600 hover:text-slate-400">
												<X size={12} />
											</button>
										</div>

										{remainingRegistrySubAgents.length > 0 ? (
											<div className="flex flex-col gap-0.5 max-h-36 overflow-y-auto">
												{remainingRegistrySubAgents.map((rsa) => (
													<button
														key={rsa.id}
														onClick={() => addFromRegistry(rsa)}
														className="flex items-start gap-2 px-2 py-1.5 rounded-lg hover:bg-[var(--bg-hover)] text-left transition-colors group/reg"
													>
														<Plus size={10} className="text-teal-600 flex-shrink-0 mt-0.5 opacity-0 group-hover/reg:opacity-100 transition-opacity" />
														<div className="min-w-0">
															<div className="text-xs text-slate-300 font-medium truncate">{rsa.label}</div>
															{rsa.description && (
																<div className="text-xs text-slate-600 truncate">{rsa.description}</div>
															)}
														</div>
													</button>
												))}
											</div>
										) : (
											<p className="text-xs text-slate-600 italic">All sub-agents are already in the pipeline.</p>
										)}
									</div>
								)}
							</div>
						</div>

						{/* Modify sub-agents section — editing only, mirrors PipelineCanvas's isEditing gate */}
						{isEditing && (
							<div className="border-t border-[var(--border)] px-3 py-3 flex flex-col gap-2">
								<label className="text-[11px] font-semibold text-slate-600 uppercase tracking-wider flex items-center gap-1.5">
									<RefreshCw size={10} />
									Modify sub-agents
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
									placeholder="Describe changes to sub-agent selection…"
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

					{/* Canvas */}
					<div className="flex-1 relative min-w-0">
						{/* Re-orchestrate button — top left of canvas, editing only */}
						{isEditing && (
							<div className="absolute top-4 left-4 z-10">
								<button
									onClick={async () => {
										if (reorchestrateLoading || !subAgentNodeId) return
										await reorchestrateWithFeedback(undefined, subAgentNodeId)
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
									<span className="text-xs text-blue-300 font-semibold">Re-orchestrating sub-agents…</span>
								</div>
							</div>
						)}
						<div className="absolute inset-0">
							{visibleSubAgents.length === 0 ? (
								<div className="flex items-center justify-center h-full text-sm text-slate-600 italic">
									No sub-agents — use "Add Sub-agent" to create one
								</div>
							) : (
								<ReactFlow
									key={subAgentNodeId}
									nodes={nodes}
									edges={edges}
									nodeTypes={subNodeTypes}
									onNodesChange={onNodesChange}
									onEdgesChange={onEdgesChange}
									onConnect={onConnect}
									onNodeClick={(_e, node) => {
										if (node.type === 'subAgentNode') {
											const sa = node.data?.sub as SubAgentDef | undefined
											if (sa) setTaskEditorId(sa.id)
										}
									}}
									onInit={(instance) => {
										rfInstanceRef.current = instance
										requestAnimationFrame(() =>
											requestAnimationFrame(() => instance.fitView({ padding: 0.4 }))
										)
									}}
									minZoom={0.15}
									maxZoom={2}
									proOptions={{ hideAttribution: true }}
								>
									<Background variant={BackgroundVariant.Dots} gap={20} size={1} color="#0d1f3c" />
									<Controls showInteractive={false} className="!bottom-4 !right-4" />
								</ReactFlow>
							)}
						</div>
					</div>
				</div>
			</div>
		</SubAgentContext.Provider>
	)
}
