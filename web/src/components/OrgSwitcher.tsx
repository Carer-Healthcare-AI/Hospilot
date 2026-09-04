import { useEffect, useState } from 'react'
import { Building2, ChevronDown, Loader2 } from 'lucide-react'
import { useStore } from '../store'
import { fetchOrgs, type Organization } from '../services/api'

/**
 * super_admin-only tenant selector. A super_admin's JWT has org_id: null, so
 * org-scoped routes (starting a workflow, listing sessions, …) need an explicit
 * target — this switcher supplies it. The choice is persisted in the store and
 * threaded into requests via ?org_id=. Regular users never see this.
 */
export function OrgSwitcher() {
  const currentUser = useStore((s) => s.currentUser)
  const activeOrgId = useStore((s) => s.activeOrgId)
  const setActiveOrgId = useStore((s) => s.setActiveOrgId)

  const [orgs, setOrgs] = useState<Organization[]>([])
  const [loading, setLoading] = useState(true)

  const isSuperAdmin = currentUser?.role === 'super_admin'

  useEffect(() => {
    if (!isSuperAdmin) return
    let cancelled = false
    fetchOrgs()
      .then((list) => {
        if (cancelled) return
        // Only tenants that can actually run workflows are selectable.
        const selectable = list.filter((o) => o.status === 'active')
        setOrgs(selectable)
        // No org targeted yet → default to the first active tenant so the very
        // first workflow doesn't 400 on "super_admin must target an org".
        if (selectable.length > 0 && !selectable.some((o) => o.id === activeOrgId)) {
          setActiveOrgId(selectable[0].id)
        }
      })
      .catch((err) => console.error('[OrgSwitcher] failed to load orgs', err))
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
    // activeOrgId is read but intentionally not a dep: we seed a default once on
    // mount, and must not re-fetch/re-seed every time the selection changes.
  }, [isSuperAdmin])

  if (!isSuperAdmin) return null

  return (
    <div
      className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-purple-500/40 bg-purple-500/10 cursor-pointer hover:bg-purple-500/15 transition-colors"
      title="Target organization for workflows and sessions"
    >
      {loading
        ? <Loader2 size={12} className="text-purple-300 animate-spin" />
        : <Building2 size={12} className="text-purple-300 flex-shrink-0" />}
      <span className="text-[10px] font-semibold text-purple-300/80 hidden md:inline">Org</span>
      <div className="relative flex items-center">
        <select
          value={activeOrgId ?? ''}
          onChange={(e) => setActiveOrgId(e.target.value)}
          disabled={loading || orgs.length === 0}
          className="bg-transparent text-xs font-semibold text-purple-200 cursor-pointer focus:outline-none appearance-none pr-4 disabled:cursor-not-allowed"
        >
          {orgs.length === 0 && <option value="">No active orgs</option>}
          {orgs.map((o) => (
            <option key={o.id} value={o.id} className="bg-[var(--bg-surface)] text-slate-200">
              {o.name}
            </option>
          ))}
        </select>
        <ChevronDown size={11} className="text-purple-400/70 pointer-events-none absolute right-0" />
      </div>
    </div>
  )
}
