const elements = {
  form: document.querySelector("#run-form"),
  prompt: document.querySelector("#prompt"),
  runButton: document.querySelector("#run-button"),
  runStatus: document.querySelector("#run-status"),
  statusDot: document.querySelector("#status-dot"),
  elapsed: document.querySelector("#elapsed"),
  sourceRoute: document.querySelector("#source-route"),
  reportRoute: document.querySelector("#report-route"),
  modelName: document.querySelector("#model-name"),
  sourceCount: document.querySelector("#source-count"),
  sourceFiles: document.querySelector("#source-files"),
  eventCount: document.querySelector("#event-count"),
  timeline: document.querySelector("#timeline"),
  artifactCount: document.querySelector("#artifact-count"),
  artifacts: document.querySelector("#artifacts"),
  reportSection: document.querySelector("#report-section"),
  reportContent: document.querySelector("#report-content"),
  reportUrl: document.querySelector("#report-url"),
  previewDialog: document.querySelector("#preview-dialog"),
  previewTitle: document.querySelector("#preview-title"),
  previewContent: document.querySelector("#preview-content"),
  closePreview: document.querySelector("#close-preview"),
};

const state = {
  eventCount: 0,
  artifactCount: 0,
  startTime: null,
  timer: null,
  activityTimer: null,
  completed: false,
};

function setRunState(label, status) {
  elements.runStatus.textContent = label;
  elements.statusDot.className = `status-dot ${status}`;
  document.body.dataset.runState = status;
}

function setActivity(activity, duration = 1400) {
  window.clearTimeout(state.activityTimer);
  delete document.body.dataset.activity;
  void document.body.offsetWidth;
  document.body.dataset.activity = activity;
  state.activityTimer = window.setTimeout(() => {
    delete document.body.dataset.activity;
  }, duration);
}

function showEventActivity(event) {
  if (event.type === "run.started" || event.type === "agent.ready") {
    setActivity("orchestrate");
  } else if (event.type === "blob.inventory") {
    setActivity("read");
  } else if (event.type === "tool.call" && event.kind === "delegation") {
    setActivity("delegate");
  } else if (event.type === "tool.call" && event.kind === "filesystem") {
    const isWrite = new Set(["write_file", "edit_file"]).has(event.tool);
    setActivity(isWrite ? "write" : "read");
  } else if (event.type === "blob.verified") {
    setActivity("verify", 1800);
  } else if (event.type === "run.completed") {
    setActivity("complete", 2200);
  } else if (event.type === "run.failed") {
    setActivity("error", 2200);
  }
}

function formatElapsed(milliseconds) {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  const minutes = Math.floor(seconds / 60).toString().padStart(2, "0");
  const remainder = (seconds % 60).toString().padStart(2, "0");
  return `${minutes}:${remainder}`;
}

function startTimer() {
  window.clearInterval(state.timer);
  state.startTime = Date.now();
  elements.elapsed.textContent = "00:00";
  state.timer = window.setInterval(() => {
    elements.elapsed.textContent = formatElapsed(Date.now() - state.startTime);
  }, 500);
}

function stopTimer() {
  window.clearInterval(state.timer);
  state.timer = null;
  if (state.startTime) {
    elements.elapsed.textContent = formatElapsed(Date.now() - state.startTime);
  }
}

function resetWorkbench() {
  window.clearTimeout(state.activityTimer);
  delete document.body.dataset.activity;
  state.eventCount = 0;
  state.artifactCount = 0;
  state.completed = false;
  elements.eventCount.textContent = "0 steps";
  elements.artifactCount.textContent = "0";
  elements.sourceCount.textContent = "0";
  elements.sourceFiles.replaceChildren(emptyMessage("Inventory pending."));
  elements.artifacts.replaceChildren(emptyMessage("Agent output pending."));
  elements.timeline.replaceChildren();
  elements.reportSection.hidden = true;
  elements.reportContent.replaceChildren();
  document.querySelectorAll(".agent-pill").forEach((pill) => {
    pill.className = "agent-pill pending";
  });
}

function emptyMessage(text) {
  const paragraph = document.createElement("p");
  paragraph.className = "empty-state";
  paragraph.textContent = text;
  return paragraph;
}

