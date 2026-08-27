const elements = {
  form: document.querySelector("#run-form"),
  prompt: document.querySelector("#prompt"),
  runButton: document.querySelector("#run-button"),
  runStatus: document.querySelector("#run-status"),
  statusDot: document.querySelector("#status-dot"),
  handoffCount: document.querySelector("#handoff-count"),
  elapsed: document.querySelector("#elapsed"),
  sourceFiles: document.querySelector("#source-files"),
  sourceAccount: document.querySelector("#source-account"),
  sourceRoute: document.querySelector("#source-route"),
  outputFiles: document.querySelector("#output-files"),
  outputAccount: document.querySelector("#output-account"),
  outputRoute: document.querySelector("#output-route"),
  modelName: document.querySelector("#model-name"),
  eventCount: document.querySelector("#event-count"),
  timeline: document.querySelector("#timeline"),
  directionBadge: document.querySelector("#direction-badge"),
  transferDetail: document.querySelector("#transfer-detail"),
  packetMap: document.querySelector("#packet-map"),
  transferLayer: document.querySelector("#transfer-layer"),
  readerRole: document.querySelector("#reader-role"),
  writerRole: document.querySelector("#writer-role"),
  decisionSection: document.querySelector("#decision-section"),
  decisionContent: document.querySelector("#decision-content"),
  previewDialog: document.querySelector("#preview-dialog"),
  previewKind: document.querySelector("#preview-kind"),
  previewTitle: document.querySelector("#preview-title"),
  previewContent: document.querySelector("#preview-content"),
  closePreview: document.querySelector("#close-preview"),
};

const expectedOutputs = [
  "01-packet-index.json",
  "02-classification.json",
  "03-extracted-facts.json",
  "04-underwriting-decision.md",
];

const state = {
  eventCount: 0,
  handoffs: 0,
  verifiedOutputs: 0,
  startTime: null,
  timer: null,
  completed: false,
  runId: null,
  activeTransfer: null,
  transferSettled: false,
};

function emptyMessage(text) {
  const paragraph = document.createElement("p");
  paragraph.className = "empty-state";
  paragraph.textContent = text;
  return paragraph;
}

