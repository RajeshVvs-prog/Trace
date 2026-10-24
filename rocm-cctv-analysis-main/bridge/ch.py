"""ClickHouse sink for the AI results the pipeline emits.

Why not sqlite any more: det_raw reached 11.65 M rows/day in a 4 GB file, was
pruned to a 25-hour window to stay usable, and still threw
`database is locked` under the live insert rate.

Design notes
  * Inserts go over HTTP with `async_insert=1`: ClickHouse batches them
    server-side, so a burst of 28 cameras does not become 28 tiny parts.
  * A failed insert is DROPPED, never retried into a growing queue. This sits on
    the ingest hot path of a live CCTV pipeline; losing a second of boxes is
    survivable, stalling the pipeline is not. Failures are counted and surfaced
    on /api/health.
  * Units are the pipeline's own: boxes and keypoints in pixels of (fw, fh),
    timestamps in ms. Nothing is silently rescaled on the way in.
"""
import json
import logging
import os
import queue
import threading
import time
from datetime import datetime

import requests

if os.environ.get("CCVT_STORAGE", "clickhouse").lower() == "sqlite":
    from sqlite_store import (
        detections_summary as sqlite_detections_summary,
        detections_window as sqlite_detections_window,
        history as sqlite_history,
        init as sqlite_init,
        insert as sqlite_insert,
        create_recording as sqlite_create_recording,
        get_recording as sqlite_get_recording,
        list_recordings as sqlite_list_recordings,
        update_recording as sqlite_update_recording,
        delete_recording_detections as sqlite_delete_recording_detections,
        insert_recording_detections as sqlite_insert_recording_detections,
        list_recording_detections as sqlite_list_recording_detections,
        add_recording_chat_message as sqlite_add_recording_chat_message,
        list_recording_chat_messages as sqlite_list_recording_chat_messages,
        poses_window as sqlite_poses_window,
        stats as sqlite_stats,
    )
    BACKEND = "sqlite"
    sqlite_init()
else:
    BACKEND = "clickhouse"

log = logging.getLogger("ch")

URL = os.environ.get("CLICKHOUSE_URL", "http://clickhouse:8123")
DB = os.environ.get("CLICKHOUSE_DB", "ccvt")
USER = os.environ.get("CLICKHOUSE_USER", "ccvt")
PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "ccvt")
TIMEOUT = float(os.environ.get("CLICKHOUSE_TIMEOUT", "5"))

_stats = {"detections": 0, "poses": 0, "dropped": 0, "errors": 0, "last_error": ""}
_lock = threading.Lock()


def _ms(iso):
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def _insert_now(table, rows):
    """JSONEachRow into `table`. Returns True on success. Runs on the worker."""
    if not rows:
        return True
    if BACKEND == "sqlite":
        return True
    body = "\n".join(json.dumps(r, separators=(",", ":")) for r in rows)
    params = {
        "database": DB,
        "query": f"INSERT INTO {table} FORMAT JSONEachRow",
        "async_insert": "1",
        "wait_for_async_insert": "0",
    }
    try:
        r = requests.post(URL, params=params, data=body.encode(),
                          auth=(USER, PASSWORD), timeout=TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"{r.status_code}: {r.text[:200]}")
    except Exception as e:
        with _lock:
            _stats["errors"] += 1
            _stats["last_error"] = str(e)[:200]
        log.warning("clickhouse insert into %s failed: %s", table, e)
        return False
    with _lock:
        _stats[table] = _stats.get(table, 0) + len(rows)
    return True


# ---- non-blocking sink ------------------------------------------------------
# These calls sit on hot paths (the /ingest handler's event loop; the matcher's
# per-tracklet assignment). A stalled ClickHouse must never stall the pipeline,
# so rows go onto a bounded queue and a daemon thread does the HTTP. When the
# queue is full the OLDEST rows are dropped and counted: losing a second of
# boxes is survivable, blocking the CCTV pipeline is not.
_q = queue.Queue(maxsize=20000)


def _worker():
    while True:
        table, rows = _q.get()
        _insert_now(table, rows)


threading.Thread(target=_worker, daemon=True).start()


def _enqueue(table, rows):
    if not rows:
        return
    try:
        _q.put_nowait((table, rows))
    except queue.Full:
        try:
            _q.get_nowait()                  # drop the oldest, keep the newest
            _q.put_nowait((table, rows))
        except queue.Empty:
            pass
        with _lock:
            _stats["dropped"] = _stats.get("dropped", 0) + len(rows)



def query(sql, params=None):
    """Run a SELECT, return parsed JSON rows."""
    p = {"database": DB, "default_format": "JSON"}
    p.update(params or {})
    r = requests.post(URL, params=p, data=sql.encode(),
                      auth=(USER, PASSWORD), timeout=TIMEOUT * 4)
    r.raise_for_status()
    return r.json().get("data", [])


