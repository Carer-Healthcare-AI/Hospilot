import { useCallback, useEffect, useState } from 'react'
import { Loader2, RefreshCw, Users } from 'lucide-react'
import { fetchUsers, updateUser, type ManagedUser, type UserRole } from '../../services/api'
import { Avatar, EmptyState, RoleBadge, StatusBadge, timeAgo } from './shared'

interface Props {
  currentUserId: string
  isSuper: boolean
  orgNames: Record<string, string>
  showOrgColumn: boolean
}

/** Org user management: change roles (doctor <-> approver) and enable/disable accounts. */
export function UsersView({ currentUserId, isSuper, orgNames, showOrgColumn }: Props) {
  const [users, setUsers] = useState<ManagedUser[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<Record<string, boolean>>({})
  const [error, setError] = useState('')

  const refresh = useCallback(async () => {
    setError('')
    try {
      setUsers(await fetchUsers())
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load users')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  async function change(user: ManagedUser, fields: { role?: UserRole; status?: 'active' | 'disabled' }) {
    setBusy((prev) => ({ ...prev, [user.id]: true }))
    setError('')
    try {
      const updated = await updateUser(user.id, fields)
      setUsers((prev) => prev.map((u) => (u.id === user.id ? { ...u, ...updated } : u)))
    } catch (err) {
      setError(err instanceof Error ? err.message : `Could not update ${user.username}`)
    } finally {
      setBusy((prev) => { const next = { ...prev }; delete next[user.id]; return next })
    }
  }

  // Admins can retune doctor<->approver; only super_admin touches admin rows.
  function canManage(u: ManagedUser): boolean {
    if (u.id === currentUserId) return false
    if (u.role === 'super_admin') return false
    if (u.role === 'admin' && !isSuper) return false
    if (u.status === 'pending' || u.status === 'rejected') return false  // approval queue's job
    return true
  }

  const roleOptions: UserRole[] = isSuper ? ['doctor', 'approver', 'admin'] : ['doctor', 'approver']

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <Users size={15} className="text-blue-400" />
          <h2 className="text-sm font-semibold text-slate-200">Users</h2>
          <span className="text-[11px] text-slate-500">{users.length} total</span>
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
        <EmptyState message="No users found." />
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
                  <StatusBadge status={u.status} />
                  {u.id === currentUserId && (
                    <span className="text-[10px] text-slate-500 font-medium">(you)</span>
                  )}
                </div>
                <div className="text-[11px] text-slate-500">
                  @{u.username}
                  {showOrgColumn && u.org_id && <> · {orgNames[u.org_id] ?? u.org_id.slice(0, 8)}</>}
                  {' · joined '}{timeAgo(u.created_at)}
                </div>
              </div>

              {canManage(u) && (
                <>
                  <select
                    value={u.role}
                    disabled={!!busy[u.id]}
                    onChange={(e) => change(u, { role: e.target.value as UserRole })}
                    className="px-2 py-1.5 rounded-lg text-xs bg-[var(--bg-raised)] border border-[var(--border-a)] text-slate-300 focus:outline-none focus:border-blue-500 disabled:opacity-50"
                  >
                    {roleOptions.map((r) => (
                      <option key={r} value={r}>{r === 'approver' ? 'Approver' : r === 'admin' ? 'Admin' : 'Doctor'}</option>
                    ))}
                  </select>
                  <button
                    onClick={() => change(u, { status: u.status === 'disabled' ? 'active' : 'disabled' })}
                    disabled={!!busy[u.id]}
                    className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold border transition-colors disabled:opacity-50 ${
                      u.status === 'disabled'
                        ? 'text-emerald-300 bg-emerald-500/10 border-emerald-500/30 hover:bg-emerald-500/20'
                        : 'text-slate-400 bg-[var(--bg-raised)] border-[var(--border-a)] hover:bg-[var(--bg-hover)] hover:text-red-300'
                    }`}
                  >
                    {busy[u.id] && <Loader2 size={12} className="animate-spin" />}
                    {u.status === 'disabled' ? 'Enable' : 'Disable'}
                  </button>
                </>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