function fileExtension(path) {
  const name = path.split("/").pop() || "file";
  const extension = name.includes(".") ? name.split(".").pop() : "file";
  return extension.slice(0, 4);
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

function sourcePathUrl(virtualPath) {
  return virtualPath
    .replace(/^\/source\//, "")
    .split("/")
    .map(encodeURIComponent)
    .join("/");
}

async function previewSource(file) {
  elements.previewTitle.textContent = file.virtualPath;
  elements.previewContent.textContent = "Loading from Blob Storage...";
  elements.previewDialog.showModal();
  try {
    const response = await fetch(`/api/source/${sourcePathUrl(file.virtualPath)}`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "Preview failed");
    elements.previewContent.textContent = payload.content;
  } catch (error) {
    elements.previewContent.textContent = `Preview unavailable: ${error.message}`;
  }
}

function renderSourceFiles(files) {
  elements.sourceFiles.replaceChildren();
  elements.sourceCount.textContent = files.length.toString();
  files.forEach((file) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "file-row";
    row.dataset.path = file.virtualPath;
    row.addEventListener("click", () => previewSource(file));

    const type = document.createElement("span");
    type.className = "file-type";
    type.textContent = fileExtension(file.virtualPath);

    const detail = document.createElement("span");
    detail.className = "file-detail";
    const name = document.createElement("span");
    name.className = "file-name";
    name.textContent = file.virtualPath.split("/").pop();
    const path = document.createElement("span");
    path.className = "file-path";
    path.textContent = file.blobName;
    const meta = document.createElement("span");
    meta.className = "file-meta";
    meta.textContent = `${formatBytes(file.size)} | click to preview`;
    detail.append(name, path, meta);
    row.append(type, detail);
    elements.sourceFiles.append(row);
  });
}

function markSourceAccessed(path) {
  document.querySelectorAll(".file-row").forEach((row) => {
    if (row.dataset.path === path || path.endsWith(row.dataset.path.replace("/source", ""))) {
      row.classList.add("accessed");
    }
  });
}

function markAgent(agent, status) {
  document.querySelectorAll(".agent-pill").forEach((pill) => {
    if (pill.dataset.agent === agent) {
      pill.className = `agent-pill ${status}`;
    } else if (status === "active" && pill.classList.contains("active")) {
      pill.className = "agent-pill complete";
    }
  });
}

function timelineMessage(event) {
  if (event.type === "run.started") {
    return { category: "read", agent: "server", text: `Opened run ${event.runId} with an isolated output prefix.` };
  }
  if (event.type === "blob.inventory") {
    return { category: "read", agent: "server", text: `Listed ${event.files.length} source blobs through AzureBlobBackend.` };
  }
  if (event.type === "agent.ready") {
    return { category: "delegation", agent: "coordinator", text: `Loaded coordinator and ${event.agents.length - 1} specialist agents.` };
  }
  if (event.type === "tool.call" && event.kind === "delegation") {
    return { category: "delegation", agent: event.agent, text: `Delegated to ${event.targetAgent}: ${event.summary}` };
  }
  if (event.type === "tool.call" && event.kind === "filesystem") {
    const writeTools = new Set(["write_file", "edit_file"]);
    const category = writeTools.has(event.tool) ? "write" : "read";
    const verb = {
      ls: "Listed",
      glob: "Searched",
      read_file: "Read",
      write_file: "Wrote",
      edit_file: "Edited",
    }[event.tool] || `Called ${event.tool} on`;
    return { category, agent: event.agent, text: `${verb} ${event.path}` };
  }
  if (event.type === "tool.call") {
    return { category: "delegation", agent: event.agent, text: event.summary || `Called ${event.tool}` };
  }
  if (event.type === "tool.result") {
    return { category: "complete", agent: event.agent, text: `${event.tool} completed with status ${event.status}.` };
  }
  if (event.type === "blob.verified") {
    return { category: "write", agent: "server", text: `Verified ${event.artifact.blobName} in Blob Storage.` };
  }
  if (event.type === "run.completed") {
    return { category: "complete", agent: "coordinator", text: "Review completed and the final report was verified." };
  }
  if (event.type === "run.failed") {
    return { category: "error", agent: "server", text: event.error };
  }
  return null;
}

function appendTimeline(event) {
  const display = timelineMessage(event);
  if (!display) return;
  const empty = elements.timeline.querySelector(".timeline-empty");
  if (empty) empty.remove();

  state.eventCount += 1;
  elements.eventCount.textContent = `${state.eventCount} steps`;
  const item = document.createElement("li");
  item.className = `timeline-event ${display.category}`;

  const meta = document.createElement("div");
  meta.className = "event-meta";
  const agent = document.createElement("span");
  agent.className = "event-agent";
  agent.textContent = display.agent;
  const time = document.createElement("time");
  const timestamp = new Date(event.timestamp);
  time.dateTime = event.timestamp;
  time.textContent = timestamp.toLocaleTimeString([], { hour12: false });
  meta.append(agent, time);

  const message = document.createElement("p");
  message.className = "event-message";
  message.textContent = display.text;
  item.append(meta, message);
  elements.timeline.append(item);
  elements.timeline.scrollTop = elements.timeline.scrollHeight;
}

function renderArtifact(artifact) {
  if (state.artifactCount === 0) elements.artifacts.replaceChildren();
  state.artifactCount += 1;
  elements.artifactCount.textContent = state.artifactCount.toString();

  const row = document.createElement("div");
  row.className = "artifact-row";
  const type = document.createElement("span");
  type.className = "file-type";
  type.textContent = "md";
  const detail = document.createElement("span");
  detail.className = "file-detail";
  const name = document.createElement("span");
  name.className = "file-name";
  name.textContent = artifact.name;
  const path = document.createElement("span");
  path.className = "file-path";
  path.textContent = artifact.blobName;
  const meta = document.createElement("span");
  meta.className = "file-meta";
  meta.textContent = `${new Blob([artifact.content]).size} B | verified`;
  detail.append(name, path, meta);
  row.append(type, detail);
  elements.artifacts.append(row);
}

function renderMarkdown(markdown) {
  elements.reportContent.replaceChildren();
  let list = null;
  markdown.split(/\r?\n/).forEach((line) => {
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      list = null;
      const node = document.createElement(`h${heading[1].length}`);
      node.textContent = heading[2];
      elements.reportContent.append(node);
      return;
    }
    const item = line.match(/^\s*-\s+(.+)$/);
    if (item) {
      if (!list) {
        list = document.createElement("ul");
        elements.reportContent.append(list);
      }
      const node = document.createElement("li");
      node.textContent = item[1];
      list.append(node);
      return;
    }
    if (!line.trim()) {
      list = null;
      return;
    }
    list = null;
    const paragraph = document.createElement("p");
    paragraph.textContent = line;
    elements.reportContent.append(paragraph);
  });
}

