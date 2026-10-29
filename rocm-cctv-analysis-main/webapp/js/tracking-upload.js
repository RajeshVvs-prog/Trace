/* Tracking — uploaded-recording mode.
 *
 * Every persistent track the analysis found in one uploaded video: a card per
 * track (best snapshot, time span, colours, movement), and a detail view that
 * replays the track in a video with its path drawn over the picture. */

import {
  attachOverlay, clock, COLORS, DetectionIndex, esc, fillRecordingSelect, getJSON, pageLink,
  seekVideo, setUrlState, trackColors, trackName, urlState,
} from "./recording-common.js";

const $ = (id) => document.getElementById(id);
const select = $("uploaded-tracking-recording");
const statusEl = $("uploaded-tracking-status");
const list = $("upl-list");
const state = { rec: null, tracks: [], events: [], tab: "people", index: null, overlay: null, selected: null };

const isVehicle = (t) => t.class_name !== "person";

function cards() {
  const q = $("upl-q").value.trim().toLowerCase();
  const shown = state.tracks.filter((t) => (state.tab === "vehicles" ? isVehicle(t) : !isVehicle(t)))
    .filter((t) => !q || `${trackName(t)} #${t.track_id} ${trackColors(t)} ${t.direction} ${t.caption || ""}`
      .toLowerCase().includes(q));
  list.innerHTML = "";
  if (!shown.length) {
    list.innerHTML = `<p class="upl-empty">No ${state.tab === "vehicles" ? "vehicles" : "people"} match.</p>`;
    return;
  }
  for (const t of shown) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "upl-card" + (state.selected === t.track_id ? " on" : "");
    card.dataset.track = t.track_id;
    card.innerHTML = `
      <span class="upl-card__img">${t.thumb_url ? `<img src="${esc(t.thumb_url)}" alt="" loading="lazy">` : ""}</span>
      <span class="upl-card__body">
        <b style="--c:${COLORS[t.class_name] || "#ffd23c"}">${esc(trackName(t))}</b>
        <span>${clock(t.first_offset_ms)}–${clock(t.last_offset_ms)} · ${((t.last_offset_ms - t.first_offset_ms) / 1000).toFixed(0)} s</span>
        ${trackColors(t) ? `<span>${esc(trackColors(t))}</span>` : ""}
        <span class="upl-card__dir">${t.stationary ? "stayed in place" : esc(t.direction)}</span>
      </span>`;
    card.onclick = () => openTrack(t.track_id);
    list.appendChild(card);
  }
}

function summary() {
  const people = state.tracks.filter((t) => !isVehicle(t)).length;
  const vehicles = state.tracks.length - people;
  const busiest = state.events.find((e) => e.kind === "busiest");
  $("upl-summary").innerHTML = `
    <strong>${esc(state.rec.camera_name)}</strong> · ${esc(state.rec.original_name)} ·
    ${people} people · ${vehicles} vehicles${busiest ? ` · busiest ${clock(busiest.start_offset_ms)} (${busiest.track_ids.length} people)` : ""}
    <a href="${pageLink("playback.html", state.rec.id)}">Playback</a>
    <a href="${pageLink("investigate.html", state.rec.id)}">Investigate</a>`;
}

/* ---- detail ----------------------------------------------------------------- */

function drawPath(canvas, video, track) {
  const ctx = canvas.getContext("2d");
  const W = canvas.clientWidth, H = canvas.clientHeight;
  const vw = video.videoWidth, vh = video.videoHeight;
  if (!vw || !vh || !W || !H) return;
  const s = Math.min(W / vw, H / vh), w = vw * s, h = vh * s, ox = (W - w) / 2, oy = (H - h) / 2;
  ctx.lineWidth = 3;
  ctx.strokeStyle = "#ffd23c";
  ctx.beginPath();
  track.path.forEach(([, x, y], i) => (i ? ctx.lineTo(ox + x * w, oy + y * h) : ctx.moveTo(ox + x * w, oy + y * h)));
  ctx.stroke();
  const [, sx, sy] = track.path[0], [, ex, ey] = track.path[track.path.length - 1];
  ctx.fillStyle = "#1f9d55"; ctx.beginPath(); ctx.arc(ox + sx * w, oy + sy * h, 6, 0, 7); ctx.fill();
  ctx.fillStyle = "#d92b2b"; ctx.beginPath(); ctx.arc(ox + ex * w, oy + ey * h, 6, 0, 7); ctx.fill();
}

