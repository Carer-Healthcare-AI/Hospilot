import { useState, useEffect } from 'react'
import { Phone, Plus, Trash2, UserCheck } from 'lucide-react'
import { identifyPatient } from '../../services/api'

const AUTO_SUBMIT_SECONDS = 5

interface Props {
	sessionId: string
	expectedCount: number | null
	autonomous: boolean
	onConfirm: () => void
	onCancel: () => void
}

export function PatientIdentificationModal({ sessionId, expectedCount, autonomous, onConfirm, onCancel }: Props) {
	// Pre-filled with the synthetic test patient's number for faster entry during
	// internal testing -- still a plain editable field, just not blank by default.
	const [mobiles, setMobiles] = useState(() =>
		Array.from({ length: Math.max(1, expectedCount ?? 1) }, () => '9999999999')
	)
	const [loading, setLoading] = useState(false)
	const [error, setError] = useState<string | null>(null)

	// Autonomous mode has no one watching -- auto-submit the pre-filled number after a
	// countdown instead of blocking the flow indefinitely on human input. Any edit to the
	// mobile list cancels it, since that's a sign someone IS present and typing.
	const [autoSubmitIn, setAutoSubmitIn] = useState<number | null>(autonomous ? AUTO_SUBMIT_SECONDS : null)
	const cancelAutoSubmit = () => setAutoSubmitIn(null)

	const updateMobile = (i: number, val: string) => {
		cancelAutoSubmit()
		setMobiles((prev) => prev.map((m, idx) => (idx === i ? val.replace(/\D/g, '').slice(0, 10) : m)))
	}

	const addRow = () => { cancelAutoSubmit(); setMobiles((prev) => [...prev, '']) }

	const removeRow = (i: number) => { cancelAutoSubmit(); setMobiles((prev) => prev.filter((_, idx) => idx !== i)) }

	const handleConfirm = async () => {
		const valid = mobiles.map((m) => m.trim()).filter(Boolean)
		if (!valid.length) {
			setError('Enter at least one mobile number.')
			return
		}
		cancelAutoSubmit()
		setLoading(true)
		setError(null)
		try {
			await identifyPatient(sessionId, valid)
			onConfirm()
		} catch (err) {
			setError(err instanceof Error ? err.message : 'Failed to identify patient.')
			setLoading(false)
		}
	}

	useEffect(() => {
		if (autoSubmitIn == null) return
		if (autoSubmitIn === 0) { handleConfirm(); return }
		const id = setTimeout(() => setAutoSubmitIn((n) => (n ?? 0) - 1), 1000)
		return () => clearTimeout(id)
	}, [autoSubmitIn]) // eslint-disable-line react-hooks/exhaustive-deps

	return (
		<div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm">
			<div className="bg-[var(--bg-surface)] border border-teal-500/40 rounded-2xl p-6 max-w-sm w-full mx-4 shadow-2xl">
				{/* Header */}
				<div className="flex items-center gap-3 mb-5">
					<div className="w-10 h-10 rounded-xl bg-teal-500/10 border border-teal-500/30 flex items-center justify-center flex-shrink-0">
						<UserCheck size={20} className="text-teal-400" />
					</div>
					<div>
						<div className="text-[10px] text-teal-400 font-semibold uppercase tracking-wider">Patient Verification</div>
						<div className="text-sm font-bold text-slate-100 mt-0.5">Identify Patient(s)</div>
					</div>
				</div>

				<p className="text-xs text-slate-400 mb-4 leading-relaxed">
					Enter the mobile number(s) to match patient records before execution begins.
				</p>

				{/* Mobile inputs */}
				<div className="space-y-2 mb-3">
					{mobiles.map((m, i) => (
						<div key={i} className="flex items-center gap-2">
							<div className="flex items-center gap-2 flex-1 px-3 py-2 rounded-lg bg-[var(--bg-base)] border border-[var(--border)] focus-within:border-teal-500/60 transition-colors">
								<Phone size={13} className="text-slate-500 flex-shrink-0" />
								<input
									type="tel"
									value={m}
									onChange={(e) => updateMobile(i, e.target.value)}
									placeholder="10-digit mobile number"
									className="flex-1 bg-transparent text-sm text-slate-200 placeholder-slate-600 outline-none min-w-0"
									autoFocus={i === 0}
								/>
							</div>
							{mobiles.length > 1 && (
								<button
									onClick={() => removeRow(i)}
									className="text-slate-600 hover:text-red-400 transition-colors flex-shrink-0"
								>
									<Trash2 size={14} />
								</button>
							)}
						</div>
					))}
				</div>

				<button
					onClick={addRow}
					className="flex items-center gap-1.5 text-xs text-slate-500 hover:text-teal-400 transition-colors mb-5"
				>
					<Plus size={12} />
					Add another patient
				</button>

				{error && <p className="text-xs text-red-400 mb-4">{error}</p>}

				{autoSubmitIn != null && (
					<p className="text-[11px] text-amber-400/80 mb-4 text-center">
						Autonomous mode — auto-submitting in {autoSubmitIn}s unless edited
					</p>
				)}

				{/* Actions */}
				<div className="flex gap-3">
					<button
						onClick={handleConfirm}
						disabled={loading}
						className="flex-1 flex items-center justify-center gap-2 py-2.5 rounded-xl bg-teal-600 hover:bg-teal-500 disabled:opacity-50 text-white text-sm font-semibold transition-colors"
					>
						<UserCheck size={15} />
						{loading ? 'Identifying...' : 'Confirm & Execute'}
					</button>
					<button
						onClick={onCancel}
						disabled={loading}
						className="px-4 py-2.5 rounded-xl bg-[var(--bg-raised)] hover:bg-[var(--bg-hover)] border border-[var(--border-a)] text-slate-400 text-sm font-semibold transition-colors disabled:opacity-50"
					>
						Cancel
					</button>
				</div>
			</div>
		</div>
	)
}