def write_records(records):
    """Split one /ingest batch into the detections and poses tables."""
    dets, poses = [], []
    for r in records:
        try:
            ts = _ms(r["timestamp"])
        except (KeyError, ValueError):
            continue
        b = r.get("bounding_box") or {}
        f = r.get("frame") or {}
        fw, fh = int(f.get("width") or 0), int(f.get("height") or 0)
        track = r.get("track_id")
        dets.append({
            "ts": ts, "camera": r.get("camera_id", ""),
            "class": r.get("class_name", ""), "conf": r.get("confidence") or 0.0,
            "track": -1 if track is None else int(track),
            "global_id": int(r.get("global_id") or 0),
            "x": b.get("x") or 0.0, "y": b.get("y") or 0.0,
            "w": b.get("w") or 0.0, "h": b.get("h") or 0.0,
            "fw": fw, "fh": fh,
        })
        kp = r.get("keypoints")
        if kp:
            poses.append({
                "ts": ts, "camera": r.get("camera_id", ""),
                "track": -1 if track is None else int(track),
                "kp_x": [p[0] for p in kp],
                "kp_y": [p[1] for p in kp],
                "kp_conf": [p[2] for p in kp],
                "pose_conf": r.get("kp_conf") or 0.0,
                "fw": fw, "fh": fh,
            })
    if BACKEND == "sqlite":
        try:
            n = sqlite_insert(dets, poses)
            with _lock:
                _stats["detections"] += len(dets)
                _stats["poses"] += len(poses)
                _stats["written"] = _stats.get("written", 0) + n
        except Exception as exc:
            with _lock:
                _stats["errors"] += 1
                _stats["last_error"] = str(exc)[:200]
            log.exception("sqlite insert failed")
        return
    _enqueue("detections", dets)
    _enqueue("poses", poses)


def is_sqlite() -> bool:
    return BACKEND == "sqlite"


def local_detections_window(camera, start_ms, end_ms):
    return sqlite_detections_window(camera, start_ms, end_ms)


def local_poses_window(camera, start_ms, end_ms):
    return sqlite_poses_window(camera, start_ms, end_ms)


def local_detections_summary(camera, start_ms, end_ms):
    return sqlite_detections_summary(camera, start_ms, end_ms)


def local_history(limit, camera=""):
    return sqlite_history(limit, camera)


def local_stats(hours):
    return sqlite_stats(hours)


def create_recording(recording):
    if not BACKEND == "sqlite":
        raise RuntimeError("source recording metadata requires SQLite storage")
    return sqlite_create_recording(recording)


def list_recordings():
    if not BACKEND == "sqlite":
        raise RuntimeError("source recording metadata requires SQLite storage")
    return sqlite_list_recordings()


def get_recording(recording_id):
    if not BACKEND == "sqlite":
        raise RuntimeError("source recording metadata requires SQLite storage")
    return sqlite_get_recording(recording_id)


def update_recording(recording_id, **changes):
    if BACKEND != "sqlite":
        raise RuntimeError("video analysis requires SQLite storage")
    return sqlite_update_recording(recording_id, **changes)


def delete_recording_detections(recording_id):
    if BACKEND != "sqlite":
        raise RuntimeError("video analysis requires SQLite storage")
    return sqlite_delete_recording_detections(recording_id)


def insert_recording_detections(rows):
    if BACKEND != "sqlite":
        raise RuntimeError("video analysis requires SQLite storage")
    return sqlite_insert_recording_detections(rows)


def list_recording_detections(recording_id):
    if BACKEND != "sqlite":
        raise RuntimeError("video analysis requires SQLite storage")
    return sqlite_list_recording_detections(recording_id)


def add_recording_chat_message(recording_id, role, content, evidence, created_at):
    if BACKEND != "sqlite":
        raise RuntimeError("recording investigation requires SQLite storage")
    return sqlite_add_recording_chat_message(recording_id, role, content, evidence, created_at)


def list_recording_chat_messages(recording_id):
    if BACKEND != "sqlite":
        raise RuntimeError("recording investigation requires SQLite storage")
    return sqlite_list_recording_chat_messages(recording_id)


def stats():
    with _lock:
        return dict(_stats, queued=_q.qsize())


def wait_ready(attempts=30, delay=2.0):
    if BACKEND == "sqlite":
        return True
    for i in range(attempts):
        try:
            requests.get(f"{URL}/ping", timeout=2).raise_for_status()
            return True
        except Exception:
            time.sleep(delay)
    log.error("clickhouse never became reachable at %s", URL)
    return False