async function openTrack(trackId) {
  const t = state.tracks.find((x) => x.track_id === trackId);
  if (!t) return;
  state.selected = trackId;
  setUrlState({ track: trackId });
  document.querySelectorAll(".upl-card").forEach((c) => c.classList.toggle("on", +c.dataset.track === trackId));
  $("detail-idle").hidden = true;
  $("detail-view").hidden = false;
  $("m-title").textContent = `${trackName(t)} · ${clock(t.first_offset_ms)}–${clock(t.last_offset_ms)}`;
  let detail = t;
  try { detail = await getJSON(`/api/uploads/${encodeURIComponent(state.rec.id)}/tracks/${trackId}`); } catch { /* use list copy */ }
  const events = detail.events || [];
  const entry = t.entry_edge || `the ${t.entry_region}`;
  const exit = t.exit_edge || `the ${t.exit_region}`;
  $("detail-content").innerHTML = `
    <div class="upl-player">
      <video id="upl-video" muted playsinline controls preload="auto" src="${esc(state.rec.file_url)}"></video>
      <canvas id="upl-anno"></canvas>
      <canvas id="upl-path"></canvas>
    </div>
    <div class="upl-actions">
      <button type="button" id="upl-replay" class="pbtn">▶ Replay this track</button>
      <label><input type="checkbox" id="upl-showpath" checked> path</label>
    </div>
    <dl class="upl-facts">
      <dt>Visible</dt><dd>${clock(t.first_offset_ms)} to ${clock(t.last_offset_ms)} (${((t.last_offset_ms - t.first_offset_ms) / 1000).toFixed(0)} s, ${t.observations} sightings)</dd>
      <dt>Movement</dt><dd>${t.stationary ? `stayed in the ${esc(t.entry_region)}` : `from ${esc(entry)} to ${esc(exit)}, ${esc(t.direction)}`}</dd>
      ${trackColors(t) ? `<dt>Colours</dt><dd>${esc(trackColors(t))} <small>(approximate, from pixels)</small></dd>` : ""}
      <dt>Confidence</dt><dd>peak ${(t.max_confidence * 100).toFixed(0)}%</dd>
      ${t.caption ? `<dt>Description</dt><dd>“${esc(t.caption)}” <small>(local vision model)</small></dd>` : ""}
      ${events.length ? `<dt>Events</dt><dd>${events.map((e) => `<button type="button" class="upl-ev" data-off="${e.start_offset_ms}">${clock(e.start_offset_ms)} · ${esc(e.description)}</button>`).join("")}</dd>` : ""}
    </dl>
    <div class="upl-links">
      <a href="${pageLink("playback.html", state.rec.id, { t: (t.best_offset_ms / 1000).toFixed(1), track: t.track_id })}">Open in Playback</a>
      <a href="${pageLink("investigate.html", state.rec.id, { t: (t.best_offset_ms / 1000).toFixed(1), track: t.track_id })}">Investigate</a>
    </div>`;
  const video = $("upl-video"), pathCanvas = $("upl-path");
  if (state.overlay) cancelAnimationFrame(state.overlay.raf);
  state.overlay = attachOverlay($("upl-anno"), video, { index: state.index, highlight: trackId });
  const redrawPath = () => {
    const dpr = window.devicePixelRatio || 1;
    pathCanvas.width = pathCanvas.clientWidth * dpr; pathCanvas.height = pathCanvas.clientHeight * dpr;
    const ctx = pathCanvas.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, pathCanvas.clientWidth, pathCanvas.clientHeight);
    if ($("upl-showpath").checked) drawPath(pathCanvas, video, t);
  };
  video.addEventListener("loadedmetadata", redrawPath);
  $("upl-showpath").addEventListener("change", redrawPath);
  seekVideo(video, t.best_offset_ms / 1000);
  let stopAt = null;
  $("upl-replay").onclick = () => { stopAt = t.last_offset_ms / 1000; seekVideo(video, t.first_offset_ms / 1000, { play: true }); };
  video.addEventListener("timeupdate", () => { if (stopAt !== null && video.currentTime >= stopAt) { video.pause(); stopAt = null; } });
  document.querySelectorAll(".upl-ev").forEach((b) => { b.onclick = () => seekVideo(video, +b.dataset.off / 1000); });
}