function handleEvent(event) {
  showEventActivity(event);
  appendTimeline(event);
  if (event.type === "run.started") {
    elements.reportRoute.textContent = `${event.reportContainer}/${event.reportPrefix}`;
  } else if (event.type === "blob.inventory") {
    renderSourceFiles(event.files);
  } else if (event.type === "agent.ready") {
    markAgent("coordinator", "active");
  } else if (event.type === "tool.call") {
    markAgent(event.targetAgent || event.agent, "active");
    if (event.kind === "filesystem" && event.path.startsWith("/source/")) {
      markSourceAccessed(event.path);
    }
  } else if (event.type === "blob.verified") {
    renderArtifact(event.artifact);
  } else if (event.type === "run.completed") {
    state.completed = true;
    markAgent("coordinator", "complete");
    document.querySelectorAll(".agent-pill").forEach((pill) => {
      pill.className = "agent-pill complete";
    });
    elements.reportUrl.href = event.report.url;
    renderMarkdown(event.report.content);
    elements.reportSection.hidden = false;
    elements.reportSection.scrollIntoView({ behavior: "smooth", block: "start" });
    setRunState("Completed", "complete");
    stopTimer();
  } else if (event.type === "run.failed") {
    setRunState("Failed", "failed");
    stopTimer();
  }
}

async function loadConfig() {
  try {
    const response = await fetch("/api/config");
    const config = await response.json();
    if (!response.ok) throw new Error(config.detail || "Configuration unavailable");
    elements.modelName.textContent = config.model;
    elements.sourceRoute.textContent = config.source;
    elements.reportRoute.textContent = config.reports;
  } catch (error) {
    setRunState("Configuration error", "failed");
    elements.sourceRoute.textContent = error.message;
  }
}

elements.form.addEventListener("submit", (submitEvent) => {
  submitEvent.preventDefault();
  const prompt = elements.prompt.value.trim();
  if (!prompt) return;

  resetWorkbench();
  elements.runButton.disabled = true;
  elements.runButton.textContent = "Running...";
  setRunState("Running", "running");
  startTimer();

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}/ws/run`);
  socket.addEventListener("open", () => socket.send(JSON.stringify({ prompt })));
  socket.addEventListener("message", (messageEvent) => {
    handleEvent(JSON.parse(messageEvent.data));
  });
  socket.addEventListener("error", () => {
    if (!state.completed) setRunState("Connection error", "failed");
  });
  socket.addEventListener("close", () => {
    elements.runButton.disabled = false;
    elements.runButton.textContent = "Run review";
    if (!state.completed && !elements.statusDot.classList.contains("failed")) {
      setRunState("Disconnected", "failed");
      stopTimer();
    }
  });
});

elements.closePreview.addEventListener("click", () => elements.previewDialog.close());
elements.previewDialog.addEventListener("click", (event) => {
  if (event.target === elements.previewDialog) elements.previewDialog.close();
});

loadConfig();