import { useEffect, useState } from 'react'
import { Check } from 'lucide-react'
import type { UserRole } from '../../services/api'

// ── Shared building blocks for the Admin views ────────────────────────────────

/** A number input that shows an explicit Save button as soon as its value differs from
 *  the committed value (and Enter as a shortcut). onSave gets the raw string so the caller
 *  can treat empty as "clear / inherit". After the parent updates `value`, the draft resets
 *  and the button disappears — so a visible button == unsaved changes. */
export function SavableNumber({
  value, onSave, min = 0, placeholder, disabled, width = 'w-16',
}: {
  value: number | null
  onSave: (raw: string) => void
  min?: number
  placeholder?: string
  disabled?: boolean
  width?: string
}) {
  const committed = value === null || value === undefined ? '' : String(value)
  const [draft, setDraft] = useState(committed)
  useEffect(() => { setDraft(committed) }, [committed])
  const dirty = draft.trim() !== committed

  return (
    <span className="inline-flex items-center gap-1">
      <input
        type="number"
        min={min}
        value={draft}
        placeholder={placeholder}
        disabled={disabled}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter' && dirty) onSave(draft) }}
        className={`${width} px-2 py-1 rounded-md bg-[var(--bg-raised)] border text-xs text-slate-200 text-right placeholder-slate-600 focus:outline-none focus:border-blue-500 disabled:opacity-50 ${
          dirty ? 'border-amber-500/50' : 'border-[var(--border-a)]'
        }`}
      />
      {dirty && (
        <button
          onClick={() => onSave(draft)}
          disabled={disabled}
          title="Apply change"
          className="flex items-center gap-0.5 px-1.5 py-1 rounded-md text-[10px] font-semibold text-emerald-300 bg-emerald-500/10 border border-emerald-500/30 hover:bg-emerald-500/20 disabled:opacity-50"
        >
          <Check size={11} /> Save
        </button>
      )}
    </span>
  )
}

export const ROLE_STYLES: Record<UserRole, { label: string; cls: string }> = {
  super_admin: { label: 'Super Admin', cls: 'bg-purple-500/15 text-purple-300 border-purple-500/30' },
  admin:       { label: 'Admin',       cls: 'bg-blue-500/15 text-blue-300 border-blue-500/30' },
  approver:    { label: 'Approver',    cls: 'bg-teal-500/15 text-teal-300 border-teal-500/30' },
  doctor:      { label: 'Doctor',      cls: 'bg-slate-500/15 text-slate-300 border-slate-500/30' },
}

export const STATUS_STYLES: Record<string, { label: string; cls: string }> = {
  pending:      { label: 'Pending',      cls: 'bg-amber-500/15 text-amber-300 border-amber-500/30' },
  active:       { label: 'Active',       cls: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30' },
  rejected:     { label: 'Rejected',     cls: 'bg-red-500/15 text-red-300 border-red-500/30' },
  disabled:     { label: 'Disabled',     cls: 'bg-slate-500/15 text-slate-400 border-slate-500/30' },
  provisioning: { label: 'Provisioning', cls: 'bg-amber-500/15 text-amber-300 border-amber-500/30' },
}

export function RoleBadge({ role }: { role: UserRole }) {
  const s = ROLE_STYLES[role] ?? ROLE_STYLES.doctor
  return (
    <span className={`inline-flex px-2 py-0.5 rounded-md border text-[10px] font-semibold ${s.cls}`}>
      {s.label}
    </span>
  )
}

export function StatusBadge({ status }: { status: string }) {
  const s = STATUS_STYLES[status] ?? { label: status, cls: 'bg-slate-500/15 text-slate-300 border-slate-500/30' }
  return (
    <span className={`inline-flex px-2 py-0.5 rounded-md border text-[10px] font-semibold ${s.cls}`}>
      {s.label}
    </span>
  )
}

export function timeAgo(iso: string | null): string {
  if (!iso) return '—'
  const secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000)
  if (secs < 60) return `${secs}s ago`
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`
  return `${Math.floor(secs / 86400)}d ago`
}

export function Avatar({ name }: { name: string }) {
  const initials = name.split(' ').map((w) => w[0]).join('').slice(0, 2).toUpperCase()
  return (
    <div className="w-8 h-8 rounded-lg bg-[var(--bg-raised)] border border-[var(--border-a)] flex items-center justify-center text-[11px] font-bold text-slate-300 flex-shrink-0">
      {initials}
    </div>
  )
}

export function EmptyState({ message }: { message: string }) {
  return (
    <div className="flex items-center justify-center py-14 text-sm text-slate-500">{message}</div>
  )
}
