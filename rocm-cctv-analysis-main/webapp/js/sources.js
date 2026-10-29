const videoInput = document.getElementById("video-files");
const dropzone = document.getElementById("dropzone");
const uploadList = document.getElementById("upload-list");
const uploadButton = document.getElementById("upload-button");
const fileMessage = document.getElementById("file-message");
const uploadNote = document.getElementById("upload-note");
const selectedFilesSummary = document.getElementById("selected-files-summary");
const selectedFilesCount = document.getElementById("selected-files-count");
const addMoreRecordings = document.getElementById("add-more-recordings");
const liveForm = document.getElementById("live-form");
const liveMessage = document.getElementById("live-message");

const allowedExtensions = new Set(["mp4", "webm", "mov"]);
const MAX_UPLOAD_BYTES = 512 * 1024 * 1024;
const UPLOAD_TIMEOUT_MS = 10 * 60 * 1000;
let selectedFiles = [];
let isUploading = false;

function formatSize(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function extension(name) {
  const parts = name.toLowerCase().split(".");
  return parts.length > 1 ? parts.pop() : "";
}

function setMessage(element, message, kind = "") {
  element.textContent = message;
  element.className = `form-message${kind ? ` form-message--${kind}` : ""}`;
}

function createField(label, type, value, required, className) {
  const wrapper = document.createElement("label");
  wrapper.className = `upload-item__field${className ? ` ${className}` : ""}`;

  const caption = document.createElement("span");
  caption.textContent = label;
  if (required) {
    const marker = document.createElement("b");
    marker.textContent = " *";
    marker.setAttribute("aria-hidden", "true");
    caption.append(marker);
  }

  const input = type === "textarea"
    ? document.createElement("textarea")
    : document.createElement("input");
  if (type !== "textarea") input.type = type;
  if (type === "date") { input.min = "1990-01-01"; input.max = "2099-12-31"; }   // keeps the year to 4 digits
  input.value = value;
  input.required = required;
  input.maxLength = type === "textarea" ? 500 : 100;
  input.addEventListener("input", updateUploadState);
  wrapper.append(caption, input);
  return wrapper;
}

function renderFiles() {
  uploadList.replaceChildren();
  const hasFiles = selectedFiles.length > 0;
  dropzone.hidden = hasFiles;
  selectedFilesSummary.hidden = !hasFiles;
  selectedFilesCount.textContent = hasFiles
    ? `${selectedFiles.length} recording${selectedFiles.length === 1 ? "" : "s"} selected`
    : "";
  selectedFiles.forEach((entry, index) => {
    const file = entry.file;
    const item = document.createElement("article");
    item.className = "upload-item";
    item.dataset.index = String(index);

    const head = document.createElement("div");
    head.className = "upload-item__head";
    const name = document.createElement("strong");
    name.textContent = file.name;
    const size = document.createElement("span");
    size.textContent = formatSize(file.size);
    const type = document.createElement("span");
    type.textContent = extension(file.name).toUpperCase();
    head.append(name, size, type);
    const state = document.createElement("span");
    state.className = "upload-item__status";
    state.textContent = entry.status === "uploading"
      ? `Uploading ${entry.progress}%`
      : entry.status === "failed" ? "Failed"
      : entry.status === "uploaded" ? "Uploaded — not analyzed yet" : "Ready";
    if (entry.status === "failed") state.classList.add("upload-item__status--error");
    if (entry.status === "uploaded") state.classList.add("upload-item__status--success");
    head.append(state);

    const progress = document.createElement("progress");
    progress.className = "upload-item__progress";
    progress.max = 100;
    progress.value = entry.progress;
    progress.setAttribute("aria-label", `${file.name} upload progress`);

    const error = document.createElement("p");
    error.className = "upload-item__error";
    error.hidden = !entry.error;
    error.textContent = entry.error || "";

    const fields = document.createElement("div");
    fields.className = "upload-item__fields";
    fields.append(
      createField("Camera name or ID", "text", entry.metadata?.cameraName || "", true),
      createField("Recording date", "date", entry.metadata?.date || "", true),
      createField("Recording time", "time", entry.metadata?.time || "", true),
      createField("Description", "textarea", entry.metadata?.description || "", false, "upload-item__field--wide"),
    );

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "upload-item__remove";
    remove.textContent = "Remove";
    remove.disabled = isUploading;
    remove.addEventListener("click", () => {
      if (isUploading) return;
      selectedFiles.splice(index, 1);
      renderFiles();
      updateUploadState();
    });

    item.append(head, progress, error, fields, remove);
    [...fields.querySelectorAll("input, textarea")].forEach((input, fieldIndex) => {
      input.addEventListener("input", () => {
        entry.metadata = entry.metadata || {};
        entry.metadata[["cameraName", "date", "time", "description"][fieldIndex]] = input.value;
      });
    });
    uploadList.append(item);
  });
  updateUploadState();
}

function addFiles(files) {
  const invalid = [...files].filter((file) => !allowedExtensions.has(extension(file.name)));
  if (invalid.length) {
    setMessage(fileMessage, `Unsupported file type: ${invalid.map((file) => file.name).join(", ")}. Choose MP4, WebM or MOV.`, "error");
  } else {
    setMessage(fileMessage, "");
  }

  const valid = [...files].filter((file) => allowedExtensions.has(extension(file.name)));
  const existing = new Set(selectedFiles.map(({ file }) =>
    `${file.name}:${file.size}:${file.lastModified}`));
  selectedFiles.push(...valid
    .filter((file) => !existing.has(`${file.name}:${file.size}:${file.lastModified}`))
    .map((file) => ({ file, status: "ready", progress: 0, metadata: {} })));
  renderFiles();
}

function updateUploadState() {
  const fieldsComplete = [...uploadList.querySelectorAll("article")].every((item) =>
    [...item.querySelectorAll("input[required]")].every((input) => input.value.trim()));
  const ready = selectedFiles.length > 0 && fieldsComplete;
  uploadButton.disabled = isUploading || !ready;
  addMoreRecordings.disabled = isUploading;
  uploadNote.textContent = selectedFiles.length
    ? `${selectedFiles.length} file${selectedFiles.length === 1 ? "" : "s"} selected · metadata ${ready ? "complete" : "required"}`
    : "No files selected";
}

videoInput.addEventListener("change", () => {
  addFiles(videoInput.files);
  videoInput.value = "";
});

["dragenter", "dragover"].forEach((eventName) => {
  dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropzone.classList.add("dropzone--active");
  });
});
["dragleave", "drop"].forEach((eventName) => {
  dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropzone.classList.remove("dropzone--active");
  });
});
dropzone.addEventListener("drop", (event) => addFiles(event.dataTransfer.files));

