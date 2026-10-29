/* Overview page: the recordings already on this machine, read from the same
 * /api/uploads endpoint the Recordings page uses. */

import { clock, esc, getJSON, pageLink } from "./recording-common.js";

const desk = document.getElementById("desk-body");
const cont = document.getElementById("hero-continue");
let poll = null;

function statusOf(r) {
  switch (r.processing_status) {
    case "completed": return ["status--ok", `${r.track_count || 0} tracked objects`];
    case "processing": return ["status--busy", `Analysing ${r.processing_progress || 0}%`];
    case "queued": return ["status--busy", "Waiting to analyse"];
    case "failed": return ["status--err", "Analysis failed"];
    default: return ["", "Not analysed yet"];
  }
}

function when(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function row(r) {
  const [cls, label] = statusOf(r);
  const done = r.processing_status === "completed";
  const dur = r.analysis?.duration_s;
  return `<li class="desk__item">
    <div class="desk__item-main">
      <strong title="${esc(r.original_name)}">${esc(r.original_name)}</strong>
      <span class="desk__meta">${esc(r.camera_name)}${r.recording_at ? `, ${esc(when(r.recording_at))}` : ""}${
        dur ? `, <span class="timecode">${clock(dur * 1000)}</span>` : ""}</span>
      <span class="status ${cls}">${esc(label)}</span>
    </div>
    <div class="desk__item-actions">${done
      ? `<a class="btn btn--sm" href="${pageLink("investigate.html", r.id)}">Investigate</a>
         <a class="btn btn--sm btn--quiet" href="${pageLink("playback.html", r.id)}">Playback</a>`
      : `<a class="btn btn--sm btn--quiet" href="/sources.html">${r.processing_status === "failed" ? "Review" : "Open"}</a>`}
    </div>
  </li>`;
}

async function strip(r) {
  try {
    const tracks = await getJSON(`/api/uploads/${encodeURIComponent(r.id)}/tracks`);
    const shown = tracks.filter((t) => t.thumb_url).slice(0, 8);
    if (!shown.length) return;
    const box = document.createElement("div");
    box.className = "desk__tracks";
    box.innerHTML = `<p>Tracked in ${esc(r.original_name)}</p><div>${shown.map((t) =>
      `<a href="${pageLink("people.html", r.id, { track: t.track_id })}" title="${esc(t.class_name)} ${t.track_id}">
        <img src="${esc(t.thumb_url)}" alt="${esc(t.class_name)} ${t.track_id}" loading="lazy"></a>`).join("")}</div>`;
    desk.appendChild(box);
  } catch { /* thumbnails are a nicety; the list above already works */ }
}

async function signedIn() {
  try {
    const res = await fetch("/auth/me", { cache: "no-store" });
    const me = await res.json();
    return !me.enabled || me.authenticated;
  } catch { return true; }   // no auth service: behave as before
}

/* ---- sign-in dialog: signs in (or creates the first admin) on this page --- */
const dlg = document.getElementById("signin-dialog");
const $ = (id) => document.getElementById(id);
let setupMode = false;
let afterSignIn = null;

async function openSignIn(next = null) {
  afterSignIn = next;
  try {
    const st = await (await fetch("/auth/status", { cache: "no-store" })).json();
    if (!st.enabled) { location.assign(next || "/"); return; }
    setupMode = !!st.needs_setup;
  } catch { /* show the form anyway; submitting reports the problem */ }
  $("signin-title").textContent = setupMode ? "Create the admin account" : "Sign in to TRACE";
  $("signin-lede").textContent = setupMode
    ? "No accounts exist on this machine yet. This first account can add other people later."
    : "Use the account you were given for this machine.";
  $("si-name-field").hidden = !setupMode;
  $("si-confirm-field").hidden = !setupMode;
  $("si-hint").hidden = !setupMode;
  $("si-remember-row").hidden = setupMode;
  $("si-password").autocomplete = setupMode ? "new-password" : "current-password";
  $("si-submit").textContent = setupMode ? "Create account and sign in" : "Sign in";
  $("si-error").textContent = "";
  if (!dlg.open) dlg.showModal();
  (setupMode ? $("si-name") : $("si-username")).focus();
}

function siError(text, field) {
  $("si-error").textContent = text;
  for (const el of dlg.querySelectorAll(".input")) el.removeAttribute("aria-invalid");
  if (field) { field.setAttribute("aria-invalid", "true"); field.focus(); }
}

// Closes only with the ✕ button or Esc. (A click that starts in a field and ends
// just outside the box, or an autofill pick, must not throw away what was typed.)
$("signin-close").addEventListener("click", () => dlg.close());
$("signin-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = $("si-username").value.trim(), password = $("si-password").value;
  if (!username) return siError("Enter your username.", $("si-username"));
  if (!password) return siError("Enter your password.", $("si-password"));
  if (setupMode) {
    if (!/^[A-Za-z0-9._@-]{3,64}$/.test(username)) {
      return siError("Usernames are 3–64 characters: letters, numbers, dot, dash, underscore or @.", $("si-username"));
    }
    if (password.length < 10) return siError("Use a password of at least 10 characters.", $("si-password"));
    if (password !== $("si-confirm").value) return siError("The two passwords do not match.", $("si-confirm"));
  }
  const btn = $("si-submit"), label = btn.textContent;
  btn.disabled = true;
  btn.textContent = setupMode ? "Creating account…" : "Signing in…";
  try {
    const res = await fetch(setupMode ? "/auth/setup" : "/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(setupMode ? { username, password, name: $("si-name").value.trim() }
        : { username, password, remember: $("si-remember").checked }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || `The server answered ${res.status}.`);
    if (afterSignIn) { location.assign(afterSignIn); return; }
    location.reload();
  } catch (err) {
    siError(err instanceof TypeError ? "The TRACE server could not be reached. Check that it is running." : err.message,
      setupMode ? null : $("si-password"));
    btn.disabled = false;
    btn.textContent = label;
  }
});

