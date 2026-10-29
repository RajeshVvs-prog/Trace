/* Sign-in page: first run creates the admin account, afterwards it signs in.
 * Talks to bridge/local_auth.py (/auth/status, /auth/setup, /auth/login). */

const $ = (id) => document.getElementById(id);
const form = $("login-form");
const error = $("login-error");
const submit = $("login-submit");
let setupMode = false;

function nextUrl() {
  const n = new URLSearchParams(location.search).get("next") || "/";
  return n.startsWith("/") && !n.startsWith("//") && !n.includes("\\") ? n : "/";
}

function showError(text, field) {
  error.textContent = text;
  for (const el of form.querySelectorAll(".input")) el.removeAttribute("aria-invalid");
  if (field) { field.setAttribute("aria-invalid", "true"); field.focus(); }
}

$("pw-toggle").addEventListener("click", (e) => {
  const show = $("password").type === "password";
  $("password").type = show ? "text" : "password";
  if (setupMode) $("confirm").type = show ? "text" : "password";
  e.currentTarget.textContent = show ? "Hide" : "Show";
  e.currentTarget.setAttribute("aria-pressed", String(show));
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = $("username").value.trim();
  const password = $("password").value;
  if (!username) return showError("Enter your username.", $("username"));
  if (!password) return showError("Enter your password.", $("password"));
  if (setupMode) {
    if (!/^[A-Za-z0-9._@-]{3,64}$/.test(username)) {
      return showError("Usernames are 3–64 characters: letters, numbers, dot, dash, underscore or @.", $("username"));
    }
    if (password.length < 10) return showError("Use a password of at least 10 characters.", $("password"));
    if (password !== $("confirm").value) return showError("The two passwords do not match.", $("confirm"));
  }
  showError("");
  submit.disabled = true;
  const label = submit.textContent;
  submit.textContent = setupMode ? "Creating account…" : "Signing in…";
  try {
    const res = await fetch(setupMode ? "/auth/setup" : "/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(setupMode
        ? { username, password, name: $("name").value.trim() }
        : { username, password, remember: $("remember").checked }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || `The server answered ${res.status}.`);
    location.replace(nextUrl());
  } catch (err) {
    const msg = err instanceof TypeError ? "The TRACE server could not be reached. Check that it is running." : err.message;
    showError(msg, setupMode ? null : $("password"));
    if (!setupMode) $("password").select();
    submit.disabled = false;
    submit.textContent = label;
  }
});

(async function boot() {
  let status;
  try {
    status = await (await fetch("/auth/status", { cache: "no-store" })).json();
  } catch {
    $("login-loading").innerHTML = `<p class="notice notice--err">The TRACE server could not be reached. Start it with
      <code>run_local.ps1</code>, then reload this page.</p>`;
    return;
  }
  $("login-loading").hidden = true;
  if (!status.enabled) { $("login-off").hidden = false; $("login-foot").hidden = true; return; }
  setupMode = status.needs_setup;
  if (setupMode) {
    document.title = "Set up TRACE";
    $("login-title").textContent = "Create the admin account";
    $("login-lede").textContent = "No accounts exist on this machine yet. This first account can add other people later.";
    $("name-field").hidden = false;
    $("confirm-field").hidden = false;
    $("pw-hint").hidden = false;
    $("remember-row").hidden = true;
    $("password").autocomplete = "new-password";
    submit.textContent = "Create account and sign in";
  }
  form.hidden = false;
  (setupMode ? $("name") : $("username")).focus();
})();
