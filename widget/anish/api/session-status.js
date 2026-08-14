/**
 * Vercel serverless: GET /api/session-status?session_id=...
 * Header: Authorization: Bearer <token>
 * Returns: { session_id, status, ready, pipeline }
 */
const { handleSessionStatus } = require("../lib/handlers");

module.exports = async function handler(req, res) {
  if (req.method === "OPTIONS") {
    res.statusCode = 204;
    res.end();
    return;
  }
  if (req.method !== "GET") {
    res.statusCode = 405;
    res.setHeader("Allow", "GET");
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ error: "Method not allowed" }));
    return;
  }

  const url = new URL(req.url, "http://localhost");
  const sessionId = url.searchParams.get("session_id");
  return handleSessionStatus(req, res, sessionId);
};
