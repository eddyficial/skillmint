const form = document.querySelector("#createForm");
const formState = document.querySelector("#formState");
const jobState = document.querySelector("#jobState");
const jobTimer = document.querySelector("#jobTimer");
const artifactList = document.querySelector("#artifactList");
const jobsList = document.querySelector("#jobsList");
const playbooksList = document.querySelector("#playbooksList");
const pipeline = document.querySelector("#pipeline");
const pipelineNote = document.querySelector("#pipelineNote");
const systemStrip = document.querySelector("#systemStrip");
const targetOptions = document.querySelector("#targetOptions");
const stepper = document.querySelector("#stepper");
const rightsBasisSelect = document.querySelector("#rightsBasis");
const rightsBasisNote = document.querySelector("#rightsBasisNote");
const exportIntentSelect = document.querySelector("#exportIntent");
const exportIntentNote = document.querySelector("#exportIntentNote");

const targetLabels = {
  claude_code: "Claude",
  codex: "Codex",
  cursor: "Cursor",
  windsurf: "Windsurf",
  markdown: "Markdown",
};

const rightsBasisNotes = {
  owned: "Low risk. Commercial and public export are generally allowed.",
  licensed: "Low risk, as long as your license actually covers this use.",
  internal: "Low risk for private/internal use. Not for public or commercial export.",
  user_attested_permission: "You're vouching for permission yourself — keep this to private or internal use.",
  creative_commons: "Check the exact license variant; \"-NC\" excludes commercial use.",
  public_domain: "Low risk. Commercial and public export are generally allowed.",
  fair_use: "Reviewed, not guaranteed — public/commercial export is likely to be blocked.",
};

const exportIntentNotes = {
  private: "Safest default. Only you (or this machine) will use the result.",
  internal: "Shared within your team/org. Same low-risk bases as private.",
  public: "Can be blocked unless the rights basis clearly supports redistribution.",
  commercial: "Can be blocked unless the rights basis clearly supports commercial use.",
};

const state = {
  activeJobId: null,
  pollTimer: null,
  timerInterval: null,
  runStartedAt: null,
  formBusy: false,
  systemReady: true,
  systemBlockReason: "",
  targets: ["claude_code", "codex", "cursor", "windsurf", "markdown"],
  currentStep: 1,
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error || `${response.status} ${response.statusText}`);
  }
  return payload;
}

function setPill(el, label, status) {
  el.textContent = label;
  el.className = `state-pill ${status || "is-idle"}`;
}

function setFormBusy(isBusy) {
  state.formBusy = isBusy;
  for (const el of form.querySelectorAll("button, input, select, textarea")) {
    if (el.type !== "reset") {
      el.disabled = isBusy;
    }
  }
  updateSubmitAvailability();
}

function updateSubmitAvailability() {
  const submit = document.querySelector("#submitButton");
  if (!submit) return;
  submit.disabled = state.formBusy || !state.systemReady;
  if (!state.systemReady) {
    submit.title = state.systemBlockReason;
  } else {
    submit.removeAttribute("title");
  }
}

// ---------------------------------------------------------------------------
// Step navigation
// ---------------------------------------------------------------------------

function goToStep(target) {
  const stepNumber = Number(target);
  if (!stepNumber) return;
  for (const fieldset of form.querySelectorAll(".step")) {
    const isActive = Number(fieldset.dataset.step) === stepNumber;
    fieldset.hidden = !isActive;
    fieldset.classList.toggle("is-active", isActive);
  }
  for (const li of stepper.querySelectorAll("li")) {
    const num = Number(li.dataset.step);
    li.classList.toggle("is-active", num === stepNumber);
    li.classList.toggle("is-done", num < stepNumber);
  }
  state.currentStep = stepNumber;
  if (stepNumber === 1) {
    document.querySelector("#source")?.focus();
  }
}

function validateStep(stepNumber) {
  if (stepNumber === 1) {
    const source = fieldValue("source");
    if (!source) {
      document.querySelector("#source")?.reportValidity();
      return false;
    }
    return true;
  }
  if (stepNumber === 2) {
    if (!fieldValue("rightsBasis")) {
      rightsBasisSelect?.reportValidity();
      return false;
    }
    return true;
  }
  return true;
}

