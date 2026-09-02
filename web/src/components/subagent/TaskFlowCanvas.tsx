import { useCallback, useEffect, useMemo, useRef } from 'react'
import {
  ReactFlow,
  Background,
  BackgroundVariant,
  Controls,
  MarkerType,
  addEdge,
  useNodesState,
  useEdgesState,
  type Connection,
  type NodeTypes,
  type Edge,
  type Node,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { TaskNode } from './TaskNode'
import { TaskDecisionNode } from './TaskDecisionNode'
import type { TaskDef, TaskEdgeDef } from '../../data/agents'
import type { TaskConditionState } from '../../store'

const nodeTypes: NodeTypes = {
  taskNode:     TaskNode,
  taskDecision: TaskDecisionNode,
}

// Highlight color for a selected (clicked) edge or task node, before it's deleted.
const SELECTED_COLOR = '#facc15'

interface TaskFlowCanvasProps {
  tasks:           TaskDef[]
  taskEdges?:      TaskEdgeDef[]
  taskConditions?: Record<string, TaskConditionState>
  agentColor:      string
  onEdgesUpdate?:  (edges: TaskEdgeDef[]) => void
  onTasksDelete?:  (deletedTaskIds: string[]) => void
}

// Layout constants
const TASK_W    = 220   // task card slot width
const DIAMOND_W = 90    // actual diamond rendered width
const GAP       = 60    // horizontal gap between slots
const H_PAD     = 60    // left padding
const MAIN_Y    = 300   // y of main row (tasks + diamonds)
const BRANCH_Y  = 20    // y of branch tasks (above main row)

const EDGE_STYLE   = { stroke: '#2d4a7a', strokeWidth: 1.5, strokeDasharray: '6,3' }
const YES_STYLE    = { stroke: '#16a34a', strokeWidth: 1.5 }
const NO_STYLE     = { stroke: '#dc2626', strokeWidth: 1.5 }
const EDGE_MARKER  = { type: MarkerType.ArrowClosed, color: '#2d4a7a', width: 20, height: 20 }
const YES_MARKER   = { type: MarkerType.ArrowClosed, color: '#16a34a', width: 20, height: 20 }
const NO_MARKER    = { type: MarkerType.ArrowClosed, color: '#dc2626', width: 20, height: 20 }

function yesLabel() {
  return {
    label:          'YES',
    labelStyle:     { fill: '#16a34a', fontSize: 14, fontFamily: 'monospace', fontWeight: 700 },
    labelBgStyle:   { fill: '#0d1729', fillOpacity: 0.9, rx: 3 },
    labelBgPadding: [3, 5] as [number, number],
  }
}
function noLabel() {
  return {
    label:          'NO',
    labelStyle:     { fill: '#dc2626', fontSize: 14, fontFamily: 'monospace', fontWeight: 700 },
    labelBgStyle:   { fill: '#0d1729', fillOpacity: 0.9, rx: 3 },
    labelBgPadding: [3, 5] as [number, number],
  }
}

export function TaskFlowCanvas({ tasks, taskEdges, taskConditions, agentColor, onEdgesUpdate, onTasksDelete }: TaskFlowCanvasProps) {
  const rfRef = useRef<{ fitView: (opts?: { padding?: number }) => void } | null>(null)

  const taskIdSet = useMemo(() => new Set(tasks.map((t) => t.id)), [tasks])

  function toTaskEdgeDefs(rfEdges: Edge[]): TaskEdgeDef[] {
    return rfEdges
      .filter((e) => taskIdSet.has(e.source) && taskIdSet.has(e.target))
      .map((e) => ({ source: e.source, target: e.target }))
  }

  const { nodes: computedNodes, edges: computedEdges } = useMemo(() => {
    if (tasks.length === 0) return { nodes: [], edges: [] }

    const taskIds   = new Set(tasks.map((t) => t.id))
    const taskOrder = Object.fromEntries(tasks.map((t, i) => [t.id, i]))

    const rawEdges: TaskEdgeDef[] = taskEdges
      ? taskEdges.filter((e) => taskIds.has(e.source) && taskIds.has(e.target))
      : tasks.slice(0, -1).map((t, i) => ({ source: t.id, target: tasks[i + 1].id }))

    const outBySource = new Map<string, TaskEdgeDef[]>()
    for (const e of rawEdges) {
      const arr = outBySource.get(e.source) ?? []
      arr.push(e)
      outBySource.set(e.source, arr)
    }

    // Branch tasks: only reachable via conditional edges (pure YES-path)
    const condTargetSet   = new Set(rawEdges.filter((e) => e.condition).map((e) => e.target))
    const uncondTargetSet = new Set(rawEdges.filter((e) => !e.condition).map((e) => e.target))
    const branchTaskIds   = new Set([...condTargetSet].filter((id) => !uncondTargetSet.has(id)))

    const mainTasks = tasks.filter((t) => !branchTaskIds.has(t.id))

    // ── Build main sequence ────────────────────────────────────────────────────
    type TaskSlot    = { kind: 'task';    task: TaskDef }
    type DiamondSlot = { kind: 'diamond'; id: string; question: string; condEdge: TaskEdgeDef }
    type Slot        = TaskSlot | DiamondSlot

    const mainSeq: Slot[] = []
    for (const task of mainTasks) {
      mainSeq.push({ kind: 'task', task })
      const condOuts = (outBySource.get(task.id) ?? []).filter((e) => e.condition)
      condOuts.forEach((e, i) => {
        mainSeq.push({ kind: 'diamond', id: `vd_${task.id}_${i}`, question: e.condition ?? '', condEdge: e })
      })
    }

    // ── x positions ────────────────────────────────────────────────────────────
    // Each slot (task OR diamond) occupies TASK_W + GAP so branch tasks above
    // diamonds are spaced identically to task cards — no overlap.
    // The diamond is centered within its slot; the branch task uses the slot left-edge.
    let x = H_PAD
    const slotX: Record<string, number> = {}   // left edge of each slot
    const nodeX: Record<string, number> = {}   // actual render x (centered for diamond)

    for (const slot of mainSeq) {
      slotX[slot.kind === 'task' ? slot.task.id : slot.id] = x
      if (slot.kind === 'task') {
        nodeX[slot.task.id] = x
      } else {
        // Center diamond horizontally within the task-width slot
        nodeX[slot.id] = x + (TASK_W - DIAMOND_W) / 2
      }
      x += TASK_W + GAP
    }

    // Branch tasks: x = slot left edge of their diamond (same slot width as a task)
    for (const bid of branchTaskIds) {
      const incEdge = rawEdges.find((e) => e.target === bid && e.condition)
      if (!incEdge) continue
      const srcCondOuts = (outBySource.get(incEdge.source) ?? []).filter((e) => e.condition)
      const edgeIdx     = srcCondOuts.findIndex((e) => e.target === bid)
      const diamondId   = `vd_${incEdge.source}_${edgeIdx}`
      nodeX[bid]  = slotX[diamondId] ?? nodeX[incEdge.source] ?? 0
    }

    // ── React Flow nodes ───────────────────────────────────────────────────────
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const rfNodes: any[] = []

    for (const slot of mainSeq) {
      if (slot.kind === 'task') {
        rfNodes.push({
          id:       slot.task.id,
          type:     'taskNode',
          position: { x: nodeX[slot.task.id], y: MAIN_Y },
          data:     {
            label:      slot.task.label,
            index:      taskOrder[slot.task.id],
            color:      agentColor,
            condition:  slot.task.condition,
            condStatus: taskConditions?.[slot.task.id]?.status,
          },
        })
      } else {
        rfNodes.push({
          id:       slot.id,
          type:     'taskDecision',
          position: { x: nodeX[slot.id], y: MAIN_Y },
          data:     { question: slot.question },
        })
      }
    }

    for (const bid of branchTaskIds) {
      const task = tasks.find((t) => t.id === bid)
      if (!task) continue
      rfNodes.push({
        id:       task.id,
        type:     'taskNode',
        position: { x: nodeX[task.id], y: BRANCH_Y },
        data:     {
          label:      task.label,
          index:      taskOrder[task.id],
          color:      agentColor,
          condition:  task.condition,
          condStatus: taskConditions?.[task.id]?.status,
        },
      })
    }

    // ── React Flow edges ───────────────────────────────────────────────────────
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const rfEdges: any[] = []
    let ei = 0
    const eid = () => `e${ei++}`

    for (const task of mainTasks) {
      const outs     = outBySource.get(task.id) ?? []
      const condOuts = outs.filter((e) => e.condition)

      if (condOuts.length === 0) {
        for (const e of outs) {
          rfEdges.push({ id: eid(), source: e.source, target: e.target, type: 'smoothstep', style: EDGE_STYLE, markerEnd: EDGE_MARKER })
        }
        continue
      }

      // Source → first diamond
      rfEdges.push({ id: eid(), source: task.id, target: `vd_${task.id}_0`, type: 'smoothstep', style: EDGE_STYLE, markerEnd: EDGE_MARKER })

      condOuts.forEach((condEdge, i) => {
        const dId    = `vd_${task.id}_${i}`
        const isLast = i === condOuts.length - 1

        // YES → branch task (above)
        rfEdges.push({
          id: eid(), source: dId, sourceHandle: 'yes', target: condEdge.target,
          type: 'smoothstep', style: YES_STYLE, markerEnd: YES_MARKER, ...yesLabel(),
        })

        if (!isLast) {
          // NO → next diamond in chain
          rfEdges.push({
            id: eid(), source: dId, sourceHandle: 'no', target: `vd_${task.id}_${i + 1}`,
            type: 'smoothstep', style: NO_STYLE, markerEnd: NO_MARKER, ...noLabel(),
          })
        } else {
          // NO → next main-row task
          const uncondOut  = outs.find((e) => !e.condition)
          let noTarget: string | null = uncondOut?.target ?? null
          if (!noTarget) {
            const condTargets = new Set(condOuts.map((e) => e.target))
            const srcIdx = taskOrder[task.id]
            for (let k = srcIdx + 1; k < tasks.length; k++) {
              const tid = tasks[k].id
              if (!condTargets.has(tid) && !branchTaskIds.has(tid)) { noTarget = tid; break }
            }
          }
          if (noTarget) {
            rfEdges.push({
              id: eid(), source: dId, sourceHandle: 'no', target: noTarget,
              type: 'smoothstep', style: NO_STYLE, markerEnd: NO_MARKER, ...noLabel(),
            })
          }
        }
      })
    }

    // Branch task outgoing edges (merge back into main flow)
    for (const bid of branchTaskIds) {
      for (const e of outBySource.get(bid) ?? []) {
        rfEdges.push({ id: eid(), source: e.source, target: e.target, type: 'smoothstep', style: EDGE_STYLE, markerEnd: EDGE_MARKER })
      }
    }

    return { nodes: rfNodes, edges: rfEdges }
  }, [tasks, taskEdges, taskConditions, agentColor])

  const [nodes, setNodes, onNodesChange] = useNodesState(computedNodes)
  const [edges, setEdges, onEdgesChange] = useEdgesState(computedEdges)

  // Re-initialize layout whenever tasks change (resets any manual drags)
  useEffect(() => {
    setNodes(computedNodes)
    setEdges(computedEdges)
    setTimeout(() => {
      requestAnimationFrame(() => rfRef.current?.fitView({ padding: 0.3 }))
    }, 30)
  }, [computedNodes, computedEdges]) // eslint-disable-line

  const handleConnect = useCallback((connection: Connection) => {
    setEdges((eds) => {
      const next = addEdge(
        { ...connection, type: 'smoothstep', style: EDGE_STYLE, markerEnd: EDGE_MARKER },
        eds,
      )
      onEdgesUpdate?.(toTaskEdgeDefs(next))
      return next
    })
  }, [onEdgesUpdate]) // eslint-disable-line

  const handleEdgesDelete = useCallback((deleted: Edge[]) => {
    setEdges((eds) => {
      const deletedIds = new Set(deleted.map((d) => d.id))
      const next = eds.filter((e) => !deletedIds.has(e.id))
      onEdgesUpdate?.(toTaskEdgeDefs(next))
      return next
    })
  }, [onEdgesUpdate]) // eslint-disable-line

  // Only real task nodes are deletable this way — decision diamonds are derived from
  // edge conditions, not standalone tasks, so deleting one wouldn't mean anything.
  const handleNodesDelete = useCallback((deleted: Node[]) => {
    const deletedTaskIds = deleted.filter((n) => n.type === 'taskNode').map((n) => n.id)
    if (deletedTaskIds.length) onTasksDelete?.(deletedTaskIds)
  }, [onTasksDelete])

  // Click-to-highlight: React Flow tracks `.selected` on click, but every edge here
  // carries a fixed inline `style` (default/YES/NO) that would otherwise mask it.
  // Override to a bright highlight color at render time so a selected edge is visibly
  // distinct before the user presses Delete.
  const displayEdges = useMemo(
    () => edges.map((e) => e.selected
      ? { ...e, style: { ...e.style, stroke: SELECTED_COLOR, strokeWidth: 3 }, markerEnd: { ...(e.markerEnd as object), color: SELECTED_COLOR } }
      : e),
    [edges],
  )

  if (tasks.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <p className="text-sm text-slate-600 italic">No tasks yet.</p>
      </div>
    )
  }

  return (
    <div style={{ width: '100%', height: '100%' }}>
      <ReactFlow
        nodes={nodes}
        edges={displayEdges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        fitView
        fitViewOptions={{ padding: 0.3, maxZoom: 1.0 }}
        nodesDraggable={true}
        nodesConnectable={true}
        elementsSelectable={true}
        panOnDrag={true}
        zoomOnScroll={true}
        minZoom={0.2}
        deleteKeyCode="Delete"
        onConnect={handleConnect}
        onEdgesDelete={handleEdgesDelete}
        onNodesDelete={handleNodesDelete}
        onInit={(instance) => { rfRef.current = instance }}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="#1e293b" />
        <Controls showInteractive={false} className="!bottom-4 !right-4" />
      </ReactFlow>
    </div>
  )
}
