import { useState, useRef, useEffect } from 'react'
import { ChevronDown, CheckCircle, Sun, Moon, Cloud, Loader2, Save, LogOut, ShieldCheck, Workflow as WorkflowIcon } from 'lucide-react'
import { useTheme } from 'next-themes'
import { useStore } from '../store'
import { OrgSwitcher } from './OrgSwitcher'

// In the widget's expand iframe the panel owns the theme — hide the in-app toggle.
const isEmbedded = typeof window !== 'undefined' && window.self !== window.top

export function Header() {
  const activeView = useStore((s) => s.activeView)
  const setActiveView = useStore((s) => s.setActiveView)
  const executionStatus = useStore((s) => s.executionStatus)
  const pipelineSaveStatus = useStore((s) => s.pipelineSaveStatus)
  const sessionId = useStore((s) => s.sessionId)
  const pipelineGenerated = useStore((s) => s.pipelineGenerated)
  const saveNow = useStore((s) => s.saveNow)
  const { theme, setTheme } = useTheme()
  const currentUser = useStore((s) => s.currentUser)
  const logout = useStore((s) => s.logout)
  const isAdmin = currentUser?.role === 'admin' || currentUser?.role === 'super_admin'

  const initials = currentUser
    ? currentUser.display_name.split(' ').map((w) => w[0]).join('').slice(0, 2).toUpperCase()
    : '?'

  // User menu dropdown (holds Admin + Sign out). Closes on outside click / Escape.
  const [userMenuOpen, setUserMenuOpen] = useState(false)
  const userMenuRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!userMenuOpen) return
    function onDown(e: MouseEvent) {
      if (userMenuRef.current && !userMenuRef.current.contains(e.target as Node)) setUserMenuOpen(false)
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setUserMenuOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [userMenuOpen])

  const roleLabel = currentUser?.role === 'super_admin'
    ? 'Platform · Super Admin'
    : `${currentUser?.org_name ?? '—'} · ${
        currentUser?.role === 'admin' ? 'Admin'
        : currentUser?.role === 'approver' ? 'Approver' : 'Doctor'}`

  return (
    <header className="relative z-[60] grid grid-cols-[1fr_auto_1fr] items-center px-5 py-0 border-b border-[var(--border)] bg-[var(--bg-base)] flex-shrink-0 h-14">
      {/* Logo */}
      <div className="flex items-center gap-2.5 col-start-1 justify-self-start min-w-0">
        <img src="/carer.png" alt="Carer" className="h-6 w-auto flex-shrink-0" />
        <div className="min-w-0">
          <div className="text-sm font-bold text-slate-100 leading-tight truncate">Hospilot</div>
          <div className="text-[10px] text-slate-500 leading-tight truncate hidden lg:block">Hospital AI Command Center</div>
        </div>
      </div>

      {/* Right: mode + status + theme toggle + user */}
      <div className="flex items-center gap-2 xl:gap-3 col-start-3 justify-self-end min-w-0">
        {/* super_admin tenant selector — supplies the org that org-scoped calls target */}
        <OrgSwitcher />


        {/* Pipeline save button — only for real (non-local) sessions in idle state */}
        {pipelineGenerated && sessionId && executionStatus === 'idle' && (
          <button
            onClick={() => saveNow()}
            disabled={pipelineSaveStatus === 'saving'}
            title={pipelineSaveStatus === 'saved' ? 'Pipeline saved — click to save again' : 'Save pipeline'}
            className={`flex items-center gap-1.5 px-2 py-1 rounded-lg text-[10px] font-medium border transition-colors ${
              pipelineSaveStatus === 'unsaved'
                ? 'border-amber-500/40 bg-amber-500/10 text-amber-400 hover:bg-amber-500/20 cursor-pointer'
                : pipelineSaveStatus === 'saving'
                ? 'border-slate-700 bg-transparent text-slate-500 cursor-not-allowed'
                : 'border-slate-700/60 bg-transparent text-slate-600 hover:text-slate-400 hover:border-slate-600 cursor-pointer'
            }`}
          >
            {pipelineSaveStatus === 'saving' && <Loader2 size={10} className="animate-spin" />}
            {pipelineSaveStatus === 'saved'  && <Cloud size={10} />}
            {pipelineSaveStatus === 'unsaved' && <Save size={10} />}
            <span>
              {pipelineSaveStatus === 'saving'  ? 'Saving…'
               : pipelineSaveStatus === 'saved' ? 'Saved'
               : 'Save'}
            </span>
          </button>
        )}

        {/* Execution status pill — text collapses to a dot below xl */}
        {executionStatus !== 'idle' && (
          <div
            className="flex items-center gap-1.5 px-2 xl:px-2.5 py-1 rounded-full bg-[var(--bg-raised)] border border-[var(--border-a)]"
            title={
              executionStatus === 'waiting_approval' ? 'Awaiting Approval'
              : executionStatus === 'pausing' ? 'Pausing…'
              : executionStatus === 'paused' ? 'Paused'
              : executionStatus === 'cancelled' ? 'Cancelled'
              : executionStatus === 'complete_pending' ? 'Finishing'
              : executionStatus
            }
          >
            <span
              className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
                executionStatus === 'running'
                  ? 'bg-blue-400 animate-pulse'
                  : executionStatus === 'waiting_approval' || executionStatus === 'pausing'
                  ? 'bg-amber-400 animate-pulse'
                  : executionStatus === 'paused'
                  ? 'bg-amber-400'
                  : executionStatus === 'cancelled'
                  ? 'bg-slate-400'
                  : executionStatus === 'complete_pending'
                  ? 'bg-teal-400 animate-pulse'
                  : 'bg-teal-400'
              }`}
            />
            <span className="hidden xl:inline text-[10px] text-slate-400 capitalize">
              {executionStatus === 'waiting_approval' ? 'Awaiting Approval'
                : executionStatus === 'pausing' ? 'Pausing…'
                : executionStatus === 'paused' ? 'Paused'
                : executionStatus === 'cancelled' ? 'Cancelled'
                : executionStatus === 'complete_pending' ? 'Finishing'
                : executionStatus}
            </span>
          </div>
        )}

        {/* Mode control moved to the Mission Brief sidebar (below its header). */}

        {/* Agent Capabilities / Autonomous Workflows — icon buttons */}
        <button
          onClick={() => setActiveView('capabilities')}
          title="Agent Capabilities"
          aria-label="Agent Capabilities"
          className={`w-8 h-8 rounded-lg flex items-center justify-center border transition-colors flex-shrink-0 ${
            activeView === 'capabilities'
              ? 'bg-blue-600 border-blue-600 text-white'
              : 'border-[var(--border-a)] bg-[var(--bg-raised)] text-slate-400 hover:bg-[var(--bg-hover)] hover:text-slate-200'
          }`}
        >
          <CheckCircle size={15} className="flex-shrink-0" />
        </button>
        <button
          onClick={() => setActiveView('workflows')}
          title="Workflows"
          aria-label="Workflows"
          className={`w-8 h-8 rounded-lg flex items-center justify-center border transition-colors flex-shrink-0 ${
            activeView === 'workflows'
              ? 'bg-blue-600 border-blue-600 text-white'
              : 'border-[var(--border-a)] bg-[var(--bg-raised)] text-slate-400 hover:bg-[var(--bg-hover)] hover:text-slate-200'
          }`}
        >
          <WorkflowIcon size={15} className="flex-shrink-0" />
        </button>

        {/* Theme toggle — hidden when embedded (the widget panel owns the theme) */}
        {!isEmbedded && (
          <button
            onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
            className="w-8 h-8 rounded-lg flex items-center justify-center border border-[var(--border-a)] bg-[var(--bg-raised)] hover:bg-[var(--bg-hover)] transition-colors flex-shrink-0"
            title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
          >
            {theme === 'dark'
              ? <Sun size={15} className="text-amber-400" />
              : <Moon size={15} className="text-blue-500" />
            }
          </button>
        )}

        {/* User menu — badge opens a dropdown with Admin (admins only) + Sign out */}
        <div className="relative" ref={userMenuRef}>
          <button
            onClick={() => setUserMenuOpen((o) => !o)}
            className={`flex items-center gap-2 pl-1 pr-1.5 py-1 rounded-lg border transition-colors ${
              userMenuOpen ? 'bg-[var(--bg-raised)] border-[var(--border-a)]' : 'border-transparent hover:bg-[var(--bg-raised)]'
            }`}
          >
            <div className="w-8 h-8 rounded-full bg-gradient-to-br from-blue-600 to-blue-800 flex items-center justify-center text-xs font-bold text-white flex-shrink-0">
              {initials}
            </div>
            <div className="hidden sm:block max-w-[160px] text-left">
              <div className="text-xs text-slate-400 truncate leading-tight">
                {currentUser?.display_name ?? ''}
              </div>
              {/* Tenant + role: which hospital this account acts in */}
              <div className="text-[10px] text-slate-600 truncate leading-tight">{roleLabel}</div>
            </div>
            <ChevronDown
              size={13}
              className={`text-slate-500 flex-shrink-0 transition-transform ${userMenuOpen ? 'rotate-180' : ''}`}
            />
          </button>

          {userMenuOpen && (
            <div className="absolute right-0 top-full mt-1.5 w-52 rounded-xl border border-[var(--border-a)] bg-[var(--bg-surface)] shadow-2xl z-[70] py-1.5 overflow-hidden">
              {/* Identity header — also carries the name/role on small screens where the badge hides them */}
              <div className="px-3 py-2 border-b border-[var(--border)] mb-1">
                <div className="text-xs font-semibold text-slate-200 truncate">{currentUser?.display_name ?? ''}</div>
                <div className="text-[10px] text-slate-500 truncate">{roleLabel}</div>
              </div>

              {isAdmin && (
                <button
                  onClick={() => { setActiveView('admin'); setUserMenuOpen(false) }}
                  className={`w-full flex items-center gap-2.5 px-3 py-2 text-xs transition-colors ${
                    activeView === 'admin'
                      ? 'text-blue-400 bg-blue-500/10'
                      : 'text-slate-300 hover:bg-[var(--bg-hover)]'
                  }`}
                >
                  <ShieldCheck size={14} className="flex-shrink-0" />
                  Admin
                  {activeView === 'admin' && <CheckCircle size={12} className="ml-auto flex-shrink-0" />}
                </button>
              )}

              <button
                onClick={() => { setUserMenuOpen(false); logout() }}
                className="w-full flex items-center gap-2.5 px-3 py-2 text-xs text-slate-300 hover:bg-[var(--bg-hover)] hover:text-red-400 transition-colors"
              >
                <LogOut size={14} className="flex-shrink-0" />
                Sign out
              </button>
            </div>
          )}
        </div>
      </div>
    </header>
  )
}