// Any "Sign in" control on this page opens the dialog instead of leaving it.
// While signed out, links into the app open it too, then continue to that page.
// Decided after the sign-in check has answered, so an early click never
// falls through to the separate /login page.
const APP_PAGES = ["/sources.html", "/investigate.html", "/playback.html", "/people.html", "/account.html"];
const authKnown = signedIn();
document.addEventListener("click", async (e) => {
  const a = e.target.closest("a[href]");
  if (!a || !dlg || e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
  const url = new URL(a.href, location.href);
  if (url.origin !== location.origin) return;
  const isLogin = url.pathname === "/login";
  if (!isLogin && !APP_PAGES.includes(url.pathname)) return;
  e.preventDefault();
  if (!isLogin && await authKnown) { location.assign(url.href); return; }   // signed in: just go
  const next = isLogin ? url.searchParams.get("next") : url.pathname + url.search + url.hash;
  openSignIn(next && next.startsWith("/") && !next.startsWith("//") ? next : null);
});

function signedOutView() {
  document.getElementById("desk-title").textContent = "Your recordings";
  document.querySelector(".desk__head a").hidden = true;
  desk.innerHTML = `<div class="desk__empty">
    <strong>Sign in to see your recordings</strong>
    <p>Recordings, their analysis and chat history are only shown to people with an account on this machine.</p>
    <a class="btn btn--primary" href="/login">Sign in</a>
  </div>`;
  const upload = document.querySelector(".hero__actions .btn--primary");
  upload.textContent = "Sign in to start";
  upload.href = "/login?next=%2Fsources.html";
}

async function load() {
  if (!(await authKnown)) { signedOutView(); return; }
  let recordings;
  try {
    recordings = await getJSON("/api/uploads");
  } catch (error) {
    desk.innerHTML = `<div class="notice notice--err">The recordings list could not be loaded (${esc(error.message)}).
      Check that the TRACE server is running, then reload this page.</div>`;
    return;
  }
  if (!recordings.length) {
    desk.innerHTML = `<div class="desk__empty">
      <strong>No recordings yet</strong>
      <p>Upload a video on the Recordings page. Analysis starts automatically and runs on this computer.</p>
      <a class="btn" href="/sources.html#upload">Choose a video</a>
    </div>`;
    return;
  }
  desk.innerHTML = `<ul class="desk__list">${recordings.slice(0, 4).map(row).join("")}</ul>${
    recordings.length > 4 ? `<a class="desk__more" href="/sources.html">All ${recordings.length} recordings</a>` : ""}`;
  const ready = recordings.find((r) => r.processing_status === "completed");
  if (ready) {
    cont.hidden = false;
    cont.href = pageLink("investigate.html", ready.id);
    cont.textContent = `Continue with ${ready.camera_name}`;
    strip(ready);
  }
  clearTimeout(poll);
  if (recordings.some((r) => ["queued", "processing"].includes(r.processing_status))) poll = setTimeout(load, 3000);
}

load();
