/* Account page: change your password; administrators manage who can sign in.
 * API: bridge/local_auth.py (/auth/me, /auth/password, /auth/users…). */

import { esc } from "./recording-common.js";

const $ = (id) => document.getElementById(id);
let me = null;

async function call(url, opts = {}) {
  const res = await fetch(url, { cache: "no-store", headers: { "Content-Type": "application/json" }, ...opts });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `The server answered ${res.status}.`);
  return body;
}

function note(el, text, kind = "") {
  el.textContent = text;
  el.className = `notice${kind ? ` notice--${kind}` : ""}`;
}

$("pw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("pw-msg");
  const current = $("pw-current").value, next = $("pw-new").value;
  if (!current) return note(msg, "Enter your current password.", "err");
  if (next.length < 10) return note(msg, "Use a new password of at least 10 characters.", "err");
  if (next !== $("pw-confirm").value) return note(msg, "The new passwords do not match.", "err");
  const btn = e.submitter; btn.disabled = true;
  try {
    await call("/auth/password", { method: "POST", body: JSON.stringify({ current, new: next }) });
    e.target.reset();
    note(msg, "Password changed. Your other sessions have been signed out.", "ok");
  } catch (err) {
    note(msg, err.message, "err");
  } finally { btn.disabled = false; }
});

function userRow(u) {
  const row = document.createElement("div");
  row.className = "acct-user";
  const self = u.username === me.username;
  row.innerHTML = `<div class="acct-user__who"><strong>${esc(u.name)}${u.admin ? '<span class="acct-tag">Admin</span>' : ""}${self ? " (you)" : ""}</strong>
    <span>${esc(u.username)}</span></div>
    <div class="acct-user__actions">${self ? "" : `
      <button type="button" class="btn btn--sm btn--quiet" data-act="reset">Reset password</button>
      <button type="button" class="btn btn--sm btn--quiet btn--danger" data-act="remove">Remove</button>`}</div>`;
  row.querySelector('[data-act="reset"]')?.addEventListener("click", async () => {
    const pw = prompt(`New temporary password for ${u.username} (at least 10 characters):`);
    if (pw === null) return;
    try {
      await call(`/auth/users/${encodeURIComponent(u.username)}/password`, { method: "POST", body: JSON.stringify({ password: pw }) });
      note($("add-msg"), `Password reset for ${u.username}. Their open sessions were signed out.`, "ok");
    } catch (err) { note($("add-msg"), err.message, "err"); }
  });
  row.querySelector('[data-act="remove"]')?.addEventListener("click", async () => {
    if (!confirm(`Remove ${u.username}? They will be signed out and can no longer sign in.`)) return;
    try {
      await call(`/auth/users/${encodeURIComponent(u.username)}`, { method: "DELETE" });
      note($("add-msg"), `Removed ${u.username}.`, "ok");
      loadUsers();
    } catch (err) { note($("add-msg"), err.message, "err"); }
  });
  return row;
}

async function loadUsers() {
  const box = $("user-list");
  try {
    const users = await call("/auth/users");
    box.replaceChildren(...users.map(userRow));
  } catch (err) {
    box.innerHTML = `<p class="notice notice--err">The account list could not be loaded: ${esc(err.message)}</p>`;
  }
}

$("add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("add-msg");
  const username = $("add-username").value.trim(), password = $("add-password").value;
  if (!/^[A-Za-z0-9._@-]{3,64}$/.test(username)) {
    return note(msg, "Usernames are 3–64 characters: letters, numbers, dot, dash, underscore or @.", "err");
  }
  if (password.length < 10) return note(msg, "Use a temporary password of at least 10 characters.", "err");
  const btn = e.submitter; btn.disabled = true;
  try {
    await call("/auth/users", { method: "POST", body: JSON.stringify({
      username, password, name: $("add-name").value.trim(), admin: $("add-admin").checked }) });
    e.target.reset();
    note(msg, `Added ${username}. Give them the temporary password privately.`, "ok");
    loadUsers();
  } catch (err) {
    note(msg, err.message, "err");
  } finally { btn.disabled = false; }
});

(async function boot() {
  try {
    me = await call("/auth/me");
    if (!me.authenticated) throw new Error("not signed in");
  } catch {
    $("acct-who").textContent = "Sign-in is not enabled on this server, so there is no account to manage.";
    for (const s of document.querySelectorAll(".panel")) s.hidden = true;
    return;
  }
  $("acct-who").textContent = `Signed in as ${me.name} (${me.username})${me.admin ? ", administrator" : ""}.`;
  if (me.admin) { $("people").hidden = false; loadUsers(); }
})();
