/* Recordings: upload videos with their camera/date/time metadata, start
 * analysis, and manage what is already on this machine.
 *
 * API (bridge.py): POST /api/uploads (multipart), POST /api/uploads/<id>/analyze,
 * GET /api/uploads, GET /api/uploads/<id>/tracks, DELETE /api/uploads/<id>. */

import { clock, esc, pageLink } from "./recording-common.js";

const $ = (id) => document.getElementById(id);
const videoInput = $("video-files");
const dropzone = $("dropzone");
const queue = $("queue");
const uploadList = $("upload-list");
const uploadButton = $("upload-button");
const uploadNote = $("upload-note");
const fileMessage = $("file-message");
const countEl = $("selected-files-count");
const addMore = $("add-more-recordings");
const library = $("recording-sources");
const libraryCount = $("library-count");

const ALLOWED = new Set(["mp4", "webm", "mov"]);
const MAX_UPLOAD_BYTES = 512 * 1024 * 1024;
const UPLOAD_TIMEOUT_MS = 10 * 60 * 1000;
const BUSY = ["queued", "processing"];

let selected = [];         // { key, file, status, progress, error, meta:{cameraName,date,time,description} }
let uploading = false;
let pollTimer = null;
const thumbCache = new Map();

/* ---- helpers -------------------------------------------------------------- */

const ext = (name) => (name.toLowerCase().split(".").length > 1 ? name.toLowerCase().split(".").pop() : "");
const pad = (n) => String(n).padStart(2, "0");

function formatSize(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function message(text, kind = "") {
  fileMessage.textContent = text;
  fileMessage.className = `notice${kind ? ` notice--${kind}` : ""}`;
}

async function request(url, options = {}, timeoutMs = 15000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(url, { cache: "no-store", ...options, signal: controller.signal });
  } catch (error) {
    throw new Error(error.name === "AbortError"
      ? "the server did not answer within 15 seconds"
      : "the TRACE server could not be reached");
  } finally {
    clearTimeout(timer);
  }
  let body = null;
  try { body = await res.json(); } catch { /* non-JSON body */ }
  if (!res.ok) throw new Error((body && body.detail) || `HTTP ${res.status}`);
  return body;
}

/* ---- selection ------------------------------------------------------------- */

function addFiles(files) {
  const list = [...files];
  const bad = list.filter((f) => !ALLOWED.has(ext(f.name)));
  const big = list.filter((f) => ALLOWED.has(ext(f.name)) && f.size > MAX_UPLOAD_BYTES);
  const problems = [];
  if (bad.length) problems.push(`${bad.map((f) => f.name).join(", ")} ${bad.length === 1 ? "is not" : "are not"} MP4, WebM or MOV`);
  if (big.length) problems.push(`${big.map((f) => f.name).join(", ")} ${big.length === 1 ? "is" : "are"} over the 512 MB limit`);
  message(problems.length ? `Skipped: ${problems.join("; ")}.` : "", problems.length ? "err" : "");

  const known = new Set(selected.map((e) => e.key));
  for (const file of list) {
    if (!ALLOWED.has(ext(file.name)) || file.size > MAX_UPLOAD_BYTES) continue;
    const key = `${file.name}:${file.size}:${file.lastModified}`;
    if (known.has(key)) continue;
    // the file's modified time is a reasonable first guess at when it was recorded
    const d = new Date(file.lastModified || Date.now());
    selected.push({
      key, file, status: "ready", progress: 0, error: "",
      meta: {
        cameraName: "", description: "",
        date: `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`,
        time: `${pad(d.getHours())}:${pad(d.getMinutes())}`,
      },
    });
  }
  renderQueue();
  const first = uploadList.querySelector('input[name="cameraName"]:placeholder-shown');
  if (first && !uploading) first.focus();
}

function validate(entry) {
  const m = entry.meta;
  if (!m.cameraName.trim()) return "Add a camera name or ID.";
  if (!/^(19|20)\d{2}-\d{2}-\d{2}$/.test(m.date)) return "Enter the recording date with a four-digit year.";
  if (!/^\d{2}:\d{2}$/.test(m.time) || Number.isNaN(Date.parse(`${m.date}T${m.time}`))) return "Enter the time the recording starts.";
  return "";
}

