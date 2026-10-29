"""Local sign-in for laptop mode.

Enabled with TRACE_AUTH=local (run_local.ps1 sets it). The GPU server keeps its
own Google sign-in (auth/auth_app.py behind Caddy) and leaves this off.

- Accounts live in <output>/users.json; passwords are PBKDF2-SHA256 hashed
  (600k iterations, per-user salt). No plain-text password is ever stored.
- The first visit creates the admin account (/login shows a setup form while
  no account exists). Admins can add, remove and reset accounts.
- A session is an HMAC-signed, HttpOnly, SameSite=Lax cookie. The signing key
  is generated once into <output>/.session_key. Changing a password or deleting
  an account invalidates that account's existing sessions.
- Every page, /api/* call and the /ws socket needs a session, except the sign-in
  page and the files it loads. State-changing requests from another origin are
  refused.

Standard library only: no new dependencies.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

ENABLED = os.environ.get("TRACE_AUTH", "").strip().lower() == "local"
COOKIE = "trace_session"
SESSION_SECONDS = 12 * 3600
REMEMBER_SECONDS = 30 * 24 * 3600
ITERATIONS = 600_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]{3,64}$")
MIN_PASSWORD = 10

# Reachable without a session: the sign-in page and what it loads.
PUBLIC_EXACT = {
    "/login", "/login.html", "/favicon.png", "/logo.png",
    "/auth/status", "/auth/login", "/auth/setup", "/auth/me", "/auth/logout", "/logout",
    "/js/login.js", "/css/app.css", "/css/trace.css", "/css/login.css",
    # the home page is public: it explains TRACE and offers "Sign in"
    "/", "/index.html", "/config.json", "/css/landing.css",
    "/js/landing.js", "/js/nav.js", "/js/mode.js", "/js/recording-common.js",
}
PUBLIC_PREFIX = ("/assets/",)

router = APIRouter()
_lock = threading.Lock()
_paths: dict[str, Path] = {}
_failures: dict[str, list[float]] = {}


def configure(output_dir: Path) -> None:
    _paths["users"] = output_dir / "users.json"
    _paths["key"] = output_dir / ".session_key"


# ---- storage ---------------------------------------------------------------

def _load() -> dict:
    path = _paths["users"]
    if not path.is_file():
        return {"users": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data.get("users"), dict) else {"users": {}}
    except (OSError, ValueError):
        return {"users": {}}


def _save(data: dict) -> None:
    path = _paths["users"]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _key() -> bytes:
    path = _paths["key"]
    with _lock:
        if not path.is_file():
            path.write_bytes(secrets.token_bytes(32))
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        return path.read_bytes()


# ---- passwords and sessions -------------------------------------------------

def _hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
        check = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(check.hex(), digest)
    except (ValueError, TypeError):
        return False


_DUMMY = _hash("timing-equaliser-not-a-password")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def session_user(request) -> dict | None:
    """The signed-in account for a request or websocket, or None."""
    raw = request.cookies.get(COOKIE, "")
    if "." not in raw:
        return None
    body, sig = raw.rsplit(".", 1)
    good = _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, good):
        return None
    try:
        payload = json.loads(_unb64(body))
    except ValueError:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    user = _load()["users"].get(payload.get("u", ""))
    if not user or user.get("gen", 0) != payload.get("g"):
        return None          # account removed or password changed since sign-in
    return {"username": payload["u"], "name": user.get("name") or payload["u"], "admin": bool(user.get("admin"))}


def _set_session(response, username: str, user: dict, remember: bool) -> None:
    seconds = REMEMBER_SECONDS if remember else SESSION_SECONDS
    token = _sign({"u": username, "g": user.get("gen", 0), "exp": int(time.time()) + seconds})
    response.set_cookie(COOKIE, token, max_age=seconds if remember else None, httponly=True,
                        samesite="lax", path="/")


def _public_user(username: str, user: dict) -> dict:
    return {"username": username, "name": user.get("name") or username, "admin": bool(user.get("admin")),
            "created_at": user.get("created_at")}


# ---- request checks ---------------------------------------------------------

def _is_public(path: str) -> bool:
    return path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIX)


def _cross_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return False
    return urlsplit(origin).netloc != request.headers.get("host", "")


async def middleware(request: Request, call_next):
    if not ENABLED:
        return await call_next(request)
    path = request.url.path
    if request.method not in ("GET", "HEAD", "OPTIONS") and _cross_origin(request):
        return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
    if _is_public(path) or session_user(request):
        return await call_next(request)
    if path.startswith(("/api/", "/ingest")) or request.method != "GET":
        return JSONResponse({"detail": "sign in required"}, status_code=401)
    target = path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)


def _throttle(request: Request) -> None:
    ip = request.client.host if request.client else "?"
    now = time.time()
    recent = [t for t in _failures.get(ip, []) if now - t < 300]
    _failures[ip] = recent
    if len(recent) >= 8:
        raise HTTPException(429, "Too many failed sign-in attempts. Wait five minutes and try again.")


def _fail(request: Request) -> None:
    ip = request.client.host if request.client else "?"
    _failures.setdefault(ip, []).append(time.time())


def _require_admin(request: Request) -> dict:
    me = session_user(request)
    if not me:
        raise HTTPException(401, "sign in required")
    if not me["admin"]:
        raise HTTPException(403, "only an administrator can manage accounts")
    return me


def _check_new(username: str, password: str) -> None:
    if not USERNAME_RE.match(username):
        raise HTTPException(400, "Usernames are 3–64 characters: letters, numbers, dot, dash, underscore or @.")
    if len(password) < MIN_PASSWORD:
        raise HTTPException(400, f"Use a password of at least {MIN_PASSWORD} characters.")
    if password.lower() == username.lower():
        raise HTTPException(400, "The password must be different from the username.")


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(400, "expected a JSON body")
    if not isinstance(data, dict):
        raise HTTPException(400, "expected a JSON object")
    return data


# ---- routes -----------------------------------------------------------------

@router.get("/login")
def login_page(request: Request):
    from fastapi.responses import FileResponse
    if not ENABLED:
        return RedirectResponse("/", status_code=303)
    if session_user(request):
        return RedirectResponse(_safe_next(request.query_params.get("next")), status_code=303)
    webapp = Path(os.environ.get("WEBAPP_DIR", Path(__file__).resolve().parent.parent / "webapp"))
    return FileResponse(webapp / "login.html", headers={"Cache-Control": "no-store"})


def _safe_next(target: str | None) -> str:
    """Only same-site paths: never redirect off this server after sign-in."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


