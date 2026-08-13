const { login, createSession } = require("./_hospilot");

module.exports = async (req, res) => {
  if (req.method !== "POST") return res.status(405).json({ error: "Method not allowed" });

  const rawGoal = typeof req.body?.goal === "string" ? req.body.goal.trim() : "";
  if (!rawGoal) return res.status(400).json({ error: "goal is required" });
  if (rawGoal.length > 500) return res.status(400).json({ error: "goal is too long" });

  const candidateName = process.env.CANDIDATE_NAME || "SarvaswaTiwari";
  const goal = rawGoal.startsWith("[CANDIDATE-")
    ? rawGoal
    : `[CANDIDATE-${candidateName}] ${rawGoal}`;

  try {
    const auth = await login();
    const session = await createSession(auth.token, goal);
    return res.status(200).json({
      token: auth.token,
      session_id: session.session_id,
      status: session.status
    });
  } catch (error) {
    console.error("session creation failed", error);
    return res.status(error.status || 500).json({ error: error.message });
  }
};