function field(entry, name, label, type, { required = true, hint = "" } = {}) {
  const id = `f-${name}-${selected.indexOf(entry)}`;
  const input = type === "textarea" ? document.createElement("textarea") : document.createElement("input");
  input.id = id;
  input.name = name;
  input.className = type === "textarea" ? "textarea" : "input";
  if (type !== "textarea") input.type = type;
  else input.rows = 2;
  if (type === "date") { input.min = "1990-01-01"; input.max = "2099-12-31"; }
  if (type === "text") input.placeholder = "e.g. Lobby entrance";
  input.maxLength = type === "textarea" ? 500 : 100;
  input.required = required;
  input.value = entry.meta[name];
  input.disabled = uploading || entry.status === "uploaded";
  input.addEventListener("input", () => {
    entry.meta[name] = input.value;
    if (entry.status === "failed" && !validate(entry)) { entry.status = "ready"; entry.error = ""; }
    input.removeAttribute("aria-invalid");
    updateState();
  });
  const wrap = document.createElement("div");
  wrap.className = `field field--${name}`;
  wrap.innerHTML = `<label class="field__label" for="${id}">${esc(label)}${required ? "" : " <small>optional</small>"}</label>`;
  wrap.appendChild(input);
  if (hint) wrap.insertAdjacentHTML("beforeend", `<span class="field__hint">${esc(hint)}</span>`);
  return wrap;
}

function renderQueue() {
  const has = selected.length > 0;
  queue.hidden = !has;
  dropzone.classList.toggle("dropzone--compact", has);
  countEl.textContent = has ? `${selected.length} file${selected.length === 1 ? "" : "s"} to upload` : "";
  uploadList.replaceChildren(...selected.map((entry) => {
    const item = document.createElement("article");
    item.className = `qitem qitem--${entry.status}`;
    const statusText = {
      ready: "", uploading: `Uploading ${entry.progress}%`, uploaded: "Uploaded, analysis started", failed: "Needs attention",
    }[entry.status];
    item.innerHTML = `
      <div class="qitem__head">
        <div class="qitem__file"><strong title="${esc(entry.file.name)}">${esc(entry.file.name)}</strong>
          <span>${formatSize(entry.file.size)}, ${ext(entry.file.name).toUpperCase()}</span></div>
        ${statusText ? `<span class="status ${entry.status === "failed" ? "status--err" : entry.status === "uploaded" ? "status--ok" : "status--busy"}">${statusText}</span>` : ""}
      </div>`;
    if (entry.status === "uploading") {
      item.insertAdjacentHTML("beforeend",
        `<progress class="progress" max="100" value="${entry.progress}" aria-label="${esc(entry.file.name)} upload progress"></progress>`);
    }
    const grid = document.createElement("div");
    grid.className = "qitem__fields";
    grid.append(
      field(entry, "cameraName", "Camera name or ID", "text"),
      field(entry, "date", "Recording date", "date", { hint: "Pre-filled from the file; correct it if needed." }),
      field(entry, "time", "Start time", "time"),
      field(entry, "description", "Notes", "textarea", { required: false }),
    );
    item.appendChild(grid);
    if (entry.error) {
      item.insertAdjacentHTML("beforeend", `<p class="qitem__error" role="alert">${esc(entry.error)}</p>`);
      const bad = entry.error.startsWith("Add a camera") ? "cameraName" : entry.error.includes("date") ? "date"
        : entry.error.includes("time") ? "time" : "";
      if (bad) grid.querySelector(`[name="${bad}"]`)?.setAttribute("aria-invalid", "true");
    }
    if (entry.status !== "uploaded") {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "btn btn--sm btn--quiet qitem__remove";
      remove.textContent = "Remove";
      remove.disabled = uploading;
      remove.setAttribute("aria-label", `Remove ${entry.file.name}`);
      remove.addEventListener("click", () => {
        selected = selected.filter((e) => e !== entry);
        renderQueue();
      });
      item.querySelector(".qitem__head").appendChild(remove);
    }
    return item;
  }));
  updateState();
}

