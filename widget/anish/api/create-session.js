/**
 * Vercel serverless: POST /api/create-session
 * Body: { goal: string }
 * Returns: { token, session_id, status, goal }
 */
const { handleCreateSession } = require("../lib/handlers");

module.exports = async function handler(req, res) {
  if (req.method === "OPTIONS") {
    res.statusCode = 204;
    res.end();
    return;
  }
  if (req.method !== "POST") {
    res.statusCode = 405;
    res.setHeader("Allow", "POST");
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ error: "Method not allowed" }));
    return;
  }
  return handleCreateSession(req, res);
};
