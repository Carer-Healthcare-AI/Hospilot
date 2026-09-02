import { useCallback, useEffect, useState } from 'react'
import { CheckCircle2, XCircle, Loader2, RefreshCw, UserCheck } from 'lucide-react'
import { fetchPendingUsers, approveUser, rejectUser, type ManagedUser } from '../../services/api'
import { Avatar, EmptyState, RoleBadge, timeAgo } from './shared'

interface Props {
  orgNames: Record<string, string>   // org_id -> name (for super_admin cross-org view)
  showOrgColumn: boolean
}

/** The new-user approval queue: approve / reject accounts requesting to join. */
export function PendingUsersView({ orgNames, showOrgColumn }: Props) {
  const [users, setUsers] = useState<ManagedUser[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<Record<string, 'approve' | 'reject'>>({})
  const [error, setError] = useState('')

  const refresh = useCallback(async () => {
    setError('')
    try {
      setUsers(await fetchPendingUsers())
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load pending users')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 15000)
    return () => clearInterval(id)
  }, [refresh])

  async function decide(user: ManagedUser, action: 'approve' | 'reject') {
    setBusy((prev) => ({ ...prev, [user.id]: action }))
    setError('')
    try {
      await (action === 'approve' ? approveUser(user.id) : rejectUser(user.id))
      setUsers((prev) => prev.filter((u) => u.id !== user.id))
    } catch (err) {
      setError(err instanceof Error ? err.message : `Could not ${action} ${user.username}`)
    } finally {
      setBusy((prev) => { const next = { ...prev }; delete next[user.id]; return next })
    }
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <UserCheck size={15} className="text-amber-400" />
          <h2 className="text-sm font-semibold text-slate-200">Pending sign-ups</h2>
          {users.length > 0 && (
            <span className="px-1.5 py-0.5 rounded-md bg-amber-500/15 border border-amber-500/30 text-[10px] font-bold text-amber-300">
              {users.length}
            </span>
          )}
        </div>
        <button
          onClick={refresh}
          className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs text-slate-400 hover:text-slate-200 bg-[var(--bg-raised)] border border-[var(--border-a)] hover:bg-[var(--bg-hover)] transition-colors"
        >
          <RefreshCw size={12} /> Refresh
        </button>
      </div>

      {error && <p className="text-xs text-red-400 mb-3">{error}</p>}

      {loading ? (
        <div className="flex justify-center py-14"><Loader2 size={18} className="animate-spin text-slate-500" /></div>
      ) : users.length === 0 ? (
        <EmptyState message="No accounts waiting for approval." />
      ) : (
        <div className="space-y-2">
          {users.map((u) => (
            <div
              key={u.id}
              className="flex items-center gap-3 p-3 rounded-xl bg-[var(--bg-surface)] border border-[var(--border)]"
            >
              <Avatar name={u.display_name} />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-slate-200 truncate">{u.display_name}</span>
                  <RoleBadge role={u.role} />
                </div>
                <div className="text-[11px] text-slate-500">
                  @{u.username}
                  {showOrgColumn && u.org_id && <> · {orgNames[u.org_id] ?? u.org_id.slice(0, 8)}</>}
                  {' · requested '}{timeAgo(u.created_at)}
                </div>
              </div>
              <button
                onClick={() => decide(u, 'reject')}
                disabled={!!busy[u.id]}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold text-red-300 bg-red-500/10 border border-red-500/30 hover:bg-red-500/20 disabled:opacity-50 transition-colors"
              >
                {busy[u.id] === 'reject' ? <Loader2 size={12} className="animate-spin" /> : <XCircle size={12} />}
                Reject
              </button>
              <button
                onClick={() => decide(u, 'approve')}
                disabled={!!busy[u.id]}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold text-emerald-300 bg-emerald-500/10 border border-emerald-500/30 hover:bg-emerald-500/20 disabled:opacity-50 transition-colors"
              >
                {busy[u.id] === 'approve' ? <Loader2 size={12} className="animate-spin" /> : <CheckCircle2 size={12} />}
                Approve
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
