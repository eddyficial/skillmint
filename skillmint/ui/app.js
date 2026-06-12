const form = document.querySelector("#createForm");
const formState = document.querySelector("#formState");
const jobState = document.querySelector("#jobState");
const artifactList = document.querySelector("#artifactList");
const jobsList = document.querySelector("#jobsList");
const playbooksList = document.querySelector("#playbooksList");
const pipeline = document.querySelector("#pipeline");
const systemStrip = document.querySelector("#systemStrip");
const targetOptions = document.querySelector("#targetOptions");

const targetLabels = {
  claude_code: "Claude",
  codex: "Codex",
  cursor: "Cursor",
  windsurf: "Windsurf",
  markdown: "Markdown",
};

const state = {
  activeJobId: null,
  pollTimer: null,
  formBusy: false,
  systemReady: true,
  systemBlockReason: "",
  targets: ["claude_code", "codex", "cursor", "windsurf", "markdown"],
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
  const submit = form.querySelector('button[type="submit"]');
  if (!submit) return;
  submit.disabled = state.formBusy || !state.systemReady;
  if (!state.systemReady) {
    submit.title = state.systemBlockReason;
  } else {
    submit.removeAttribute("title");
  }
}

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
    shape: fieldValue("shape") || "skill",
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
    "captionLanguage",
    "whisperModel",
    "whisperDevice",
    "maxPages",
    "frameWidth",
    "maxHeight",
    "fps",
    "keyframeDiffThreshold",
    "minStepSeconds",
    "downloadTimeoutSeconds",
    "processTimeoutSeconds",
    "timeoutSeconds",
    "sectionDiffScore",
    "codifyTimeoutSeconds",
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

async function submitCreate(event) {
  event.preventDefault();
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
    renderJob(result.job);
    pollActiveJob();
  } catch (error) {
    setPill(formState, "Needs input", "is-bad");
    renderError(error.message);
  } finally {
    setFormBusy(false);
  }
}

function pipelineClass(status) {
  if (status === "running" || status === "queued") return "pipeline is-running";
  if (status === "succeeded") return "pipeline is-succeeded";
  if (status === "failed") return "pipeline is-failed";
  return "pipeline";
}

function statusClass(status) {
  if (status === "succeeded") return "is-ok";
  if (status === "failed") return "is-bad";
  if (status === "running" || status === "queued") return "is-warn";
  return "is-idle";
}

function renderError(message, trace = "") {
  artifactList.className = "artifact-list error-box";
  artifactList.innerHTML = `
    <div>
      <strong>Error</strong>
      <p>${escapeHtml(message)}</p>
      ${trace ? `<pre>${escapeHtml(trace)}</pre>` : ""}
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
  pipeline.className = pipelineClass(status);

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
  state.activeJobId = null;
  setPill(formState, "Ready", "is-idle");
  setPill(jobState, "No job", "is-idle");
  pipeline.className = "pipeline";
  artifactList.className = "artifact-list empty";
  artifactList.innerHTML = "<p>No run selected.</p>";
});

document.querySelector("#refreshJobs").addEventListener("click", loadJobs);
document.querySelector("#refreshPlaybooks").addEventListener("click", loadPlaybooks);

loadStatus();
loadJobs();
loadPlaybooks();
