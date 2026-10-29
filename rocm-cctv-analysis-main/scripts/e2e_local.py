#!/usr/bin/env python3
"""End-to-end check of the laptop (uploaded-recording) workflow against a running bridge.

    python scripts/e2e_local.py path/to/clip.mp4 [--base http://localhost:8080] [--keep]
                                [--user NAME --password PW]

With sign-in on (TRACE_AUTH=local, the run_local.ps1 default) pass an account,
or set TRACE_USER / TRACE_PASSWORD. On a fresh install with no accounts the
script creates that account as the first administrator.

Uploads the clip, runs analysis, then checks every API the Sources, Playback,
Investigator and Tracking pages rely on, and deletes the recording at the end
(unless --keep). Exits non-zero on the first failed check. Standard library only.
"""

import argparse
import http.cookiejar
import json
import os
import mimetypes
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


JAR = http.cookiejar.CookieJar()
SIGNED_IN = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


ANONYMOUS = urllib.request.build_opener(_NoRedirect)


def request(base, method, path, body=None, headers=None, raw=False, opener=None):
    req = urllib.request.Request(base + path, data=body, method=method, headers=headers or {})
    try:
        with (opener or SIGNED_IN).open(req, timeout=120) as res:
            data = res.read()
            return res.status, (data if raw else json.loads(data or b"null")), res.headers
    except urllib.error.HTTPError as err:
        data = err.read()
        try:
            return err.code, json.loads(data), err.headers
        except ValueError:
            return err.code, data, err.headers


def upload(base, clip: Path):
    boundary = uuid.uuid4().hex
    fields = {"camera_name": "E2E test camera", "recording_at": "2026-10-28T09:00:00+05:30",
              "description": "end-to-end test"}
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    ctype = mimetypes.guess_type(clip.name)[0] or "video/mp4"
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{clip.name}"\r\n'
                  f"Content-Type: {ctype}\r\n\r\n").encode() + clip.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return request(base, "POST", "/api/uploads", b"".join(parts),
                   {"Content-Type": f"multipart/form-data; boundary={boundary}"})