addMoreRecordings.addEventListener("click", () => {
  if (!isUploading) videoInput.click();
});

function uploadRequest(entry, metadata) {
  return new Promise((resolve, reject) => {
    const body = new FormData();
    body.append("file", entry.file, entry.file.name);
    body.append("camera_name", metadata.cameraName);
    body.append("recording_at", `${metadata.date}T${metadata.time}`);
    body.append("description", metadata.description);
    const request = new XMLHttpRequest();
    request.open("POST", "/api/uploads");
    request.timeout = UPLOAD_TIMEOUT_MS;
    request.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) return;
      entry.progress = Math.round((event.loaded / event.total) * 100);
      renderFiles();
    });
    request.addEventListener("load", () => {
      let response;
      try { response = JSON.parse(request.responseText); } catch { response = {}; }
      if (request.status >= 200 && request.status < 300) resolve(response);
      else reject(new Error(response.detail || `Upload failed (HTTP ${request.status})`));
    });
    request.addEventListener("error", () => reject(new Error("Network error while uploading")));
    request.addEventListener("abort", () => reject(new Error("Upload was cancelled")));
    request.addEventListener("timeout", () =>
      reject(new Error("Upload timed out after 10 minutes. Check the connection and try again")));
    request.send(body);
  });
}

function itemMetadata(item) {
  const inputs = item.querySelectorAll("input, textarea");
  const metadata = {
    cameraName: inputs[0].value.trim(),
    date: inputs[1].value,
    time: inputs[2].value,
    description: inputs[3].value.trim(),
  };
  const entry = selectedFiles[Number(item.dataset.index)];
  if (entry) entry.metadata = metadata;
  return metadata;
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 15000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } catch (error) {
    if (error.name === "AbortError") throw new Error("request timed out after 15 seconds");
    throw new Error("network error while contacting the server");
  } finally {
    clearTimeout(timer);
  }
}