/* ---- entering / leaving --------------------------------------------------------- */

async function enter(id) {
  let payload;
  try { payload = await getJSON(`/api/uploads/${encodeURIComponent(id)}/tracking`); } catch (error) {
    statusEl.textContent = error.message;
    return;
  }
  state.rec = payload.recording;
  state.tracks = payload.tracks;
  state.events = payload.events;
  document.body.classList.add("trk-upload");
  $("upl-results").hidden = false;
  setUrlState({ recording: state.rec.id });
  statusEl.textContent = state.rec.processing_status === "completed" ? payload.limitation
    : "This recording has not been analysed yet. Start it on the Recordings page.";
  summary();
  cards();
  state.index = null;
  if (state.rec.processing_status === "completed") {
    DetectionIndex.load(state.rec.id).then((index) => { state.index = index; if (state.overlay) state.overlay.index = index; });
  }
  const { track } = urlState();
  if (track != null && state.tracks.some((t) => t.track_id === track)) {
    const t = state.tracks.find((x) => x.track_id === track);
    if (isVehicle(t)) document.querySelector('.trk-tab[data-tab="vehicles"]').click();
    openTrack(track);
  }
}

for (const b of document.querySelectorAll(".trk-tab")) {
  b.setAttribute("aria-pressed", String(b.classList.contains("trk-tab--on")));
  b.addEventListener("click", () => {
    for (const o of document.querySelectorAll(".trk-tab")) {
      o.classList.toggle("trk-tab--on", o === b);
      o.setAttribute("aria-pressed", String(o === b));
    }
    if (!state.rec) return;
    state.tab = b.dataset.tab;
    cards();
  });
}
$("upl-q").addEventListener("input", () => { if (state.rec) cards(); });
select.addEventListener("change", () => {
  if (select.value) { setUrlState({ track: null }); enter(select.value); return; }
  setUrlState({ recording: null, track: null });
  location.reload();
});

(async function boot() {
  const wanted = urlState().recording;
  let recordings = [];
  try { recordings = await fillRecordingSelect(select, { selected: wanted }); } catch {
    statusEl.textContent = "Recordings could not be loaded. Check that the TRACE server is running, then reload.";
    return;
  }
  let live = false;
  const { getMode } = await import("./mode.js");
  if (await getMode() === "server") {
    try { live = (await fetch("/api/reid/persons?hours=1", { cache: "no-store" })).ok; } catch { live = false; }
  }
  let pick = wanted && recordings.some((r) => r.id === wanted) ? wanted : "";
  if (!live) {
    select.options[0].textContent = "Live tracking (needs the GPU server)";
    select.options[0].disabled = true;
    if (!pick) pick = (recordings.find((r) => r.processing_status === "completed") || recordings[0])?.id || "";
  }
  if (pick) { select.value = pick; enter(pick); }
  else if (!live) {
    document.body.classList.add("trk-upload");
    $("upl-results").hidden = false;
    select.disabled = true;
    $("upl-summary").innerHTML = `<div class="empty"><strong>Nothing tracked yet</strong>
      <p>Upload a recording and let it analyse. Every person and vehicle it follows will appear here.</p>
      <a class="btn btn--primary" href="sources.html#upload">Upload a recording</a></div>`;
  }
})();
