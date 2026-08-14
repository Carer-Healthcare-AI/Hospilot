(() => {
  const POLL_MS = 2000;
  const MAX_POLLS = 45;

  const root = document.getElementById("hospilot-widget");
  const goalInput = document.getElementById("goal");
  const createBtn = document.getElementById("create-plan");
  const viewBtn = document.getElementById("view-plan");
  const statusEl = document.getElementById("status");
  const statusBlock = document.getElementById("status-block");
  const container = document.getElementById("hospilot-container");
  const frame = document.getElementById("hospilot-frame");
  const closeBtn = document.getElementById("close-plan");
  const scrim = document.getElementById("pipeline-scrim");
  const toggleBtn = document.getElementById("widget-toggle");
  const panel = document.getElementById("widget-panel");
  const steps = [...document.querySelectorAll(".hp-step")];
  const sessionLabel = document.getElementById("pipeline-session");
  const signalEl = document.getElementById("pipeline-signal");
  const goalLabel = document.getElementById("pipeline-goal");

  let token = null;
  let sessionId = null;
  let lastGoal = "";
  let pollTimer = null;
  let pendingInit = false;

  function setStageOpen(open) {
    container.classList.toggle("is-open", open);
    container.setAttribute("aria-hidden", open ? "false" : "true");
    if (open) {
      // Keep HIS readable; tuck the mission console while the sheet is up
      setOpen(false);
    }
    if (!open) {
      frame.removeAttribute("src");
      pendingInit = false;
      if (sessionLabel) sessionLabel.textContent = "—";
      if (signalEl) signalEl.textContent = "IDLE";
      if (goalLabel) goalLabel.textContent = "—";
    }
  }

  function setStep(active) {
    const order = ["goal", "plan", "view"];
    const idx = order.indexOf(active);
    steps.forEach((el) => {
      const key = el.dataset.step;
      const i = order.indexOf(key);
      el.classList.toggle("is-active", key === active);
      el.classList.toggle("is-done", i < idx);
    });
  }

  function setStatus(message, kind = "") {
    statusEl.textContent = message || "";
    statusEl.dataset.kind = kind;
    if (statusBlock) statusBlock.dataset.kind = kind;
  }

  function setBusy(busy) {
    createBtn.disabled = busy;
    goalInput.disabled = busy;
    createBtn.textContent = busy ? "Building plan…" : "Generate plan";
  }

  function setOpen(open) {
    root?.classList.toggle("is-open", open);
    panel?.classList.toggle("open", open);
    if (toggleBtn) toggleBtn.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function stopPolling() {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  async function createSession(goal) {
    const res = await fetch("/api/create-session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ goal }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.error || `Create failed (${res.status})`);
    }
    return data;
  }

  async function fetchStatus(id, bearer) {
    const res = await fetch(
      `/api/session-status?session_id=${encodeURIComponent(id)}`,
      { headers: { Authorization: `Bearer ${bearer}` } }
    );
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.error || `Status failed (${res.status})`);
    }
    return data;
  }

  function sendWidgetInit() {
    if (!frame?.contentWindow || !token || !sessionId) return;
    frame.contentWindow.postMessage(
      { type: "widget_init", token, sessionId },
      "*"
    );
    setStatus("Pipeline loaded in Hospilot.", "ok");
    if (signalEl) signalEl.textContent = "SYNCED";
    setStep("view");
  }

  function openPlan() {
    if (!token || !sessionId) return;
    if (sessionLabel) sessionLabel.textContent = sessionId;
    if (goalLabel) goalLabel.textContent = lastGoal || goalInput.value || "—";
    if (signalEl) signalEl.textContent = "LINKING";
    setStageOpen(true);
    pendingInit = true;
    frame.src = "https://hospilot.carer.ai";
    setStatus("Loading Hospilot in bottom workspace…", "info");
  }

  function onFrameLoad() {
    if (pendingInit) {
      pendingInit = false;
      setTimeout(sendWidgetInit, 300);
    }
  }

  async function pollUntilReady(id, bearer, attempt = 0) {
    if (attempt >= MAX_POLLS) {
      throw new Error("Timed out waiting for plan. Try again.");
    }
    setStatus(`Planning in progress… ${attempt + 1}/${MAX_POLLS}`, "info");
    const data = await fetchStatus(id, bearer);
    if (data.ready) return data;
    await new Promise((r) => {
      pollTimer = setTimeout(r, POLL_MS);
    });
    return pollUntilReady(id, bearer, attempt + 1);
  }

  async function onCreate() {
    const goal = (goalInput.value || "").trim();
    if (!goal) {
      setStatus("Enter an operational goal first.", "error");
      goalInput.focus();
      return;
    }

    stopPolling();
    viewBtn.classList.remove("is-visible");
    setStageOpen(false);
    token = null;
    sessionId = null;
    lastGoal = goal;
    setBusy(true);
    setStep("plan");
    setStatus("Authenticating & creating session…", "info");

    try {
      const created = await createSession(goal);
      token = created.token;
      sessionId = created.session_id;
      lastGoal = created.goal || goal;
      setStatus("Session created. Waiting for pipeline…", "info");
      await pollUntilReady(sessionId, token);
      viewBtn.classList.add("is-visible");
      setStatus("Plan ready — pull up the live pipeline workspace.", "ok");
      setStep("view");
    } catch (err) {
      setStatus(err.message || "Something went wrong", "error");
      setStep("goal");
    } finally {
      setBusy(false);
    }
  }

  createBtn?.addEventListener("click", onCreate);
  goalInput?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) onCreate();
  });
  viewBtn?.addEventListener("click", openPlan);
  frame?.addEventListener("load", onFrameLoad);
  closeBtn?.addEventListener("click", () => setStageOpen(false));
  scrim?.addEventListener("click", () => setStageOpen(false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && container.classList.contains("is-open")) {
      setStageOpen(false);
    }
  });
  toggleBtn?.addEventListener("click", () => {
    setOpen(!root?.classList.contains("is-open"));
  });

  setOpen(true);
  setStep("goal");
})();
