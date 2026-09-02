import { useCallback, useEffect, useState } from 'react'
import { Building2, Loader2, Plus, RefreshCw } from 'lucide-react'
import { createOrg, fetchOrgs, updateOrg, type Organization } from '../../services/api'
import { EmptyState, StatusBadge, timeAgo } from './shared'

/** super_admin only: create / enable / disable tenant organizations. */
export function OrgsView() {
  const [orgs, setOrgs] = useState<Organization[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<Record<string, boolean>>({})
  const [error, setError] = useState('')

  // Create form
  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState('')
  const [newSlug, setNewSlug] = useState('')
  const [creating, setCreating] = useState(false)
  // Slug currently being provisioned in the background (drives the poll + notice).
  const [provisioningSlug, setProvisioningSlug] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    setError('')
    try {
      setOrgs(await fetchOrgs())
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load organizations')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  // While an org provisions, poll until it flips out of 'provisioning'. Give up
  // after ~90s and stop the spinner rather than polling forever; the org row's
  // status badge continues to reflect where it landed.
  useEffect(() => {
    if (!provisioningSlug) return
    let cancelled = false
    const poll = setInterval(async () => {
      try {
        const list = await fetchOrgs()
        if (cancelled) return
        setOrgs(list)
        const org = list.find((o) => o.slug === provisioningSlug)
        if (org && org.status !== 'provisioning') setProvisioningSlug(null)
      } catch { /* transient; next tick retries */ }
    }, 2500)
    const giveUp = setTimeout(() => {
      if (!cancelled) setProvisioningSlug(null)
    }, 90_000)
    return () => { cancelled = true; clearInterval(poll); clearTimeout(giveUp) }
  }, [provisioningSlug])

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    setCreating(true)
    try {
      const org = await createOrg(newName.trim(), newSlug.trim().toLowerCase())
      setOrgs((prev) => [...prev, org])
      setProvisioningSlug(org.slug)
      setNewName(''); setNewSlug(''); setShowCreate(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not create organization')
    } finally {
      setCreating(false)
    }
  }

  async function toggleStatus(org: Organization) {
    setBusy((prev) => ({ ...prev, [org.id]: true }))
    setError('')
    try {
      const updated = await updateOrg(org.id, { status: org.status === 'disabled' ? 'active' : 'disabled' })
      setOrgs((prev) => prev.map((o) => (o.id === org.id ? { ...o, ...updated } : o)))
    } catch (err) {
      setError(err instanceof Error ? err.message : `Could not update ${org.name}`)
    } finally {
      setBusy((prev) => { const next = { ...prev }; delete next[org.id]; return next })
    }
  }

  const inputCls = 'w-full px-3 py-2 rounded-lg bg-[var(--bg-raised)] border border-[var(--border-a)] text-sm text-slate-100 placeholder-slate-600 focus:outline-none focus:border-blue-500 transition-colors'

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <Building2 size={15} className="text-purple-400" />
          <h2 className="text-sm font-semibold text-slate-200">Organizations</h2>
          <span className="text-[11px] text-slate-500">{orgs.length} total</span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={refresh}
            className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs text-slate-400 hover:text-slate-200 bg-[var(--bg-raised)] border border-[var(--border-a)] hover:bg-[var(--bg-hover)] transition-colors"
          >
            <RefreshCw size={12} /> Refresh
          </button>
          <button
            onClick={() => setShowCreate((v) => !v)}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold text-white bg-blue-600 hover:bg-blue-500 transition-colors"
          >
            <Plus size={12} /> New organization
          </button>
        </div>
      </div>

      {error && <p className="text-xs text-red-400 mb-3">{error}</p>}

      {provisioningSlug && (
        <div className="flex items-center gap-2 mb-4 p-3 rounded-lg bg-blue-500/10 border border-blue-500/30">
          <Loader2 size={13} className="text-blue-400 animate-spin flex-shrink-0" />
          <div className="text-xs text-blue-200 leading-snug">
            Provisioning <strong>{provisioningSlug}</strong> — creating the tenant database and wiring Hasura.
            This usually takes a few seconds; it will go <strong>active</strong> automatically.
          </div>
        </div>
      )}

      {showCreate && (
        <form onSubmit={handleCreate} className="flex items-end gap-2 mb-4 p-3 rounded-xl bg-[var(--bg-surface)] border border-[var(--border)]">
          <div className="flex-1">
            <label className="block text-[11px] font-medium text-slate-400 mb-1">Hospital name</label>
            <input required value={newName} onChange={(e) => setNewName(e.target.value)}
                   placeholder="Acme General Hospital" className={inputCls} />
          </div>
          <div className="flex-1">
            <label className="block text-[11px] font-medium text-slate-400 mb-1">Slug (lowercase, a-z 0-9 -)</label>
            <input required value={newSlug} onChange={(e) => setNewSlug(e.target.value)}
                   pattern="[a-z0-9][a-z0-9-]*" placeholder="acme-general" className={inputCls} />
          </div>
          <button
            type="submit"
            disabled={creating}
            className="flex items-center gap-1.5 px-4 py-2 rounded-lg text-xs font-semibold text-white bg-blue-600 hover:bg-blue-500 disabled:opacity-60 transition-colors"
          >
            {creating && <Loader2 size={12} className="animate-spin" />}
            Create
          </button>
        </form>
      )}

      {loading ? (
        <div className="flex justify-center py-14"><Loader2 size={18} className="animate-spin text-slate-500" /></div>
      ) : orgs.length === 0 ? (
        <EmptyState message="No organizations yet." />
      ) : (
        <div className="space-y-2">
          {orgs.map((o) => (
            <div key={o.id} className="flex items-center gap-3 p-3 rounded-xl bg-[var(--bg-surface)] border border-[var(--border)]">
              <div className="w-8 h-8 rounded-lg bg-purple-500/15 border border-purple-500/30 flex items-center justify-center flex-shrink-0">
                <Building2 size={14} className="text-purple-300" />
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-slate-200 truncate">{o.name}</span>
                  <StatusBadge status={o.status} />
                </div>
                <div className="text-[11px] text-slate-500 font-mono">
                  {o.slug}
                  {o.hasura_source && <> · source: {o.hasura_source}</>}
                  {' · created '}{timeAgo(o.created_at)}
                </div>
              </div>
              {o.slug !== 'carer' && o.status !== 'provisioning' && (
                <button
                  onClick={() => toggleStatus(o)}
                  disabled={!!busy[o.id]}
                  className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold border transition-colors disabled:opacity-50 ${
                    o.status === 'disabled'
                      ? 'text-emerald-300 bg-emerald-500/10 border-emerald-500/30 hover:bg-emerald-500/20'
                      : 'text-slate-400 bg-[var(--bg-raised)] border-[var(--border-a)] hover:bg-[var(--bg-hover)] hover:text-red-300'
                  }`}
                >
                  {busy[o.id] && <Loader2 size={12} className="animate-spin" />}
                  {o.status === 'disabled' ? 'Enable' : 'Disable'}
                </button>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
