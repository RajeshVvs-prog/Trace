/* Site navigation, shared by every page.
 *
 * Renders into <header class="nav" id="site-nav"> when a page has one; older
 * server pages still carry the legacy .topbar, which is replaced by the same
 * nav with the page's own title kept as a slim heading strip underneath.
 *
 * Investigate / Playback / Tracking links carry the current ?recording=<id>
 * so moving between pages keeps the recording in view. */

import { getMode } from "./mode.js";

const PRIMARY = [
  ["/", "Overview", "overview"],
  ["/sources.html", "Recordings", "recordings"],
  ["/investigate.html", "Investigate", "investigate", true],
  ["/playback.html", "Playback", "playback", true],
  ["/people.html", "Tracking", "tracking", true],
];

// GPU-server pages: live NVR cameras and the building-wide agent.
const SERVER_PAGES = [
  ["/cameras.html", "Live cameras"],
  ["/dashboard.html", "Dashboard"],
  ["/graph.html", "Camera graph"],
  ["/faces.html", "Face registration"],
  ["/system.html", "System monitor"],
  ["/settings.html", "Settings"],
];

const ALIASES = { "/index.html": "/", "/investigation.html": "/investigate.html" };

function currentPath() {
  return ALIASES[location.pathname] || location.pathname;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  node.append(...children.filter(Boolean));
  return node;
}

function withRecording(href) {
  const id = new URLSearchParams(location.search).get("recording");
  return id ? `${href}?recording=${encodeURIComponent(id)}` : href;
}

function brand() {
  return el("a", { class: "nav__brand", href: "/", "aria-label": "TRACE overview" },
    el("img", { src: "/assets/logo.png", alt: "", width: "28", height: "28" }),
    el("span", { text: "TRACE" }));
}

function link(href, label, keepRecording) {
  const cur = currentPath() === href;
  const a = el("a", {
    class: "nav__link" + (cur ? " is-current" : ""),
    href: keepRecording ? withRecording(href) : href,
    "aria-current": cur ? "page" : null,
    text: label,
  });
  if (keepRecording) a.dataset.keepRecording = href;
  return a;
}

function serverMenu() {
  const wrap = el("div", { class: "nav__more" });
  const btn = el("button", { class: "nav__link nav__more-btn", type: "button",
    "aria-expanded": "false", "aria-controls": "nav-more-menu", text: "Live system" });
  const menu = el("div", { class: "nav__more-menu", id: "nav-more-menu", hidden: true });
  for (const [href, label] of SERVER_PAGES) menu.append(link(href, label));
  const account = el("div", { class: "nav__account", id: "nav-account", hidden: true });
  menu.append(account);
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = menu.hidden;
    menu.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
  });
  document.addEventListener("click", (e) => { if (!wrap.contains(e.target)) close(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  function close() { menu.hidden = true; btn.setAttribute("aria-expanded", "false"); }
  if (SERVER_PAGES.some(([h]) => h === currentPath())) btn.classList.add("is-current");
  wrap.append(btn, menu);
  loadAccount(account);
  return wrap;
}

async function loadAccount(box) {
  try {
    const res = await fetch("/auth/me", { cache: "no-store" });
    if (!res.ok) return;
    const me = await res.json();
    if (!me.enabled || !me.authenticated) return;
    box.append(
      el("span", { class: "nav__account-name", text: me.name || me.email || "Signed in" }),
      el("a", { class: "nav__link", href: "/logout", text: "Sign out" }));
    if (me.admin) box.append(el("a", { class: "nav__link", href: "/access-requests", text: "Access requests" }));
    box.hidden = false;
  } catch { /* auth service not deployed */ }
}

function build(host, mode) {
  const links = el("nav", { class: "nav__links", id: "nav-links", "aria-label": "Main" });
  for (const [href, label, , keep] of PRIMARY) links.append(link(href, label, keep));
  if (mode === "server") links.append(serverMenu());

  const toggle = el("button", { class: "nav__toggle", type: "button", "aria-expanded": "false",
    "aria-controls": "nav-links", "aria-label": "Open menu" },
  el("span", { "aria-hidden": "true" }), el("span", { "aria-hidden": "true" }));
  toggle.addEventListener("click", () => {
    const open = !host.classList.contains("is-open");
    host.classList.toggle("is-open", open);
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
  });

  const user = el("div", { class: "nav__user", id: "nav-user", hidden: true });
  const cta = currentPath() === "/sources.html" ? null
    : el("a", { class: "btn btn--primary btn--sm nav__cta", href: "/sources.html#upload", text: "Upload recording" });

  host.replaceChildren(el("div", { class: "nav__inner" }, brand(), toggle, links, cta, user));
  if (mode === "local") loadLocalAccount(user);
}

/* Laptop-mode sign-in (bridge/local_auth.py): who is signed in, and sign out. */
async function loadLocalAccount(box) {
  let me;
  try {
    const res = await fetch("/auth/me", { cache: "no-store" });
    me = await res.json();
  } catch { return; }
  if (!me.enabled) return;
  if (!me.authenticated) {
    document.documentElement.dataset.auth = "out";
    const here = location.pathname + location.search;
    box.replaceChildren(el("a", { class: "btn btn--primary btn--sm nav__signin",
      href: here === "/" ? "/login" : `/login?next=${encodeURIComponent(here)}`, text: "Sign in" }));
    box.hidden = false;
    return;
  }
  document.documentElement.dataset.auth = "in";
  const initial = (me.name || me.username || "?").trim().charAt(0).toUpperCase();
  const btn = el("button", { class: "nav__user-btn", type: "button", "aria-expanded": "false",
    "aria-controls": "nav-user-menu", "aria-label": `Account: ${me.name}` },
  el("span", { class: "nav__avatar", "aria-hidden": "true", text: initial }),
  el("span", { class: "nav__user-name", text: me.name }));
  const menu = el("div", { class: "nav__user-menu", id: "nav-user-menu", hidden: true },
    el("div", { class: "nav__user-head" },
      el("strong", { text: me.name }),
      el("span", { text: `${me.username}${me.admin ? ", administrator" : ""}` })),
    link("/account.html", me.admin ? "Account and people" : "Account"),
    el("form", { method: "post", action: "/auth/logout" },
      el("button", { class: "nav__link", type: "submit", text: "Sign out" })));
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = menu.hidden;
    menu.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
  });
  document.addEventListener("click", (e) => {
    if (!box.contains(e.target)) { menu.hidden = true; btn.setAttribute("aria-expanded", "false"); }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !menu.hidden) { menu.hidden = true; btn.setAttribute("aria-expanded", "false"); btn.focus(); }
  });
  box.replaceChildren(btn, menu);
  box.hidden = false;
}

