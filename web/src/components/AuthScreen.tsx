import { useEffect, useState } from 'react'
import { Loader2, Clock } from 'lucide-react'
import { loginUser, signupUser, setToken, fetchPublicOrgs, type AuthUser, type PublicOrg } from '../services/api'

interface Props {
  onAuth: (user: AuthUser) => void
}

export function AuthScreen({ onAuth }: Props) {
  const [tab, setTab] = useState<'login' | 'signup'>('login')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')      // post-signup "awaiting approval" message

  // Login fields
  const [loginUsername, setLoginUsername] = useState('')
  const [loginPassword, setLoginPassword] = useState('')

  // Signup fields
  const [signupUsername, setSignupUsername] = useState('')
  const [signupDisplayName, setSignupDisplayName] = useState('')
  const [signupPassword, setSignupPassword] = useState('')
  const [signupConfirm, setSignupConfirm] = useState('')
  const [signupOrgId, setSignupOrgId] = useState('')
  const [signupRole, setSignupRole] = useState<'doctor' | 'approver' | 'admin'>('doctor')

  // Org picker (multi-tenancy): the hospital this account belongs to.
  const [orgs, setOrgs] = useState<PublicOrg[]>([])
  useEffect(() => {
    fetchPublicOrgs()
      .then((list) => {
        setOrgs(list)
        if (list.length === 1) setSignupOrgId(list[0].id)
      })
      .catch(() => setOrgs([]))
  }, [])

  async function handleLogin(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    setNotice('')
    setLoading(true)
    try {
      const res = await loginUser(loginUsername.trim(), loginPassword)
      setToken(res.token)
      onAuth(res.user)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed')
    } finally {
      setLoading(false)
    }
  }

  async function handleSignup(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    setNotice('')
    if (signupPassword !== signupConfirm) {
      setError('Passwords do not match')
      return
    }
    if (!signupOrgId) {
      setError('Select your organization')
      return
    }
    setLoading(true)
    try {
      // No token comes back -- the account is pending until an admin approves it.
      const res = await signupUser(
        signupUsername.trim(), signupPassword, signupDisplayName.trim(), signupOrgId, signupRole)
      setNotice(res.message || 'Account created. Awaiting approval by your organization admin.')
      setTab('login')
      setLoginUsername(signupUsername.trim())
      setSignupUsername(''); setSignupDisplayName(''); setSignupPassword(''); setSignupConfirm('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign up failed')
    } finally {
      setLoading(false)
    }
  }

  const inputCls = 'w-full px-3 py-2.5 rounded-lg bg-[#0f172a] border border-slate-700 text-sm text-slate-100 placeholder-slate-600 focus:outline-none focus:border-blue-500 transition-colors'
  const labelCls = 'block text-xs font-medium text-slate-400 mb-1'

  return (
    <div className="auth-screen h-screen overflow-y-auto flex justify-center bg-[#080d14] px-4 py-10">
      <div className="w-full max-w-sm h-fit my-auto">

        {/* Logo */}
        <div className="flex flex-col items-center mb-8">
          <img src="/carer.png" alt="Carer" className="h-11 w-auto mb-3" />
          <h1 className="text-xl font-bold text-slate-100">Hospilot</h1>
          <p className="text-xs text-slate-500 mt-0.5">Hospital AI Command Center</p>
        </div>

        {/* Card */}
        <div className="bg-[#0d1625] border border-slate-800 rounded-2xl p-6 shadow-xl">

          {/* Tabs */}
          <div className="flex gap-1 p-1 rounded-lg bg-[#080d14] mb-6">
            {(['login', 'signup'] as const).map((t) => (
              <button
                key={t}
                onClick={() => { setTab(t); setError(''); setNotice('') }}
                className={`flex-1 py-1.5 rounded-md text-sm font-medium transition-all ${
                  tab === t
                    ? 'bg-blue-600 text-white shadow-sm'
                    : 'text-slate-500 hover:text-slate-300'
                }`}
              >
                {t === 'login' ? 'Sign in' : 'Sign up'}
              </button>
            ))}
          </div>

          {notice && (
            <div className="flex items-start gap-2 mb-4 p-3 rounded-lg bg-amber-500/10 border border-amber-500/30">
              <Clock size={14} className="text-amber-400 mt-0.5 flex-shrink-0" />
              <p className="text-xs text-amber-200 leading-snug">{notice}</p>
            </div>
          )}

          {tab === 'login' ? (
            <form onSubmit={handleLogin} className="space-y-4">
              <div>
                <label className={labelCls}>Username</label>
                <input
                  type="text"
                  required
                  autoFocus
                  autoComplete="username"
                  value={loginUsername}
                  onChange={(e) => setLoginUsername(e.target.value)}
                  placeholder="Enter username"
                  className={inputCls}
                />
              </div>
              <div>
                <label className={labelCls}>Password</label>
                <input
                  type="password"
                  required
                  autoComplete="current-password"
                  value={loginPassword}
                  onChange={(e) => setLoginPassword(e.target.value)}
                  placeholder="Enter password"
                  className={inputCls}
                />
              </div>
              {error && <p className="text-xs text-red-400">{error}</p>}
              <button
                type="submit"
                disabled={loading}
                className="w-full py-2.5 rounded-lg bg-blue-600 hover:bg-blue-500 disabled:opacity-60 disabled:cursor-not-allowed text-white text-sm font-semibold transition-colors flex items-center justify-center gap-2"
              >
                {loading && <Loader2 size={14} className="animate-spin" />}
                Sign in
              </button>
            </form>
          ) : (
            <form onSubmit={handleSignup} className="space-y-4">
              <div>
                <label className={labelCls}>Organization</label>
                <select
                  required
                  value={signupOrgId}
                  onChange={(e) => setSignupOrgId(e.target.value)}
                  className={inputCls}
                >
                  <option value="" disabled>Select your hospital…</option>
                  {orgs.map((o) => (
                    <option key={o.id} value={o.id}>{o.name}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className={labelCls}>Role</label>
                <select
                  value={signupRole}
                  onChange={(e) => setSignupRole(e.target.value as 'doctor' | 'approver' | 'admin')}
                  className={inputCls}
                >
                  <option value="doctor">Doctor</option>
                  <option value="approver">Approver</option>
                  <option value="admin">Admin</option>
                </select>
                <p className="text-[10px] text-slate-500 mt-1">
                  Your account needs approval by {signupRole === 'admin' ? 'the platform admin' : "your organization's admin"} before you can sign in.
                </p>
              </div>
              <div>
                <label className={labelCls}>Username</label>
                <input
                  type="text"
                  required
                  autoFocus
                  autoComplete="username"
                  value={signupUsername}
                  onChange={(e) => setSignupUsername(e.target.value)}
                  placeholder="Choose a username"
                  className={inputCls}
                />
              </div>
              <div>
                <label className={labelCls}>Display name</label>
                <input
                  type="text"
                  required
                  autoComplete="name"
                  value={signupDisplayName}
                  onChange={(e) => setSignupDisplayName(e.target.value)}
                  placeholder="Dr. Jane Smith"
                  className={inputCls}
                />
              </div>
              <div>
                <label className={labelCls}>Password</label>
                <input
                  type="password"
                  required
                  autoComplete="new-password"
                  value={signupPassword}
                  onChange={(e) => setSignupPassword(e.target.value)}
                  placeholder="Create a password"
                  className={inputCls}
                />
              </div>
              <div>
                <label className={labelCls}>Confirm password</label>
                <input
                  type="password"
                  required
                  autoComplete="new-password"
                  value={signupConfirm}
                  onChange={(e) => setSignupConfirm(e.target.value)}
                  placeholder="Repeat password"
                  className={inputCls}
                />
              </div>
              {error && <p className="text-xs text-red-400">{error}</p>}
              <button
                type="submit"
                disabled={loading}
                className="w-full py-2.5 rounded-lg bg-blue-600 hover:bg-blue-500 disabled:opacity-60 disabled:cursor-not-allowed text-white text-sm font-semibold transition-colors flex items-center justify-center gap-2"
              >
                {loading && <Loader2 size={14} className="animate-spin" />}
                Create account
              </button>
            </form>
          )}
        </div>
      </div>
    </div>
  )
}
