"""Portable SQLite event store for laptop deployments.

The schema keeps the event columns emitted by the detector. ClickHouse remains
the default for high-volume server deployments; this store is intentionally
bounded by retention rather than pretending SQLite has ClickHouse semantics.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


DB_PATH = Path(os.environ.get("CCVT_SQLITE_PATH", "output/ccvt.db"))
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init() -> None:
    with _connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS detections (
                id INTEGER PRIMARY KEY,
                ts INTEGER NOT NULL,
                camera TEXT NOT NULL,
                class TEXT NOT NULL,
                conf REAL NOT NULL,
                track INTEGER NOT NULL,
                global_id INTEGER NOT NULL,
                x REAL NOT NULL, y REAL NOT NULL, w REAL NOT NULL, h REAL NOT NULL,
                fw INTEGER NOT NULL, fh INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS detections_camera_ts
                ON detections(camera, ts);
            CREATE INDEX IF NOT EXISTS detections_ts
                ON detections(ts);
            CREATE TABLE IF NOT EXISTS poses (
                id INTEGER PRIMARY KEY,
                ts INTEGER NOT NULL,
                camera TEXT NOT NULL,
                track INTEGER NOT NULL,
                kp_x TEXT NOT NULL, kp_y TEXT NOT NULL, kp_conf TEXT NOT NULL,
                pose_conf REAL NOT NULL, fw INTEGER NOT NULL, fh INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS poses_camera_ts ON poses(camera, ts);
            CREATE TABLE IF NOT EXISTS source_recordings (
                id TEXT PRIMARY KEY,
                camera_name TEXT NOT NULL,
                recording_at TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL UNIQUE,
                size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL,
                processing_status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS recording_detections (
                id INTEGER PRIMARY KEY,
                recording_id TEXT NOT NULL REFERENCES source_recordings(id) ON DELETE CASCADE,
                timestamp_ms INTEGER NOT NULL,
                frame_number INTEGER NOT NULL,
                class_name TEXT NOT NULL,
                confidence REAL NOT NULL,
                track_id INTEGER,
                x REAL NOT NULL,
                y REAL NOT NULL,
                w REAL NOT NULL,
                h REAL NOT NULL,
                frame_width INTEGER NOT NULL,
                frame_height INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS recording_detections_recording_ts
                ON recording_detections(recording_id, timestamp_ms);
            CREATE INDEX IF NOT EXISTS source_recordings_camera_at
                ON source_recordings(camera_name, recording_at);
            CREATE INDEX IF NOT EXISTS source_recordings_status
                ON source_recordings(status, processing_status);
            CREATE TABLE IF NOT EXISTS recording_chat_messages (
                id INTEGER PRIMARY KEY,
                recording_id TEXT NOT NULL REFERENCES source_recordings(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS recording_chat_messages_recording
                ON recording_chat_messages(recording_id, id);
            """
        )
        columns = {row["name"] for row in con.execute("PRAGMA table_info(source_recordings)")}
        for name, definition in (
            ("processing_progress", "INTEGER NOT NULL DEFAULT 0"),
            ("detection_count", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                con.execute(f"ALTER TABLE source_recordings ADD COLUMN {name} {definition}")


def insert(detections: list[dict], poses: list[dict]) -> int:
    if not detections and not poses:
        return 0
    with _lock, _connect() as con:
        con.executemany(
            """INSERT INTO detections
               (ts,camera,class,conf,track,global_id,x,y,w,h,fw,fh)
               VALUES (:ts,:camera,:class,:conf,:track,:global_id,
                       :x,:y,:w,:h,:fw,:fh)""",
            detections,
        )
        con.executemany(
            """INSERT INTO poses
               (ts,camera,track,kp_x,kp_y,kp_conf,pose_conf,fw,fh)
               VALUES (:ts,:camera,:track,:kp_x,:kp_y,:kp_conf,
                       :pose_conf,:fw,:fh)""",
            [
                dict(row, kp_x=json.dumps(row["kp_x"]),
                     kp_y=json.dumps(row["kp_y"]),
                     kp_conf=json.dumps(row["kp_conf"]))
                for row in poses
            ],
        )
    return len(detections) + len(poses)


def detections_window(camera: str, start_ms: int, end_ms: int) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            """SELECT ts, class, conf, track, x, y, w, h, fw, fh
               FROM detections
               WHERE camera = ? AND ts BETWEEN ? AND ?
               ORDER BY ts LIMIT 20000""",
            (camera, start_ms, end_ms),
        ).fetchall()
    return [dict(row) for row in rows]


def poses_window(camera: str, start_ms: int, end_ms: int) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            """SELECT ts, track, kp_x, kp_y, kp_conf, pose_conf, fw, fh
               FROM poses
               WHERE camera = ? AND ts BETWEEN ? AND ?
               ORDER BY ts LIMIT 20000""",
            (camera, start_ms, end_ms),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        for key in ("kp_x", "kp_y", "kp_conf"):
            item[key] = json.loads(item[key])
        out.append(item)
    return out


def detections_summary(camera: str, start_ms: int, end_ms: int) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            """SELECT (ts / 60000) * 60 AS m, class,
                      COUNT(DISTINCT CASE WHEN track >= 0
                                          THEN track ELSE id END) AS n
               FROM detections
               WHERE camera = ? AND ts >= ? AND ts < ?
               GROUP BY m, class HAVING n > 0 ORDER BY m""",
            (camera, start_ms, end_ms),
        ).fetchall()
    return [dict(row) for row in rows]


