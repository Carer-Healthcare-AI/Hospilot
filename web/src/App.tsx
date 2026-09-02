import { useEffect, useState } from 'react'
import { useStore } from './store'
import { useSessionWebSocket } from './hooks/useSessionWebSocket'
import { Header } from './components/Header'
import { Sidebar } from './components/Sidebar'
import { PipelineCanvas } from './components/canvas/PipelineCanvas'
import { AgentFindings } from './components/execution/AgentFindings'
import { Toaster } from './components/execution/Toaster'
import { ApprovalModal } from './components/execution/ApprovalModal'
import { PatientIdentificationModal } from './components/execution/PatientIdentificationModal'
import { SubAgentView } from './components/subagent/SubAgentView'
import { CheckpointEditorScreen } from './components/canvas/CheckpointEditorScreen'
import { AgentCapabilitiesView } from './components/capabilities/AgentCapabilitiesView'
import { WorkflowsPage } from './components/WorkflowsPage'
import { ApprovalsPage } from './components/ApprovalsPage'
import { AdminPage } from './components/admin/AdminPage'
import { AuthScreen } from './components/AuthScreen'
import { getToken, setToken, getMe, type AuthUser } from './services/api'
import { Loader2, ChevronLeft, ChevronRight } from 'lucide-react'

// Embedded in the widget's overlay iframe -- a widget_init handshake carrying a
// fresh token is expected shortly (see below), so the boot-check must not
// conclude "not logged in" the instant it doesn't find one already in localStorage.
const isEmbedded = typeof window !== 'undefined' && window.self !== window.top

function AppShell() {
  useSessionWebSocket()   // opens WS to /ws/{sessionId} whenever a session is active

  const loadAgentRegistry = useStore((s) => s.loadAgentRegistry)
  useEffect(() => { loadAgentRegistry() }, [loadAgentRegistry])

  const panelOpen = useStore((s) => s.panelOpen)
  const subAgentNodeId = useStore((s) => s.subAgentNodeId)
  const checkpointEditorOpen = useStore((s) => s.checkpointEditorOpen)
  const activeView = useStore((s) => s.activeView)
  const setActiveView = useStore((s) => s.setActiveView)
  const currentUser = useStore((s) => s.currentUser)
  const sessionId = useStore((s) => s.sessionId)
  const patientIdentificationPending = useStore((s) => s.patientIdentificationPending)
  const patientIdentificationCount = useStore((s) => s.patientIdentificationCount)
  const clearPatientIdentification = useStore((s) => s.clearPatientIdentification)

  const isApprover = currentUser?.role === 'approver'
  const isAdmin = currentUser?.role === 'admin' || currentUser?.role === 'super_admin'

  // Role-gated views: approvals is approver-only; admin console is admin/super_admin-only.
  useEffect(() => {
    if (activeView === 'approvals' && !isApprover) setActiveView('orchestrator')
    if (activeView === 'admin' && !isAdmin) setActiveView('orchestrator')
    if ((activeView === 'orchestrator' || activeView === 'capabilities' || activeView === 'workflows') && isApprover) setActiveView('approvals')
  }, [activeView, isApprover, isAdmin, setActiveView])

  const isSubAgentView = !!subAgentNodeId

  // User-collapsed state for the Agent Output panel — independent of `panelOpen`
  // (which tracks whether there's output *to* show, not whether the user wants
  // to see it right now).
  const [outputCollapsed, setOutputCollapsed] = useState(false)

  return (
    <div className="flex flex-col h-screen overflow-hidden">
      <Header />
      <Toaster />

      {activeView === 'approvals' && isApprover ? (
        <ApprovalsPage />
      ) : activeView === 'admin' && isAdmin ? (
        <AdminPage />
      ) : activeView === 'capabilities' ? (
        <AgentCapabilitiesView />
      ) : activeView === 'workflows' ? (
        <WorkflowsPage />
      ) : isSubAgentView ? (
        <div className="flex flex-1 overflow-hidden">
          <SubAgentView />
        </div>
      ) : checkpointEditorOpen ? (
        <CheckpointEditorScreen />
      ) : (
        <div className="flex flex-1 overflow-hidden">
          <Sidebar />
          <main className="flex-1 min-w-0 relative overflow-hidden">
            <PipelineCanvas />
          </main>
          {panelOpen && (
            outputCollapsed ? (
              <button
                onClick={() => setOutputCollapsed(false)}
                title="Show Agent Output"
                className="w-7 flex-shrink-0 bg-[var(--bg-base)] border-l border-[var(--border)] flex items-center justify-center hover:bg-[var(--bg-surface)] transition-colors"
              >
                <ChevronLeft size={14} className="text-slate-500" />
              </button>
            ) : (
              <aside className="w-80 2xl:w-96 flex-shrink-0 bg-[var(--bg-base)] border-l border-[var(--border)] flex flex-col overflow-hidden">
                <div className="px-4 py-2.5 border-b border-[var(--border)] flex-shrink-0 flex items-center justify-between">
                  <span className="text-xs font-semibold text-slate-500 uppercase tracking-wider">Agent Output</span>
                  <button
                    onClick={() => setOutputCollapsed(true)}
                    title="Collapse Agent Output"
                    className="text-slate-500 hover:text-slate-300 transition-colors"
                  >
                    <ChevronRight size={14} />
                  </button>
                </div>
                <div className="flex-1 min-h-0 overflow-hidden">
                  <AgentFindings />
                </div>
              </aside>
            )
          )}
        </div>
      )}

      {activeView === 'orchestrator' && <ApprovalModal />}
      {patientIdentificationPending && sessionId && (
        <PatientIdentificationModal
          sessionId={sessionId}
          expectedCount={patientIdentificationCount}
          autonomous={false}
          onConfirm={clearPatientIdentification}
          onCancel={clearPatientIdentification}
        />
      )}
    </div>
  )
}

