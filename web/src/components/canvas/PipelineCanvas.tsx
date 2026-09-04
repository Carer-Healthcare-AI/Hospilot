import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
	ReactFlow,
	Background,
	Controls,
	MiniMap,
	BackgroundVariant,
	MarkerType,
	useNodesState,
	useEdgesState,
	addEdge,
	type Connection,
	type Edge,
	type Node,
	type ReactFlowInstance,
	type NodeChange,
	type EdgeChange,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { Plus, Play, Loader2, Zap, ArrowLeftRight, CheckCircle, RefreshCw, Eye, X, AlertTriangle, Pause, PauseCircle, Ban, Pencil, ShieldAlert, ChevronRight } from 'lucide-react'
import { AgentNode } from './AgentNode'
import { DecisionNode } from './DecisionNode'
import { TerminalNode } from '../subagent/TerminalNode'
import { AgentPalette } from './AgentPalette'
import { InsertableEdge } from './InsertableEdge'
import { CoordinatingLoader } from './CoordinatingLoader'
import { PlanningThinkingText } from './PlanningThinkingText'
import { useStore } from '../../store'
import { AGENTS } from '../../data/agents'
import { CONDITION_LABELS } from '../../lib/layout'
import { computeWaves, buildCheckpointGroupNodes } from '../../lib/waves'
import { CheckpointGroupNode } from './CheckpointGroupNode'

const CONDITIONS = Object.entries(CONDITION_LABELS).map(([key, label]) => ({ key, label }))

type PickerState = { rawSource: string; rawTarget: string; currentCondition: string | null; x: number; y: number }

const nodeTypes = {
	agentNode: AgentNode,
	decisionNode: DecisionNode,
	terminalNode: TerminalNode,
	checkpointGroup: CheckpointGroupNode,
}
const edgeTypes = { insertable: InsertableEdge }

const EDGE_STYLE = { stroke: '#2d4a7a', strokeWidth: 1.5, strokeDasharray: '6,3' }
const EDGE_MARKER = { type: MarkerType.ArrowClosed, color: '#2d4a7a', width: 20, height: 20 }
const NODE_WIDTH = 300

// X-range based edge detection: finds the edge whose source–target gap contains the drop X
function findEdgeByXRange(
	pos: { x: number; y: number },
	nodes: Node[],
	edges: Edge[],
): Edge | null {
	const nodeMap = new Map(nodes.map((n) => [n.id, n]))

	for (const edge of edges) {
		const src = nodeMap.get(edge.source)
		const tgt = nodeMap.get(edge.target)
		if (!src || !tgt) continue

		const srcRight = src.position.x + NODE_WIDTH
		const tgtLeft = tgt.position.x

		if (pos.x > srcRight && pos.x < tgtLeft) {
			const midY = (src.position.y + 95 + tgt.position.y + 95) / 2
			if (Math.abs(pos.y - midY) < 200) {
				return edge
			}
		}
	}
	return null
}