def history(limit: int, camera: str = "") -> list[dict]:
    where = "WHERE camera = ?" if camera else ""
    params = (camera, limit) if camera else (limit,)
    with _connect() as con:
        rows = con.execute(
            f"""SELECT ts, camera, class, conf, track, global_id, x, y, w, h, fw, fh
                FROM detections {where} ORDER BY ts DESC LIMIT ?""",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def stats(hours: int) -> dict:
    since = int((time.time() - hours * 3600) * 1000)
    with _connect() as con:
        totals = con.execute(
            """SELECT class, COUNT(DISTINCT camera || ':' || track) AS objects
               FROM detections WHERE ts >= ? AND track >= 0 GROUP BY class""",
            (since,),
        ).fetchall()
        cameras = con.execute(
            """SELECT camera, COUNT(DISTINCT camera || ':' || track) AS objects
               FROM detections WHERE ts >= ? AND track >= 0 GROUP BY camera
               ORDER BY objects DESC""",
            (since,),
        ).fetchall()
    return {
        "totals": {row["class"]: int(row["objects"]) for row in totals},
        "cameras": [{"camera": row["camera"], "objects": int(row["objects"])}
                    for row in cameras],
    }


def create_recording(recording: dict) -> dict:
    with _lock, _connect() as con:
        con.execute(
            """INSERT INTO source_recordings
               (id, camera_name, recording_at, description, original_name,
                stored_name, size_bytes, status, processing_status, error,
                created_at, updated_at)
               VALUES (:id, :camera_name, :recording_at, :description,
                       :original_name, :stored_name, :size_bytes, :status,
                       :processing_status, :error, :created_at, :updated_at)""",
            recording,
        )
    return dict(recording)


def list_recordings() -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            """SELECT id, camera_name, recording_at, description, original_name,
                      size_bytes, status, processing_status, processing_progress,
                      detection_count, error,
                      created_at, updated_at
               FROM source_recordings ORDER BY recording_at DESC, created_at DESC"""
        ).fetchall()
    return [dict(row) for row in rows]


def get_recording(recording_id: str) -> dict | None:
    with _connect() as con:
        row = con.execute(
            """SELECT id, camera_name, recording_at, description, original_name,
                      stored_name, size_bytes, status, processing_status, error,
                      processing_progress, detection_count, created_at, updated_at
               FROM source_recordings WHERE id = ?""",
            (recording_id,),
        ).fetchone()
    return dict(row) if row else None


def update_recording(recording_id: str, **changes) -> dict | None:
    allowed = {"status", "processing_status", "processing_progress", "detection_count", "error", "updated_at"}
    values = {key: value for key, value in changes.items() if key in allowed}
    if not values:
        return get_recording(recording_id)
    assignments = ", ".join(f"{key} = ?" for key in values)
    with _lock, _connect() as con:
        con.execute(
            f"UPDATE source_recordings SET {assignments} WHERE id = ?",
            (*values.values(), recording_id),
        )
    return get_recording(recording_id)


def delete_recording_detections(recording_id: str) -> None:
    with _lock, _connect() as con:
        con.execute("DELETE FROM recording_detections WHERE recording_id = ?", (recording_id,))


def insert_recording_detections(rows: list[dict]) -> int:
    if not rows:
        return 0
    with _lock, _connect() as con:
        con.executemany(
            """INSERT INTO recording_detections
               (recording_id, timestamp_ms, frame_number, class_name, confidence,
                track_id, x, y, w, h, frame_width, frame_height)
               VALUES (:recording_id, :timestamp_ms, :frame_number, :class_name,
                       :confidence, :track_id, :x, :y, :w, :h, :frame_width,
                       :frame_height)""",
            rows,
        )
    return len(rows)


def list_recording_detections(recording_id: str) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            """SELECT timestamp_ms, frame_number, class_name, confidence, track_id,
                      x, y, w, h, frame_width, frame_height
               FROM recording_detections
               WHERE recording_id = ? ORDER BY timestamp_ms, id""",
            (recording_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def add_recording_chat_message(recording_id: str, role: str, content: str,
                               evidence: list[dict], created_at: str) -> dict:
    with _lock, _connect() as con:
        cursor = con.execute(
            """INSERT INTO recording_chat_messages
               (recording_id, role, content, evidence_json, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (recording_id, role, content, json.dumps(evidence), created_at),
        )
        row = con.execute(
            """SELECT id, recording_id, role, content, evidence_json, created_at
               FROM recording_chat_messages WHERE id = ?""",
            (cursor.lastrowid,),
        ).fetchone()
    item = dict(row)
    item["evidence"] = json.loads(item.pop("evidence_json"))
    return item


def list_recording_chat_messages(recording_id: str) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            """SELECT id, recording_id, role, content, evidence_json, created_at
               FROM recording_chat_messages WHERE recording_id = ? ORDER BY id""",
            (recording_id,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["evidence"] = json.loads(item.pop("evidence_json"))
        result.append(item)
    return result
