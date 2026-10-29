/* Playback — uploaded-recording mode.
 *
 * Selecting an upload (or opening playback.html?recording=<id>&t=<s>&track=<n>)
 * plays the actual file with the persisted boxes and track numbers drawn on it,
 * a recording-length timeline of every track and event, and a side panel to
 * jump to tracks, events or appearance-search results. */

import {
  attachOverlay, clock, COLORS, DetectionIndex, EVENT_COLORS, EVENT_LABELS, esc, fillRecordingSelect, getJSON, pageLink,
  seekVideo, setUrlState, trackColors, trackName, urlState,
} from "./recording-common.js";

const $ = (id) => document.getElementById(id);
const select = $("uploaded-recording");
const player = $("player");
const panel = $("pbu-panel");
const tlWrap = $("pbu-tl-wrap");
const tl = $("pbu-tl");
const tip = $("pb-tl-tip");
const thr = $("pb-thr");
const aiBtn = $("pb-ai");

const state = { rec: null, tracks: [], events: [], overlay: null, lanes: [], highlight: null };

/* Laptop mode: playback.js (live archive) is not loaded, so the shared
 * controls are wired here. In server mode playback.js owns them. */
import("./mode.js").then(async ({ getMode }) => {
  if (await getMode() !== "local") return;
  const thrVal = $("pb-thr-val");
  const setThr = (v) => { thr.value = v; thrVal.textContent = Number(v).toFixed(2); if (state.overlay) state.overlay.minConf = +v; };
  setThr(0.3);
  thr.addEventListener("input", () => setThr(thr.value));
  aiBtn.addEventListener("click", () => {
    const on = !aiBtn.classList.contains("pb-toggle--on");
    aiBtn.classList.toggle("pb-toggle--on", on);
    aiBtn.setAttribute("aria-pressed", String(on));
    if (state.overlay) state.overlay.enabled = on;
  });
  const play = $("pb-play");
  play.addEventListener("click", () => { if (player.paused) player.play().catch(() => {}); else player.pause(); });
  player.addEventListener("play", () => { play.textContent = "❚❚"; play.setAttribute("aria-label", "Pause"); });
  player.addEventListener("pause", () => { play.textContent = "▶"; play.setAttribute("aria-label", "Play"); });
});

function durationMs() {
  return (player.duration && Number.isFinite(player.duration) ? player.duration
    : state.rec?.analysis?.duration_s || 1) * 1000;
}

/* ---- timeline: one lane row per non-overlapping set of tracks ------------- */

function layoutLanes() {
  const lanes = [];
  for (const t of [...state.tracks].sort((a, b) => a.first_offset_ms - b.first_offset_ms)) {
    let lane = lanes.findIndex((end) => end < t.first_offset_ms - 400);
    if (lane < 0) { lane = lanes.length; lanes.push(0); }
    lanes[lane] = t.last_offset_ms;
    t._lane = lane;
  }
  state.laneCount = Math.max(1, lanes.length);
}

function drawTimeline() {
  if (!state.rec) return;
  const dpr = window.devicePixelRatio || 1;
  const W = tl.clientWidth, H = tl.clientHeight;
  if (tl.width !== W * dpr || tl.height !== H * dpr) { tl.width = W * dpr; tl.height = H * dpr; }
  const ctx = tl.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = "#f4f5f7";
  ctx.fillRect(0, 0, W, H);
  const dur = durationMs(), top = 16, laneH = Math.max(3, Math.min(9, (H - top - 6) / state.laneCount));
  const x = (ms) => (ms / dur) * W;
  // time ticks
  ctx.fillStyle = "#676e76";
  ctx.font = "10px ui-monospace, monospace";
  const step = dur > 600000 ? 60000 : dur > 120000 ? 30000 : 10000;
  for (let t = 0; t <= dur; t += step) {
    ctx.fillRect(x(t), 0, 1, 5);
    ctx.fillText(clock(t), x(t) + 2, 11);
  }
  for (const t of state.tracks) {
    const focus = state.highlight === t.track_id;
    ctx.fillStyle = focus ? "#16181b" : COLORS[t.class_name] || "#ffd23c";
    ctx.globalAlpha = state.highlight != null && !focus ? 0.35 : 0.9;
    ctx.fillRect(x(t.first_offset_ms), top + t._lane * laneH, Math.max(2, x(t.last_offset_ms) - x(t.first_offset_ms)), laneH - 1);
  }
  ctx.globalAlpha = 1;
  for (const e of state.events) {
    if (e.kind === "parked") continue;
    ctx.fillStyle = EVENT_COLORS[e.kind] || "#0b5fd9";
    const ex = x(e.start_offset_ms);
    ctx.beginPath(); ctx.moveTo(ex - 4, top - 2); ctx.lineTo(ex + 4, top - 2); ctx.lineTo(ex, top + 4); ctx.fill();
  }
  ctx.fillStyle = "#d92b2b";
  ctx.fillRect(x(player.currentTime * 1000) - 1, 0, 2, H);
}

