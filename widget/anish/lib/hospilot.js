const HOSPILOT_BASE = "https://hospilot.carer.ai";

function getCredentials() {
  const username = process.env.HOSPILOT_USERNAME;
  const password = process.env.HOSPILOT_PASSWORD;
  if (!username || !password) {
    const err = new Error(
      "Missing HOSPILOT_USERNAME / HOSPILOT_PASSWORD environment variables"
    );
    err.status = 500;
    throw err;
  }
  return { username, password };
}

async function hospilotFetch(path, { method = "GET", token, body } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;

  const res = await fetch(`${HOSPILOT_BASE}${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  const text = await res.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }

  if (!res.ok) {
    const err = new Error(
      data.detail || data.message || data.error || `Hospilot ${res.status}`
    );
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

async function login() {
  const { username, password } = getCredentials();
  return hospilotFetch("/api/auth/login", {
    method: "POST",
    body: { username, password },
  });
}

async function createSession(token, goal) {
  return hospilotFetch("/api/sessions", {
    method: "POST",
    token,
    body: {
      goal,
      constraints: "",
      autonomous: false,
    },
  });
}

async function getSession(token, sessionId) {
  return hospilotFetch(`/api/sessions/${encodeURIComponent(sessionId)}`, {
    token,
  });
}

function isPipelineReady(session) {
  const pipeline = session?.pipeline;
  if (pipeline == null) return false;
  if (typeof pipeline === "string") return pipeline.trim().length > 0;
  if (Array.isArray(pipeline)) return pipeline.length > 0;
  if (typeof pipeline === "object") return Object.keys(pipeline).length > 0;
  return Boolean(pipeline);
}

module.exports = {
  HOSPILOT_BASE,
  login,
  createSession,
  getSession,
  isPipelineReady,
};
