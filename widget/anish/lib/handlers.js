const {
  login,
  createSession,
  getSession,
  isPipelineReady,
} = require("../lib/hospilot");

const CANDIDATE_PREFIX = "[CANDIDATE-anish]";

function sendJson(res, status, body) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json");
  res.setHeader("Cache-Control", "no-store");
  res.end(JSON.stringify(body));
}

function readJsonBody(req) {
  // Vercel / Express may already parse JSON onto req.body
  if (req.body && typeof req.body === "object" && !Buffer.isBuffer(req.body)) {
    return Promise.resolve(req.body);
  }
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      try {
        const raw = Buffer.concat(chunks).toString("utf8");
        resolve(raw ? JSON.parse(raw) : {});
      } catch (err) {
        reject(err);
      }
    });
    req.on("error", reject);
  });
}

function normalizeGoal(goal) {
  const trimmed = String(goal || "").trim();
  if (!trimmed) return "";
  if (trimmed.toUpperCase().startsWith("[CANDIDATE-")) return trimmed;
  return `${CANDIDATE_PREFIX} ${trimmed}`;
}

async function handleCreateSession(req, res) {
  try {
    const body = await readJsonBody(req);
    const goal = normalizeGoal(body.goal);
    if (!goal) {
      return sendJson(res, 400, { error: "goal is required" });
    }

    const auth = await login();
    const token = auth.token;
    if (!token) {
      return sendJson(res, 502, { error: "Login succeeded but no token returned" });
    }

    const session = await createSession(token, goal);
    return sendJson(res, 200, {
      token,
      session_id: session.session_id,
      status: session.status || "planning",
      goal,
    });
  } catch (err) {
    return sendJson(res, err.status || 500, {
      error: err.message || "Failed to create session",
      details: err.data || undefined,
    });
  }
}

async function handleSessionStatus(req, res, sessionId) {
  try {
    if (!sessionId) {
      return sendJson(res, 400, { error: "session_id is required" });
    }

    const authHeader = req.headers.authorization || "";
    const token = authHeader.startsWith("Bearer ")
      ? authHeader.slice(7)
      : null;
    if (!token) {
      return sendJson(res, 401, {
        error: "Authorization: Bearer <token> required",
      });
    }

    const session = await getSession(token, sessionId);
    const ready = isPipelineReady(session);
    return sendJson(res, 200, {
      session_id: sessionId,
      status: session.status,
      ready,
      pipeline: session.pipeline ?? null,
    });
  } catch (err) {
    return sendJson(res, err.status || 500, {
      error: err.message || "Failed to fetch session",
      details: err.data || undefined,
    });
  }
}

module.exports = {
  handleCreateSession,
  handleSessionStatus,
  sendJson,
};