form.addEventListener("click", (event) => {
  const nextButton = event.target.closest("[data-next]");
  if (nextButton) {
    if (validateStep(state.currentStep)) {
      goToStep(nextButton.dataset.next);
    }
    return;
  }
  const backButton = event.target.closest("[data-back]");
  if (backButton) {
    goToStep(backButton.dataset.back);
  }
});

rightsBasisSelect?.addEventListener("change", () => {
  const note = rightsBasisNotes[rightsBasisSelect.value];
  rightsBasisNote.textContent = note || "Required — this feeds the rights and provenance gate.";
});

exportIntentSelect?.addEventListener("change", () => {
  exportIntentNote.textContent = exportIntentNotes[exportIntentSelect.value] || "";
});

// ---------------------------------------------------------------------------
// System status + targets
// ---------------------------------------------------------------------------

function renderTargets(targets) {
  state.targets = Array.isArray(targets) && targets.length ? targets : state.targets;
  targetOptions.innerHTML = state.targets
    .map((target, index) => {
      const checked = index === 0 ? "checked" : "";
      const label = targetLabels[target] || target;
      return `
        <label>
          <input type="radio" name="target" value="${escapeHtml(target)}" ${checked}>
          <span>${escapeHtml(label)}</span>
        </label>
      `;
    })
    .join("");
}

async function loadStatus() {
  try {
    const status = await fetchJson("/api/status");
    renderTargets(status.targets);
    const cli = status.claudeCli || {};
    const cliClass = cli.available ? "is-ok" : "is-warn";
    const cliText = cli.available ? "Claude CLI found" : "Claude CLI required";
    state.systemReady = Boolean(cli.available);
    state.systemBlockReason = cli.available
      ? ""
      : "Claude CLI is required for certified GUI creation.";
    systemStrip.innerHTML = `
      <span class="chip is-ok">Playbooks: ${escapeHtml(status.playbookRoot)}</span>
      <span class="chip ${cliClass}">${escapeHtml(cliText)}</span>
    `;
  } catch (error) {
    renderTargets();
    state.systemReady = false;
    state.systemBlockReason = error.message;
    systemStrip.innerHTML = `<span class="chip is-bad">${escapeHtml(error.message)}</span>`;
  }
  updateSubmitAvailability();
}

// ---------------------------------------------------------------------------
// Payload
// ---------------------------------------------------------------------------

function fieldValue(name) {
  const el = form.elements[name];
  return el ? String(el.value || "").trim() : "";
}

function addIfValue(payload, key, value) {
  if (value !== "") {
    payload[key] = value;
  }
}

function selectedTarget() {
  const checked = form.querySelector('input[name="target"]:checked');
  return checked ? checked.value : "claude_code";
}

function collectPayload() {
  const payload = {
    source: fieldValue("source"),
    skillName: fieldValue("skillName"),
    sourceType: fieldValue("sourceType") || "auto",
    shape: "skill",
    target: selectedTarget(),
    overwrite: Boolean(form.elements.overwrite.checked),
    codify: true,
    validate: true,
    requireCertification: true,
    keepPlaybook: Boolean(form.elements.keepPlaybook.checked),
    sameOriginOnly: Boolean(form.elements.sameOriginOnly.checked),
    transcribe: Boolean(form.elements.transcribe.checked),
    keepValidationSandbox: Boolean(form.elements.keepValidationSandbox.checked),
  };

  for (const key of [
    "playbookName",
    "summary",
    "scopeNotes",
    "ownerAgent",
    "triggerDescription",
    "skillsRoot",
    "rightsBasis",
    "sourceOwner",
    "sourceLicense",
    "exportIntent",
    "codifyProvider",
    "urlPattern",
    "captionsPath",
    "maxPages",
    "frameWidth",
    "fps",
    "timeoutSeconds",
    "validationTimeoutSeconds",
    "pageStart",
    "pageEnd",
  ]) {
    addIfValue(payload, key, fieldValue(key));
  }

  const captionLanguages = fieldValue("captionLanguages")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  if (captionLanguages.length) {
    payload.captionLanguages = captionLanguages;
  }
  return payload;
}