export function PipelineCanvas() {
	const storeNodes = useStore((s) => s.nodes)
	const storeEdges = useStore((s) => s.edges)
	const setStoreNodes = useStore((s) => s.setNodes)
	const setStoreEdges = useStore((s) => s.setEdges)
	const executionStatus = useStore((s) => s.executionStatus)
	const executionMode = useStore((s) => s.executionMode)
	const pendingApprovals = useStore((s) => s.pendingApprovals)
	const approvalMinimized = useStore((s) => s.approvalMinimized)
	const setApprovalMinimized = useStore((s) => s.setApprovalMinimized)
	const pipelineGenerated = useStore((s) => s.pipelineGenerated)
	const pipelineLoading = useStore((s) => s.pipelineLoading)
	const pipelineError = useStore((s) => s.pipelineError)
	const startExecution = useStore((s) => s.startExecution)
	const reorchestrateLoading = useStore((s) => s.reorchestrateLoading)
	const reorchestrateWithFeedback = useStore((s) => s.reorchestrateWithFeedback)
	const sessionId = useStore((s) => s.sessionId)
	const confirmAndExecute = useStore((s) => s.confirmAndExecute)
	const pauseFlow = useStore((s) => s.pauseFlow)
	const resumeFlow = useStore((s) => s.resumeFlow)
	const cancelFlow = useStore((s) => s.cancelFlow)
	const openCheckpointEditor = useStore((s) => s.openCheckpointEditor)
	const generatePipeline = useStore((s) => s.generatePipeline)
	const updateEdgeCondition = useStore((s) => s.updateEdgeCondition)
	const syncRawEdge = useStore((s) => s.syncRawEdge)
	const removeRawEdge = useStore((s) => s.removeRawEdge)
	const removeRawEdgesForNodes = useStore((s) => s.removeRawEdgesForNodes)
	const triggerPipelineSave = useStore((s) => s.triggerPipelineSave)
	const rawEdgeDefs = useStore((s) => s.rawEdgeDefs)
	const backendPipeline = useStore((s) => s.backendPipeline)
	const nodeStates = useStore((s) => s.nodeStates)

	const [nodes, setNodes, onNodesChange] = useNodesState(
		storeNodes.map((n) =>
			n.type === 'decisionNode' ? { ...n, style: { ...n.style, width: 260, height: 260 } } : n
		)
	)
	const [edges, setEdges, onEdgesChange] = useEdgesState(storeEdges)
	const [paletteOpen, setPaletteOpen] = useState(false)
	const [selectedIds, setSelectedIds] = useState<string[]>([])
	const [insertingEdge, setInsertingEdge] = useState<{ id: string; screenX: number; screenY: number } | null>(null)
	const [conditionPicker, setConditionPicker] = useState<PickerState | null>(null)
	const rfRef = useRef<ReactFlowInstance | null>(null)
	const dragStartPosRef = useRef<{ x: number; y: number } | null>(null)
	const nodesRef = useRef<Node[]>([])

	useEffect(() => {
		const currentPosById = new Map(nodes.map((n) => [n.id, n.position]))
		setNodes(storeNodes.map((n) => {
			const styled = n.type === 'decisionNode' ? { ...n, style: { ...n.style, width: 260, height: 260 } } : n
			const existingPos = currentPosById.get(n.id)
			return existingPos ? { ...styled, position: existingPos } : styled
		}))
		setEdges(storeEdges)
	}, [storeNodes, storeEdges]) // eslint-disable-line

	// Keep a stable ref to latest nodes for use inside setTimeout callbacks
	useEffect(() => {
		nodesRef.current = nodes
	}, [nodes])

	const syncNodes = useCallback(
		(updater: Node[] | ((ns: Node[]) => Node[])) => {
			const next = typeof updater === 'function' ? updater(nodes) : updater
			setNodes(next)
			setStoreNodes(next)
		},
		[nodes, setNodes, setStoreNodes]
	)

	const syncEdges = useCallback(
		(updater: Edge[] | ((es: Edge[]) => Edge[])) => {
			const next = typeof updater === 'function' ? updater(edges) : updater
			setEdges(next)
			setStoreEdges(next)
		},
		[edges, setEdges, setStoreEdges]
	)

	const onInsertClick = useCallback(
		(edgeId: string, screenX: number, screenY: number) => {
			setInsertingEdge({ id: edgeId, screenX, screenY })
		},
		[]
	)

	// ── Change handlers that also sync removes to store ───────────────────────
	// Checkpoint-group boxes live only in displayNodes (never in the store-backed
	// `nodes`), so any change event referencing one (e.g. a dimension measurement)
	// is dropped here before it can reach useNodesState/the store.
	const isCheckpointGroupId = (id: string) => id.startsWith('ckpt-group-')

	const handleNodesChange = useCallback(
		(changes: NodeChange[]) => {
			const realChanges = changes.filter((c) => !isCheckpointGroupId((c as { id: string }).id))
			onNodesChange(realChanges)
			const removedIds = new Set(
				realChanges.filter((c) => c.type === 'remove').map((c) => (c as { id: string }).id)
			)
			if (removedIds.size > 0) {
				setStoreNodes(nodes.filter((n) => !removedIds.has(n.id)))
				setStoreEdges(
					edges.filter((e) => !removedIds.has(e.source) && !removedIds.has(e.target))
				)
				removeRawEdgesForNodes([...removedIds])
				triggerPipelineSave()
			}
		},
		[onNodesChange, nodes, edges, setStoreNodes, setStoreEdges, removeRawEdgesForNodes, triggerPipelineSave]
	)

	const handleLiveNodesChange = useCallback(
		(changes: NodeChange[]) => {
			onNodesChange(changes.filter((c) => !isCheckpointGroupId((c as { id: string }).id)))
		},
		[onNodesChange]
	)

	const handleEdgesChange = useCallback(
		(changes: EdgeChange[]) => {
			onEdgesChange(changes)
			const removedIds = new Set(
				changes.filter((c) => c.type === 'remove').map((c) => (c as { id: string }).id)
			)
			if (removedIds.size > 0) {
				const removedEdges = edges.filter((e) => removedIds.has(e.id))
				setStoreEdges(edges.filter((e) => !removedIds.has(e.id)))
				for (const e of removedEdges) removeRawEdge(e.source, e.target)
				triggerPipelineSave()
			}
		},
		[onEdgesChange, edges, setStoreEdges, removeRawEdge, triggerPipelineSave]
	)

	const onConnect = useCallback(
		(params: Connection) => {
			syncEdges((eds) => addEdge({ ...params, type: 'smoothstep', style: EDGE_STYLE }, eds))
			if (params.source && params.target) {
				syncRawEdge(params.source, params.target)
				triggerPipelineSave()
			}
		},
		[syncEdges, syncRawEdge, triggerPipelineSave]
	)

	// ── Drag-to-swap: drag one node onto another to swap agents + positions ───
	const onNodeDragStart = useCallback(
		(_event: MouseEvent | TouchEvent, node: Node) => {
			dragStartPosRef.current = { x: node.position.x, y: node.position.y }
		},
		[]
	)

	const onNodeDragStop = useCallback(
		(_event: MouseEvent | TouchEvent, draggedNode: Node) => {
			const startPos = dragStartPosRef.current
			dragStartPosRef.current = null
			if (!startPos) return

			// Snapshot candidates from latest ref BEFORE the drop settles
			const currentNodes = nodesRef.current
			const other = currentNodes.find((n) => {
				if (n.id === draggedNode.id) return false
				return (
					Math.abs(n.position.x - draggedNode.position.x) < 140 &&
					Math.abs(n.position.y - draggedNode.position.y) < 140
				)
			})

			// Delay so React Flow's final onNodesChange fires first, then we override positions
			setTimeout(() => {
				const latest = nodesRef.current
				if (other) {
					// Swap positions between dragged node and the node it was dropped onto
					const otherPos = { x: other.position.x, y: other.position.y }
					const next = latest.map((n) => {
						if (n.id === draggedNode.id) return { ...n, position: otherPos }
						if (n.id === other.id) return { ...n, position: startPos }
						return n
					})
					setNodes(next)
					setStoreNodes(next)
				} else {
					// Regular drag — persist final positions to store
					setStoreNodes(latest)
				}
			}, 30)
		},
		[setNodes, setStoreNodes]
	)

	// ── Drag-and-drop from palette ───────────────────────────────────────────
	const onDragOver = useCallback((event: React.DragEvent) => {
		event.preventDefault()
		event.dataTransfer.dropEffect = 'move'
	}, [])

	const onDrop = useCallback(
		(event: React.DragEvent) => {
			event.preventDefault()
			const agentId = event.dataTransfer.getData('agentId')
			if (!agentId || !rfRef.current) return

			const flowPos = rfRef.current.screenToFlowPosition({
				x: event.clientX,
				y: event.clientY,
			})

			const newNodeId = `${agentId}-${Date.now()}`
			let newPosition = flowPos

			const nearEdge = findEdgeByXRange(flowPos, nodes, edges)

			if (nearEdge) {
				// Insert between two existing nodes
				const nodeMap = new Map(nodes.map((n) => [n.id, n]))
				const src = nodeMap.get(nearEdge.source)
				const tgt = nodeMap.get(nearEdge.target)
				if (src && tgt) {
					const gap = tgt.position.x - (src.position.x + NODE_WIDTH)
					newPosition = {
						x: src.position.x + NODE_WIDTH + gap / 2 - NODE_WIDTH / 2,
						y: (src.position.y + tgt.position.y) / 2,
					}
				}
				const newEdge1: Edge = {
					id: `e-${nearEdge.source}-${newNodeId}`,
					source: nearEdge.source,
					target: newNodeId,
					type: 'smoothstep',
					style: EDGE_STYLE,
				}
				const newEdge2: Edge = {
					id: `e-${newNodeId}-${nearEdge.target}`,
					source: newNodeId,
					target: nearEdge.target,
					type: 'smoothstep',
					style: EDGE_STYLE,
				}
				syncEdges((eds) => [...eds.filter((e) => e.id !== nearEdge.id), newEdge1, newEdge2])
				removeRawEdge(nearEdge.source, nearEdge.target)
				syncRawEdge(nearEdge.source, newNodeId)
				syncRawEdge(newNodeId, nearEdge.target)
			} else if (nodes.length > 0) {
				// Auto-connect to the rightmost or leftmost node
				const sorted = [...nodes].sort((a, b) => a.position.x - b.position.x)
				const leftmost = sorted[0]
				const rightmost = sorted[sorted.length - 1]

				if (flowPos.x > rightmost.position.x + NODE_WIDTH) {
					syncEdges((eds) => [
						...eds,
						{ id: `e-end-${rightmost.id}-${newNodeId}`, source: rightmost.id, target: newNodeId, type: 'smoothstep', style: EDGE_STYLE },
					])
					syncRawEdge(rightmost.id, newNodeId)
				} else if (flowPos.x < leftmost.position.x) {
					syncEdges((eds) => [
						...eds,
						{ id: `e-start-${newNodeId}-${leftmost.id}`, source: newNodeId, target: leftmost.id, type: 'smoothstep', style: EDGE_STYLE },
					])
					syncRawEdge(newNodeId, leftmost.id)
				}
			}

			const newNode: Node = {
				id: newNodeId,
				type: 'agentNode',
				position: newPosition,
				data: { agentId, nodeId: newNodeId },
			}
			syncNodes((ns) => [...ns, newNode])
			triggerPipelineSave()
		},
		[nodes, edges, syncNodes, syncEdges, triggerPipelineSave]
	)

	// ── Swap two selected agents ──────────────────────────────────────────────
	function swapSelected() {
		if (selectedIds.length !== 2) return
		const [id1, id2] = selectedIds
		syncNodes((ns) =>
			ns.map((n) => {
				if (n.id === id1) {
					const other = ns.find((x) => x.id === id2)!
					return { ...n, data: { ...n.data, agentId: (other.data as { agentId: string }).agentId } }
				}
				if (n.id === id2) {
					const other = ns.find((x) => x.id === id1)!
					return { ...n, data: { ...n.data, agentId: (other.data as { agentId: string }).agentId } }
				}
				return n
			})
		)
	}

	const isEditing = pipelineGenerated && executionStatus === 'idle'
	const isRunning = executionStatus === 'running' || executionStatus === 'waiting_approval'
		|| executionStatus === 'pausing' || executionStatus === 'paused'
	const isCompletePending = executionStatus === 'complete_pending'
	const isSubmitted = executionStatus === 'submitted'
	const isAdvisory = executionMode === 'advisory'

	// ── Checkpoint boxes (autonomous mode only) ───────────────────────────────
	// Passive dashed boxes drawn behind each wave of agents that has fully
	// completed -- a live, client-derived approximation of "here's a natural
	// checkpoint boundary," not a guarantee that the backend has a real rewindable
	// checkpoint at exactly this line (that's what Edit Checkpoint's stepper is
	// for, sourced from the real GET /checkpoints history). Derived from the same
	// wave computation as CheckpointEditorScreen, but driven by live nodeStates so
	// it updates in real time as agents complete -- no extra API calls.
	const checkpointGroupNodes = useMemo(() => {
		if (executionMode !== 'autonomous' || !backendPipeline) return []
		const waves = computeWaves(backendPipeline)

		// Only a leading run of fully-completed waves counts -- stop at the first
		// wave that isn't done yet (an in-progress wave never gets a box).
		const boxedWaves: string[][] = []
		for (const wave of waves) {
			if (!wave.every((id) => nodeStates[id]?.status === 'complete')) break
			boxedWaves.push(wave)
		}
		return buildCheckpointGroupNodes(boxedWaves, nodes, (i) => `${i + 1}`)
	}, [executionMode, backendPipeline, nodeStates, nodes])

	// Group boxes are purely derived for display -- never enter the store's
	// persisted nodes, and their ids are filtered out of any change handler below
	// so a stray dimension-measurement event can't touch real pipeline state.
	const displayNodes = useMemo(
		() => (checkpointGroupNodes.length ? [...checkpointGroupNodes, ...nodes] : nodes),
		[checkpointGroupNodes, nodes]
	)

	// ── Edge condition picker ─────────────────────────────────────────────────
	const handleEdgeClick = useCallback(
		(_evt: React.MouseEvent, edge: Edge) => {
			if (!isEditing) return
			if (edge.target.startsWith('vd_')) return  // skip agent→diamond connector

			const rawSource = edge.source.startsWith('vd_') ? edge.source.slice(3) : edge.source
			const rawTarget = edge.target
			const rawEdge = rawEdgeDefs.find((e) => e.source === rawSource && e.target === rawTarget)
			const currentCondition = rawEdge?.condition ?? null

			setConditionPicker({ rawSource, rawTarget, currentCondition, x: _evt.clientX, y: _evt.clientY })
		},
		[isEditing, rawEdgeDefs]
	)

	useEffect(() => {
		if (!conditionPicker) return
		const close = () => setConditionPicker(null)
		window.addEventListener('mousedown', close)
		return () => window.removeEventListener('mousedown', close)
	}, [conditionPicker])

	const edgesWithInsert = useMemo(
		() => edges.map((e) => ({
			...e,
			type: 'insertable' as const,
			markerEnd: EDGE_MARKER,
			data: { ...(e.data ?? {}), isEditing, onInsert: onInsertClick },
		})),
		[edges, isEditing, onInsertClick]
	)

	function insertAgent(agentId: string) {
		if (!insertingEdge) return
		const edge = edges.find((e) => e.id === insertingEdge.id)
		if (!edge) return

		const nodeMap = new Map(nodes.map((n) => [n.id, n]))
		const src = nodeMap.get(edge.source)
		const tgt = nodeMap.get(edge.target)

		const newNodeId = `${agentId}-${Date.now()}`
		const newPosition = src && tgt
			? {
				x: src.position.x + NODE_WIDTH + (tgt.position.x - src.position.x - NODE_WIDTH) / 2 - NODE_WIDTH / 2,
				y: (src.position.y + tgt.position.y) / 2,
			}
			: { x: 0, y: 0 }

		syncEdges((eds) => [
			...eds.filter((e) => e.id !== edge.id),
			{ id: `e-${edge.source}-${newNodeId}`, source: edge.source, target: newNodeId, type: 'smoothstep', style: EDGE_STYLE },
			{ id: `e-${newNodeId}-${edge.target}`, source: newNodeId, target: edge.target, type: 'smoothstep', style: EDGE_STYLE },
		])
		syncNodes((ns) => [...ns, {
			id: newNodeId, type: 'agentNode', position: newPosition,
			data: { agentId, nodeId: newNodeId },
		}])
		// Keep rawEdgeDefs in sync: replace split edge with two new edges
		removeRawEdge(edge.source, edge.target)
		syncRawEdge(edge.source, newNodeId)
		syncRawEdge(newNodeId, edge.target)
		setInsertingEdge(null)
		triggerPipelineSave()
	}

	// ── Empty / loading state ─────────────────────────────────────────────────
	if (!pipelineGenerated) {
		return (
			<div className="w-full h-full flex flex-col items-center justify-center bg-[var(--bg-base)] relative">
				<div
					className="absolute inset-0 pointer-events-none"
					style={{
						backgroundImage: 'radial-gradient(circle, var(--border) 1px, transparent 1px)',
						backgroundSize: '24px 24px',
					}}
				/>
				<div className="relative flex flex-col items-center gap-4 text-center px-8">
					{pipelineLoading ? (
						<>
							<CoordinatingLoader />
							<PlanningThinkingText />
						</>
					) : pipelineError ? (
						<>
							<div className="w-16 h-16 rounded-2xl bg-red-600/10 border border-red-600/30 flex items-center justify-center">
								<AlertTriangle size={28} className="text-red-400" />
							</div>
							<div>
								<div className="text-base font-semibold text-red-300">Couldn't Generate Workflow</div>
								<div className="text-xs text-slate-500 mt-1 max-w-sm">{pipelineError}</div>
							</div>
							<button
								onClick={generatePipeline}
								className="flex items-center gap-2 px-4 py-2 rounded-xl bg-red-600/15 border border-red-600/40 text-red-300 text-sm font-semibold hover:bg-red-600/25 transition-colors mt-1"
							>
								<RefreshCw size={14} />
								Retry
							</button>
						</>
					) : (
						<>
							<div className="w-16 h-16 rounded-2xl bg-[var(--bg-surface)] border border-[var(--border-a)] flex items-center justify-center">
								<Zap size={28} className="text-slate-600" />
							</div>
							<div>
								<div className="text-base font-semibold text-slate-400">No Pipeline Yet</div>
								<div className="text-xs text-slate-600 mt-1">
									Fill in the prompt and click <span className="text-blue-400">Run Mission</span> to generate the agent workflow
								</div>
							</div>
						</>
					)}
				</div>
			</div>
		)
	}

	// ── Pipeline canvas ───────────────────────────────────────────────────────
	return (
		<div
			className="relative w-full h-full"
			onDrop={isEditing ? onDrop : undefined}
			onDragOver={isEditing ? onDragOver : undefined}
		>
			<ReactFlow
				nodes={displayNodes}
				edges={edgesWithInsert as Edge[]}
				nodeTypes={nodeTypes}
				edgeTypes={edgeTypes}
				onNodesChange={isEditing ? handleNodesChange : handleLiveNodesChange}
				onEdgesChange={isEditing ? handleEdgesChange : undefined}
				onConnect={isEditing ? onConnect : undefined}
				onNodeDragStart={isEditing ? onNodeDragStart : undefined}
				onNodeDragStop={isEditing ? onNodeDragStop : undefined}
				onEdgeClick={isEditing ? handleEdgeClick : undefined}
				nodesDraggable={!isRunning}
				nodesConnectable={isEditing}
				elementsSelectable={!isRunning}
				deleteKeyCode={['Delete', 'Backspace']}
				onSelectionChange={({ nodes: sel }) => setSelectedIds(sel.map((n) => n.id))}
				onInit={(instance) => { rfRef.current = instance }}
				fitView
				fitViewOptions={{ padding: 0.2, minZoom: 0.5 }}
				minZoom={0.3}
				maxZoom={2}
				proOptions={{ hideAttribution: true }}
			>
				<Background variant={BackgroundVariant.Dots} gap={24} size={1} color="#0d1f3c" />
				<Controls showInteractive={false} className="!bottom-4 !left-4" />
				<MiniMap
					nodeColor={() => '#1e3a73'}
					maskColor="rgba(6,11,24,0.75)"
					className="!bottom-4 !right-4 !bg-[var(--bg-surface)] !border-[var(--border-a)]"
				/>
			</ReactFlow>

			{/* Minimized-approval pill — shown when the canvas approval modal has been
			    collapsed so the user can keep working while it awaits a decision. Clicking
			    reopens the modal (ApprovalModal returns null while approvalMinimized). Pinned
			    top-left, which is free during execution (Re-orchestrate only shows when editing). */}
			{approvalMinimized && pendingApprovals.length > 0 && (
				<button
					onClick={() => setApprovalMinimized(false)}
					title="Reopen the pending approval"
					className="absolute top-4 left-4 z-20 flex items-center gap-2 pl-2.5 pr-3 py-1.5 rounded-lg bg-amber-500/15 border border-amber-500/40 text-amber-300 text-xs font-semibold hover:bg-amber-500/25 transition-colors shadow-lg"
				>
					<span className="relative flex items-center justify-center">
						<ShieldAlert size={14} className="text-amber-400" />
						<span className="absolute inline-flex h-full w-full rounded-full bg-amber-400/40 animate-ping" />
					</span>
					Approval
					<span className="flex items-center justify-center min-w-[18px] h-[18px] px-1 rounded-full bg-amber-500/30 text-amber-200 text-[10px] font-bold">
						{pendingApprovals.length}
					</span>
					<ChevronRight size={13} className="text-amber-400/70" />
				</button>
			)}

			{/* Canvas toolbar — editing state */}
			{isEditing && (
				<>
					{/* Left: Re-orchestrate. With a live session this must reuse it (full replan against
					    the session's audit-log history + prior_plan) rather than minting a brand-new
					    session via generatePipeline() — a new session has no history to replay, so prior
					    "Modify pipeline" revisions would silently revert. generatePipeline() is only the
					    right call when there's no session yet (scenario/simulation flows). */}
					<div className="absolute top-4 left-4 z-10">
						<button
							onClick={() => (sessionId ? reorchestrateWithFeedback() : generatePipeline())}
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

					{/* Right: selection controls + Execute */}
					<div className="absolute top-4 right-4 flex items-center gap-2 z-10">
						{selectedIds.length === 2 && (
							<button
								onClick={swapSelected}
								className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-purple-900/50 border border-purple-700 text-purple-300 text-xs hover:bg-purple-800/60 transition-colors"
							>
								<ArrowLeftRight size={12} />
								Swap
							</button>
						)}
						<button
							onClick={() => setPaletteOpen(!paletteOpen)}
							className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-[var(--bg-raised)] border border-[var(--border-a)] text-slate-300 text-xs hover:bg-[var(--bg-hover)] transition-colors"
						>
							<Plus size={12} />
							Add Agent
						</button>
						<button
							onClick={isAdvisory ? startExecution : confirmAndExecute}
							className={`flex items-center gap-1.5 px-4 py-1.5 rounded-lg text-white text-xs font-semibold transition-colors shadow-lg ${isAdvisory
									? 'bg-blue-600 hover:bg-blue-500 shadow-blue-900/40'
									: 'bg-teal-600 hover:bg-teal-500 shadow-teal-900/40'
								}`}
						>
							{isAdvisory ? <Eye size={12} /> : <Play size={12} />}
							{isAdvisory ? 'Preview' : 'Confirm & Execute'}
						</button>
					</div>
				</>
			)}

			{/* Condition picker */}
			{conditionPicker && (
				<div
					onMouseDown={(e) => e.stopPropagation()}
					style={{
						position: 'fixed',
						left: conditionPicker.x,
						top: conditionPicker.y + 12,
						zIndex: 1000,
						transform: 'translateX(-50%)',
					}}
					className="bg-[#0b1425] border border-slate-700 rounded-xl shadow-2xl overflow-hidden min-w-[230px]"
				>
					<div className="flex items-center justify-between px-3 py-2 border-b border-slate-800">
						<span className="text-[11px] text-slate-400 font-mono tracking-wide uppercase">Edge Condition</span>
						<button onClick={() => setConditionPicker(null)} className="text-slate-600 hover:text-slate-400">
							<X size={12} />
						</button>
					</div>
					<div className="p-1.5 space-y-0.5">
						<button
							onMouseDown={(e) => { e.stopPropagation(); updateEdgeCondition(conditionPicker.rawSource, conditionPicker.rawTarget, null); setConditionPicker(null) }}
							className={`w-full text-left text-xs px-2.5 py-1.5 rounded-lg font-mono transition-colors ${!conditionPicker.currentCondition
									? 'bg-slate-700/70 text-slate-200'
									: 'text-slate-500 hover:text-slate-300 hover:bg-slate-800/60'
								}`}
						>
							None — always run
						</button>
						{CONDITIONS.map(({ key, label }) => (
							<button
								key={key}
								onMouseDown={(e) => { e.stopPropagation(); updateEdgeCondition(conditionPicker.rawSource, conditionPicker.rawTarget, key); setConditionPicker(null) }}
								className={`w-full text-left text-xs px-2.5 py-1.5 rounded-lg font-mono transition-colors ${conditionPicker.currentCondition === key
										? 'bg-blue-900/70 text-blue-300 border border-blue-800'
										: 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
									}`}
							>
								{label}
							</button>
						))}
					</div>
				</div>
			)}

			{/* Running indicator — autonomous mode gets Pause/Cancel; assisted stays passive
			    (assisted already has its own intervention points via approval gates). */}
			{executionStatus === 'running' && (
				<div className="absolute top-4 right-4 flex items-center gap-2 z-10">
					<div className="flex items-center gap-1.5 px-4 py-1.5 rounded-lg bg-blue-900/50 border border-blue-700 text-blue-300 text-xs font-semibold">
						<Loader2 size={12} className="animate-spin" />
						{executionMode === 'autonomous' ? 'Autonomous Run...' : 'Running...'}
					</div>
					{executionMode === 'autonomous' && (
						<>
							<button
								onClick={pauseFlow}
								className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-[var(--bg-overlay-base)] border border-[var(--border-a)] hover:bg-[var(--bg-hover)] text-slate-300 text-xs font-semibold transition-colors"
							>
								<Pause size={11} />
								Pause
							</button>
							<button
								onClick={cancelFlow}
								className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-red-950/40 border border-red-800/50 hover:bg-red-900/40 text-red-300 text-xs font-semibold transition-colors"
							>
								<Ban size={11} />
								Cancel
							</button>
						</>
					)}
				</div>
			)}

			{/* Approval gate — existing UX, unaffected by Pause */}
			{executionStatus === 'waiting_approval' && (
				<div className="absolute top-4 right-4 flex items-center gap-1.5 px-4 py-1.5 rounded-lg bg-blue-900/50 border border-blue-700 text-blue-300 text-xs font-semibold z-10">
					<Loader2 size={12} className="animate-spin" />
					Running...
				</div>
			)}

			{/* Pause requested — cooperative wait until the flow actually parks
			    (no dedicated "confirmed" WS event; see startPausePoll in store.ts) */}
			{executionStatus === 'pausing' && (
				<div className="absolute top-4 right-4 flex items-center gap-2 z-10">
					<div className="flex items-center gap-1.5 px-4 py-1.5 rounded-lg bg-amber-900/40 border border-amber-700/60 text-amber-300 text-xs font-semibold">
						<Loader2 size={12} className="animate-spin" />
						Pausing…
					</div>
					<button
						onClick={cancelFlow}
						className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-red-950/40 border border-red-800/50 hover:bg-red-900/40 text-red-300 text-xs font-semibold transition-colors"
					>
						<Ban size={11} />
						Cancel
					</button>
				</div>
			)}

			{/* Confirmed paused — Resume, open the dedicated Edit Checkpoint screen to
			    rewind/skip/reorder, or Cancel outright. Editing happens on that separate
			    screen now, not inline here — see CheckpointEditorScreen. */}
			{executionStatus === 'paused' && (
				<div className="absolute top-4 right-4 flex items-center gap-2 z-10">
					<div className="flex items-center gap-1.5 px-4 py-1.5 rounded-lg bg-amber-900/40 border border-amber-700/60 text-amber-300 text-xs font-semibold">
						<PauseCircle size={12} />
						Paused
					</div>
					<button
						onClick={resumeFlow}
						className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-teal-600 hover:bg-teal-500 text-white text-xs font-semibold transition-colors"
					>
						<Play size={11} />
						Resume
					</button>
					<button
						onClick={openCheckpointEditor}
						className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-[var(--bg-overlay-base)] border border-[var(--border-a)] hover:bg-[var(--bg-hover)] text-slate-300 text-xs font-semibold transition-colors"
					>
						<Pencil size={11} />
						Edit Checkpoint
					</button>
					<button
						onClick={cancelFlow}
						className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-red-950/40 border border-red-800/50 hover:bg-red-900/40 text-red-300 text-xs font-semibold transition-colors"
					>
						<Ban size={11} />
						Cancel
					</button>
				</div>
			)}

			{/* Cancelled — terminal, matches the Workflows table's "Cancelled" status
			    rather than silently falling back to "idle" (which would wrongly re-show
			    Confirm & Execute for a workflow that isn't meant to run anymore). */}
			{executionStatus === 'cancelled' && (
				<div className="absolute top-4 right-4 flex items-center gap-1.5 px-4 py-1.5 rounded-lg bg-slate-800/60 border border-slate-600/60 text-slate-300 text-xs font-semibold z-10">
					<Ban size={12} />
					Cancelled
				</div>
			)}

			{/* Advisory preview ready — confirm & execute */}
			{isCompletePending && isAdvisory && (
				<div className="absolute bottom-20 left-1/2 -translate-x-1/2 flex items-center gap-3 z-10 px-5 py-3 rounded-2xl bg-[var(--bg-overlay-base)] border border-[var(--border-a)] backdrop-blur-sm shadow-2xl">
					<Eye size={14} className="text-blue-400 flex-shrink-0" />
					<span className="text-xs text-blue-400 font-semibold">Advisory preview ready</span>
					<div className="w-px h-4 bg-[var(--border-a)]" />
					<button
						onClick={confirmAndExecute}
						className="flex items-center gap-1.5 px-4 py-1.5 rounded-lg bg-teal-600 hover:bg-teal-500 text-white text-xs font-semibold transition-colors"
					>
						<Play size={11} />
						Confirm & Execute
					</button>
				</div>
			)}

			{/* Re-orchestrating overlay */}
			{reorchestrateLoading && (
				<div className="absolute inset-0 z-20 flex items-center justify-center bg-black/40 backdrop-blur-sm pointer-events-none">
					<div className="flex items-center gap-3 px-5 py-3 rounded-2xl bg-[var(--bg-overlay-base)] border border-blue-500/40 shadow-2xl">
						<Loader2 size={14} className="text-blue-400 animate-spin flex-shrink-0" />
						<span className="text-xs text-blue-300 font-semibold">Re-orchestrating pipeline…</span>
					</div>
				</div>
			)}

			{/* Submitted state bar */}
			{isSubmitted && (
				<div className="absolute bottom-20 left-1/2 -translate-x-1/2 flex items-center gap-3 z-10 px-5 py-3 rounded-2xl bg-[var(--bg-overlay-base)] border border-teal-700/50 backdrop-blur-sm shadow-2xl">
					<CheckCircle size={14} className="text-teal-400 flex-shrink-0" />
					<span className="text-xs text-teal-400 font-semibold">Execution submitted successfully</span>
					</div>
			)}

			{/* Drop hint when palette open */}
			{paletteOpen && isEditing && (
				<div className="absolute bottom-20 left-1/2 -translate-x-1/2 px-3 py-1.5 rounded-lg bg-[var(--bg-overlay-surface)] border border-[var(--border-a)] text-[10px] text-slate-500 z-10 pointer-events-none">
					Drag between two nodes to auto-connect — or drop anywhere on canvas
				</div>
			)}

			{paletteOpen && <AgentPalette onClose={() => setPaletteOpen(false)} />}

			{/* Edge insert picker */}
			{insertingEdge && (() => {
				const pickerLeft = Math.min(Math.max(8, insertingEdge.screenX - 128), window.innerWidth - 272)
				const pickerTop = insertingEdge.screenY + 292 > window.innerHeight
					? insertingEdge.screenY - 300
					: insertingEdge.screenY + 12
				return (
					<>
						<div className="fixed inset-0 z-40" onClick={() => setInsertingEdge(null)} />
						<div
							className="fixed z-50 bg-[var(--bg-surface)] border border-[var(--border-a)] rounded-xl shadow-2xl p-2 w-64"
							style={{ top: pickerTop, left: pickerLeft }}
						>
							<div className="text-[10px] font-bold text-slate-500 uppercase tracking-wider px-2 py-1.5">
								Insert Agent
							</div>
							<div className="grid grid-cols-2 gap-0.5 max-h-56 overflow-y-auto">
								{AGENTS.map((agent) => (
									<button
										key={agent.id}
										onClick={() => insertAgent(agent.id)}
										className="flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-[var(--bg-raised)] transition-colors text-left"
									>
										<span className="text-sm flex-shrink-0">{agent.emoji}</span>
										<span className="text-xs text-slate-300 truncate">{agent.label}</span>
									</button>
								))}
							</div>
						</div>
					</>
				)
			})()}
		</div>
	)
}
