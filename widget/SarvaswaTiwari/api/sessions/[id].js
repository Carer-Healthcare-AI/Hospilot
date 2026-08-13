const { getSession } = require("../_hospilot");

module.exports = async (req, res) => {
  if (req.method !== "GET") return res.status(405).json({ error: "Method not allowed" });

  const sessionId = req.query.id;
  const token = req.headers.authorization?.replace(/^Bearer\s+/i, "");
  if (!sessionId || !token) {
    return res.status(400).json({ error: "session id and token are required" });
  }

  try {
    const session = await getSession(token, sessionId);
    const ready = Boolean(
      session.pipeline &&
        (Array.isArray(session.pipeline)
          ? session.pipeline.length > 0
          : Object.keys(session.pipeline).length > 0)
    );
    return res.status(200).json({ ...session, ready });
  } catch (error) {
    console.error("session polling failed", error);
    return res.status(error.status || 500).json({ error: error.message });
  }
};
