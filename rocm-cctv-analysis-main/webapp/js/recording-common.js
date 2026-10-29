/* Shared helpers for uploaded recordings: one recording id travels between
 * Sources, Playback, Investigator and Tracking as ?recording=<id> (plus
 * optional &t=<seconds> and &track=<id>), and every page reads the same
 * persisted analysis from /api/uploads/<id>/… */

export const COLORS = {
  person: "#ff6a00", car: "#2ec5ff", truck: "#b085ff",
  motorcycle: "#ff4fa3", bicycle: "#3ddc84", bus: "#ffd23c",
};

export function clock(ms) {
  const s = Math.max(0, Math.round((ms || 0) / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export function trackName(t) {
  const n = t.class_name === "person" ? "Person" : t.class_name[0].toUpperCase() + t.class_name.slice(1);
  return `${n} ${t.track_id}`;
}

export function trackColors(t) {
  const c = t.colors || {};
  if (t.class_name === "person") {
    return [c.upper?.length ? `${c.upper.join("/")} top` : "", c.lower?.length ? `${c.lower.join("/")} bottom` : ""]
      .filter(Boolean).join(", ");
  }
  return c.body?.length ? `${c.body.join("/")} body` : "";
}

export function urlState() {
  const p = new URLSearchParams(location.search);
  const t = p.get("t");
  return {
    recording: p.get("recording") || "",
    t: t !== null && t !== "" && Number.isFinite(+t) ? +t : null,
    track: p.get("track") ? +p.get("track") : null,
  };
}

export function setUrlState(changes) {
  const p = new URLSearchParams(location.search);
  for (const [k, v] of Object.entries(changes)) {
    if (v === null || v === undefined || v === "") p.delete(k); else p.set(k, v);
  }
  const qs = p.toString();
  history.replaceState(null, "", location.pathname + (qs ? `?${qs}` : ""));
}

export function pageLink(page, recordingId, extra = {}) {
  const p = new URLSearchParams({ recording: recordingId, ...extra });
  return `${page}?${p}`;
}

export async function getJSON(url, opts) {
  const res = await fetch(url, { cache: "no-store", ...opts });
  let body = null;
  try { body = await res.json(); } catch { /* empty body */ }
  if (!res.ok) throw new Error((body && body.detail) || `${res.status} ${res.statusText}`);
  return body;
}

export async function listRecordings() {
  return getJSON("/api/uploads");
}

export function recordingLabel(r) {
  const state = r.processing_status === "completed" ? `${r.track_count || 0} tracks`
    : r.processing_status === "processing" ? `analysing ${r.processing_progress || 0}%`
    : r.processing_status.replace("_", " ");
  return `${r.camera_name} · ${r.original_name} · ${state}`;
}

/* Fill a <select> with recordings; returns the list. Keeps the first option
 * (e.g. "Live-camera data") when keepFirst is true. */
export async function fillRecordingSelect(select, { keepFirst = true, selected = "" } = {}) {
  const recordings = await listRecordings();
  while (select.options.length > (keepFirst ? 1 : 0)) select.remove(select.options.length - 1);
  for (const r of recordings) select.appendChild(new Option(recordingLabel(r), r.id));
  if (selected && recordings.some((r) => r.id === selected)) select.value = selected;
  return recordings;
}

/* Detections grouped by sampled frame offset, for fast lookup while playing. */
export class DetectionIndex {
  constructor(rows) {
    this.byOffset = new Map();
    for (const r of rows) {
      if (r.offset_ms == null) continue;
      if (!this.byOffset.has(r.offset_ms)) this.byOffset.set(r.offset_ms, []);
      this.byOffset.get(r.offset_ms).push(r);
    }
    this.offsets = [...this.byOffset.keys()].sort((a, b) => a - b);
  }
  static async load(recordingId) {
    return new DetectionIndex(await getJSON(`/api/uploads/${encodeURIComponent(recordingId)}/detections`));
  }
  at(ms, tolerance = 320) {
    const o = this.offsets;
    if (!o.length) return [];
    let lo = 0, hi = o.length - 1;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (o[mid] <= ms) lo = mid; else hi = mid - 1; }
    let best = o[lo];
    if (lo + 1 < o.length && Math.abs(o[lo + 1] - ms) < Math.abs(best - ms)) best = o[lo + 1];
    return Math.abs(best - ms) <= tolerance ? this.byOffset.get(best) : [];
  }
}

function videoRect(canvas, video) {
  const ew = canvas.clientWidth, eh = canvas.clientHeight;
  const vw = video.videoWidth, vh = video.videoHeight;
  if (!ew || !eh || !vw || !vh) return null;
  const scale = Math.min(ew / vw, eh / vh), w = vw * scale, h = vh * scale;
  return { x: (ew - w) / 2, y: (eh - h) / 2, w, h };
}

/* Draw the boxes for the video's current time. `highlight` is a track id. */
export function drawOverlay(canvas, video, index, { highlight = null, minConf = 0, enabled = true, showPose = true } = {}) {
  const dpr = window.devicePixelRatio || 1;
  const cw = Math.round(canvas.clientWidth * dpr), ch = Math.round(canvas.clientHeight * dpr);
  if (canvas.width !== cw || canvas.height !== ch) { canvas.width = cw; canvas.height = ch; }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
  if (!enabled || !index) return;
  const rect = videoRect(canvas, video);
  if (!rect) return;
  const rows = index.at(video.currentTime * 1000);
  ctx.font = "600 11px ui-monospace, monospace";
  ctx.textBaseline = "top";
  for (const r of rows) {
    if ((r.confidence || 0) < minConf) continue;
    const fw = r.frame?.width || 1, fh = r.frame?.height || 1, b = r.bounding_box;
    const x = rect.x + (b.x / fw) * rect.w, y = rect.y + (b.y / fh) * rect.h;
    const w = (b.w / fw) * rect.w, h = (b.h / fh) * rect.h;
    const focus = highlight != null && r.track_id === highlight;
    const dim = highlight != null && !focus;
    const col = COLORS[r.class_name] || "#ffd23c";
    ctx.globalAlpha = dim ? 0.35 : 1;
    ctx.lineWidth = focus ? 3.5 : 2;
    ctx.strokeStyle = focus ? "#ffffff" : col;
    ctx.strokeRect(x, y, w, h);
    const name = r.class_name === "person" ? "P" : r.class_name[0].toUpperCase();
    const label = r.track_id != null ? `${name}${r.track_id}` : `${r.class_name} ${(r.confidence * 100) | 0}%`;
    const tw = ctx.measureText(label).width + 8, ly = y >= 16 ? y - 16 : y + 1;
    ctx.fillStyle = focus ? "#ffffff" : col;
    ctx.fillRect(x - 1, ly, tw, 15);
    ctx.fillStyle = "#111";
    ctx.fillText(label, x + 3, ly + 2);
    if (r.keypoints && showPose) drawSkeleton(ctx, r.keypoints, rect, fw, fh, focus ? "#ffffff" : "#ffe14d");
  }
  ctx.globalAlpha = 1;
}

/* COCO-17 limbs: arms, shoulders, torso, legs, head-to-shoulders */
export const SKELETON = [[5, 7], [7, 9], [6, 8], [8, 10], [5, 6], [5, 11], [6, 12], [11, 12],
  [11, 13], [13, 15], [12, 14], [14, 16], [0, 5], [0, 6]];

function drawSkeleton(ctx, kps, rect, fw, fh, colour) {
  const px = (k) => [rect.x + (k[0] / fw) * rect.w, rect.y + (k[1] / fh) * rect.h];
  ctx.save();
  ctx.strokeStyle = colour;
  ctx.lineWidth = 2;
  ctx.lineCap = "round";
  for (const [a, b] of SKELETON) {
    if (kps[a][2] < 0.3 || kps[b][2] < 0.3) continue;
    const [x1, y1] = px(kps[a]), [x2, y2] = px(kps[b]);
    ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
  }
  ctx.fillStyle = colour;
  for (const k of kps) {
    if (k[2] < 0.3) continue;
    const [x, y] = px(k);
    ctx.beginPath(); ctx.arc(x, y, 2.2, 0, Math.PI * 2); ctx.fill();
  }
  ctx.restore();
}

/* Keep an overlay canvas in sync with a video element. Returns a controller. */
export function attachOverlay(canvas, video, opts = {}) {
  const state = { index: null, highlight: null, enabled: true, minConf: 0, ...opts };
  const loop = () => {
    drawOverlay(canvas, video, state.index, state);
    state.raf = requestAnimationFrame(loop);
  };
  state.raf = requestAnimationFrame(loop);
  return state;
}

/* Seek a video to seconds once metadata is ready. */
export function seekVideo(video, seconds, { play = false } = {}) {
  const go = () => {
    video.currentTime = Math.max(0, Math.min(seconds, (video.duration || seconds + 1) - 0.05));
    if (play) video.play().catch(() => {});
  };
  if (video.readyState >= 1) go(); else video.addEventListener("loadedmetadata", go, { once: true });
}

/* Event marker colours on recording timelines. */
export const EVENT_COLORS = {
  busiest: "#d92b2b", running: "#e8590c", fall: "#d92b2b", lying: "#c2255c",
  hands_raised: "#7048e8", hand_raised: "#7048e8", waving: "#7048e8",
  bending: "#1098ad", sitting: "#1098ad", stopped: "#868e96", following: "#f08c00",
  together: "#0b5fd9", lingering: "#5c940d",
};

export const EVENT_LABELS = {
  busiest: "busiest moment", running: "running", fall: "possible fall", lying: "lying down",
  hands_raised: "hands raised", hand_raised: "hand raised", waving: "waving", bending: "bending",
  sitting: "sitting / crouching", stopped: "stopped", following: "following", together: "together",
  lingering: "lingering", parked: "parked",
};
