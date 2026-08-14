require("dotenv").config();

const path = require("path");
const express = require("express");
const { handleCreateSession, handleSessionStatus } = require("./lib/handlers");

const app = express();
const PORT = process.env.PORT || 3000;

app.use(express.json({ limit: "1mb" }));

app.post("/api/create-session", (req, res) => handleCreateSession(req, res));

app.get("/api/session-status", (req, res) => {
  const sessionId = req.query.session_id;
  return handleSessionStatus(req, res, sessionId);
});

app.get(["/", "/demo.html"], (_req, res) => {
  res.sendFile(path.join(__dirname, "demo.html"));
});

app.get("/widget.js", (_req, res) => {
  res.type("application/javascript");
  res.sendFile(path.join(__dirname, "widget.js"));
});

app.listen(PORT, () => {
  console.log(`Hospilot widget (anish) at http://localhost:${PORT}`);
  console.log(`Open http://localhost:${PORT}/demo.html`);
});
