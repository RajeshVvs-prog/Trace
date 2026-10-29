/* Investigator — uploaded-recording mode.
 *
 * Picking an uploaded recording (or opening /?recording=<id>) switches the page
 * into "rec-mode": the chat talks to /api/uploads/<id>/investigation, history
 * is the recording's own persisted thread, and a video stage on the right
 * plays the actual file. Every evidence item in an answer carries offset_ms,
 * so clicking it seeks the video and highlights that track's box. */

import {
  attachOverlay, clock, DetectionIndex, esc, fillRecordingSelect, getJSON, pageLink,
  seekVideo, setUrlState, urlState,
} from "./recording-common.js";
import { getMode } from "./mode.js";

const $ = (id) => document.getElementById(id);
const selector = $("recording-investigation");
const status = $("recording-investigation-status");
const links = $("recording-links");
const thread = $("thread");
const input = $("input");
const compose = $("compose");
const stage = $("rec-stage");
const video = $("rec-video");
const evidenceBox = $("rec-evidence");
const nowEl = $("rec-now");
const focusEl = $("rec-focus");

const SUGGESTIONS = [
  "Summarize what happened",
  "How many people appear?",
  "Did anyone run?",
  "When did someone raise their hands?",
  "Is anyone wearing a mask?",
  "Did anyone follow someone?",
  "What is visible at 0:10?",
];

const state = { recording: null, overlay: null, busy: false, poll: null, liveAvailable: false };

function show(text, kind = "") {
  status.textContent = text;
  status.dataset.kind = kind;
}

function api(path = "") {
  return `/api/uploads/${encodeURIComponent(state.recording.id)}${path}`;
}

/* ---- thread -------------------------------------------------------------- */

function welcome(rec) {
  const ready = rec.processing_status === "completed";
  const dur = rec.analysis?.duration_s;
  const facts = [esc(rec.original_name)];
  if (dur) facts.push(`<span class="timecode">${clock(dur * 1000)}</span> long`);
  if (ready) facts.push(`${rec.track_count} tracked object${rec.track_count === 1 ? "" : "s"}`);
  thread.innerHTML = `
    <div class="inv-welcome rec-welcome" id="welcome">
      <h2>What happened at ${esc(rec.camera_name)}?</h2>
      <p class="rec-welcome__facts">${facts.join(", ")}</p>
      ${ready
        ? `<p>Ask about people, vehicles, movement or body actions. Try one of these:</p>
           <div class="inv-suggest rec-suggest">${SUGGESTIONS.map(
             (q) => `<button type="button" data-q="${esc(q)}">${esc(q)}</button>`).join("")}</div>`
        : rec.processing_status === "failed"
          ? `<div class="notice notice--err">Analysis of this recording failed${rec.error ? `: ${esc(rec.error)}` : ""}.
              Retry it on the <a href="sources.html">Recordings</a> page.</div>`
          : ["queued", "processing"].includes(rec.processing_status)
            ? `<div class="loading"><span class="spinner" aria-hidden="true"></span>Analysing this recording. You can ask questions as soon as it finishes.</div>`
            : `<div class="notice">This recording has not been analysed yet. Start analysis on the
                <a href="sources.html">Recordings</a> page, then come back.</div>`}
    </div>`;
  setComposeEnabled(ready);
}

function setComposeEnabled(on) {
  input.disabled = !on;
  $("send").disabled = !on;
  input.placeholder = on ? `Ask about ${state.recording?.camera_name || "this recording"}…` : "Questions open once analysis finishes";
}

function chip(e) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "rec-chip";
  b.dataset.offset = e.offset_ms;
  if (e.track_id != null) b.dataset.track = e.track_id;
  b.setAttribute("aria-label", `Show ${clock(e.offset_ms)} in the video${e.track_id != null ? `, track ${e.track_id}` : ""}`);
  b.innerHTML = `${e.thumb_url ? `<img src="${esc(e.thumb_url)}" alt="" loading="lazy">` : ""}
    <span class="rec-chip__time timecode">${clock(e.offset_ms)}</span>
    <span class="rec-chip__label">${esc((e.label || "").replace(/^\d+:\d{2} · /, ""))}</span>`;
  return b;
}

function addMessage(role, text, evidence = [], ts = Date.now()) {
  $("welcome")?.remove();
  const item = document.createElement("div");
  item.className = `msg msg--${role}`;
  const body = document.createElement("div");
  body.className = "msg__body";
  body.textContent = text;
  item.appendChild(body);
  if (evidence.length) {
    const row = document.createElement("div");
    row.className = "rec-chips";
    evidence.slice(0, 8).forEach((e) => row.appendChild(chip(e)));
    item.appendChild(row);
  }
  const time = document.createElement("span");
  time.className = "msg__time";
  time.textContent = new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  item.appendChild(time);
  thread.appendChild(item);
  thread.scrollTop = thread.scrollHeight;
  return item;
}