function timelineHit(ev) {
  const r = tl.getBoundingClientRect();
  const ms = ((ev.clientX - r.left) / r.width) * durationMs();
  const top = 16, laneH = Math.max(3, Math.min(9, (r.height - top - 6) / state.laneCount));
  const lane = Math.floor((ev.clientY - r.top - top) / laneH);
  const track = state.tracks.find((t) => t._lane === lane && ms >= t.first_offset_ms - 300 && ms <= t.last_offset_ms + 300);
  return { ms, track };
}

tl.addEventListener("click", (ev) => {
  const { ms, track } = timelineHit(ev);
  focus(track ? Math.max(ms, track.first_offset_ms) : ms, track ? track.track_id : null, false);
});
tl.addEventListener("mousemove", (ev) => {
  const { ms, track } = timelineHit(ev);
  tip.hidden = false;
  tip.textContent = track ? `${trackName(track)} · ${clock(track.first_offset_ms)}–${clock(track.last_offset_ms)}` : clock(ms);
  const r = tl.getBoundingClientRect();
  tip.style.left = `${ev.clientX - r.left + 10}px`;
});
tl.addEventListener("mouseleave", () => { tip.hidden = true; });

/* ---- focus: seek + highlight a track everywhere --------------------------- */

function focus(offsetMs, trackId, play = false) {
  state.highlight = trackId ?? null;
  if (state.overlay) state.overlay.highlight = state.highlight;
  seekVideo(player, offsetMs / 1000, { play });
  document.querySelectorAll(".pbu-item.on").forEach((n) => n.classList.remove("on"));
  if (trackId != null) document.querySelectorAll(`.pbu-item[data-track="${trackId}"]`).forEach((n) => n.classList.add("on"));
  setUrlState({ t: (offsetMs / 1000).toFixed(1), track: trackId ?? null });
  $("pbu-investigate").href = pageLink("investigate.html", state.rec.id, { t: (offsetMs / 1000).toFixed(1), ...(trackId != null ? { track: trackId } : {}) });
  drawTimeline();
}

function trackItem(t, label) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "pbu-item";
  b.dataset.track = t.track_id;
  b.innerHTML = `${t.thumb_url ? `<img src="${esc(t.thumb_url)}" alt="" loading="lazy">` : ""}
    <b>${esc(label || trackName(t))}</b>
    <small>${clock(t.first_offset_ms)}–${clock(t.last_offset_ms)}${trackColors(t) ? ` · ${esc(trackColors(t))}` : ""}${
      t.stationary ? " · stayed in place" : ` · ${esc(t.direction)}`}</small>`;
  b.onclick = () => focus(t.best_offset_ms, t.track_id);
  return b;
}

function renderLists() {
  const evBox = $("pbu-events"), trBox = $("pbu-tracks");
  evBox.innerHTML = "";
  trBox.innerHTML = "";
  if (!state.events.length) evBox.innerHTML = `<div class="pbu-empty">No derived events.</div>`;
  for (const e of state.events) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "pbu-item";
    b.innerHTML = `<b>${clock(e.start_offset_ms)} · ${esc(EVENT_LABELS[e.kind] || e.kind)}</b><small>${esc(e.description)}</small>`;
    b.onclick = () => focus(e.start_offset_ms, e.track_ids.length === 1 ? e.track_ids[0] : null);
    evBox.appendChild(b);
  }
  if (!state.tracks.length) trBox.innerHTML = `<div class="pbu-empty">No tracked objects.</div>`;
  for (const t of state.tracks) trBox.appendChild(trackItem(t));
}

$("pbu-search-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const q = $("pbu-search").value.trim();
  const box = $("pbu-results");
  if (!q || !state.rec) return;
  box.innerHTML = `<p>Searching…</p>`;
  try {
    const res = await getJSON(`/api/uploads/${encodeURIComponent(state.rec.id)}/search?q=${encodeURIComponent(q)}`);
    box.innerHTML = `<p>${esc(res.answer)}</p>`;
    const list = document.createElement("div");
    list.className = "pbu-list";
    for (const r of res.results) {
      const t = state.tracks.find((x) => x.track_id === r.track_id);
      if (t) list.appendChild(trackItem(t));
    }
    box.appendChild(list);
    if (res.results[0]) focus(res.results[0].offset_ms, res.results[0].track_id ?? null);
  } catch (error) {
    box.innerHTML = `<p>${esc(error.message)}</p>`;
  }
});

