# Hospilot Web

The React frontend: the pipeline canvas, approval modals, agent capability views, and admin
console shown in the root README's screenshot. It renders whatever the
[agentic-framework](../agentic-framework) API and its session WebSocket send — it holds no
hospital data or business logic of its own.

---

## Setup

Requires **Node 20+**. The agentic-framework backend (see root [Quick
Start](../README.md#quick-start)) needs to be running first — this app has nothing to
render without it.

```bash
cd web
npm install
cp .env.example .env    # points VITE_API_URL at the agentic-framework API
npm run dev
```

Open `http://localhost:3000`. Vite proxies `/api` and `/ws` to `VITE_API_URL`
(`http://localhost:8000` by default).

| Script | What it does |
| --- | --- |
| `npm run dev` | Vite dev server with hot reload, port 3000 |
| `npm run build` | Type-checks (`tsc`) then produces a production bundle |
| `npm run preview` | Serves the production build locally |

---

## Structure

```
src/
  store/        zustand store -- session, pipeline, approvals, auth, active view
  services/     api.ts: every REST call + types for the agentic-framework API
  hooks/        useSessionWebSocket: the live session event stream
  components/
    canvas/       the agent pipeline graph (@xyflow/react)
    execution/    findings, toasts, approval + patient-ID modals
    subagent/     drill-down view into one agent's sub-agents and tasks
    capabilities/ agent/task registry browser
    admin/        org, user, and capability-authoring management
  data/         static fallback agent/capability/scenario definitions
  lib/          layout + canvas-animation helpers
```

## How it fits together

- **Auth.** A JWT from `POST /auth/login` is kept in `localStorage`
  (`services/api.ts`: `getToken`/`setToken`) and sent as `Authorization: Bearer` on every
  request. `App.tsx` resolves the logged-in user via `getMe()` on boot before rendering
  anything else.
- **One session, one WebSocket.** Submitting a goal creates a session; `useSessionWebSocket`
  opens `/ws/{sessionId}` and is the only path that mutates pipeline/execution state in the
  store afterwards — REST calls kick things off, the socket is what drives the UI live as
  agents run, pause for approval, and finish.
- **Role-gated views.** `activeView` (`orchestrator` / `capabilities` / `approvals` /
  `admin` / `workflows`) is restricted in `App.tsx`: approvers land on `approvals`, only
  `admin`/`super_admin` can reach `admin`.
- **Embedding.** The app detects running inside an iframe (`isEmbedded` in `App.tsx`) and
  expects a `widget_init` postMessage handshake carrying a token, instead of assuming
  "no token in localStorage" means logged out.

See [`CONTRIBUTING.md`](../CONTRIBUTING.md) for coding conventions shared across the repo.
