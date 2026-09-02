import type { Node, Edge } from '@xyflow/react'
import type { PipelineNodeDef, PipelineEdgeDef } from '../data/scenarios'

export const CONDITION_LABELS: Record<string, string> = {
  er_critical_patients:     'if critical patients',
  no_er_critical_patients:  'if no critical patients',
  icu_full:                 'if ICU full',
  icu_not_full:             'if ICU has space',
  has_stepdown_candidates:  'if step-downs available',
  beds_available:           'if beds free',
  no_beds_available:        'if no beds free',
  has_discharge_candidates: 'if discharge candidates',
  high_acuity:              'if CTAS 1–3',
  low_acuity:               'if CTAS 4–5',
  discharge_ready:          'if discharge ready',
  discharge_not_ready:      'if barriers found',
  bed_reserved:             'if bed reserved',
  bed_not_reserved:         'if reservation skipped',
  candidates_found:         'if candidates found',
  no_candidates_found:      'if no candidates',
  ventilator_needed:        'if ventilator needed',
  no_ventilator_needed:     'if no ventilator',
  isolation_needed:         'if isolation needed',
  no_isolation_needed:      'if no isolation',
}

function _conditionLabel(condition: string | undefined): string | null {
  if (!condition) return null
  return CONDITION_LABELS[condition] ?? condition.replace(/_/g, ' ')
}

// Pairs of logically opposite conditions — these collapse into a single decision diamond
const OPPOSITE_CONDITIONS: Record<string, string> = {
  icu_full:                'icu_not_full',
  icu_not_full:            'icu_full',
  er_critical_patients:    'no_er_critical_patients',
  no_er_critical_patients: 'er_critical_patients',
  beds_available:          'no_beds_available',
  no_beds_available:       'beds_available',
  high_acuity:             'low_acuity',
  low_acuity:              'high_acuity',
  discharge_ready:         'discharge_not_ready',
  discharge_not_ready:     'discharge_ready',
  bed_reserved:            'bed_not_reserved',
  bed_not_reserved:        'bed_reserved',
  candidates_found:        'no_candidates_found',
  no_candidates_found:     'candidates_found',
  ventilator_needed:       'no_ventilator_needed',
  no_ventilator_needed:    'ventilator_needed',
  isolation_needed:        'no_isolation_needed',
  no_isolation_needed:     'isolation_needed',
  dirty_beds:              'no_dirty_beds',
  no_dirty_beds:           'dirty_beds',
}

// Positive (YES) side of each pair
const POSITIVE_CONDITIONS = new Set([
  'icu_full', 'er_critical_patients', 'beds_available',
  'has_stepdown_candidates', 'has_discharge_candidates',
  'high_acuity', 'discharge_ready', 'bed_reserved',
  'candidates_found', 'ventilator_needed', 'isolation_needed',
  'dirty_beds',
])

const COL_WIDTH  = 380
const ROW_HEIGHT = 220
const H_PAD = 80
const V_PAD = 60

export interface LayoutOpts {
  colWidth?:        number
  rowHeight?:       number
  hPad?:            number
  vPad?:            number
  leafNodeType?:    string  // defaults to 'agentNode'
  decisionNodeType?: string  // defaults to 'decisionNode'
  terminalNodeType?: string  // defaults to 'terminalNode'
  simpleLayout?:    boolean // skip detour detection, use plain column grid
}