function updateState() {
  const pending = selected.filter((e) => e.status !== "uploaded");
  const missing = pending.filter((e) => validate(e)).length;
  uploadButton.disabled = uploading || !pending.length;
  addMore.disabled = uploading;
  uploadButton.textContent = pending.length > 1 ? `Upload and analyse ${pending.length} files` : "Upload and analyse";
  uploadNote.textContent = uploading ? "Keep this page open until the uploads finish."
    : missing ? `${missing} file${missing === 1 ? " needs" : "s need"} a camera name, date and time.` : "";
}

videoInput.addEventListener("change", () => { addFiles(videoInput.files); videoInput.value = ""; });
addMore.addEventListener("click", () => { if (!uploading) videoInput.click(); });
for (const type of ["dragenter", "dragover"]) {
  dropzone.addEventListener(type, (e) => { e.preventDefault(); dropzone.classList.add("is-over"); });
}
for (const type of ["dragleave", "drop"]) {
  dropzone.addEventListener(type, (e) => { e.preventDefault(); dropzone.classList.remove("is-over"); });
}
dropzone.addEventListener("drop", (e) => { if (!uploading) addFiles(e.dataTransfer.files); });

/* ---- upload ---------------------------------------------------------------- */

function send(entry) {
  return new Promise((resolve, reject) => {
    const body = new FormData();
    body.append("file", entry.file, entry.file.name);
    body.append("camera_name", entry.meta.cameraName.trim());
    body.append("recording_at", `${entry.meta.date}T${entry.meta.time}`);
    body.append("description", entry.meta.description.trim());
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/uploads");
    xhr.timeout = UPLOAD_TIMEOUT_MS;
    let last = 0;
    xhr.upload.addEventListener("progress", (e) => {
      if (!e.lengthComputable) return;
      entry.progress = Math.round((e.loaded / e.total) * 100);
      const bar = uploadList.querySelectorAll(".qitem")[selected.indexOf(entry)]?.querySelector("progress");
      if (bar) bar.value = entry.progress;
      if (entry.progress - last >= 5) { last = entry.progress; renderQueue(); }
    });
    xhr.addEventListener("load", () => {
      let res = {};
      try { res = JSON.parse(xhr.responseText); } catch { /* keep {} */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(res);
      else reject(new Error(res.detail || `the server refused the upload (HTTP ${xhr.status})`));
    });
    xhr.addEventListener("error", () => reject(new Error("the connection dropped while uploading")));
    xhr.addEventListener("abort", () => reject(new Error("the upload was cancelled")));
    xhr.addEventListener("timeout", () => reject(new Error("the upload took longer than 10 minutes")));
    xhr.send(body);
  });
}

async function uploadAll() {
  if (uploading) return;
  const pending = selected.filter((e) => e.status !== "uploaded");
  let invalid = 0;
  for (const entry of pending) {
    entry.error = validate(entry);
    if (entry.error) { entry.status = "failed"; invalid += 1; }
  }
  if (invalid) {
    renderQueue();
    message(`${invalid} file${invalid === 1 ? " is" : "s are"} missing details. Fill in the highlighted fields.`, "err");
    uploadList.querySelector('[aria-invalid="true"]')?.focus();
    return;
  }
  uploading = true;
  message("");
  let failed = 0, done = 0;
  for (const entry of pending) {
    entry.status = "uploading";
    entry.progress = 0;
    renderQueue();
    try {
      const saved = await send(entry);
      entry.status = "uploaded";
      entry.progress = 100;
      done += 1;
      if (saved?.id) {
        try { await request(`/api/uploads/${encodeURIComponent(saved.id)}/analyze`, { method: "POST" }); }
        catch { /* the recording's "Analyse" button stays available */ }
      }
    } catch (error) {
      entry.status = "failed";
      entry.error = `Upload failed: ${error.message}.`;
      failed += 1;
    }
    renderQueue();
    refresh().catch(() => {});
  }
  uploading = false;
  selected = selected.filter((e) => e.status !== "uploaded");
  renderQueue();
  if (failed) message(`${done} uploaded, ${failed} failed. Check the files marked below and try again.`, "err");
  else message(`${done === 1 ? "Recording" : `${done} recordings`} uploaded. Analysis is running; progress shows below.`, "ok");
}

uploadButton.addEventListener("click", uploadAll);
window.addEventListener("beforeunload", (e) => { if (uploading) { e.preventDefault(); e.returnValue = ""; } });

/* ---- library --------------------------------------------------------------- */

function statusFor(r) {
  const caps = r.analysis?.captions;
  switch (r.processing_status) {
    case "completed": {
      let text = `Analysed: ${r.track_count || 0} tracked objects`;
      if (caps?.status === "processing") text += `, describing people ${caps.progress || 0}%`;
      return ["status--ok", text];
    }
    case "processing": return ["status--busy", `Analysing ${r.processing_progress || 0}%`];
    case "queued": return ["status--busy", "Waiting to analyse"];
    case "failed": return ["status--err", "Analysis failed"];
    default: return ["", "Not analysed yet"];
  }
}

function recordingCard(r) {
  const [cls, label] = statusFor(r);
  const done = r.processing_status === "completed";
  const busy = BUSY.includes(r.processing_status);
  const when = new Date(r.recording_at);
  const dur = r.analysis?.duration_s;
  const card = document.createElement("article");
  card.className = "rcard";
  card.dataset.recordingId = r.id;
  card.innerHTML = `
    <div class="rcard__main">
      <div class="rcard__title">
        <h3 title="${esc(r.original_name)}">${esc(r.original_name)}</h3>
        <span class="status ${cls}">${esc(label)}</span>
      </div>
      <p class="rcard__meta">${esc(r.camera_name)}${Number.isNaN(when.getTime()) ? "" : `, recorded ${esc(when.toLocaleString([], { dateStyle: "medium", timeStyle: "short" }))}`}${
        dur ? `, length <span class="timecode">${clock(dur * 1000)}</span>` : ""}${r.size_bytes ? `, ${formatSize(r.size_bytes)}` : ""}</p>
      ${r.description ? `<p class="rcard__desc">${esc(r.description)}</p>` : ""}
      ${busy ? `<progress class="progress" max="100" value="${r.processing_progress || 0}" aria-label="Analysis progress for ${esc(r.original_name)}"></progress>` : ""}
      ${r.processing_status === "failed" && r.error ? `<p class="rcard__error">${esc(r.error)}</p>` : ""}
      <div class="rcard__tracks" hidden></div>
    </div>
    <div class="rcard__actions">
      ${done ? `<a class="btn btn--primary btn--sm" href="${pageLink("investigate.html", r.id)}">Investigate</a>
        <a class="btn btn--sm" href="${pageLink("playback.html", r.id)}">Playback</a>
        <a class="btn btn--sm" href="${pageLink("people.html", r.id)}">Tracking</a>` : ""}
      <button type="button" class="btn btn--sm ${done ? "btn--quiet" : r.processing_status === "failed" || r.processing_status === "not_processed" ? "btn--primary" : ""}" data-act="analyze" ${busy ? "disabled" : ""}>${
        r.processing_status === "failed" ? "Retry analysis" : done ? "Re-analyse" : busy ? "Analysing…" : "Analyse"}</button>
      <button type="button" class="btn btn--sm btn--quiet btn--danger" data-act="delete" ${busy ? "disabled" : ""}>Delete</button>
    </div>`;
  card.querySelector('[data-act="analyze"]').addEventListener("click", (e) => analyse(r, e.currentTarget));
  card.querySelector('[data-act="delete"]').addEventListener("click", (e) => remove(r, e.currentTarget));
  if (done) tracks(r, card.querySelector(".rcard__tracks"));
  return card;
}

async function tracks(r, box) {
  const key = `${r.id}:${r.updated_at}`;
  try {
    if (!thumbCache.has(key)) thumbCache.set(key, request(`/api/uploads/${encodeURIComponent(r.id)}/tracks`));
    const list = await thumbCache.get(key);
    const counts = {};
    list.forEach((t) => { counts[t.class_name] = (counts[t.class_name] || 0) + 1; });
    const summary = Object.entries(counts).map(([n, c]) => `${c} ${n}${c > 1 ? (n === "person" ? "s" : "s") : ""}`)
      .join(", ").replace(/persons/g, "people");
    const shown = list.filter((t) => t.thumb_url).slice(0, 12);
    box.innerHTML = `<p>${list.length ? `Tracked: ${esc(summary)}` : "No people or vehicles were tracked in this video."}</p>${
      shown.length ? `<div class="rcard__strip">${shown.map((t) =>
        `<a href="${pageLink("people.html", r.id, { track: t.track_id })}" title="${esc(t.class_name)} ${t.track_id}">
          <img src="${esc(t.thumb_url)}" alt="${esc(t.class_name)} ${t.track_id}" loading="lazy" width="40" height="54"></a>`).join("")}</div>` : ""}`;
    box.hidden = false;
  } catch (error) {
    thumbCache.delete(key);
    box.innerHTML = `<p class="rcard__error">Tracks could not be loaded: ${esc(error.message)}.</p>`;
    box.hidden = false;
  }
}

async function analyse(r, btn) {
  btn.disabled = true;
  btn.textContent = "Starting…";
  try {
    await request(`/api/uploads/${encodeURIComponent(r.id)}/analyze`, { method: "POST" });
    message(`Analysis started for ${r.original_name}.`, "ok");
  } catch (error) {
    message(`Analysis could not start for ${r.original_name}: ${error.message}.`, "err");
  }
  refresh().catch(() => {});
}

async function remove(r, btn) {
  if (!confirm(`Delete “${r.original_name}”? Its video, analysis and chat history will be removed from this machine.`)) return;
  btn.disabled = true;
  try {
    await request(`/api/uploads/${encodeURIComponent(r.id)}`, { method: "DELETE" });
    message(`Deleted ${r.original_name}.`, "ok");
  } catch (error) {
    message(`Delete failed for ${r.original_name}: ${error.message}.`, "err");
    btn.disabled = false;
  }
  refresh().catch(() => {});
}

async function refresh() {
  clearTimeout(pollTimer);
  let list;
  try {
    list = await request("/api/uploads");
    if (!Array.isArray(list)) throw new Error("the server sent an unexpected response");
  } catch (error) {
    library.innerHTML = `<div class="notice notice--err">The recordings list could not be loaded: ${esc(error.message)}.
      <button type="button" class="btn btn--sm" id="retry-list">Try again</button></div>`;
    $("retry-list").addEventListener("click", () => refresh().catch(() => {}));
    pollTimer = setTimeout(() => refresh().catch(() => {}), 10000);
    return;
  }
  libraryCount.textContent = list.length ? `${list.length} recording${list.length === 1 ? "" : "s"}` : "";
  if (!list.length) {
    library.innerHTML = `<div class="empty">
      <strong>No recordings yet</strong>
      <p>Add a video above. Once it is analysed you can ask what happened in it, replay it with
        detections drawn on, and follow each person or vehicle.</p></div>`;
    return;
  }
  // re-render in place, keeping focus on the same control where possible
  const focusedAct = document.activeElement?.dataset?.act;
  const focusedId = document.activeElement?.closest?.(".rcard")?.dataset.recordingId;
  const wrap = document.createElement("div");
  wrap.className = "rlist";
  wrap.append(...list.map(recordingCard));
  library.replaceChildren(wrap);
  if (focusedId && focusedAct) library.querySelector(`[data-recording-id="${focusedId}"] [data-act="${focusedAct}"]`)?.focus();
  if (list.some((r) => BUSY.includes(r.processing_status) || r.analysis?.captions?.status === "processing")) {
    pollTimer = setTimeout(() => refresh().catch(() => {}), 2500);
  }
}

refresh().catch(() => {});

