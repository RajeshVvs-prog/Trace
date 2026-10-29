/* Which deployment is serving this page.
 *
 * "local"  — laptop mode (run_local.ps1): uploaded recordings only. The bridge
 *            serves webapp/local-config.json as /config.json with mode "local".
 * "server" — the GPU server with live NVR cameras and the /api/reid agent.
 *
 * Sets <html data-mode="…"> so CSS can hide live-only controls, and lets pages
 * load the live-camera modules only where their APIs exist. */

let pending = null;

export function getMode() {
  if (!pending) {
    pending = fetch("/config.json", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : {}))
      .catch(() => ({}))
      .then((cfg) => {
        const mode = cfg && cfg.mode === "local" ? "local" : "server";
        document.documentElement.dataset.mode = mode;
        return mode;
      });
  }
  return pending;
}

getMode();