/* ---- entering / leaving --------------------------------------------------- */

async function enter(id) {
  let rec;
  try { rec = await getJSON(`/api/uploads/${encodeURIComponent(id)}`); } catch (error) {
    $("status-text").textContent = error.message;
    return;
  }
  state.rec = rec;
  document.body.classList.add("pb-upload");
  panel.hidden = false;
  tlWrap.hidden = false;
  setUrlState({ recording: rec.id });
  $("pbu-title").textContent = `${rec.camera_name} · ${rec.original_name}`;
  $("pbu-meta").textContent = rec.processing_status === "completed"
    ? `${rec.track_count} tracks · ${rec.detection_count} detections · ${rec.analysis?.detector || ""}`
    : `Not analysed yet (${rec.processing_status.replace("_", " ")}). Start it on the Recordings page.`;
  $("pbu-investigate").href = pageLink("investigate.html", rec.id);
  $("pbu-tracking").href = pageLink("people.html", rec.id);
  $("empty").hidden = true;
  $("empty").style.display = "none";
  $("seg-count").textContent = "uploaded file";
  $("status-text").textContent = `uploaded recording · ${rec.camera_name}`;
  $("status-foot").textContent = `${rec.original_name} · recorded ${new Date(rec.recording_at).toLocaleString()}`;
  if (player.dataset.recording !== rec.id) {
    player.dataset.recording = rec.id;
    player.src = rec.file_url;
    player.load();
  }
  if (!state.overlay) {
    state.overlay = attachOverlay($("pb-anno"), player, {
      enabled: aiBtn.classList.contains("pb-toggle--on"), minConf: +thr.value || 0 });
    aiBtn.addEventListener("click", () => queueMicrotask(() => { state.overlay.enabled = aiBtn.classList.contains("pb-toggle--on"); }));
    thr.addEventListener("input", () => { state.overlay.minConf = +thr.value; });
  }
  state.tracks = [];
  state.events = [];
  state.overlay.index = null;
  if (rec.processing_status === "completed") {
    const [index, tracks, ev] = await Promise.all([
      DetectionIndex.load(rec.id),
      getJSON(`/api/uploads/${encodeURIComponent(rec.id)}/tracks`),
      getJSON(`/api/uploads/${encodeURIComponent(rec.id)}/events`),
    ]);
    state.overlay.index = index;
    state.tracks = tracks;
    state.events = ev.events;
  }
  layoutLanes();
  renderLists();
  const { t, track } = urlState();
  if (t !== null) focus(t * 1000, track);
  else drawTimeline();
}

function leave() {
  setUrlState({ recording: null, t: null, track: null });
  location.reload();
}

select.addEventListener("change", () => {
  if (select.value) { setUrlState({ t: null, track: null }); enter(select.value); } else leave();
});
player.addEventListener("timeupdate", () => {
  if (!state.rec) return;
  $("playing").textContent = `${clock(player.currentTime * 1000)} / ${clock(durationMs())}`;
  drawTimeline();
});
player.addEventListener("loadedmetadata", drawTimeline);
window.addEventListener("resize", drawTimeline);

(async function boot() {
  const wanted = urlState().recording;
  let recordings = [];
  try {
    recordings = await fillRecordingSelect(select, { selected: wanted });
  } catch {
    select.appendChild(new Option("Uploaded recordings unavailable", ""));
    return;
  }
  let liveCams = false;
  try { liveCams = ((await getJSON("config.json")).nvrs || []).length > 0; } catch { /* no live archive */ }
  let pick = wanted && recordings.some((r) => r.id === wanted) ? wanted : "";
  if (!pick && !liveCams) {
    // laptop mode: no live archive, so open the newest analysed upload
    pick = (recordings.find((r) => r.processing_status === "completed") || recordings[0])?.id || "";
    select.options[0].textContent = "Live camera archive (needs cameras)";
    select.options[0].disabled = true;
  }
  if (pick) { select.value = pick; enter(pick); }
  else if (!liveCams) {
    select.disabled = true;
    $("empty").innerHTML = `No recordings yet. <a href="sources.html#upload">Upload one</a> to play it back here.`;
  }
})();