async function uploadSelected() {
  if (isUploading) return;
  const entries = selectedFiles.filter((entry) => entry.status !== "uploaded");
  const invalid = [];
  for (const entry of entries) {
    const item = uploadList.querySelector(`[data-index="${selectedFiles.indexOf(entry)}"]`);
    const metadata = item ? itemMetadata(item) : null;
    if (!metadata || !metadata.cameraName || !metadata.date || !metadata.time) {
      entry.error = "Camera name, recording date and recording time are required.";
      invalid.push(entry);
    } else if (!/^(19|20)\d{2}-\d{2}-\d{2}$/.test(metadata.date) || Number.isNaN(Date.parse(`${metadata.date}T${metadata.time}`))) {
      entry.error = "Recording date looks wrong. Use a real date with a four-digit year, for example 22-02-2026.";
      invalid.push(entry);
    } else if (entry.file.size > MAX_UPLOAD_BYTES) {
      entry.error = "File exceeds the 512 MB upload limit.";
      invalid.push(entry);
    }
  }
  if (invalid.length) {
    invalid.forEach((entry) => { entry.status = "failed"; });
    renderFiles();
    setMessage(fileMessage, "Fix the highlighted file errors before uploading.", "error");
    return;
  }

  isUploading = true;
  updateUploadState();
  setMessage(fileMessage, "Uploading selected recordings…");
  let failed = false;
  for (const entry of entries) {
    if (entry.status === "uploaded") continue;
    const item = uploadList.querySelector(`[data-index="${selectedFiles.indexOf(entry)}"]`);
    const metadata = item ? itemMetadata(item) : null;
    if (!metadata || !metadata.cameraName || !metadata.date || !metadata.time) {
      entry.status = "failed";
      entry.error = "Camera name, recording date and recording time are required.";
      failed = true;
      continue;
    }
    entry.status = "uploading";
    entry.progress = 0;
    renderFiles();
    try {
      const saved = await uploadRequest(entry, metadata);
      entry.status = "uploaded";
      if (saved && saved.id) {
        try { await fetchWithTimeout(`/api/uploads/${saved.id}/analyze`, { method: "POST" }); }
        catch { /* the card's Analyze button stays available */ }
      }
      entry.progress = 100;
    } catch (error) {
      entry.status = "failed";
      entry.error = error.message;
      failed = true;
    }
    renderFiles();
  }
  let refreshFailed = false;
  try {
    await refreshRecordings();
  } catch (error) {
    refreshFailed = true;
    const outcome = failed ? "Some recordings failed" : "Upload completed";
    setMessage(fileMessage, `${outcome}; the recordings list could not be refreshed: ${error.message}`, "error");
  }
  isUploading = false;
  selectedFiles = selectedFiles.filter((entry) => entry.status !== "uploaded");
  renderFiles();
  if (!refreshFailed) {
    setMessage(fileMessage, failed
      ? "Some recordings failed. Review the file status and try again."
      : "Recordings uploaded. Analysis has started; progress is shown below.", failed ? "error" : "success");
  }
}

uploadButton.addEventListener("click", uploadSelected);

liveForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!liveForm.checkValidity()) {
    liveForm.reportValidity();
    return;
  }
  setMessage(liveMessage, "Not connected yet. The backend gateway is required before this stream can be tested.", "warning");
});

function selectSourceTab(activeButton, activePanel, inactiveButton, inactivePanel) {
  activeButton.classList.add("tab--active");
  activeButton.setAttribute("aria-selected", "true");
  inactiveButton.classList.remove("tab--active");
  inactiveButton.setAttribute("aria-selected", "false");
  activePanel.hidden = false;
  inactivePanel.hidden = true;
}

