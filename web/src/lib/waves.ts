import type { Node } from '@xyflow/react'
import type { BackendPipeline } from '../services/api'

// Level-assigns each agent to the wave it runs in (0 = no dependencies, else
// 1 + the deepest predecessor's wave) -- the same synchronization points the
// backend's checkpoints land on. Used by CheckpointEditorScreen for the initial
// seed layout and the locked/pending wave grouping.
export function computeWaves(pipeline: BackendPipeline): string[][] {
  const ids = pipeline.agents.map((a) => a.id)
  const idSet = new Set(ids)
  const incoming = new Map<string, string[]>(ids.map((id) => [id, []]))
  for (const e of pipeline.edges ?? []) {
    if (idSet.has(e.source) && idSet.has(e.target)) incoming.get(e.target)!.push(e.source)
  }
  const level = new Map<string, number>()
  function levelOf(id: string): number {
    const cached = level.get(id)
    if (cached !== undefined) return cached
    level.set(id, 0)   // cycle guard -- pipelines are DAGs, but never hang on a bad one
    const preds = incoming.get(id) ?? []
    const lvl = preds.length === 0 ? 0 : 1 + Math.max(...preds.map(levelOf))
    level.set(id, lvl)
    return lvl
  }
  for (const id of ids) levelOf(id)
  const maxLevel = Math.max(0, ...ids.map((id) => level.get(id)!))
  const waves: string[][] = Array.from({ length: maxLevel + 1 }, () => [])
  for (const id of ids) waves[level.get(id)!].push(id)
  return waves.filter((w) => w.length > 0)
}

// Fallback node footprint used only until xyflow reports the real measured size
// (node.measured.width/height, populated by its own ResizeObserver shortly after
// mount) -- AgentNode's actual height varies with content (description length, how
// many sub-agent pills wrap to a second row), so a fixed guess alone would
// sometimes size the box too short and let the card poke out the bottom.
export const CKPT_NODE_W = 300
export const CKPT_NODE_H = 220
const CKPT_PAD_X = 24
const CKPT_PAD_TOP = 44
const CKPT_PAD_BOTTOM = 24

// Rotates per checkpoint so adjacent boxes are visually distinct from each other --
// and deliberately avoids the hues AgentNode already uses heavily (blue, red,
// orange, emerald) plus the dedicated "complete" teal (#14b8a6, AgentNode's
// isComplete border), so a box never blends into the cards it's grouping.
const CKPT_COLORS = ['#f59e0b', '#a855f7', '#ec4899', '#6366f1']

// One dashed "checkpointGroup" node per wave, sized to bound that wave's agents at
// their current on-canvas positions and REAL rendered size (falling back to the
// guess above before xyflow has measured them). `labelFor` lets each call site
// pick its own numbering (the checkpoint editor matches its "Branch from"
// stepper's own checkpoint-step numbers; the main canvas uses a clean C1/C2/...).
// `colorFor` defaults to the rotating palette above.
export function buildCheckpointGroupNodes(
  waves: string[][],
  nodes: Node[],
  labelFor: (waveIndex: number) => string,
  colorFor: (waveIndex: number) => string = (i) => CKPT_COLORS[i % CKPT_COLORS.length],
): Node[] {
  const byId = new Map(nodes.map((n) => [n.id, n]))
  const boxes: Node[] = []
  for (let i = 0; i < waves.length; i++) {
    const wave = waves[i]
    const waveNodes = wave.map((id) => byId.get(id)).filter((n): n is Node => !!n)
    if (waveNodes.length === 0) continue
    const minX = Math.min(...waveNodes.map((n) => n.position.x))
    const minY = Math.min(...waveNodes.map((n) => n.position.y))
    const maxX = Math.max(...waveNodes.map((n) => n.position.x + (n.measured?.width ?? CKPT_NODE_W)))
    const maxY = Math.max(...waveNodes.map((n) => n.position.y + (n.measured?.height ?? CKPT_NODE_H)))
    boxes.push({
      id: `ckpt-group-${i}`,
      type: 'checkpointGroup',
      position: { x: minX - CKPT_PAD_X, y: minY - CKPT_PAD_TOP },
      style: { width: maxX - minX + CKPT_PAD_X * 2, height: maxY - minY + CKPT_PAD_TOP + CKPT_PAD_BOTTOM },
      draggable: false,
      selectable: false,
      connectable: false,
      deletable: false,
      zIndex: -1,
      data: { label: labelFor(i), color: colorFor(i) },
    })
  }
  return boxes
}