// ---------------------------------------------------------------------------
// Submit + polling
// ---------------------------------------------------------------------------

async function submitCreate(event) {
  event.preventDefault();
  if (!validateStep(1) || !validateStep(2)) {
    return;
  }
  if (!state.systemReady) {
    setPill(formState, "Blocked", "is-bad");
    renderError(state.systemBlockReason || "Required system dependency is unavailable.");
    return;
  }
  const payload = collectPayload();
  setFormBusy(true);
  setPill(formState, "Submitting", "is-warn");
  try {
    const result = await fetchJson("/api/create", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.activeJobId = result.job.id;
    startTimer();
    renderJob(result.job);
    pollActiveJob();
  } catch (error) {
    setPill(formState, "Needs input", "is-bad");
    renderError(error.message);
  } finally {
    setFormBusy(false);
  }
}

function startTimer() {
  stopTimer();
  state.runStartedAt = Date.now();
  jobTimer.hidden = false;
  tickTimer();
  state.timerInterval = setInterval(tickTimer, 1000);
}

function stopTimer() {
  if (state.timerInterval) {
    clearInterval(state.timerInterval);
    state.timerInterval = null;
  }
}

function tickTimer() {
  if (!state.runStartedAt) return;
  const seconds = Math.round((Date.now() - state.runStartedAt) / 1000);
  jobTimer.textContent = `${seconds}s elapsed`;
}

function statusClass(status) {
  if (status === "succeeded") return "is-ok";
  if (status === "failed") return "is-bad";
  if (status === "running" || status === "queued") return "is-warn";
  return "is-idle";
}

const STAGE_HINTS = [
  { stage: "capture", patterns: ["ffmpeg", "yt-dlp", "download", "decode", "capture", "playbook"] },
  { stage: "distill", patterns: ["distill", "lesson"] },
  { stage: "compose", patterns: ["codify", "scaffold", "compose", "claude -p", "claude cli"] },
  { stage: "export", patterns: ["certification", "validation", "rights gate", "export", "prompt injection", "prompt-injection"] },
];

function guessFailedStage(message) {
  const lower = String(message || "").toLowerCase();
  for (const { stage, patterns } of STAGE_HINTS) {
    if (patterns.some((p) => lower.includes(p))) {
      return stage;
    }
  }
  return "export";
}

function renderPipeline(status, errorMessage) {
  const spans = [...pipeline.querySelectorAll("span")];
  for (const span of spans) {
    span.classList.remove("is-failed-step");
  }
  if (status === "running" || status === "queued") {
    pipeline.className = "pipeline is-working";
    pipelineNote.textContent = "Working through capture → distill → compose → export. Typically 5–90s depending on whether validation runs.";
    return;
  }
  if (status === "succeeded") {
    pipeline.className = "pipeline is-succeeded";
    pipelineNote.textContent = "All stages completed.";
    return;
  }
  if (status === "failed") {
    pipeline.className = "pipeline is-failed";
    const failedStage = guessFailedStage(errorMessage);
    const failedSpan = pipeline.querySelector(`[data-step="${failedStage}"]`);
    failedSpan?.classList.add("is-failed-step");
    pipelineNote.textContent = "Stopped before completing — see the error below.";
    return;
  }
  pipeline.className = "pipeline";
  pipelineNote.textContent = "";
}

const ERROR_TRANSLATIONS = [
  {
    match: /rights gate blocked/i,
    title: "Blocked by the rights gate",
    body: "The requested export intent goes further than your rights basis supports. Try a more conservative export intent (private/internal), or pick a stronger rights basis if you actually have one.",
  },
  {
    match: /certification rejected/i,
    title: "Certification didn't pass",
    body: "The pipeline ran to completion and a skill file was written, but execution validation didn't fully pass — open the generated SKILL.md yourself before relying on it. This can vary between identical runs since validation is graded by a live model call.",
  },
  {
    match: /claude cli.*not found|claude.*not available|claude CLI is required/i,
    title: "Claude Code CLI not found",
    body: "Install the Claude Code CLI and make sure `claude` is on PATH, then restart this page.",
  },
  {
    match: /ffmpeg/i,
    title: "ffmpeg problem",
    body: "Confirm ffmpeg is installed and on PATH (winget install Gyan.FFmpeg on Windows). If you just installed it, open a fresh terminal — the current one won't see the updated PATH.",
  },
  {
    match: /prompt.?injection/i,
    title: "Blocked by the prompt-injection guard",
    body: "The captured source contains text that looks like it's trying to direct the assistant rather than describe content. Review the source manually or try a different one.",
  },
];

function friendlyError(message) {
  const text = String(message || "Skillmint failed");
  for (const entry of ERROR_TRANSLATIONS) {
    if (entry.match.test(text)) {
      return { title: entry.title, body: entry.body, raw: text };
    }
  }
  return { title: "Something went wrong", body: text, raw: "" };
}

function renderError(message, trace = "") {
  const { title, body, raw } = friendlyError(message);
  const showRaw = raw && raw !== body;
  artifactList.className = "artifact-list";
  artifactList.innerHTML = `
    <div class="error-card">
      <div class="error-title"><span class="error-badge">!</span>${escapeHtml(title)}</div>
      <p>${escapeHtml(body)}</p>
      ${showRaw || trace ? `
        <details>
          <summary>Technical details</summary>
          <pre>${escapeHtml([showRaw ? raw : "", trace].filter(Boolean).join("\n\n"))}</pre>
        </details>
      ` : ""}
    </div>
  `;
}

function renderArtifact(label, value) {
  if (!value) return "";
  return `
    <div class="artifact-row">
      <strong>${escapeHtml(label)}</strong>
      <span class="path-text">${escapeHtml(value)}</span>
      <button type="button" class="copy-button" data-copy="${escapeHtml(value)}">Copy</button>
    </div>
  `;
}

function renderPlaybookArtifact(result) {
  if (result.playbookRetained === false) {
    const directory = result.playbookCleanup?.directory || "";
    return `
      <div class="artifact-row">
        <strong>Playbook discarded</strong>
        <span class="path-text">${escapeHtml(directory)}</span>
      </div>
    `;
  }
  return renderArtifact("Playbook", result.playbookDirectory);
}

function renderValidation(validation) {
  if (!validation) return "";
  const label = validation.skipped
    ? "Validation skipped"
    : validation.ok
      ? `Validation passed (${validation.passed || 0}/${(validation.passed || 0) + (validation.failed || 0)})`
      : `Validation failed (${validation.passed || 0}/${(validation.passed || 0) + (validation.failed || 0)})`;
  const detail = validation.error || validation.claudeStderr || validation.sandboxDir || "";
  return `
    <div class="artifact-row">
      <strong>${escapeHtml(label)}</strong>
      <span class="path-text">${escapeHtml(detail)}</span>
    </div>
  `;
}

function renderJob(job) {
  const status = job.status || "idle";
  setPill(jobState, status[0].toUpperCase() + status.slice(1), statusClass(status));
  setPill(formState, status === "failed" ? "Failed" : status === "succeeded" ? "Ready" : "Running", statusClass(status));
  renderPipeline(status, job.error);

  if (status !== "running" && status !== "queued") {
    stopTimer();
  }

  if (status === "failed") {
    renderError(job.error || "Skillmint failed", job.traceback || "");
    return;
  }

  if (!job.result) {
    artifactList.className = "artifact-list empty";
    artifactList.innerHTML = `<p>${escapeHtml(job.input?.skillName || "Job")} is ${escapeHtml(status)}.</p>`;
    return;
  }

  const result = job.result;
  artifactList.className = "artifact-list";
  artifactList.innerHTML = [
    renderArtifact("Skill", result.outputPath),
    renderArtifact("Claude path", result.claudeCodePath),
    renderPlaybookArtifact(result),
    renderArtifact("Lessons", result.lessonsMarkdownPath),
    renderArtifact("Sidecar", result.linkManifestPath),
    renderArtifact("Export dir", result.outputDirectory),
    renderValidation(result.validation),
  ].join("");
}

async function pollActiveJob() {
  clearTimeout(state.pollTimer);
  if (!state.activeJobId) return;
  try {
    const job = await fetchJson(`/api/jobs/${state.activeJobId}`);
    renderJob(job);
    await loadJobs();
    if (job.status === "queued" || job.status === "running") {
      state.pollTimer = setTimeout(pollActiveJob, 1300);
    } else {
      await loadPlaybooks();
    }
  } catch (error) {
    renderError(error.message);
  }
}

async function loadJobs() {
  try {
    const payload = await fetchJson("/api/jobs");
    const jobs = payload.jobs || [];
    if (!jobs.length) {
      jobsList.innerHTML = `<p class="chip is-muted">No jobs yet</p>`;
      return;
    }
    jobsList.innerHTML = jobs.slice(0, 8).map((job) => `
      <button class="row-button" type="button" data-job-id="${escapeHtml(job.id)}">
        <strong>${escapeHtml(job.input?.skillName || job.id)}</strong>
        <span>${escapeHtml(job.status)} / ${escapeHtml(job.input?.sourceType || "auto")} / ${escapeHtml(job.input?.target || "claude_code")}</span>
        <small>${escapeHtml(job.input?.source || "")}</small>
      </button>
    `).join("");
  } catch (error) {
    jobsList.innerHTML = `<p class="chip is-bad">${escapeHtml(error.message)}</p>`;
  }
}

async function loadPlaybooks() {
  try {
    const payload = await fetchJson("/api/playbooks");
    const playbooks = payload.playbooks || [];
    if (!playbooks.length) {
      playbooksList.innerHTML = `<p class="chip is-muted">No playbooks yet</p>`;
      return;
    }
    playbooksList.innerHTML = playbooks.slice(0, 8).map((playbook) => `
      <div class="row-button" role="group">
        <strong>${escapeHtml(playbook.name)}</strong>
        <span>${escapeHtml(playbook.stepCount || 0)} steps</span>
        <small>${escapeHtml(playbook.directory)}</small>
      </div>
    `).join("");
  } catch (error) {
    playbooksList.innerHTML = `<p class="chip is-bad">${escapeHtml(error.message)}</p>`;
  }
}

async function selectJob(jobId) {
  state.activeJobId = jobId;
  try {
    const job = await fetchJson(`/api/jobs/${jobId}`);
    renderJob(job);
    if (job.status === "queued" || job.status === "running") {
      if (!state.runStartedAt) startTimer();
      pollActiveJob();
    }
  } catch (error) {
    renderError(error.message);
  }
}

document.addEventListener("click", async (event) => {
  const copyButton = event.target.closest("[data-copy]");
  if (copyButton) {
    await navigator.clipboard.writeText(copyButton.dataset.copy || "");
    copyButton.textContent = "Copied";
    setTimeout(() => {
      copyButton.textContent = "Copy";
    }, 1000);
    return;
  }

  const jobButton = event.target.closest("[data-job-id]");
  if (jobButton) {
    selectJob(jobButton.dataset.jobId);
  }
});

form.addEventListener("submit", submitCreate);
form.addEventListener("reset", () => {
  clearTimeout(state.pollTimer);
  stopTimer();
  jobTimer.hidden = true;
  state.activeJobId = null;
  state.runStartedAt = null;
  setPill(formState, "Ready", "is-idle");
  setPill(jobState, "No job", "is-idle");
  pipeline.className = "pipeline";
  pipelineNote.textContent = "";
  artifactList.className = "artifact-list empty";
  artifactList.innerHTML = "<p>No run selected.</p>";
  goToStep(1);
});

document.querySelector("#refreshJobs").addEventListener("click", loadJobs);
document.querySelector("#refreshPlaybooks").addEventListener("click", loadPlaybooks);

loadStatus();
loadJobs();
loadPlaybooks();