export default function App() {
  const setCurrentUser = useStore((s) => s.setCurrentUser)
  const currentUser = useStore((s) => s.currentUser)
  const loadSession = useStore((s) => s.loadSession)
  const setActiveView = useStore((s) => s.setActiveView)
  const [checking, setChecking] = useState(true)

  useEffect(() => {
    let cancelled = false
    async function onWidgetInit(e: MessageEvent) {
      if (e.data?.type !== 'widget_init') return
      const { token, sessionId } = e.data
      if (token) {
        setToken(token)
        try {
          const user = await getMe()
          if (cancelled) return
          setCurrentUser(user)
        } catch (_) {
          if (!cancelled) setChecking(false)
          return
        }
      }
      // Idempotent: only (re)load if this is a DIFFERENT session than the iframe is
      // already tracking. Re-expanding the same live session must NOT call loadSession —
      // that does a destructive snapshot rebuild and wipes the live WS-driven state.
      if (sessionId && sessionId !== useStore.getState().sessionId) {
        loadSession(sessionId)
      }
      setActiveView('orchestrator')
      setChecking(false)
    }
    window.addEventListener('message', onWidgetInit)
    return () => { cancelled = true; window.removeEventListener('message', onWidgetInit) }
  }, [setCurrentUser, loadSession, setActiveView])

  useEffect(() => {
    const token = getToken()
    if (!token) {
      // Embedded: a fresh token is likely already on its way via widget_init
      // (postMessage necessarily arrives after this synchronous check runs) --
      // hold the loader instead of flashing AuthScreen and racing the user into
      // signing in as someone else. Give up after a few seconds in case the
      // parent page never sends the handshake (standalone iframe, broken embed).
      // Not embedded: there's no handshake coming, resolve immediately as before.
      if (isEmbedded) {
        const timeout = setTimeout(() => setChecking(false), 4000)
        return () => clearTimeout(timeout)
      }
      setChecking(false)
      return
    }
    getMe()
      .then((user: AuthUser) => {
        setCurrentUser(user)
        setChecking(false)
        if (user.role === 'approver') {
          setActiveView('approvals')
        } else {
          const savedId = localStorage.getItem('hospilot_session_id')
          if (savedId) loadSession(savedId)
        }
      })
      .catch(() => {
        setChecking(false)
      })
  }, [setCurrentUser, loadSession, setActiveView])

  if (checking) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-[#080d14]">
        <Loader2 size={24} className="animate-spin text-blue-500" />
      </div>
    )
  }

  if (!currentUser) {
    return <AuthScreen onAuth={(user) => setCurrentUser(user)} />
  }

  return <AppShell />
}
