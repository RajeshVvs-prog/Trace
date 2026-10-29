"""CPU analysis worker for uploaded recordings.

Pipeline (all local, no GPU, no API key):
  ffmpeg decode at a few fps -> YOLOX-Nano detection -> persistent tracker ->
  per-track evidence (time span, path, entry/exit, approximate colours, best
  crop, CLIP embedding) -> derived events (groups, lingering, parked vehicles,
  busiest moment).

Every fact the investigator later reports is computed here from pixels the
detector actually saw; nothing is invented to fill gaps.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

sys.path.insert(0, os.environ.get("PORTABLE_DETECTOR_DIR", "/ai_rocm"))
import portable_detector  # noqa: E402
from portable_detector import SIZE, detect  # noqa: E402

import actions  # noqa: E402
import appearance  # noqa: E402
import pose  # noqa: E402
from tracker import Tracker  # noqa: E402

MODEL = os.environ.get("VIDEO_ANALYSIS_MODEL", "/models/yolox_nano.onnx")
SAMPLE_FPS = max(0.5, float(os.environ.get("VIDEO_ANALYSIS_FPS", "5")))
MAX_DECODE_WIDTH = int(os.environ.get("VIDEO_ANALYSIS_MAX_WIDTH", "960"))
STORE_UNTRACKED_CONF = 0.35       # untracked boxes below this are not evidence
MIN_TRACK_HITS = 3                # shorter tracks are flicker, not objects
VEHICLES = {"car", "truck", "motorcycle", "bus", "bicycle"}


def _probe(path: Path) -> tuple[float, int, int, int, float]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    payload = json.loads(result.stdout)
    stream = next((item for item in payload.get("streams", []) if item.get("codec_type") == "video"), None)
    if not stream:
        raise ValueError("uploaded file contains no video stream")
    rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/1"
    numerator, denominator = (int(part) for part in rate.split("/", 1))
    fps = numerator / denominator if denominator else 0
    frame_count = int(stream.get("nb_frames") or 0)
    duration = float(stream.get("duration") or payload.get("format", {}).get("duration") or 0)
    if frame_count <= 0 and duration > 0 and fps > 0:
        frame_count = max(1, int(round(duration * fps)))
    if fps <= 0 or duration <= 0:
        raise ValueError("video has no usable frame rate or duration")
    return fps, frame_count, int(stream.get("width") or 0), int(stream.get("height") or 0), duration


def _region(cx: float, cy: float) -> str:
    """Name a normalised image position on a 3x3 grid."""
    col = "left" if cx < 1 / 3 else "right" if cx > 2 / 3 else "center"
    row = "top" if cy < 1 / 3 else "bottom" if cy > 2 / 3 else "middle"
    if row == "middle":
        return "center" if col == "center" else f"middle-{col}"
    return f"{row}-{col}" if col != "center" else f"{row}-center"


def _edge(cx: float, cy: float, margin: float = 0.08) -> str | None:
    if cx < margin:
        return "left edge"
    if cx > 1 - margin:
        return "right edge"
    if cy < margin:
        return "top edge"
    if cy > 1 - margin:
        return "bottom edge"
    return None


def _direction(dx: float, dy: float) -> str:
    if math.hypot(dx, dy) < 0.08:
        return "stayed roughly in place"
    parts = []
    if abs(dx) >= 0.06:
        parts.append("left to right" if dx > 0 else "right to left")
    if abs(dy) >= 0.06:
        parts.append("toward the camera (down the frame)" if dy > 0 else "away from the camera (up the frame)")
    return " and ".join(parts)


@dataclass
class _TrackAcc:
    track_id: int
    class_name: str
    points: list = field(default_factory=list)       # (offset_ms, cx, cy, w, h) normalised
    confs: list = field(default_factory=list)
    crops: list = field(default_factory=list)        # (score, offset_ms, crop)
    crop_every: int = 1
    poses: list = field(default_factory=list)        # (offset_ms, (17,3) keypoints px, box_w, box_h)


class _Decoder:
    def __init__(self, path: Path, width: int, height: int):
        self.w, self.h = width, height
        self.frame_bytes = width * height * 3
        self.process = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path),
             "-vf", f"fps={SAMPLE_FPS},scale={width}:{height}",
             "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def frames(self):
        assert self.process.stdout is not None
        while True:
            data = self.process.stdout.read(self.frame_bytes)
            if not data:
                return
            if len(data) != self.frame_bytes:
                raise ValueError("video ended with an incomplete decoded frame")
            yield np.frombuffer(data, dtype=np.uint8).reshape(self.h, self.w, 3)

    def finish(self):
        code = self.process.wait()
        if code:
            err = self.process.stderr.read().decode(errors="replace") if self.process.stderr else ""
            raise RuntimeError(f"video decoding failed: {err[:300] or f'ffmpeg exit {code}'}")

    def kill(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()


def analyze(path: Path, recording_id: str, recording_at: str, progress, track_dir: Path | None = None) -> dict:
    if not path.is_file():
        raise FileNotFoundError("recording file not found")
    if not Path(MODEL).is_file():
        raise FileNotFoundError(f"analysis model not found: {MODEL}")
    fps, frame_count, src_w, src_h, duration = _probe(path)
    if src_w <= 0 or src_h <= 0:
        raise ValueError("video has no usable frame size")
    scale = min(1.0, MAX_DECODE_WIDTH / src_w)
    width, height = int(src_w * scale) // 2 * 2, int(src_h * scale) // 2 * 2
    sx, sy = width / SIZE, height / SIZE

    portable_detector.CONF = Tracker.TRACK_LOW
    session = ort.InferenceSession(MODEL, providers=["CPUExecutionProvider"])
    tracker = Tracker(SAMPLE_FPS)
    pose_model = pose.PoseModel.get()
    pose_jobs: list = []
    pose_ms, pose_count = 0.0, 0
    start = datetime.fromisoformat(recording_at.replace("Z", "+00:00"))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    start_ms = int(start.timestamp() * 1000)
    expected = max(1, int(duration * SAMPLE_FPS))

    rows: list[dict] = []
    accs: dict[int, _TrackAcc] = {}
    per_frame_people: list[tuple[int, list[int]]] = []
    decoder = _Decoder(path, width, height)
    sampled = 0
    try:
        for frame in decoder.frames():
            sampled += 1
            offset_ms = int(round((sampled - 1) * 1000 / SAMPLE_FPS))
            small = cv2.resize(frame, (SIZE, SIZE), interpolation=cv2.INTER_AREA)
            found = detect(session, small)
            dets = []
            for item in found:
                b = item["bounding_box"]
                x1, y1 = b["x"] * sx, b["y"] * sy
                dets.append(((x1, y1, x1 + b["w"] * sx, y1 + b["h"] * sy), item["class_name"], item["confidence"]))
            assigned = tracker.update(dets, frame)
            people_here = []
            for index, (box, cls, conf) in enumerate(dets):
                tid = assigned.get(index)
                if tid is None and conf < STORE_UNTRACKED_CONF:
                    continue
                x1, y1, x2, y2 = box
                rows.append({
                    "recording_id": recording_id,
                    "timestamp_ms": start_ms + offset_ms,
                    "offset_ms": offset_ms,
                    "frame_number": max(1, int(round((sampled - 1) * fps / SAMPLE_FPS)) + 1),
                    "class_name": cls,
                    "confidence": conf,
                    "track_id": tid,
                    "x": round(x1, 1), "y": round(y1, 1),
                    "w": round(x2 - x1, 1), "h": round(y2 - y1, 1),
                    "frame_width": width, "frame_height": height,
                    "keypoints": None,
                })
                if tid is None:
                    continue
                acc = accs.setdefault(tid, _TrackAcc(tid, cls))
                acc.points.append((offset_ms, (x1 + x2) / 2 / width, (y1 + y2) / 2 / height,
                                   (x2 - x1) / width, (y2 - y1) / height))
                acc.confs.append(conf)
                if cls == "person":
                    people_here.append(tid)
                _keep_crop(acc, frame, box, conf, offset_ms, width, height)
                if cls == "person" and pose_model is not None and (y2 - y1) >= pose.MIN_PERSON_HEIGHT:
                    pose_jobs.append((len(rows) - 1, acc, box))
            if pose_jobs:                       # one batched pose call per frame
                started = time.perf_counter()
                for (row_index, acc, box), kps in zip(pose_jobs, pose_model.estimate(frame, [j[2] for j in pose_jobs])):
                    rows[row_index]["keypoints"] = json.dumps(
                        [[round(float(x), 1), round(float(y), 1), round(float(s), 2)] for x, y, s in kps])
                    acc.poses.append((offset_ms, kps, box[2] - box[0], box[3] - box[1]))
                pose_ms += (time.perf_counter() - started) * 1000
                pose_count += len(pose_jobs)
                pose_jobs = []
            per_frame_people.append((offset_ms, people_here))
            if sampled % 5 == 0:
                progress(min(90, int(sampled / expected * 90)))
        decoder.finish()
    finally:
        decoder.kill()

    progress(92)
    tracks = _summarise_tracks(accs, start_ms, track_dir)
    confirmed = {t["track_id"] for t in tracks}
    for row in rows:                   # flicker tracks are not identities
        if row["track_id"] is not None and row["track_id"] not in confirmed:
            row["track_id"] = None
    rows = [r for r in rows if r["track_id"] is not None or r["confidence"] >= STORE_UNTRACKED_CONF]
    progress(96)
    events = _derive_events(tracks, accs, per_frame_people, start_ms, confirmed)
    action_events = actions.movement_events(tracks, accs, width, height)
    pose_found, coverage = actions.pose_events(tracks, accs) if pose_model is not None else ([], {})
    for e in action_events + pose_found:
        e["start_ms"], e["end_ms"] = start_ms + e["start_offset_ms"], start_ms + e["end_offset_ms"]
    events = sorted(events + action_events + pose_found, key=lambda e: e["start_offset_ms"])
    return {
        "rows": rows,
        "tracks": tracks,
        "events": events,
        "meta": {
            "sampled_frames": sampled, "sample_fps": SAMPLE_FPS, "duration_s": round(duration, 2),
            "source_fps": round(fps, 3), "width": src_w, "height": src_h,
            "analysis_width": width, "analysis_height": height,
            "detector": Path(MODEL).name, "tracker": "Kalman + IoU + colour re-find",
            "appearance_model": "clip-vit-b32-quantized" if appearance.clip_available() else None,
            "pose_model": "rtmpose-t (COCO-17)" if pose_model is not None else None,
            "pose": {"crops": pose_count, "ms_per_crop": round(pose_ms / pose_count, 1) if pose_count else None,
                     "min_person_height_px": pose.MIN_PERSON_HEIGHT, **coverage},
        },
    }


def _keep_crop(acc: _TrackAcc, frame, box, conf, offset_ms, width, height):
    """Keep a bounded, evenly spread sample of crops for each track."""
    if len(acc.confs) % acc.crop_every:
        return
    x1, y1, x2, y2 = (int(round(v)) for v in box)
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(width, x2), min(height, y2)
    if x2 - x1 < 6 or y2 - y1 < 10:
        return
    touches_edge = x1 <= 2 or y1 <= 2 or x2 >= width - 2 or y2 >= height - 2
    score = conf * math.sqrt((x2 - x1) * (y2 - y1)) * (0.6 if touches_edge else 1.0)
    acc.crops.append((score, offset_ms, frame[y1:y2, x1:x2].copy()))
    if len(acc.crops) > 24:            # thin out: keep every other, sample half as often
        acc.crops = acc.crops[::2]
        acc.crop_every *= 2


def _summarise_tracks(accs: dict[int, _TrackAcc], start_ms: int, track_dir: Path | None) -> list[dict]:
    clip = appearance.Clip.get()
    if track_dir:
        track_dir.mkdir(parents=True, exist_ok=True)
        for old in track_dir.glob("*.jpg"):
            old.unlink(missing_ok=True)
    tracks = []
    for tid, acc in sorted(accs.items()):
        if len(acc.points) < MIN_TRACK_HITS:
            continue
        pts = np.array([p[1:3] for p in acc.points])
        first, last = acc.points[0], acc.points[-1]
        spread = float(np.linalg.norm(pts - pts.mean(axis=0), axis=1).max())
        path_len = float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum())
        stationary = spread < 0.04
        dx, dy = last[1] - first[1], last[2] - first[2]
        best = sorted(acc.crops, key=lambda c: c[0], reverse=True)
        colors = _track_colors(acc.class_name, [c[2] for c in best[:6]])
        embedding = None
        if clip is not None and best:
            feats = clip.embed_images([c[2] for c in best[:4]])
            if feats is not None:
                mean = feats.mean(axis=0)
                embedding = (mean / max(np.linalg.norm(mean), 1e-6)).astype(np.float32)
        thumb = None
        if track_dir and best:
            thumb = f"{tid}.jpg"
            cv2.imwrite(str(track_dir / thumb), best[0][2], [cv2.IMWRITE_JPEG_QUALITY, 88])
        sample_step = max(1, len(acc.points) // 40)
        tracks.append({
            "track_id": tid,
            "class_name": acc.class_name,
            "first_offset_ms": first[0],
            "last_offset_ms": last[0],
            "first_ms": start_ms + first[0],
            "last_ms": start_ms + last[0],
            "observations": len(acc.points),
            "max_confidence": round(max(acc.confs), 3),
            "stationary": stationary,
            "entry_region": _region(first[1], first[2]),
            "exit_region": _region(last[1], last[2]),
            "entry_edge": _edge(first[1], first[2]),
            "exit_edge": _edge(last[1], last[2]),
            "direction": "stationary" if stationary else _direction(dx, dy),
            "path_length": round(path_len, 3),
            "colors": colors,
            "path": [[p[0], round(p[1], 4), round(p[2], 4)] for p in acc.points[::sample_step]],
            "best_offset_ms": best[0][1] if best else first[0],
            "thumb": thumb,
            "embedding": embedding,
        })
    return tracks


def _track_colors(class_name: str, crops: list) -> dict:
    if not crops:
        return {}
    if class_name == "person":
        upper = np.mean([appearance.color_votes(appearance.person_regions(c)[0]) for c in crops], axis=0)
        lower = np.mean([appearance.color_votes(appearance.person_regions(c)[1]) for c in crops], axis=0)
        return {"upper": appearance.dominant_colors(upper), "lower": appearance.dominant_colors(lower)}
    body = np.mean([appearance.color_votes(c[int(c.shape[0] * 0.2): int(c.shape[0] * 0.8)]) for c in crops], axis=0)
    return {"body": appearance.dominant_colors(body)}


def _derive_events(tracks, accs, per_frame_people, start_ms, confirmed) -> list[dict]:
    events = []
    by_id = {t["track_id"]: t for t in tracks}

    def add(kind, start_off, end_off, ids, text):
        events.append({
            "kind": kind, "start_offset_ms": start_off, "end_offset_ms": end_off,
            "start_ms": start_ms + start_off, "end_ms": start_ms + end_off,
            "track_ids": ids, "description": text,
        })

    # busiest moment: most confirmed people visible in one sampled frame
    best = max(per_frame_people, key=lambda f: len(set(f[1]) & confirmed), default=None)
    if best and len(set(best[1]) & confirmed) >= 2:
        ids = sorted(set(best[1]) & confirmed)
        add("busiest", best[0], best[0], ids, f"{len(ids)} people visible at the same time")

    for t in tracks:
        span = t["last_offset_ms"] - t["first_offset_ms"]
        if t["class_name"] == "person" and span >= 15000 and t["path_length"] < 0.6:
            add("lingering", t["first_offset_ms"], t["last_offset_ms"], [t["track_id"]],
                f"person {t['track_id']} stayed in view for {span / 1000:.0f} s without moving far")
        if t["class_name"] in VEHICLES and t["stationary"] and span >= 10000:
            add("parked", t["first_offset_ms"], t["last_offset_ms"], [t["track_id"]],
                f"{t['class_name']} {t['track_id']} stationary for {span / 1000:.0f} s")

    # people moving together: one continuous run (gaps <= 1 s) of shared
    # samples where their centres stay within ~half a body height and both move
    people = [t for t in tracks if t["class_name"] == "person" and not t["stationary"]]
    gap_ms = 1000
    for i, a in enumerate(people):
        pa = {p[0]: p for p in accs[a["track_id"]].points}
        for b in people[i + 1:]:
            runs, run = [], []
            for p in accs[b["track_id"]].points:
                q = pa.get(p[0])
                close = False
                if q is not None:
                    height = max((p[4] + q[4]) / 2, 1e-3)
                    close = math.hypot(p[1] - q[1], p[2] - q[2]) / height < 0.6
                if close and (not run or p[0] - run[-1][0] <= gap_ms):
                    run.append((p[0], p[1], p[2]))
                elif close:
                    runs.append(run)
                    run = [(p[0], p[1], p[2])]
            runs.append(run)
            longest = max(runs, key=len)
            if len(longest) < 2 or longest[-1][0] - longest[0][0] < 4000:
                continue
            moved = math.hypot(longest[-1][1] - longest[0][1], longest[-1][2] - longest[0][2])
            if moved < 0.08:
                continue
            add("together", longest[0][0], longest[-1][0], [a["track_id"], b["track_id"]],
                f"persons {a['track_id']} and {b['track_id']} walked close together for "
                f"{(longest[-1][0] - longest[0][0]) / 1000:.0f} s")
    events.sort(key=lambda e: e["start_offset_ms"])
    return [e for e in events if all(tid in by_id for tid in e["track_ids"])]
