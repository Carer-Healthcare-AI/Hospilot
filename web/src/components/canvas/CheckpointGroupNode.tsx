import type { NodeProps } from '@xyflow/react'

// Passive dashed box drawn behind a wave of agents -- a visual hint of "this is a
// checkpoint boundary." Not clickable; purely informational. Used by the checkpoint
// editor's locked-wave grouping (matching its "Branch from" stepper) and the main
// canvas's live autonomous-mode overlay, built via lib/waves.ts's
// buildCheckpointGroupNodes, which also assigns each box's `color` (rotates per
// checkpoint so adjacent boxes are distinguishable).
export function CheckpointGroupNode({ data }: NodeProps) {
  const { label, color } = data as { label: string; color?: string }
  const c = color ?? '#f59e0b'
  return (
    <div
      className="relative w-full h-full rounded-2xl border-2 border-dashed pointer-events-none"
      style={{ borderColor: `${c}55`, background: `${c}0a` }}
    >
      {/* Checkpoint number badge -- overlaps the box's top-left corner; the ring
          matching the page background cleanly separates it from the dashed line
          passing behind it, rather than the dashes just running through the text. */}
      <div
        className="absolute -top-5 -left-5 w-10 h-10 rounded-full flex items-center justify-center text-sm font-extrabold text-white"
        style={{ background: c, boxShadow: `0 0 0 3px var(--bg-base), 0 2px 10px ${c}80` }}
      >
        {label}
      </div>
    </div>
  )
}