export function computeLayout(
  nodeDefs: PipelineNodeDef[],
  edgeDefs: PipelineEdgeDef[],
  existingPositions: Record<string, { x: number; y: number }> = {},
  opts: LayoutOpts = {}
): { nodes: Node[]; edges: Edge[] } {
  const COL_W  = opts.colWidth    ?? COL_WIDTH
  const ROW_H  = opts.rowHeight   ?? ROW_HEIGHT
  const H_P    = opts.hPad        ?? H_PAD
  const V_P    = opts.vPad        ?? V_PAD
  const LEAF_TYPE     = opts.leafNodeType     ?? 'agentNode'
  const DECISION_TYPE = opts.decisionNodeType ?? 'decisionNode'
  const TERMINAL_TYPE = opts.terminalNodeType ?? 'terminalNode'
  const SIMPLE_LAYOUT = opts.simpleLayout     ?? false

  // ── Decision-node injection ────────────────────────────────────────────────
  // When a source has two conditional edges with opposite conditions, collapse
  // them into a single virtual DecisionNode with YES / NO outgoing branches.
  const augNodeDefs: PipelineNodeDef[] = [...nodeDefs]
  let augEdgeDefs: PipelineEdgeDef[]   = [...edgeDefs]

  const usedEdgeIdx = new Set<number>()
  for (let i = 0; i < edgeDefs.length; i++) {
    const e1 = edgeDefs[i]
    if (!e1.condition || usedEdgeIdx.has(i)) continue
    const opp = OPPOSITE_CONDITIONS[e1.condition]
    if (!opp) continue
    const j = edgeDefs.findIndex(
      (e, idx) => idx !== i && e.source === e1.source && e.condition === opp && !usedEdgeIdx.has(idx)
    )
    if (j === -1) continue

    const e2 = edgeDefs[j]
    usedEdgeIdx.add(i); usedEdgeIdx.add(j)

    const yesEdge = POSITIVE_CONDITIONS.has(e1.condition) ? e1 : e2
    const noEdge  = yesEdge === e1 ? e2 : e1
    const decId   = `vd_${e1.source}`
    const question = (yesEdge.condition_label ?? _conditionLabel(yesEdge.condition) ?? yesEdge.condition ?? '')
      .replace(/^if /i, '')

    augNodeDefs.push({ id: decId, agentId: '_decision', isDecision: true, question })

    augEdgeDefs = augEdgeDefs.filter(e => e !== e1 && e !== e2)
    augEdgeDefs.push(
      { source: e1.source, target: decId },
      { source: decId, target: yesEdge.target, condition: yesEdge.condition, condition_label: 'YES', isDecisionBranch: 'yes' },
      { source: decId, target: noEdge.target,  condition: noEdge.condition,  condition_label: 'NO',  isDecisionBranch: 'no'  },
    )
  }

  // ── Unpaired conditional edges → diamond + STOP terminal ──────────────────
  // A single conditional edge with no matching opposite becomes a diamond whose
  // YES branch leads to the existing target and NO branch leads to a stop node.
  for (let i = 0; i < edgeDefs.length; i++) {
    if (usedEdgeIdx.has(i)) continue
    const e = edgeDefs[i]
    if (!e.condition) continue

    const decId  = `vd_${e.source}`
    const stopId = `stop_${e.source}`
    if (augNodeDefs.some((n) => n.id === decId)) continue   // already processed

    const question = (_conditionLabel(e.condition) ?? e.condition)
      .replace(/^if /i, '')

    augNodeDefs.push({ id: decId,  agentId: '_decision', isDecision: true,  question })
    augNodeDefs.push({ id: stopId, agentId: '_terminal', isTerminal: true })

    augEdgeDefs = augEdgeDefs.filter((edge) => edge !== edgeDefs[i])
    augEdgeDefs.push(
      { source: e.source, target: decId },
      { source: decId, target: e.target, condition: e.condition, condition_label: 'YES', isDecisionBranch: 'yes'  },
      { source: decId, target: stopId,                           condition_label: 'NO',  isDecisionBranch: 'no'   },
    )
    usedEdgeIdx.add(i)
  }

  // ── Topological sort → column assignment ──────────────────────────────────
  const ids = augNodeDefs.map((n) => n.id)

  const inDegree: Record<string, number> = {}
  const children: Record<string, string[]> = {}
  for (const id of ids) { inDegree[id] = 0; children[id] = [] }
  for (const e of augEdgeDefs) {
    if (!children[e.source] || inDegree[e.target] === undefined) continue
    inDegree[e.target] = (inDegree[e.target] ?? 0) + 1
    children[e.source].push(e.target)
  }

  const col: Record<string, number> = {}
  const queue = ids.filter((id) => inDegree[id] === 0)
  for (const id of queue) col[id] = 0

  const topoOrder: string[] = []
  const tempDeg = { ...inDegree }

  while (queue.length) {
    const cur = queue.shift()!
    topoOrder.push(cur)
    for (const child of children[cur]) {
      col[child] = Math.max(col[child] ?? 0, (col[cur] ?? 0) + 1)
      tempDeg[child]--
      if (tempDeg[child] === 0) queue.push(child)
    }
  }

  const byCol: Record<number, string[]> = {}
  for (const id of topoOrder) {
    const c = col[id]
    if (!byCol[c]) byCol[c] = []
    byCol[c].push(id)
  }

  const maxRows = Math.max(...Object.values(byCol).map((a) => a.length))
  const canvasCenterY = (maxRows * ROW_H) / 2

  // ── Conditional branch-detour detection ───────────────────────────────────
  // Branch detour: node with ≥1 incoming edge, all conditional, from a single source.
  // Decision-branch (YES/NO) edges count as conditional for this check.
  // Skipped when simpleLayout is true (task canvas uses plain column grid instead).
  const hasUnconditionalIncoming = new Set<string>()
  for (const e of augEdgeDefs) {
    if (!e.condition && !e.isDecisionBranch) hasUnconditionalIncoming.add(e.target)
  }
  const isBranchDetour = (id: string): boolean => {
    if (SIMPLE_LAYOUT) return false
    const inc = augEdgeDefs.filter((e) => e.target === id)
    const out = augEdgeDefs.filter((e) => e.source === id)
    if (inc.length === 0 || out.length === 0) return false
    if (hasUnconditionalIncoming.has(id)) return false
    const condSources = new Set(inc.map((e) => e.source))
    return condSources.size === 1
  }
  const hasDetours = !SIMPLE_LAYOUT && topoOrder.some(isBranchDetour)

  // Propagate branch row to single-parent descendants of branch detours.
  // A node extends the branch band if its only incoming node is already in-branch.
  // Nodes with multiple branch parents are merge points and stay in the main row.
  const inBranchRow = new Set<string>()
  if (hasDetours) {
    for (const id of topoOrder) {
      if (isBranchDetour(id)) { inBranchRow.add(id); continue }
      const inc = augEdgeDefs.filter((e) => e.target === id)
      if (inc.length === 1 && inBranchRow.has(inc[0].source)) inBranchRow.add(id)
    }
  }

  // Assign each branch-row node a fixed vertical lane so its y is consistent
  // across all columns. Branch detours get sequential lanes; single-parent
  // descendants inherit their parent's lane.
  const branchLane: Record<string, number> = {}
  let totalLanes = 0
  if (hasDetours) {
    for (const id of topoOrder) {
      if (isBranchDetour(id)) branchLane[id] = totalLanes++
    }
    for (const id of topoOrder) {
      if (!inBranchRow.has(id) || branchLane[id] !== undefined) continue
      const inc = augEdgeDefs.filter((e) => e.target === id)
      if (inc.length === 1 && branchLane[inc[0].source] !== undefined) {
        branchLane[id] = branchLane[inc[0].source]
      }
    }
  }

  // ── Position assignment ────────────────────────────────────────────────────
  const position: Record<string, { x: number; y: number }> = {}

  if (!hasDetours) {
    for (const [c, colIds] of Object.entries(byCol)) {
      const colNum = Number(c)
      const count  = colIds.length
      colIds.forEach((id, i) => {
        const colHeight = count * ROW_H
        const startY = canvasCenterY - colHeight / 2 + ROW_H / 2
        position[id] = { x: H_P + colNum * COL_W, y: V_P + startY + i * ROW_H }
      })
    }
  } else {
    const BRANCH_CY = ROW_H / 2
    // Main-row nodes centered at the branch-band midpoint so the backbone
    // threads through the middle of the YES/NO lanes.
    const MAIN_CY = BRANCH_CY

    for (const [c, colIds] of Object.entries(byCol)) {
      const colNum    = Number(c)
      const mainIds   = colIds.filter((id) => !inBranchRow.has(id))
      const branchIds = colIds.filter((id) => inBranchRow.has(id))

      mainIds.forEach((id, i) => {
        const h = mainIds.length * ROW_H
        position[id] = {
          x: H_P + colNum * COL_W,
          y: V_P + MAIN_CY - h / 2 + ROW_H / 2 + i * ROW_H,
        }
      })
      branchIds.forEach((id) => {
        const lane = branchLane[id] ?? 0
        position[id] = {
          x: H_P + colNum * COL_W,
          y: V_P + BRANCH_CY - (totalLanes * ROW_H) / 2 + ROW_H / 2 + lane * ROW_H,
        }
      })
    }
  }

  // ── React Flow output ──────────────────────────────────────────────────────
  const nodes: Node[] = augNodeDefs.map((n) => ({
    id:       n.id,
    type:     n.isTerminal ? TERMINAL_TYPE : (n.isDecision ? DECISION_TYPE : LEAF_TYPE),
    position: existingPositions[n.id] ?? position[n.id] ?? { x: 0, y: 0 },
    data:     n.isTerminal
      ? { label: 'Condition not met' }
      : n.isDecision
        ? { question: n.question }
        : { agentId: n.agentId, nodeId: n.id, taskType: n.taskType },
    ...(n.isDecision ? { style: { width: 260, height: 260 } } : {}),
  }))

  const edges: Edge[] = augEdgeDefs.map((e, i) => {
    const isDB = e.isDecisionBranch
    const resolvedLabel = e.condition_label ?? _conditionLabel(e.condition)
    return {
      id:           `e${i}-${e.source}-${e.target}`,
      source:       e.source,
      target:       e.target,
      sourceHandle: isDB === 'yes' ? 'yes' : isDB === 'no' ? 'no' : undefined,
      type:         'insertable',
      animated:     false,
      data: {
        condition:        isDB ? null : (e.condition ?? null),
        condition_label:  isDB ? null : (resolvedLabel ?? null),
        isDecisionBranch: isDB ?? null,
      },
      style: (e.condition && !isDB)
        ? { stroke: '#475569', strokeWidth: 1.5, strokeDasharray: '5,4' }
        : { stroke: '#2d4a7a', strokeWidth: 1.5, strokeDasharray: '6,3' },
    }
  })

  return { nodes, edges }
}