class Checker:
    def __init__(self):
        self.passed = 0

    def check(self, name, ok, detail=""):
        if not ok:
            print(f"FAIL  {name}  {detail}")
            sys.exit(1)
        self.passed += 1
        print(f"ok    {name}" + (f"  ({detail})" if detail else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clip", type=Path)
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--keep", action="store_true", help="do not delete the test recording")
    ap.add_argument("--user", default=os.environ.get("TRACE_USER", ""))
    ap.add_argument("--password", default=os.environ.get("TRACE_PASSWORD", ""))
    args = ap.parse_args()
    base, c = args.base.rstrip("/"), Checker()

    status, auth, _ = request(base, "GET", "/auth/status")
    if status == 200 and auth.get("enabled"):
        status, _, _ = request(base, "GET", "/api/uploads", opener=ANONYMOUS)
        c.check("API refuses requests without sign-in", status == 401, f"HTTP {status}")
        status, _, headers = request(base, "GET", "/sources.html", raw=True, opener=ANONYMOUS)
        c.check("pages redirect to sign-in", status in (302, 303) and "/login" in (headers.get("Location") or ""),
                f"HTTP {status}")
        status, _, _ = request(base, "POST", "/auth/login",
                               json.dumps({"username": args.user or "nobody", "password": "wrong-password-x"}).encode(),
                               {"Content-Type": "application/json"}, opener=ANONYMOUS)
        c.check("wrong password refused", status == 401, f"HTTP {status}")
        if not (args.user and args.password):
            c.check("sign-in details given (--user/--password or TRACE_USER/TRACE_PASSWORD)", False)
        path = "/auth/setup" if auth.get("needs_setup") else "/auth/login"
        status, me, _ = request(base, "POST", path, json.dumps({"username": args.user, "password": args.password}).encode(),
                                {"Content-Type": "application/json"})
        c.check("signed in" + (" (created first admin)" if path == "/auth/setup" else ""), status == 200,
                f"HTTP {status} {me if status != 200 else args.user}")

    status, health, _ = request(base, "GET", "/api/health")
    c.check("bridge health", status == 200 and health.get("storage") == "sqlite", str(health.get("storage")))

    status, rec, _ = upload(base, args.clip)
    c.check("upload", status == 200 and rec.get("id"), f"HTTP {status}")
    rid = rec["id"]
    api = f"/api/uploads/{rid}"

    status, _, _ = request(base, "POST", f"{api}/investigation", json.dumps({"message": "hi"}).encode(),
                           {"Content-Type": "application/json"})
    c.check("chat refused before analysis", status == 409, f"HTTP {status}")

    status, rec, _ = request(base, "POST", f"{api}/analyze")
    c.check("analysis queued", status == 200 and rec["processing_status"] in {"queued", "processing"})
    started = time.time()
    while True:
        status, rec, _ = request(base, "GET", f"{api}/analysis")
        if rec["processing_status"] in {"completed", "failed"}:
            break
        if time.time() - started > 900:
            c.check("analysis finished in 15 min", False)
        time.sleep(2)
    c.check("analysis completed", rec["processing_status"] == "completed", rec.get("error", ""))
    meta = rec["analysis"]
    c.check("analysis metadata", meta.get("duration_s", 0) > 0 and meta.get("sampled_frames", 0) > 0,
            f"{meta.get('duration_s')} s video in {meta.get('analysis_seconds')} s, {rec['track_count']} tracks")

    status, data, headers = request(base, "GET", f"{api}/file", headers={"Range": "bytes=0-1023"}, raw=True)
    c.check("video file supports range requests", status == 206 and len(data) == 1024, f"HTTP {status}")

    status, dets, _ = request(base, "GET", f"{api}/detections")
    c.check("detections have offsets", status == 200 and dets and all(d["offset_ms"] is not None for d in dets),
            f"{len(dets)} detections")
    tracked = [d for d in dets if d["track_id"] is not None]
    c.check("detections carry track ids", len(tracked) > 0, f"{len(tracked)} tracked")
    posed = [d for d in dets if d.get("keypoints")]
    c.check("people carry body keypoints", not meta.get("pose_model") or (posed and len(posed[0]["keypoints"]) == 17),
            f"{len(posed)} with pose, {meta.get('pose', {}).get('ms_per_crop')} ms per person")

    status, tracks, _ = request(base, "GET", f"{api}/tracks")
    c.check("tracks listed", status == 200 and len(tracks) == rec["track_count"], f"{len(tracks)} tracks")
    if tracks:
        t = tracks[0]
        status, one, _ = request(base, "GET", f"{api}/tracks/{t['track_id']}")
        c.check("single track with events", status == 200 and "events" in one)
        if t.get("thumb_url"):
            status, img, _ = request(base, "GET", t["thumb_url"], raw=True)
            c.check("track thumbnail is a JPEG", status == 200 and img[:2] == b"\xff\xd8")

    status, ev, _ = request(base, "GET", f"{api}/events")
    c.check("events endpoint", status == 200 and "events" in ev and "captions" in ev, f"{len(ev['events'])} events")
    status, trk, _ = request(base, "GET", f"{api}/tracking")
    c.check("tracking page payload", status == 200 and trk["persistent_tracking_supported"] is True)
    status, res, _ = request(base, "GET", f"{api}/search?q=red%20top")
    c.check("appearance search", status == 200 and "answer" in res)

    questions = ["Summarize what happened", "How many people are there?", "When does the first person appear?",
                 "Anyone wearing a red top?", "What is visible at 0:05?", "Did anyone walk together?",
                 "Were any vehicles parked?", "What is his gender?", "Did anyone run?",
                 "When did someone raise their hands?", "Did anyone follow someone?", "Hi"]
    for q in questions:
        status, msg, _ = request(base, "POST", f"{api}/investigation", json.dumps({"message": q}).encode(),
                                 {"Content-Type": "application/json"})
        ok = status == 200 and msg.get("role") == "assistant" and msg.get("content")
        ok = ok and all("offset_ms" in e for e in msg.get("evidence", []))
        c.check(f"chat: {q}", ok, (msg.get("content") or "")[:90] if isinstance(msg, dict) else status)

    status, hist, _ = request(base, "GET", f"{api}/investigation")
    c.check("chat history persisted", status == 200 and len(hist["messages"]) == 2 * len(questions),
            f"{len(hist['messages'])} messages")
    status, _, _ = request(base, "DELETE", f"{api}/investigation")
    status, hist, _ = request(base, "GET", f"{api}/investigation")
    c.check("chat history cleared", status == 200 and not hist["messages"])

    for page in ("/", "/playback.html", "/people.html", "/sources.html", "/js/recording-common.js"):
        status, _, _ = request(base, "GET", page, raw=True)
        c.check(f"page {page}", status == 200)

    if not args.keep:
        status, _, _ = request(base, "DELETE", api)
        c.check("recording deleted", status == 200)
        status, _, _ = request(base, "GET", api)
        c.check("deleted recording is gone", status == 404)
    print(f"\nall {c.passed} checks passed" + (f"; kept recording {rid}" if args.keep else ""))


if __name__ == "__main__":
    main()