function renderEvidence(evidence) {
  evidenceBox.innerHTML = "";
  if (!evidence?.length) {
    evidenceBox.innerHTML = `<p class="rec-stage__empty">This answer has no timestamped evidence.</p>`;
    return;
  }
  for (const e of evidence) {
    const card = chip(e);
    card.classList.add("rec-chip--card");
    if (e.start_offset_ms != null && e.end_offset_ms != null && e.end_offset_ms > e.start_offset_ms) {
      card.insertAdjacentHTML("beforeend",
        `<span class="rec-chip__span">visible ${clock(e.start_offset_ms)}–${clock(e.end_offset_ms)}</span>`);
    }
    evidenceBox.appendChild(card);
  }
}

/* ---- video stage ----------------------------------------------------------- */

function focus(offsetMs, trackId) {
  seekVideo(video, offsetMs / 1000);
  video.pause();
  if (state.overlay) state.overlay.highlight = Number.isFinite(trackId) ? trackId : null;
  focusEl.textContent = Number.isFinite(trackId) ? `Highlighting track ${trackId}` : "";
  document.querySelectorAll(".rec-chip.on").forEach((c) => c.classList.remove("on"));
  document.querySelectorAll(`.rec-chip[data-offset="${offsetMs}"]`).forEach((c) => {
    if (!Number.isFinite(trackId) || +c.dataset.track === trackId) c.classList.add("on");
  });
  setUrlState({ t: (offsetMs / 1000).toFixed(1), track: Number.isFinite(trackId) ? trackId : null });
  $("recording-investigation-playback").href = pageLink("playback.html", state.recording.id, {
    t: (offsetMs / 1000).toFixed(1), ...(Number.isFinite(trackId) ? { track: trackId } : {}) });
}

document.addEventListener("click", (event) => {
  const c = event.target.closest(".rec-chip");
  if (!c || !document.body.classList.contains("rec-mode")) return;
  focus(+c.dataset.offset, c.dataset.track !== undefined ? +c.dataset.track : NaN);
});

video.addEventListener("timeupdate", () => { nowEl.textContent = clock(video.currentTime * 1000); });
$("rec-boxes").addEventListener("change", (e) => { if (state.overlay) state.overlay.enabled = e.target.checked; });

/* ---- asking ---------------------------------------------------------------- */

async function ask(text) {
  text = (text || "").trim();
  if (!text || state.busy || !state.recording) return;
  if (state.recording.processing_status !== "completed") {
    show("Analyse this recording before asking about it.", "error");
    return;
  }
  state.busy = true;
  $("send").disabled = true;
  input.value = "";
  addMessage("user", text);
  const thinking = addMessage("assistant", "Searching the analysis…");
  thinking.classList.add("msg--thinking");
  thinking.querySelector(".msg__body").insertAdjacentHTML("afterbegin", `<span class="spinner" aria-hidden="true"></span>`);
  try {
    const reply = await getJSON(api("/investigation"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });
    thinking.remove();
    addMessage("assistant", reply.content, reply.evidence);
    renderEvidence(reply.evidence);
    if (reply.evidence?.length) {
      const first = reply.evidence[0];
      focus(first.offset_ms, first.track_id ?? NaN);
    }
  } catch (error) {
    thinking.remove();
    const failed = addMessage("assistant", `That question could not be answered: ${error.message}.`);
    failed.classList.add("msg--error");
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "btn btn--sm";
    retry.textContent = "Ask again";
    retry.addEventListener("click", () => { failed.remove(); ask(text); });
    failed.insertBefore(retry, failed.querySelector(".msg__time"));
  } finally {
    state.busy = false;
    $("send").disabled = false;
    autosize();
    input.focus();
  }
}

function autosize() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
}
input.addEventListener("input", () => { if (document.body.classList.contains("rec-mode")) autosize(); });

// Capture phase: runs before the live investigator's handlers; they also
// check rec-mode, so only one chat ever handles a message.
compose.addEventListener("submit", (e) => {
  if (!document.body.classList.contains("rec-mode")) return;
  e.preventDefault();
  ask(input.value);
}, true);
input.addEventListener("keydown", (e) => {
  if (!document.body.classList.contains("rec-mode")) return;
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(input.value); }
}, true);
document.addEventListener("click", (e) => {
  const b = e.target.closest(".rec-suggest button");
  if (b && document.body.classList.contains("rec-mode")) ask(b.dataset.q);
});

$("recording-clear").addEventListener("click", async () => {
  if (!state.recording || !confirm("Clear this recording’s chat history? The analysis is kept.")) return;
  try {
    await getJSON(api("/investigation"), { method: "DELETE" });
    welcome(state.recording);
    renderEvidence([]);
  } catch (error) {
    show(error.message, "error");
  }
});

/* ---- entering / leaving recording mode ------------------------------------ */

