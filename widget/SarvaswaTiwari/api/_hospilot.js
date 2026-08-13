const BASE_URL = process.env.HOSPILOT_BASE_URL || "https://hospilot.carer.ai";

async function hospilot(path, options = {}) {
  const response = await fetch(`${BASE_URL}${path}`, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {})
    }
  });

  const text = await response.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    data = { raw: text };
  }

  if (!response.ok) {
    const message =
      data?.detail || data?.message || data?.error || `Hospilot API returned ${response.status}`;
    const error = new Error(message);
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

async function login() {
  if (!process.env.HOSPILOT_USERNAME || !process.env.HOSPILOT_PASSWORD) {
    throw new Error("HOSPILOT_USERNAME and HOSPILOT_PASSWORD are not configured.");
  }
  return hospilot("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({
      username: process.env.HOSPILOT_USERNAME,
      password: process.env.HOSPILOT_PASSWORD
    })
  });
}

async function createSession(token, goal) {
  return hospilot("/api/sessions", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    body: JSON.stringify({ goal, constraints: "", autonomous: false })
  });
}

async function getSession(token, sessionId) {
  return hospilot(`/api/sessions/${encodeURIComponent(sessionId)}`, {
    headers: { Authorization: `Bearer ${token}` }
  });
}

module.exports = { login, createSession, getSession };