const liveTab = document.getElementById("live-tab");
const recordingTab = document.getElementById("recording-tab");
const liveSources = document.getElementById("live-sources");
const recordingSources = document.getElementById("recording-sources");
liveTab.addEventListener("click", () =>
  selectSourceTab(liveTab, liveSources, recordingTab, recordingSources));
recordingTab.addEventListener("click", () =>
  selectSourceTab(recordingTab, recordingSources, liveTab, liveSources));

function renderRecording(recording) {
  const item = document.createElement("article");
  item.className = "registered-item";
  item.dataset.recordingId = recording.id;
  const title = document.createElement("strong");
  title.textContent = recording.original_name;
  const meta = document.createElement("span");
  meta.textContent = `${recording.camera_name} · ${new Date(recording.recording_at).toLocaleString()}`;
  const status = document.createElement("span");
  status.className = "registered-item__status";
  const statusText = recording.processing_status === "not_processed"
    ? "Uploaded — not analyzed yet"
    : recording.processing_status === "completed"
      ? `Analysis completed · ${recording.track_count || 0} tracks · ${recording.detection_count || 0} detections${
        recording.analysis?.captions?.status === "completed" ? " · captions ready"
        : recording.analysis?.captions?.status === "processing" ? ` · captioning ${recording.analysis.captions.progress || 0}%` : ""}`
      : recording.processing_status === "failed"
        ? "Analysis failed"
        : `${recording.processing_status} · ${recording.processing_progress || 0}%`;
  status.textContent = statusText;
  if (recording.processing_status === "failed") status.classList.add("registered-item__status--error");
  if (recording.processing_status === "completed") status.classList.add("registered-item__status--success");
  item.append(title, meta, status);
  const action = document.createElement("button");
  action.type = "button";
  action.className = "btn-secondary registered-item__analyze";
  action.textContent = recording.processing_status === "failed" ? "Retry analysis"
    : recording.processing_status === "completed" ? "Re-analyze" : "Analyze video";
  action.disabled = ["queued", "processing"].includes(recording.processing_status);
  action.addEventListener("click", () => startAnalysis(recording.id, action));
  const actions = document.createElement("div");
  actions.className = "registered-item__actions";
  actions.append(action);
  item.append(actions);
  if (recording.processing_status === "completed") {
    for (const [label, href] of [["Investigate", recording.investigate_url], ["Playback", recording.playback_url],
      ["Tracking", recording.tracking_url]]) {
      const link = document.createElement("a");
      link.className = "btn-secondary registered-item__analyze";
      link.href = href;
      link.textContent = label;
      actions.append(link);
    }
  }
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "btn-secondary registered-item__analyze registered-item__delete";
  remove.textContent = "Delete";
  remove.disabled = ["queued", "processing"].includes(recording.processing_status);
  remove.addEventListener("click", async () => {
    if (!confirm(`Delete ${recording.original_name} and all of its analysis and chat history?`)) return;
    try {
      const response = await fetchWithTimeout(`/api/uploads/${recording.id}`, { method: "DELETE" });
      if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `HTTP ${response.status}`);
      await refreshRecordings();
    } catch (error) {
      setMessage(fileMessage, `Delete failed: ${error.message}`, "error");
    }
  });
  actions.append(remove);
  if (["queued", "processing"].includes(recording.processing_status)) {
    const progress = document.createElement("progress");
    progress.className = "registered-item__progress";
    progress.max = 100;
    progress.value = recording.processing_progress || 0;
    progress.setAttribute("aria-label", `${recording.original_name} analysis progress`);
    item.append(progress);
  }
  if (recording.description) {
    const description = document.createElement("p");
    description.textContent = recording.description;
    item.append(description);
  }
  if (recording.error) {
    const error = document.createElement("p");
    error.className = "registered-item__error";
    error.textContent = recording.error;
    item.append(error);
  }
  if (recording.processing_status === "completed") {
    loadRecordingDetections(recording.id, item);
  }
  return item;
}

