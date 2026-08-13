const http = require("http");
const fs = require("fs");
const path = require("path");
const url = require("url");

const envPath = path.join(__dirname, ".env");
if (fs.existsSync(envPath)) {
  for (const line of fs.readFileSync(envPath, "utf8").split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const idx = trimmed.indexOf("=");
    if (idx === -1) continue;
    const key = trimmed.slice(0, idx).trim();
    const value = trimmed.slice(idx + 1).trim();
    if (!process.env[key]) process.env[key] = value;
  }
}

const { login, createSession, getSession } = require("./api/_hospilot");

const PORT = process.env.PORT || 3000;
const ROOT = __dirname;

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (chunk) => (data += chunk));
    req.on("end", () => {
      try {
        resolve(data ? JSON.parse(data) : {});
      } catch (error) {
        reject(error);
      }
    });
    req.on("error", reject);
  });
}

function sendJson(res, status, payload) {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(payload));
}

function sendFile(res, filePath, contentType) {
  fs.readFile(filePath, (err, data) => {
    if (err) {
      res.writeHead(404);
      res.end("Not found");
      return;
    }
    res.writeHead(200, { "Content-Type": contentType });
    res.end(data);
  });
}

const server = http.createServer(async (req, res) => {
  const parsed = url.parse(req.url, true);
  const pathname = parsed.pathname;

  if (req.method === "POST" && pathname === "/api/session") {
    try {
      const body = await readBody(req);
      const rawGoal = typeof body.goal === "string" ? body.goal.trim() : "";
      if (!rawGoal) return sendJson(res, 400, { error: "goal is required" });

      const candidateName = process.env.CANDIDATE_NAME || "SarvaswaTiwari";
      const goal = rawGoal.startsWith("[CANDIDATE-")
        ? rawGoal
        : `[CANDIDATE-${candidateName}] ${rawGoal}`;

      const auth = await login();
      const session = await createSession(auth.token, goal);
      return sendJson(res, 200, {
        token: auth.token,
        session_id: session.session_id,
        status: session.status
      });
    } catch (error) {
      console.error(error);
      return sendJson(res, error.status || 500, { error: error.message });
    }
  }

  const sessionMatch = pathname.match(/^\/api\/sessions\/([^/]+)$/);
  if (req.method === "GET" && sessionMatch) {
    const sessionId = decodeURIComponent(sessionMatch[1]);
    const token = req.headers.authorization?.replace(/^Bearer\s+/i, "");
    if (!sessionId || !token) {
      return sendJson(res, 400, { error: "session id and token are required" });
    }
    try {
      const session = await getSession(token, sessionId);
      const ready = Boolean(
        session.pipeline &&
          (Array.isArray(session.pipeline)
            ? session.pipeline.length > 0
            : Object.keys(session.pipeline).length > 0)
      );
      return sendJson(res, 200, { ...session, ready });
    } catch (error) {
      console.error(error);
      return sendJson(res, error.status || 500, { error: error.message });
    }
  }

  if (pathname === "/" || pathname === "/index.html") {
    return sendFile(res, path.join(ROOT, "index.html"), "text/html; charset=utf-8");
  }

  res.writeHead(404);
  res.end("Not found");
});

server.listen(PORT, () => {
  console.log(`Hospilot widget running at http://localhost:${PORT}`);
});
