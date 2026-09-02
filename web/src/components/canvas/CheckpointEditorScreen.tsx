import { useState, useEffect, useMemo, useCallback } from 'react'
import {
  ReactFlow, Background, Controls, MiniMap, addEdge,
  useNodesState, useEdgesState,
  type Node, type Edge, type Connection, type NodeChange,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { ArrowLeft, CheckCircle, Loader2, Plus, RotateCcw } from 'lucide-react'
import { useStore, BACKEND_TO_FRONTEND, FRONTEND_TO_BACKEND } from '../../store'
import { editResumeSession, type BackendPipeline } from '../../services/api'
import { AGENTS } from '../../data/agents'
import { computeWaves, buildCheckpointGroupNodes } from '../../lib/waves'
import { AgentNode } from './AgentNode'
import { CheckpointGroupNode } from './CheckpointGroupNode'
import clsx from 'clsx'

// Dedicated screen for acting on a paused workflow's checkpoints: rewind to an
// earlier one, then freely edit what runs next -- drag agents around, draw/delete
// connections, remove agents, add new ones -- then edit-resume from that point.
//
// Same rendering as the main PipelineCanvas -- the real AgentNode component and the
// same checkpoint-group dashed boxes, not a bespoke look. AgentNode's status/lock
// styling normally comes from live execution state, which doesn't mean the same
// thing here (rewinding to an EARLIER checkpoint than the flow's real progress must
// show an actually-complete agent as pending again) -- so this screen passes
// explicit `statusOverride`/`editable` overrides per node instead of trusting
// nodeStates, and `disableClick` since there's no live sub-agent view to open mid-edit.
//
// Completed agents (as of the selected checkpoint) are LOCKED: dimmed, non-
// draggable, and you can't rewire INTO them (they already ran). Everything after
// the checkpoint is fully editable, and the pipeline sent to editResumeSession is
// derived directly from the canvas -- what you see is exactly what gets submitted.

// Wider than lib/layout.ts's computeLayout constants (380/220) -- this screen's
// AgentNode cards run taller on average (locked ones plus a checkpoint box's own
// padding above them), and cards were visually overlapping in dense columns at the
// main canvas's own spacing.
const COL_W = 440
const ROW_H = 300
const EDGE_STYLE = { stroke: '#2d4a7a', strokeWidth: 1.5 }

function frontendIdFor(backendAgentId: string): string {
  return BACKEND_TO_FRONTEND[backendAgentId] ?? BACKEND_TO_FRONTEND[backendAgentId.split(':')[0]] ?? backendAgentId
}

function backendIdFor(frontendAgentId: string): string {
  return FRONTEND_TO_BACKEND[frontendAgentId] ?? `${frontendAgentId}_agent`
}

const nodeTypes = { agentNode: AgentNode, checkpointGroup: CheckpointGroupNode }
const isCheckpointGroupId = (id: string) => id.startsWith('ckpt-group-')

export function CheckpointEditorScreen() {
  const sessionId = useStore((s) => s.sessionId)
  const backendPipeline = useStore((s) => s.backendPipeline)
  const checkpoints = useStore((s) => s.checkpoints)
  const closeCheckpointEditor = useStore((s) => s.closeCheckpointEditor)
  const fetchCheckpointsForSession = useStore((s) => s.fetchCheckpointsForSession)
  const applyEditedPipeline = useStore((s) => s.applyEditedPipeline)
  const pushToast = useStore((s) => s.pushToast)

  // Default to the latest checkpoint — checkpoints are newest-first per the doc.
  const [selectedCheckpointId, setSelectedCheckpointId] = useState<string | null>(
    checkpoints[0]?.checkpoint_id ?? null
  )
  const selectedCheckpoint = checkpoints.find((c) => c.checkpoint_id === selectedCheckpointId) ?? null
  const completedSet = useMemo(
    () => new Set(selectedCheckpoint?.completed_agents ?? []),
    [selectedCheckpoint]
  )

  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])
  const [showAddPicker, setShowAddPicker] = useState(false)
  const [applying, setApplying] = useState(false)

  const lockedIdSet = useMemo(
    () => new Set(nodes.filter((n) => (n.data as { editable?: boolean }).editable === false).map((n) => n.id)),
    [nodes]
  )

  // Seed the canvas from the pipeline + selected checkpoint. Completed agents (up to
  // the checkpoint) are locked; everything else is editable. Starting layout follows
  // the wave structure so it opens looking like the real flow; the original edges are
  // seeded so parallel branches are preserved until the user changes them. Reseeds
  // whenever the branch point or pipeline changes (switching checkpoints discards edits).
  useEffect(() => {
    if (!backendPipeline) return
    const waves = computeWaves(backendPipeline)

    const positions = new Map<string, { x: number; y: number }>()
    waves.forEach((wave, col) => {
      const h = wave.length * ROW_H
      wave.forEach((id, row) => {
        positions.set(id, { x: col * COL_W, y: -h / 2 + row * ROW_H + ROW_H / 2 })
      })
    })

    const seededNodes: Node[] = backendPipeline.agents.map((a) => {
      const locked = completedSet.has(a.id)
      return {
        id: a.id,
        type: 'agentNode',
        position: positions.get(a.id) ?? { x: 0, y: 0 },
        draggable: !locked,
        data: {
          agentId: frontendIdFor(a.id),
          taskType: (a as { task_type?: string }).task_type,
          statusOverride: locked ? 'complete' : 'idle',
          editable: !locked,
          disableClick: true,
        },
      }
    })

    const seededEdges: Edge[] = (backendPipeline.edges ?? []).map((e) => ({
      id: `${e.source}-${e.target}`,
      source: e.source,
      target: e.target,
      style: EDGE_STYLE,
    }))

    setNodes(seededNodes)
    setEdges(seededEdges)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [backendPipeline, selectedCheckpointId])

  // Checkpoint-group boxes for the locked waves only -- labeled to match the
  // "Branch from" stepper's own checkpoint-step numbers (not a clean 1-index),
  // since both live on this same screen and must agree on which checkpoint is which.
  const lockedWaves = useMemo(() => {
    if (!backendPipeline) return []
    const waves = computeWaves(backendPipeline)
    const i = waves.findIndex((wave) => !wave.every((id) => completedSet.has(id)))
    return waves.slice(0, i === -1 ? waves.length : i)
  }, [backendPipeline, completedSet])

  const checkpointStepsAsc = useMemo(
    () => [...checkpoints].filter((c) => c.step !== -1).sort((a, b) => a.step - b.step).map((c) => c.step),
    [checkpoints]
  )

  const checkpointGroupNodes = useMemo(
    () => buildCheckpointGroupNodes(lockedWaves, nodes, (i) => `${checkpointStepsAsc[i] ?? i}`),
    [lockedWaves, nodes, checkpointStepsAsc]
  )

  // Group boxes are purely derived for display -- never enter useNodesState, and
  // their ids are filtered out of the change handler below so a stray dimension-
  // measurement event can't touch real node state.
  const displayNodes = useMemo(
    () => (checkpointGroupNodes.length ? [...checkpointGroupNodes, ...nodes] : nodes),
    [checkpointGroupNodes, nodes]
  )

  const handleNodesChange = useCallback(
    (changes: NodeChange[]) => {
      onNodesChange(changes.filter((c) => !isCheckpointGroupId((c as { id: string }).id)))
    },
    [onNodesChange]
  )

  // Can't rewire INTO a completed agent (it already ran); source can be anything.
  const isValidConnection = useCallback(
    (c: Connection | Edge) => !!c.target && !lockedIdSet.has(c.target) && c.source !== c.target,
    [lockedIdSet]
  )

  const onConnect = useCallback(
    (params: Connection) => {
      if (lockedIdSet.has(params.target ?? '')) return
      setEdges((eds) => addEdge({ ...params, style: EDGE_STYLE }, eds))
    },
    [lockedIdSet, setEdges]
  )

  // Protect locked structure from the Delete key: completed nodes and the edges
  // between two completed nodes are historical and can't be removed.
  const onBeforeDelete = useCallback(
    async ({ nodes: delNodes, edges: delEdges }: { nodes: Node[]; edges: Edge[] }) => {
      const allowedNodes = delNodes.filter((n) => (n.data as { editable?: boolean }).editable !== false)
      const allowedEdges = delEdges.filter((e) => !(lockedIdSet.has(e.source) && lockedIdSet.has(e.target)))
      return { nodes: allowedNodes, edges: allowedEdges }
    },
    [lockedIdSet]
  )

  const pendingCount = useMemo(
    () => nodes.filter((n) => (n.data as { editable?: boolean }).editable !== false).length,
    [nodes]
  )

  const presentIds = useMemo(() => new Set(nodes.map((n) => n.id)), [nodes])
  const availableToAdd = useMemo(
    () => AGENTS.filter((a) => !presentIds.has(backendIdFor(a.id))),
    [presentIds]
  )

  function handleAddAgent(frontendAgentId: string) {
    const id = backendIdFor(frontendAgentId)
    setShowAddPicker(false)
    if (presentIds.has(id)) return
    const maxX = Math.max(0, ...nodes.map((n) => n.position.x))
    setNodes((nds) => [
      ...nds,
      {
        id, type: 'agentNode', position: { x: maxX + COL_W, y: 0 }, draggable: true,
        data: { agentId: frontendAgentId, statusOverride: 'idle', editable: true, disableClick: true },
      },
    ])
  }

  // The submitted pipeline IS the canvas: completed agents carried forward unchanged
  // (by id, so their results attach and they're skipped, not re-run), plus every
  // editable agent and every edge currently drawn.
  function buildEditedPipeline(): BackendPipeline {
    const agentsById = new Map(backendPipeline!.agents.map((a) => [a.id, a]))
    const orderedIds = [
      ...nodes.filter((n) => (n.data as { editable?: boolean }).editable === false).map((n) => n.id),
      ...nodes.filter((n) => (n.data as { editable?: boolean }).editable !== false).map((n) => n.id),
    ]
    const agents = orderedIds.map(
      (id) => agentsById.get(id) ?? ({ id } as BackendPipeline['agents'][number])
    )
    const edgeList = edges.map((e) => ({ source: e.source, target: e.target }))
    return { ...backendPipeline!, agents, edges: edgeList }
  }

  async function handleApply() {
    if (!sessionId || !backendPipeline || !selectedCheckpointId || applying) return
    setApplying(true)
    const edited = buildEditedPipeline()
    // Name the delta so the resume is verifiable at a glance, not a silent no-op.
    const before = new Set(backendPipeline.agents.map((a) => a.id))
    const after = new Set(edited.agents.map((a) => a.id))
    const added = [...after].filter((id) => !before.has(id)).length
    const removed = [...before].filter((id) => !after.has(id)).length
    try {
      await editResumeSession(sessionId, edited, selectedCheckpointId)
      // Rebuild the canvas from the edited pipeline OPTIMISTICALLY (from what we just
      // submitted, not a re-fetch — edit-resume resets the checkpoint thread and may not
      // have written the edited topology back to sessions.pipeline yet). Now the canvas
      // shows the edit: removed agents gone, added ones present, and live WS events land
      // on the right nodes.
      applyEditedPipeline(edited, [...completedSet])
      pushToast({
        severity: 'info',
        title: 'Changes applied',
        message: added || removed
          ? `Pipeline updated — ${added} added, ${removed} removed. Resuming from the checkpoint.`
          : 'Resuming from the checkpoint.',
      })
      await fetchCheckpointsForSession()
      closeCheckpointEditor()
    } catch (err) {
      pushToast({
        severity: 'critical',
        title: 'Could not apply changes',
        message: err instanceof Error ? err.message : 'Please try again.',
      })
    } finally {
      setApplying(false)
    }
  }

  return (
    <div className="flex flex-col flex-1 overflow-hidden bg-[var(--bg-base)]">
      {/* Header */}
      <div className="flex items-center gap-3 px-6 py-4 border-b border-[var(--border)] flex-shrink-0">
        <button
          onClick={closeCheckpointEditor}
          className="flex items-center gap-1.5 text-slate-400 hover:text-slate-200 text-sm font-semibold transition-colors"
        >
          <ArrowLeft size={15} />
          Back to canvas
        </button>
        <div className="w-px h-5 bg-[var(--border-a)]" />
        <span className="text-sm font-bold text-slate-100">Edit Checkpoint</span>
        <span className="text-xs text-slate-500">
          Branch from a checkpoint — drag to rearrange, draw connections, remove or add agents
        </span>
      </div>

      {/* Stepper — "branch from" selector (also the rewind + skip mechanism). */}
      <div className="flex items-center gap-2 px-6 py-3 border-b border-[var(--border)] flex-shrink-0 overflow-x-auto">
        <span className="text-[10px] font-bold text-slate-500 uppercase tracking-wider flex-shrink-0 flex items-center gap-1.5">
          <RotateCcw size={11} className="text-amber-400" />
          Branch from
        </span>
        {checkpoints.length === 0 && (
          <span className="text-xs text-slate-600 italic">No checkpoints yet</span>
        )}
        {[...checkpoints].reverse().map((c, i) => (
          <button
            key={c.checkpoint_id}
            onClick={() => setSelectedCheckpointId(c.checkpoint_id)}
            className={clsx(
              'flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-xs font-semibold transition-colors flex-shrink-0',
              selectedCheckpointId === c.checkpoint_id
                ? 'bg-amber-950/30 border-amber-700/60 text-amber-300'
                : 'bg-[var(--bg-surface)] border-[var(--border-a)] text-slate-400 hover:bg-[var(--bg-hover)]'
            )}
          >
            <span className="w-4 h-4 rounded-full bg-[var(--bg-raised)] flex items-center justify-center text-[9px] flex-shrink-0">
              {i + 1}
            </span>
            Checkpoint {c.step}
            <span className="text-slate-600">· {c.completed_agents.length} done</span>
          </button>
        ))}
      </div>

      {/* Toolbar — legend + count + add-agent picker */}
      <div className="flex items-center justify-between gap-2 px-6 py-2.5 border-b border-[var(--border)] flex-shrink-0">
        <div className="flex items-center gap-3 text-[10px] font-bold uppercase tracking-wider">
          <span className="flex items-center gap-1.5 text-slate-500">
            <CheckCircle size={11} className="text-teal-600" />
            Completed (locked)
          </span>
          <span className="text-slate-500">Editable ({pendingCount})</span>
        </div>
        <div className="relative">
          <button
            onClick={() => setShowAddPicker((v) => !v)}
            className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-[var(--bg-surface)] border border-[var(--border-a)] text-slate-300 text-xs font-semibold hover:bg-[var(--bg-hover)] transition-colors"
          >
            <Plus size={12} />
            Add Agent
          </button>
          {showAddPicker && (
            <>
              <div className="fixed inset-0 z-40" onClick={() => setShowAddPicker(false)} />
              <div className="absolute right-0 top-full mt-1 z-50 bg-[var(--bg-surface)] border border-[var(--border-a)] rounded-xl shadow-2xl p-2 w-64">
                <div className="text-[10px] font-bold text-slate-500 uppercase tracking-wider px-2 py-1.5">
                  Add agent to canvas
                </div>
                {availableToAdd.length === 0 ? (
                  <div className="text-xs text-slate-600 italic px-2 py-2">All agents already on the canvas</div>
                ) : (
                  <div className="grid grid-cols-2 gap-0.5 max-h-56 overflow-y-auto">
                    {availableToAdd.map((agent) => (
                      <button
                        key={agent.id}
                        onClick={() => handleAddAgent(agent.id)}
                        className="flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-[var(--bg-raised)] transition-colors text-left"
                      >
                        <span className="text-sm flex-shrink-0">{agent.emoji}</span>
                        <span className="text-xs text-slate-300 truncate">{agent.label}</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </div>

      {/* Canvas — same rendering as the main PipelineCanvas: real AgentNode cards,
          checkpoint-group dashed boxes. Drag nodes, draw edges between handles,
          select + Delete to remove. Locked (completed) nodes are fixed and can't be
          rewired into. */}
      <div className="flex-1 relative">
        <ReactFlow
          nodes={displayNodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodesChange={handleNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          isValidConnection={isValidConnection}
          onBeforeDelete={onBeforeDelete}
          nodesConnectable
          elementsSelectable
          deleteKeyCode={['Delete', 'Backspace']}
          fitView
          fitViewOptions={{ padding: 0.2, maxZoom: 1 }}
          minZoom={0.3}
          maxZoom={1.5}
          proOptions={{ hideAttribution: true }}
        >
          <Background gap={24} size={1} color="#0d1f3c" />
          <Controls showInteractive={false} />
          <MiniMap
            nodeColor={() => '#1e3a73'}
            maskColor="rgba(6,11,24,0.75)"
            className="!bg-[var(--bg-surface)] !border-[var(--border-a)]"
          />
        </ReactFlow>
      </div>

      {/* Actions */}
      <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-[var(--border)] flex-shrink-0">
        <button
          onClick={closeCheckpointEditor}
          className="px-4 py-2 rounded-xl text-sm font-semibold text-slate-400 bg-[var(--bg-raised)] hover:bg-[var(--bg-hover)] border border-[var(--border-a)] transition-colors"
        >
          Cancel
        </button>
        <button
          onClick={handleApply}
          disabled={!selectedCheckpointId || applying}
          className="flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-semibold text-white bg-teal-600 hover:bg-teal-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {applying ? <Loader2 size={14} className="animate-spin" /> : <CheckCircle size={14} />}
          {applying ? 'Applying…' : 'Apply & Resume'}
        </button>
      </div>
    </div>
  )
}