function setRunState(label, status) {
  elements.runStatus.textContent = label;
  elements.statusDot.className = `status-dot ${status}`;
  document.body.dataset.runState = status;
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

function fileExtension(path) {
  const name = path.split("/").pop() || "file";
  return (name.includes(".") ? name.split(".").pop() : "file").slice(0, 4);
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

function encodedPath(path, root) {
  return path
    .replace(new RegExp(`^/${root}/`), "")
    .split("/")
    .map(encodeURIComponent)
    .join("/");
}

async function openPreview(kind, path, url) {
  elements.previewKind.textContent = `${kind} Blob preview`;
  elements.previewTitle.textContent = path;
  elements.previewContent.textContent = "Loading from Blob Storage...";
  elements.previewDialog.showModal();
  try {
    const response = await fetch(url);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "Preview failed");
    elements.previewContent.textContent = payload.content;
  } catch (error) {
    elements.previewContent.textContent = `Preview unavailable: ${error.message}`;
  }
}

function renderSourceFiles(files) {
  elements.sourceFiles.replaceChildren();
  files.forEach((file) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "file-row";
    row.dataset.path = file.virtualPath;
    row.addEventListener("click", () => {
      openPreview(
        "Source",
        file.virtualPath,
        `/api/source/${encodedPath(file.virtualPath, "source")}`,
      );
    });

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

function renderOutputTargets() {
  elements.outputFiles.replaceChildren();
  expectedOutputs.forEach((name) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "artifact-row";
    row.dataset.path = `/output/${name}`;
    row.disabled = true;

    const type = document.createElement("span");
    type.className = "file-type";
    type.textContent = fileExtension(name);
    const detail = document.createElement("span");
    detail.className = "file-detail";
    const fileName = document.createElement("span");
    fileName.className = "file-name";
    fileName.textContent = name;
    const path = document.createElement("span");
    path.className = "file-path";
    path.textContent = "Awaiting agent write";
    const meta = document.createElement("span");
    meta.className = "file-meta";
    meta.textContent = "pending";
    detail.append(fileName, path, meta);
    row.append(type, detail);
    elements.outputFiles.append(row);
  });
}

function verifyOutput(artifact) {
  const row = elements.outputFiles.querySelector(`[data-path="${CSS.escape(artifact.virtualPath)}"]`);
  if (!row) return;
  row.disabled = false;
  row.classList.add("verified");
  row.querySelector(".file-path").textContent = artifact.blobName;
  row.querySelector(".file-meta").textContent = `${formatBytes(new Blob([artifact.content]).size)} | verified, click to preview`;
  row.addEventListener("click", () => {
    openPreview(
      "Output",
      artifact.virtualPath,
      `/api/output/${encodeURIComponent(artifact.runId)}/${encodedPath(artifact.virtualPath, "output")}`,
    );
  }, { once: false });
  state.verifiedOutputs += 1;
}

function markAgent(agent, status) {
  document.querySelectorAll(".pipeline-agent").forEach((node) => {
    if (node.dataset.agent === agent) {
      if (status === "complete") {
        node.classList.remove("active");
        node.classList.add("complete");
      } else {
        node.classList.remove("complete");
        node.classList.add("active");
      }
    }
  });
}

function resetWorkbench() {
  state.eventCount = 0;
  state.handoffs = 0;
  state.verifiedOutputs = 0;
  state.completed = false;
  state.runId = null;
  state.activeTransfer = null;
  state.transferSettled = false;
  elements.eventCount.textContent = "0 steps";
  elements.handoffCount.textContent = "0 / 4 handoffs";
  elements.sourceFiles.replaceChildren(emptyMessage("Packet inventory pending."));
  elements.timeline.replaceChildren();
  elements.directionBadge.className = "direction-badge idle";
  elements.directionBadge.textContent = "Waiting";
  elements.transferDetail.replaceChildren(emptyMessage("The exact file lines will appear when an agent reads or writes a Blob."));
  elements.decisionSection.hidden = true;
  elements.decisionContent.replaceChildren();
  document.querySelectorAll(".pipeline-agent").forEach((node) => {
    node.classList.remove("active", "complete");
  });
  renderOutputTargets();
  clearTransferPath();
}

function timelineMessage(event) {
  if (event.type === "run.started") {
    return { category: "read", agent: "server", text: `Opened isolated run ${event.runId}.` };
  }
  if (event.type === "blob.inventory") {
    return { category: "read", agent: "server", text: `Listed ${event.files.length} packet Blobs.` };
  }
  if (event.type === "agent.ready") {
    return { category: "delegation", agent: "orchestrator", text: "Loaded the orchestrator and four specialist agents." };
  }
  if (event.type === "tool.call" && event.kind === "delegation") {
    return { category: "delegation", agent: event.agent, text: `Handoff to ${event.targetAgent}: ${event.summary}` };
  }
  if (event.type === "tool.call" && event.kind === "filesystem") {
    const write = new Set(["write_file", "edit_file"]).has(event.tool);
    return { category: write ? "write" : "read", agent: event.agent, text: `${event.tool} ${event.path}` };
  }
  if (event.type === "data.transfer") {
    return { category: event.direction, agent: event.agent, text: `${event.direction.toUpperCase()} ${event.path}, ${event.lines.length} exact lines observed.` };
  }
  if (event.type === "handoff.completed") {
    return { category: "complete", agent: event.agent, text: `Handoff ${event.handoff} of 4 completed.` };
  }
  if (event.type === "blob.verified") {
    return { category: "write", agent: "server", text: `Verified ${event.artifact.name} in Blob Storage.` };
  }
  if (event.type === "output.recovered") {
    return {
      category: "write",
      agent: event.agent,
      text: `Persisted ${event.path} from ${event.sourceAgent}'s returned decision.`,
    };
  }
  if (event.type === "run.completed") {
    return { category: "complete", agent: "orchestrator", text: "All four outputs and the final decision are verified." };
  }
  if (event.type === "run.failed") {
    return { category: "error", agent: "server", text: event.error };
  }
  return null;
}

function appendTimeline(event) {
  const display = timelineMessage(event);
  if (!display) return;
  elements.timeline.querySelector(".timeline-empty")?.remove();
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
  time.dateTime = event.timestamp;
  time.textContent = new Date(event.timestamp).toLocaleTimeString([], { hour12: false });
  meta.append(agent, time);
  const message = document.createElement("p");
  message.className = "event-message";
  message.textContent = display.text;
  item.append(meta, message);
  elements.timeline.append(item);
  elements.timeline.scrollTop = elements.timeline.scrollHeight;
}

function transferEndpoints(event) {
  const agent = elements.packetMap.querySelector(`[data-agent="${CSS.escape(event.agent)}"]`);
  const file = elements.packetMap.querySelector(`[data-path="${CSS.escape(event.path)}"]`);
  if (!agent || !file) return null;
  return event.direction === "write" ? [agent, file] : [file, agent];
}

function clearTransferPath() {
  elements.transferLayer.querySelectorAll(".transfer-path, .transfer-particle").forEach((node) => node.remove());
  document.querySelectorAll(".active-transfer").forEach((node) => node.classList.remove("active-transfer"));
}

function markSourceAccessed(event) {
  if (event.direction !== "read" || !event.path.startsWith("/source/")) return;
  elements.sourceFiles
    .querySelector(`[data-path="${CSS.escape(event.path)}"]`)
    ?.classList.add("accessed");
}

function edgeMidpoint(rect, target, mapBox) {
  const center = {
    x: rect.left + rect.width / 2 - mapBox.left,
    y: rect.top + rect.height / 2 - mapBox.top,
  };
  const deltaX = target.x - center.x;
  const deltaY = target.y - center.y;

  if (Math.abs(deltaX) >= Math.abs(deltaY)) {
    return {
      x: (deltaX >= 0 ? rect.right : rect.left) - mapBox.left,
      y: center.y,
    };
  }

  return {
    x: center.x,
    y: (deltaY >= 0 ? rect.bottom : rect.top) - mapBox.top,
  };
}

function settleTransferPath() {
  elements.transferLayer.querySelector(".transfer-path")?.classList.add("settled");
  elements.transferLayer.querySelectorAll(".transfer-particle").forEach((node) => node.remove());
}

function drawTransferPath(event) {
  clearTransferPath();
  const endpoints = transferEndpoints(event);
  if (!endpoints || window.matchMedia("(max-width: 760px)").matches) return;
  const [origin, destination] = endpoints;
  origin.classList.add("active-transfer");
  destination.classList.add("active-transfer");

  const mapBox = elements.packetMap.getBoundingClientRect();
  const originBox = origin.getBoundingClientRect();
  const destinationBox = destination.getBoundingClientRect();
  const originCenter = {
    x: originBox.left + originBox.width / 2 - mapBox.left,
    y: originBox.top + originBox.height / 2 - mapBox.top,
  };
  const destinationCenter = {
    x: destinationBox.left + destinationBox.width / 2 - mapBox.left,
    y: destinationBox.top + destinationBox.height / 2 - mapBox.top,
  };
  const start = edgeMidpoint(originBox, destinationCenter, mapBox);
  const end = edgeMidpoint(destinationBox, originCenter, mapBox);
  const deltaX = end.x - start.x;
  const deltaY = end.y - start.y;
  const distance = Math.hypot(deltaX, deltaY);
  const normalX = -deltaY / distance;
  const normalY = deltaX / distance;
  const bow = Math.min(32, distance * 0.08) * (deltaY >= 0 ? -1 : 1);
  const firstControl = {
    x: start.x + deltaX * 0.34 + normalX * bow,
    y: start.y + deltaY * 0.34 + normalY * bow,
  };
  const secondControl = {
    x: start.x + deltaX * 0.68 + normalX * bow,
    y: start.y + deltaY * 0.68 + normalY * bow,
  };
  const pathData = `M ${start.x} ${start.y} C ${firstControl.x} ${firstControl.y}, ${secondControl.x} ${secondControl.y}, ${end.x} ${end.y}`;
  const namespace = "http://www.w3.org/2000/svg";

  const path = document.createElementNS(namespace, "path");
  path.setAttribute("d", pathData);
  path.setAttribute("class", `transfer-path ${event.direction}`);
  const particle = document.createElementNS(namespace, "polygon");
  particle.setAttribute("points", "-7,-4 7,0 -7,4");
  particle.setAttribute("class", "transfer-particle");
  particle.setAttribute("fill", event.direction === "write" ? "var(--green)" : "var(--cyan)");
  const movement = document.createElementNS(namespace, "animateMotion");
  movement.setAttribute("dur", "1.2s");
  movement.setAttribute("repeatCount", "indefinite");
  movement.setAttribute("rotate", "auto");
  movement.setAttribute("path", pathData);
  particle.append(movement);
  elements.transferLayer.append(path, particle);
  if (state.transferSettled) settleTransferPath();
}

function renderTransfer(event) {
  state.activeTransfer = event;
  markSourceAccessed(event);
  elements.directionBadge.className = `direction-badge ${event.direction}`;
  elements.directionBadge.textContent = event.direction.toUpperCase();
  elements.transferDetail.replaceChildren();

  const route = document.createElement("p");
  route.className = "transfer-route";
  const origin = document.createElement("strong");
  const arrow = document.createElement("span");
  const destination = document.createElement("strong");
  if (event.direction === "write") {
    origin.textContent = event.agent;
    destination.textContent = event.path;
  } else {
    origin.textContent = event.path;
    destination.textContent = event.agent;
  }
  arrow.textContent = "→";
  route.append(origin, arrow, destination);

  const path = document.createElement("p");
  path.className = "transfer-path-label";
  path.textContent = `${event.tool} | ${event.path}`;
  const excerpt = document.createElement("pre");
  excerpt.className = "line-excerpt";
  if (!event.lines.length) {
    excerpt.textContent = "No non-empty lines returned.";
  } else {
    event.lines.forEach((line) => {
      const row = document.createElement("span");
      row.className = "excerpt-line";
      const number = document.createElement("span");
      number.className = "line-number";
      number.textContent = String(line.line);
      const text = document.createElement("mark");
      text.textContent = line.text;
      row.append(number, text);
      excerpt.append(row);
    });
  }
  elements.transferDetail.append(route, path, excerpt);
  drawTransferPath(event);
}

function renderMarkdown(markdown) {
  elements.decisionContent.replaceChildren();
  let list = null;
  markdown.split(/\r?\n/).forEach((line) => {
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      list = null;
      const node = document.createElement(`h${heading[1].length}`);
      node.textContent = heading[2];
      elements.decisionContent.append(node);
      return;
    }
    const item = line.match(/^\s*-\s+(.+)$/);
    if (item) {
      if (!list) {
        list = document.createElement("ul");
        elements.decisionContent.append(list);
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
    elements.decisionContent.append(paragraph);
  });
}

function handleEvent(event) {
  appendTimeline(event);
  if (event.type === "run.started") {
    state.runId = event.runId;
    elements.modelName.textContent = event.model;
    markAgent("orchestrator", "active");
  } else if (event.type === "blob.inventory") {
    renderSourceFiles(event.files);
  } else if (event.type === "tool.call" && event.kind === "delegation") {
    markAgent(event.targetAgent, "active");
  } else if (event.type === "tool.call" && event.kind === "filesystem") {
    markAgent(event.agent, "active");
  } else if (event.type === "data.transfer") {
    renderTransfer(event);
  } else if (event.type === "handoff.completed") {
    state.handoffs = event.handoff;
    elements.handoffCount.textContent = `${state.handoffs} / 4 handoffs`;
    markAgent(event.agent, "complete");
    if (state.handoffs < 4) markAgent("orchestrator", "active");
  } else if (event.type === "blob.verified") {
    verifyOutput(event.artifact);
  } else if (event.type === "run.completed") {
    state.completed = true;
    state.transferSettled = true;
    document.querySelectorAll(".pipeline-agent").forEach((node) => {
      node.classList.remove("active");
      node.classList.add("complete");
    });
    renderMarkdown(event.decision.content);
    elements.decisionSection.hidden = false;
    setRunState("Completed", "complete");
    stopTimer();
    settleTransferPath();
  } else if (event.type === "run.failed") {
    state.transferSettled = true;
    setRunState("Failed", "failed");
    stopTimer();
    settleTransferPath();
  }
}

async function loadConfig() {
  try {
    const response = await fetch("/api/config");
    const config = await response.json();
    if (!response.ok) throw new Error(config.detail || "Configuration unavailable");
    const accountName = new URL(config.account).hostname.split(".")[0];
    elements.modelName.textContent = config.model;
    elements.sourceAccount.textContent = accountName;
    elements.sourceAccount.title = config.account;
    elements.sourceRoute.textContent = config.source;
    elements.sourceRoute.title = config.source;
    elements.outputAccount.textContent = accountName;
    elements.outputAccount.title = config.account;
    elements.outputRoute.textContent = config.outputs;
    elements.outputRoute.title = config.outputs;
    elements.readerRole.textContent = config.readerRole;
    elements.writerRole.textContent = config.writerRole;
  } catch (error) {
    setRunState("Configuration error", "failed");
    elements.modelName.textContent = error.message;
  }
}

elements.form.addEventListener("submit", (submitEvent) => {
  submitEvent.preventDefault();
  const prompt = elements.prompt.value.trim();
  if (!prompt) return;

  resetWorkbench();
  elements.runButton.disabled = true;
  elements.runButton.textContent = "Processing...";
  setRunState("Running", "running");
  startTimer();

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}/ws/run`);
  socket.addEventListener("open", () => socket.send(JSON.stringify({ prompt })));
  socket.addEventListener("message", (messageEvent) => handleEvent(JSON.parse(messageEvent.data)));
  socket.addEventListener("error", () => {
    if (!state.completed) setRunState("Connection error", "failed");
  });
  socket.addEventListener("close", () => {
    elements.runButton.disabled = false;
    elements.runButton.textContent = "Process packet";
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

const resizeObserver = new ResizeObserver(() => {
  if (state.activeTransfer) drawTransferPath(state.activeTransfer);
});
resizeObserver.observe(elements.packetMap);

renderOutputTargets();
loadConfig();