function statusLine(rec) {
  const meta = rec.analysis || {};
  const caps = meta.captions?.status;
  const bits = [rec.camera_name];
  if (rec.processing_status === "completed") {
    bits.push(`${rec.track_count} tracks`, `${rec.detection_count} detections`);
    if (caps === "completed") bits.push(`captions: ${meta.captions.model}`);
    else if (caps === "processing") bits.push(`captioning ${meta.captions.progress || 0}%`);
  } else if (rec.processing_status === "failed") {
    bits.push(`analysis failed: ${rec.error}`);
  } else {
    bits.push(rec.processing_status === "processing" ? `analysing ${rec.processing_progress}%`
      : rec.processing_status === "queued" ? "waiting to analyse" : "not analysed yet");
  }
  return bits.join(", ");
}

async function enter(id) {
  clearInterval(state.poll);
  let payload;
  try {
    payload = await getJSON(`/api/uploads/${encodeURIComponent(id)}/investigation`);
  } catch (error) {
    show(error.message, "error");
    return;
  }
  const rec = payload.recording;
  state.recording = rec;
  document.body.classList.add("rec-mode");
  stage.hidden = false;
  links.hidden = false;
  setUrlState({ recording: rec.id });
  show(statusLine(rec), rec.processing_status === "failed" ? "error" : "");
  $("recording-investigation-playback").href = pageLink("playback.html", rec.id);
  $("recording-investigation-tracking").href = pageLink("people.html", rec.id);

  if (video.dataset.recording !== rec.id) {
    video.dataset.recording = rec.id;
    video.src = rec.file_url;
  }
  if (!state.overlay) state.overlay = attachOverlay($("rec-anno"), video);
  state.overlay.index = null;
  if (rec.processing_status === "completed") {
    DetectionIndex.load(rec.id).then((index) => { if (state.recording?.id === rec.id) state.overlay.index = index; })
      .catch(() => show("Detections could not be loaded for the overlay.", "error"));
  }

  welcome(rec);
  renderEvidence([]);
  let lastEvidence = [];
  for (const m of payload.messages) {
    addMessage(m.role, m.content, m.evidence, Date.parse(m.created_at));
    if (m.role === "assistant" && m.evidence?.length) lastEvidence = m.evidence;
  }
  if (!payload.messages.length) welcome(rec);
  if (lastEvidence.length) renderEvidence(lastEvidence);

  const { t, track } = urlState();
  if (t !== null) focus(t * 1000, track ?? NaN);

  if (["queued", "processing"].includes(rec.processing_status) || rec.analysis?.captions?.status === "processing") {
    state.poll = setInterval(async () => {
      try {
        const fresh = await getJSON(`/api/uploads/${encodeURIComponent(rec.id)}/analysis`);
        show(statusLine(fresh));
        const doneAnalysis = fresh.processing_status !== rec.processing_status && !["queued", "processing"].includes(fresh.processing_status);
        const doneCaptions = fresh.analysis?.captions?.status !== "processing";
        if (doneAnalysis) { clearInterval(state.poll); enter(rec.id); }
        else if (doneCaptions && !["queued", "processing"].includes(fresh.processing_status)) {
          clearInterval(state.poll); state.recording = fresh;
        }
      } catch { /* keep polling */ }
    }, 2500);
  }
}

function leave() {
  setUrlState({ recording: null, t: null, track: null });
  location.reload();     // the live investigator boots fresh
}

selector.addEventListener("change", () => {
  if (selector.value) {
    setUrlState({ t: null, track: null });
    enter(selector.value);
  } else if (state.liveAvailable) {
    leave();
  }
});

(async function boot() {
  state.liveAvailable = false;
  if (await getMode() === "server") {
    try { state.liveAvailable = (await fetch("/agent/meta", { cache: "no-store" })).ok; } catch { /* no agent */ }
  }
  const live = selector.options[0];
  if (!state.liveAvailable) live.remove();
  else live.textContent = "Live cameras (whole building)";
  let recordings = [];
  const wanted = urlState().recording;
  try {
    recordings = await fillRecordingSelect(selector, { selected: wanted });
  } catch (error) {
    show(`Recordings could not be loaded: ${error.message}. Check that the TRACE server is running, then reload.`, "error");
    thread.innerHTML = "";
    return;
  }
  let pick = wanted && recordings.some((r) => r.id === wanted) ? wanted : "";
  if (!pick && !state.liveAvailable) {
    pick = (recordings.find((r) => r.processing_status === "completed") || recordings[0])?.id || "";
  }
  if (pick) {
    selector.value = pick;
    enter(pick);
  } else if (!state.liveAvailable) {
    document.body.classList.add("rec-mode", "rec-empty");
    selector.disabled = true;
    selector.appendChild(new Option("No recordings yet", ""));
    show("");
    setComposeEnabled(false);
    input.placeholder = "Upload a recording to start asking";
    thread.innerHTML = `<div class="inv-welcome" id="welcome">
      <div class="empty"><strong>Nothing to investigate yet</strong>
      <p>Upload a CCTV recording and let it analyse. Then you can ask what happened in it, and every
        answer will link to the moment in the video.</p>
      <a class="btn btn--primary" href="sources.html#upload">Upload a recording</a></div></div>`;
  }
})();
