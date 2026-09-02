import { useEffect, useState } from 'react'
import { Building2, UserCheck, Users } from 'lucide-react'
import { useStore } from '../../store'
import { fetchOrgs, fetchPendingUsers } from '../../services/api'
import { PendingUsersView } from './PendingUsersView'
import { UsersView } from './UsersView'
import { OrgsView } from './OrgsView'

type AdminTab = 'pending' | 'users' | 'orgs'

/** Admin console (multi-tenancy): pending sign-ups + user management for org
 *  admins, plus organization management for the super admin. */
export function AdminPage() {
  const currentUser = useStore((s) => s.currentUser)
  const isSuper = currentUser?.role === 'super_admin'
  const [tab, setTab] = useState<AdminTab>('pending')
  const [pendingCount, setPendingCount] = useState(0)

  // Org id -> name map for cross-org rows (super admin sees every org's users).
  const [orgNames, setOrgNames] = useState<Record<string, string>>({})
  useEffect(() => {
    if (!isSuper) return
    fetchOrgs()
      .then((orgs) => setOrgNames(Object.fromEntries(orgs.map((o) => [o.id, o.name]))))
      .catch(() => setOrgNames({}))
  }, [isSuper])

  // Badge count on the Pending tab, kept warm across tab switches.
  useEffect(() => {
    let cancelled = false
    const poll = () =>
      fetchPendingUsers()
        .then((us) => { if (!cancelled) setPendingCount(us.length) })
        .catch(() => {})
    poll()
    const id = setInterval(poll, 15000)
    return () => { cancelled = true; clearInterval(id) }
  }, [])

  if (!currentUser) return null

  const tabs: { id: AdminTab; label: string; icon: React.ReactNode; badge?: number; show: boolean }[] = [
    { id: 'pending', label: 'Pending sign-ups', icon: <UserCheck size={13} />, badge: pendingCount, show: true },
    { id: 'users',   label: 'Users',            icon: <Users size={13} />,     show: true },
    { id: 'orgs',    label: 'Organizations',    icon: <Building2 size={13} />, show: isSuper },
  ]

  return (
    <div className="flex-1 overflow-y-auto bg-[var(--bg-base)]">
      <div className="max-w-3xl mx-auto px-6 py-6">
        <h1 className="text-lg font-bold text-slate-100 mb-1">Administration</h1>
        <p className="text-xs text-slate-500 mb-5">
          {isSuper
            ? 'Platform administration — all organizations.'
            : 'Manage your organization’s users and sign-up requests.'}
        </p>

        {/* Topic tabs */}
        <div className="flex items-center gap-1 bg-[var(--bg-surface)] border border-[var(--border)] rounded-xl p-1 mb-6 w-fit">
          {tabs.filter((t) => t.show).map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`flex items-center gap-1.5 px-3.5 py-1.5 rounded-lg text-xs font-medium transition-all ${
                tab === t.id ? 'bg-blue-600 text-white shadow-sm' : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              {t.icon}
              {t.label}
              {!!t.badge && (
                <span className={`px-1.5 py-0.5 rounded-md text-[10px] font-bold ${
                  tab === t.id ? 'bg-white/20 text-white' : 'bg-amber-500/15 text-amber-300'
                }`}>
                  {t.badge}
                </span>
              )}
            </button>
          ))}
        </div>

        {tab === 'pending' && (
          <PendingUsersView orgNames={orgNames} showOrgColumn={isSuper} />
        )}
        {tab === 'users' && (
          <UsersView
            currentUserId={currentUser.id}
            isSuper={isSuper}
            orgNames={orgNames}
            showOrgColumn={isSuper}
          />
        )}
        {tab === 'orgs' && isSuper && <OrgsView />}
      </div>
    </div>
  )
}
