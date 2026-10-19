import { useEffect, useState } from 'react'
import { X, Loader2, AlertTriangle, Bot, ListChecks, GitBranch, CheckCircle2, XCircle, SkipForward, Clock } from 'lucide-react'
import { fetchTrace, type TraceStep } from '../../services/api'

interface Props {
	sessionId: string
	onClose: () => void
}

const KIND_META: Record<TraceStep['kind'], { label: string; Icon: typeof Bot }> = {
	agent: { label: 'Agent', Icon: Bot },
	task: { label: 'Task', Icon: ListChecks },
	decision: { label: 'Decision', Icon: GitBranch },
}

const STATUS_META: Record<TraceStep['status'], { color: string; Icon: typeof Clock }> = {
	running: { color: '#3b82f6', Icon: Loader2 },
	completed: { color: '#10b981', Icon: CheckCircle2 },
	failed: { color: '#ef4444', Icon: XCircle },
	skipped: { color: '#94a3b8', Icon: SkipForward },
}

function formatTs(ts: number) {
	return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function FieldList({ label, fields }: { label: string; fields: TraceStep['inputs'] }) {
	if (!fields.length) return null
	return (
		<div className="mt-2">
			<div className="text-[9px] font-bold text-slate-500 uppercase tracking-wider mb-1">{label}</div>
			<div className="space-y-0.5">
				{fields.map((f, i) => (
					<div key={i} className="text-[11px] text-slate-400 flex gap-1.5">
						<span className="text-slate-500 flex-shrink-0">{f.label}:</span>
						<span className="text-slate-300 break-words">{f.value}</span>
					</div>
				))}
			</div>
		</div>
	)
}

// Renders the Redis-backed execution trace for a session (steps ordered by seq) —
// the same feed `api.ts` calls "Langfuse logs" in its comment. Read-only, polls once
// on open rather than live since a finished/paused session's trace doesn't change.
export function TraceModal({ sessionId, onClose }: Props) {
	const [steps, setSteps] = useState<TraceStep[] | null>(null)
	const [error, setError] = useState<string | null>(null)

	useEffect(() => {
		let cancelled = false
		fetchTrace(sessionId)
			.then((res) => { if (!cancelled) setSteps(res.steps) })
			.catch((err) => { if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load trace.') })
		return () => { cancelled = true }
	}, [sessionId])

	return (
		<div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm" onClick={onClose}>
			<div
				className="bg-[var(--bg-surface)] border border-[var(--border-a)] rounded-2xl w-full max-w-2xl mx-4 shadow-2xl max-h-[80vh] flex flex-col"
				onClick={(e) => e.stopPropagation()}
			>
				<div className="flex items-center gap-3 px-5 py-4 border-b border-[var(--border)] flex-shrink-0">
					<div className="w-9 h-9 rounded-xl bg-violet-500/15 border border-violet-500/30 flex items-center justify-center flex-shrink-0">
						<ListChecks size={16} className="text-violet-400" />
					</div>
					<div className="min-w-0">
						<div className="text-sm font-bold text-slate-100 leading-tight">Execution Trace</div>
						<div className="text-[11px] text-slate-500 truncate">{sessionId}</div>
					</div>
					<button
						onClick={onClose}
						className="ml-auto text-slate-500 hover:text-slate-300 transition-colors flex-shrink-0"
						title="Close"
					>
						<X size={18} />
					</button>
				</div>

				<div className="flex-1 overflow-y-auto px-5 py-4">
					{error ? (
						<div className="flex items-center gap-2 text-red-400 text-sm py-10 justify-center">
							<AlertTriangle size={16} /> {error}
						</div>
					) : steps === null ? (
						<div className="flex items-center gap-2 text-slate-500 text-sm py-10 justify-center">
							<Loader2 size={16} className="animate-spin" /> Loading trace…
						</div>
					) : steps.length === 0 ? (
						<div className="text-center text-sm text-slate-600 py-10">No trace steps recorded for this session.</div>
					) : (
						<div className="space-y-2">
							{steps.map((step) => {
								const kindMeta = KIND_META[step.kind]
								const statusMeta = STATUS_META[step.status]
								const KindIcon = kindMeta.Icon
								const StatusIcon = statusMeta.Icon
								return (
									<div key={step.seq} className="rounded-xl border border-[var(--border)] bg-[var(--bg-raised)] px-3.5 py-3">
										<div className="flex items-center gap-2">
											<KindIcon size={13} className="text-slate-500 flex-shrink-0" />
											<span className="text-[9px] font-bold text-slate-500 uppercase tracking-wider">{kindMeta.label}</span>
											{step.agent_id && (
												<span className="text-[10px] text-slate-500">· {step.agent_id.replace(/_/g, ' ')}</span>
											)}
											<span
												className="ml-auto inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold"
												style={{ background: statusMeta.color + '1a', color: statusMeta.color, border: `1px solid ${statusMeta.color}45` }}
											>
												<StatusIcon size={10} className={step.status === 'running' ? 'animate-spin' : ''} />
												{step.status}
											</span>
											<span className="text-[10px] text-slate-600 font-mono flex-shrink-0">{formatTs(step.ts)}</span>
										</div>
										<div className="text-sm font-semibold text-slate-200 mt-1.5">{step.title}</div>
										{step.summary && <div className="text-xs text-slate-400 mt-0.5 leading-snug">{step.summary}</div>}
										{step.error && (
											<div className="text-xs text-red-400 mt-1.5 flex items-start gap-1.5">
												<AlertTriangle size={12} className="flex-shrink-0 mt-0.5" /> {step.error}
											</div>
										)}
										<FieldList label="Inputs" fields={step.inputs} />
										<FieldList label="Outputs" fields={step.outputs} />
									</div>
								)
							})}
						</div>
					)}
				</div>
			</div>
		</div>
	)
}