/* A session can expire while a page is open: any API call answered with 401
 * sends the browser to the sign-in page and back here afterwards. */
function watchSession() {
  const orig = window.fetch;
  window.fetch = async (...args) => {
    const res = await orig(...args);
    if (res.status === 401) {
      const url = new URL(typeof args[0] === "string" ? args[0] : args[0].url, location.href);
      if (url.origin === location.origin && url.pathname.startsWith("/api/")) {
        location.assign(`/login?next=${encodeURIComponent(location.pathname + location.search)}`);
      }
    }
    return res;
  };
}

/* Keep the recording id on nav links when a page switches recording after load
 * (the pages update the URL with history.replaceState). */
function syncRecordingLinks() {
  for (const a of document.querySelectorAll("[data-keep-recording]")) {
    a.href = withRecording(a.dataset.keepRecording);
  }
}
if (!window.__traceNav) for (const fn of ["replaceState", "pushState"]) {
  const orig = history[fn];
  history[fn] = function (...args) { const r = orig.apply(this, args); queueMicrotask(syncRecordingLinks); return r; };
}

async function init() {
  const mode = await getMode();
  let host = document.getElementById("site-nav");
  const legacy = document.querySelector("body > .topbar");
  if (!host) {
    host = el("header", { class: "nav", id: "site-nav" });
    document.body.prepend(host);
  }
  if (legacy) {
    // legacy server page: keep its title as a heading strip under the nav
    const title = legacy.querySelector(".topbar__title")?.textContent || "";
    const sub = legacy.querySelector(".topbar__sub")?.textContent || "";
    const meta = legacy.querySelector(".topbar__meta");   // live status blocks keep their ids
    const strip = el("div", { class: "page-strip" },
      el("div", {}, el("h1", { class: "page-strip__title", text: title }),
        sub ? el("p", { class: "page-strip__sub", text: sub }) : null),
      meta && meta.children.length ? meta : null);
    legacy.replaceWith(strip);
  }
  build(host, mode);
}

// pages may import this module under more than one URL (cache-busting query);
// only the first copy builds the nav
if (!window.__traceNav) {
  window.__traceNav = true;
  watchSession();
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
}
