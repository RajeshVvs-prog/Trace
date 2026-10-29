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
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS recording_tracks (
                recording_id TEXT NOT NULL,
                track_id INTEGER NOT NULL,
                class_name TEXT NOT NULL,
                first_offset_ms INTEGER NOT NULL,
                last_offset_ms INTEGER NOT NULL,
                first_ms INTEGER NOT NULL,
                last_ms INTEGER NOT NULL,
                observations INTEGER NOT NULL,
                max_confidence REAL NOT NULL,
                stationary INTEGER NOT NULL,
                entry_region TEXT NOT NULL,
                exit_region TEXT NOT NULL,
                entry_edge TEXT,
                exit_edge TEXT,
                direction TEXT NOT NULL,
                path_length REAL NOT NULL,
                colors_json TEXT NOT NULL DEFAULT '{}',
                path_json TEXT NOT NULL DEFAULT '[]',
                best_offset_ms INTEGER NOT NULL,
                thumb TEXT,
                embedding BLOB,
                caption TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (recording_id, track_id)
            );
            CREATE TABLE IF NOT EXISTS recording_events (
                id INTEGER PRIMARY KEY,
                recording_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                start_offset_ms INTEGER NOT NULL,
                end_offset_ms INTEGER NOT NULL,
                start_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL,
                track_ids_json TEXT NOT NULL,
                description TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS recording_events_recording
                ON recording_events(recording_id, start_offset_ms);
            CREATE TABLE IF NOT EXISTS recording_captions (
                id INTEGER PRIMARY KEY,
                recording_id TEXT NOT NULL,
                offset_ms INTEGER NOT NULL,
                caption TEXT NOT NULL,
                model TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS recording_captions_recording
                ON recording_captions(recording_id, offset_ms);
            """
        )
        columns = {row["name"] for row in con.execute("PRAGMA table_info(source_recordings)")}
        for name, definition in (
            ("processing_progress", "INTEGER NOT NULL DEFAULT 0"),
            ("detection_count", "INTEGER NOT NULL DEFAULT 0"),
            ("track_count", "INTEGER NOT NULL DEFAULT 0"),
            ("analysis_meta", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            if name not in columns:
                con.execute(f"ALTER TABLE source_recordings ADD COLUMN {name} {definition}")
        det_columns = {row["name"] for row in con.execute("PRAGMA table_info(recording_detections)")}
        if "offset_ms" not in det_columns:
            con.execute("ALTER TABLE recording_detections ADD COLUMN offset_ms INTEGER")
        if "keypoints" not in det_columns:
            con.execute("ALTER TABLE recording_detections ADD COLUMN keypoints TEXT")
        track_columns = {row["name"] for row in con.execute("PRAGMA table_info(recording_tracks)")}
        if "max_speed" not in track_columns:
            con.execute("ALTER TABLE recording_tracks ADD COLUMN max_speed REAL")
        con.execute("""CREATE INDEX IF NOT EXISTS recording_detections_track
                       ON recording_detections(recording_id, track_id)""")


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
                      detection_count, track_count, analysis_meta, error,
                      created_at, updated_at
               FROM source_recordings ORDER BY recording_at DESC, created_at DESC"""
        ).fetchall()
    return [dict(row) for row in rows]


def get_recording(recording_id: str) -> dict | None:
    with _connect() as con:
        row = con.execute(
            """SELECT id, camera_name, recording_at, description, original_name,
                      stored_name, size_bytes, status, processing_status, error,
                      processing_progress, detection_count, track_count, analysis_meta,
                      created_at, updated_at
               FROM source_recordings WHERE id = ?""",
            (recording_id,),
        ).fetchone()
    return dict(row) if row else None


def update_recording(recording_id: str, **changes) -> dict | None:
    allowed = {"status", "processing_status", "processing_progress", "detection_count",
               "track_count", "analysis_meta", "error", "updated_at"}
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
               (recording_id, timestamp_ms, offset_ms, frame_number, class_name,
                confidence, track_id, x, y, w, h, frame_width, frame_height)
               VALUES (:recording_id, :timestamp_ms, :offset_ms, :frame_number,
                       :class_name, :confidence, :track_id, :x, :y, :w, :h,
                       :frame_width, :frame_height)""",
            rows,
        )
    return len(rows)


def list_recording_detections(recording_id: str) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            """SELECT timestamp_ms, offset_ms, frame_number, class_name, confidence,
                      track_id, x, y, w, h, frame_width, frame_height, keypoints
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


# ---- uploaded-recording analysis: tracks, events, captions ---------------

_TRACK_COLUMNS = (
    "recording_id", "track_id", "class_name", "first_offset_ms", "last_offset_ms",
    "first_ms", "last_ms", "observations", "max_confidence", "stationary",
    "entry_region", "exit_region", "entry_edge", "exit_edge", "direction",
    "path_length", "colors_json", "path_json", "best_offset_ms", "thumb",
    "embedding", "caption", "max_speed",
)


def replace_recording_analysis(recording_id: str, rows: list[dict], tracks: list[dict],
                               events: list[dict]) -> None:
    """Swap in a complete analysis result in one transaction."""
    import numpy as np  # local: the live-ingest path never needs numpy here

    with _lock, _connect() as con:
        for table in ("recording_detections", "recording_tracks", "recording_events", "recording_captions"):
            con.execute(f"DELETE FROM {table} WHERE recording_id = ?", (recording_id,))
        con.executemany(
            """INSERT INTO recording_detections
               (recording_id, timestamp_ms, offset_ms, frame_number, class_name,
                confidence, track_id, x, y, w, h, frame_width, frame_height, keypoints)
               VALUES (:recording_id, :timestamp_ms, :offset_ms, :frame_number,
                       :class_name, :confidence, :track_id, :x, :y, :w, :h,
                       :frame_width, :frame_height, :keypoints)""",
            [{"keypoints": None, **r} for r in rows],
        )
        track_rows = []
        for t in tracks:
            emb = t.get("embedding")
            track_rows.append((
                recording_id, t["track_id"], t["class_name"], t["first_offset_ms"], t["last_offset_ms"],
                t["first_ms"], t["last_ms"], t["observations"], t["max_confidence"], int(t["stationary"]),
                t["entry_region"], t["exit_region"], t["entry_edge"], t["exit_edge"], t["direction"],
                t["path_length"], json.dumps(t["colors"]), json.dumps(t["path"]), t["best_offset_ms"],
                t["thumb"], None if emb is None else np.asarray(emb, np.float32).tobytes(), t.get("caption", ""),
                t.get("max_speed"),
            ))
        con.executemany(
            f"INSERT INTO recording_tracks ({', '.join(_TRACK_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in _TRACK_COLUMNS)})",
            track_rows,
        )
        con.executemany(
            """INSERT INTO recording_events
               (recording_id, kind, start_offset_ms, end_offset_ms, start_ms, end_ms,
                track_ids_json, description)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [(recording_id, e["kind"], e["start_offset_ms"], e["end_offset_ms"], e["start_ms"],
              e["end_ms"], json.dumps(e["track_ids"]), e["description"]) for e in events],
        )


def _track_from_row(row, with_embedding: bool) -> dict:
    item = dict(row)
    item["stationary"] = bool(item["stationary"])
    item["colors"] = json.loads(item.pop("colors_json") or "{}")
    item["path"] = json.loads(item.pop("path_json") or "[]")
    blob = item.pop("embedding", None)
    if with_embedding:
        import numpy as np
        item["embedding"] = None if blob is None else np.frombuffer(blob, np.float32)
    item["has_embedding"] = blob is not None
    return item


def list_recording_tracks(recording_id: str, with_embedding: bool = False) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            f"SELECT {', '.join(_TRACK_COLUMNS)} FROM recording_tracks "
            "WHERE recording_id = ? ORDER BY first_offset_ms, track_id",
            (recording_id,),
        ).fetchall()
    return [_track_from_row(row, with_embedding) for row in rows]


def get_recording_track(recording_id: str, track_id: int) -> dict | None:
    with _connect() as con:
        row = con.execute(
            f"SELECT {', '.join(_TRACK_COLUMNS)} FROM recording_tracks "
            "WHERE recording_id = ? AND track_id = ?",
            (recording_id, track_id),
        ).fetchone()
    return _track_from_row(row, False) if row else None


def list_recording_events(recording_id: str) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            """SELECT id, kind, start_offset_ms, end_offset_ms, start_ms, end_ms,
                      track_ids_json, description
               FROM recording_events WHERE recording_id = ? ORDER BY start_offset_ms, id""",
            (recording_id,),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["track_ids"] = json.loads(item.pop("track_ids_json"))
        out.append(item)
    return out


def set_track_caption(recording_id: str, track_id: int, caption: str) -> None:
    with _lock, _connect() as con:
        con.execute("UPDATE recording_tracks SET caption = ? WHERE recording_id = ? AND track_id = ?",
                    (caption, recording_id, track_id))


def add_recording_caption(recording_id: str, offset_ms: int, caption: str, model: str) -> None:
    with _lock, _connect() as con:
        con.execute(
            "INSERT INTO recording_captions (recording_id, offset_ms, caption, model) VALUES (?, ?, ?, ?)",
            (recording_id, offset_ms, caption, model),
        )


def list_recording_captions(recording_id: str) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT offset_ms, caption, model FROM recording_captions WHERE recording_id = ? ORDER BY offset_ms",
            (recording_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def clear_recording_chat(recording_id: str) -> None:
    with _lock, _connect() as con:
        con.execute("DELETE FROM recording_chat_messages WHERE recording_id = ?", (recording_id,))


def delete_recording(recording_id: str) -> None:
    with _lock, _connect() as con:
        for table in ("recording_detections", "recording_tracks", "recording_events",
                      "recording_captions", "recording_chat_messages"):
            con.execute(f"DELETE FROM {table} WHERE recording_id = ?", (recording_id,))
        con.execute("DELETE FROM source_recordings WHERE id = ?", (recording_id,))


def clear_recording_captions(recording_id: str) -> None:
    with _lock, _connect() as con:
        con.execute("DELETE FROM recording_captions WHERE recording_id = ?", (recording_id,))


def recover_interrupted_jobs() -> int:
    """Jobs run in-process; after a restart, anything mid-flight is dead."""
    now = datetime.now(timezone.utc).isoformat()
    with _lock, _connect() as con:
        cur = con.execute(
            """UPDATE source_recordings SET status = 'failed', processing_status = 'failed',
                      error = 'analysis was interrupted by a server restart; run it again', updated_at = ?
               WHERE processing_status IN ('queued', 'processing')""", (now,))
        rows = con.execute("SELECT id, analysis_meta FROM source_recordings").fetchall()
        fixed = cur.rowcount
        for row in rows:
            try:
                meta = json.loads(row["analysis_meta"] or "{}")
            except ValueError:
                continue
            if (meta.get("captions") or {}).get("status") == "processing":
                meta["captions"] = {"status": "interrupted", "model": meta["captions"].get("model"),
                                    "detail": "captioning was interrupted by a server restart"}
                con.execute("UPDATE source_recordings SET analysis_meta = ? WHERE id = ?",
                            (json.dumps(meta), row["id"]))
                fixed += 1
    return fixed
