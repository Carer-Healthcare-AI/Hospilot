import { useEffect } from 'react'
import { AlertTriangle, AlertCircle, ArrowUpCircle, X } from 'lucide-react'
import { useStore } from '../../store'
import type { Toast } from '../../store'

// Transient notifications for autonomous-mode exceptions (approval needed,
// escalation, auto-reject). Persistent detail still lives in the Agent Output
// "Autonomous Decisions" stream — these just grab attention for a few seconds.
const STYLES: Record<Toast['severity'], { border: string; icon: typeof AlertCircle; iconColor: string }> = {
  info:     { border: '#3b82f6', icon: AlertCircle,   iconColor: '#60a5fa' },
  warning:  { border: '#f59e0b', icon: AlertTriangle, iconColor: '#fbbf24' },
  critical: { border: '#ef4444', icon: ArrowUpCircle, iconColor: '#f87171' },
}

function ToastCard({ toast }: { toast: Toast }) {
  const dismissToast = useStore((s) => s.dismissToast)
  const s = STYLES[toast.severity]
  const Icon = s.icon

  useEffect(() => {
    if (toast.sticky) return   // sticky toasts stay until dismissed by hand
    const t = setTimeout(() => dismissToast(toast.id), 6000)
    return () => clearTimeout(t)
  }, [toast.id, toast.sticky, dismissToast])

  return (
    <div
      className="w-80 max-w-[calc(100vw-2rem)] rounded-xl border bg-[var(--bg-surface)] shadow-2xl px-3.5 py-3 flex items-start gap-2.5"
      style={{ borderColor: s.border + '66', borderLeftColor: s.border, borderLeftWidth: 3 }}
    >
      <Icon size={16} className="flex-shrink-0 mt-0.5" style={{ color: s.iconColor }} />
      <div className="min-w-0 flex-1">
        <div className="text-xs font-bold text-slate-200 leading-tight">{toast.title}</div>
        {toast.message ? (
          <div className="text-[11px] text-slate-400 leading-snug mt-0.5 break-words">{toast.message}</div>
        ) : null}
      </div>
      <button
        onClick={() => dismissToast(toast.id)}
        className="text-slate-600 hover:text-slate-300 transition-colors flex-shrink-0"
        title="Dismiss"
      >
        <X size={13} />
      </button>
    </div>
  )
}

export function Toaster() {
  const toasts = useStore((s) => s.toasts)
  if (toasts.length === 0) return null
  return (
    <div className="fixed top-16 right-4 z-[80] flex flex-col gap-2 pointer-events-none">
      {toasts.map((t) => (
        <div key={t.id} className="pointer-events-auto">
          <ToastCard toast={t} />
        </div>
      ))}
    </div>
  )
}