@router.get("/auth/status")
def status():
    return {"enabled": ENABLED, "needs_setup": ENABLED and not _load()["users"]}


@router.get("/auth/me")
def me(request: Request):
    if not ENABLED:
        return {"enabled": False, "authenticated": False}
    user = session_user(request)
    if not user:
        return {"enabled": True, "authenticated": False, "mode": "local"}
    return {"enabled": True, "authenticated": True, "mode": "local", "name": user["name"],
            "email": user["username"], "username": user["username"], "admin": user["admin"]}


@router.post("/auth/setup")
async def setup(request: Request):
    if not ENABLED:
        raise HTTPException(404, "sign-in is not enabled")
    data = await _body(request)
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    name = str(data.get("name", "")).strip()[:80]
    _check_new(username, password)
    with _lock:
        store = _load()
        if store["users"]:
            raise HTTPException(409, "An account already exists. Sign in instead.")
        user = {"hash": _hash(password), "name": name, "admin": True, "gen": 1,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        store["users"][username] = user
        _save(store)
    response = JSONResponse({"ok": True, "user": _public_user(username, user)})
    _set_session(response, username, user, remember=False)
    return response


@router.post("/auth/login")
async def login(request: Request):
    if not ENABLED:
        raise HTTPException(404, "sign-in is not enabled")
    _throttle(request)
    data = await _body(request)
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    user = _load()["users"].get(username)
    if not user or not _verify(password, user["hash"]):
        if not user:
            _verify(password, _DUMMY)          # same work whether or not the name exists
        _fail(request)
        raise HTTPException(401, "That username and password do not match.")
    _failures.pop(request.client.host if request.client else "?", None)
    response = JSONResponse({"ok": True, "user": _public_user(username, user)})
    _set_session(response, username, user, remember=bool(data.get("remember")))
    return response


@router.post("/auth/logout")
@router.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE, path="/")
    return response


@router.post("/auth/password")
async def change_password(request: Request):
    me_ = session_user(request)
    if not me_:
        raise HTTPException(401, "sign in required")
    data = await _body(request)
    current, new = str(data.get("current", "")), str(data.get("new", ""))
    _check_new(me_["username"], new)
    with _lock:
        store = _load()
        user = store["users"][me_["username"]]
        if not _verify(current, user["hash"]):
            raise HTTPException(400, "The current password is not correct.")
        user["hash"] = _hash(new)
        user["gen"] = user.get("gen", 0) + 1     # signs out other sessions
        _save(store)
    response = JSONResponse({"ok": True})
    _set_session(response, me_["username"], user, remember=False)
    return response


@router.get("/auth/users")
def list_users(request: Request):
    _require_admin(request)
    return [_public_user(u, d) for u, d in sorted(_load()["users"].items())]


@router.post("/auth/users")
async def add_user(request: Request):
    _require_admin(request)
    data = await _body(request)
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    _check_new(username, password)
    with _lock:
        store = _load()
        if username in store["users"]:
            raise HTTPException(409, f"An account called {username} already exists.")
        user = {"hash": _hash(password), "name": str(data.get("name", "")).strip()[:80],
                "admin": bool(data.get("admin")), "gen": 1, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        store["users"][username] = user
        _save(store)
    return _public_user(username, user)


@router.post("/auth/users/{username}/password")
async def reset_password(username: str, request: Request):
    _require_admin(request)
    data = await _body(request)
    password = str(data.get("password", ""))
    _check_new(username, password)
    with _lock:
        store = _load()
        user = store["users"].get(username)
        if not user:
            raise HTTPException(404, "No such account.")
        user["hash"] = _hash(password)
        user["gen"] = user.get("gen", 0) + 1
        _save(store)
    return {"ok": True}


@router.delete("/auth/users/{username}")
def delete_user(username: str, request: Request):
    me_ = _require_admin(request)
    if username == me_["username"]:
        raise HTTPException(400, "You cannot delete the account you are signed in with.")
    with _lock:
        store = _load()
        if username not in store["users"]:
            raise HTTPException(404, "No such account.")
        del store["users"][username]
        _save(store)
    return {"ok": True}