async function startAnalysis(recordingId, action) {
  action.disabled = true;
  try {
    const response = await fetchWithTimeout(`/api/uploads/${recordingId}/analyze`, { method: "POST" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `analysis could not start (HTTP ${response.status})`);
    await refreshRecordings();
  } catch (error) {
    setMessage(fileMessage, error.message, "error");
    action.disabled = false;
  }
}

async function loadRecordingDetections(recordingId, item) {
  try {
    const response = await fetchWithTimeout(`/api/uploads/${recordingId}/tracks`);
    const tracks = await response.json();
    if (!response.ok) throw new Error(tracks.detail || "tracks unavailable");
    const section = document.createElement("div");
    section.className = "recording-detections";
    const counts = {};
    tracks.forEach((t) => { counts[t.class_name] = (counts[t.class_name] || 0) + 1; });
    const heading = document.createElement("strong");
    heading.textContent = tracks.length
      ? `Tracked: ${Object.entries(counts).map(([name, n]) => `${n} ${name}${n > 1 ? "s" : ""}`).join(", ")}`
      : "No supported objects were tracked.";
    section.append(heading);
    const strip = document.createElement("div");
    strip.className = "recording-detections__strip";
    tracks.filter((t) => t.thumb_url).slice(0, 14).forEach((t) => {
      const link = document.createElement("a");
      link.href = `/people.html?recording=${encodeURIComponent(recordingId)}&track=${t.track_id}`;
      link.title = `${t.class_name} ${t.track_id}`;
      link.innerHTML = `<img src="${t.thumb_url}" alt="${t.class_name} ${t.track_id}" loading="lazy">`;
      strip.append(link);
    });
    section.append(strip);
    item.append(section);
  } catch (error) {
    const errorMessage = document.createElement("p");
    errorMessage.className = "registered-item__error";
    errorMessage.textContent = `Analysis results unavailable: ${error.message}`;
    item.append(errorMessage);
  }
}

async function refreshRecordings() {
  const response = await fetchWithTimeout("/api/uploads", { cache: "no-store" });
  let payload;
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  if (!response.ok) {
    throw new Error(payload.detail || `recordings unavailable (HTTP ${response.status})`);
  }
  if (!Array.isArray(payload)) throw new Error("recordings response was invalid");
  if (payload.length === 0) {
    recordingSources.classList.add("empty-state");
    recordingSources.classList.remove("registered-list");
    recordingSources.replaceChildren(
      Object.assign(document.createElement("span"), { className: "empty-state__mark", textContent: "—" }),
      Object.assign(document.createElement("strong"), { textContent: "No uploaded recordings" }),
      Object.assign(document.createElement("span"), { textContent: "Uploaded files and their processing status will appear here." }),
    );
    return;
  }
  recordingSources.classList.remove("empty-state");
  recordingSources.classList.add("registered-list");
  recordingSources.replaceChildren();
  payload.forEach((recording) => recordingSources.append(renderRecording(recording)));
  payload.filter((recording) => ["queued", "processing"].includes(recording.processing_status)
    || recording.analysis?.captions?.status === "processing")
    .forEach((recording) => {
      setTimeout(async () => {
        try {
          const response = await fetchWithTimeout(`/api/uploads/${recording.id}/analysis`);
          if (response.ok) {
            const latest = await response.json();
            if (["queued", "processing"].includes(latest.processing_status)) {
              await refreshRecordings();
            } else {
              await refreshRecordings();
            }
          }
        } catch (error) {
          setMessage(fileMessage, `Analysis status unavailable: ${error.message}`, "error");
        }
      }, 2000);
    });
}

refreshRecordings().then(() => {
  // laptop mode has no live sources: open on the uploads people actually use
  if (recordingSources.classList.contains("registered-list")) {
    selectSourceTab(recordingTab, recordingSources, liveTab, liveSources);
  }
}).catch((error) => {
  setMessage(fileMessage, `Uploaded recordings are unavailable: ${error.message}`, "error");
});
