"""CPU analysis worker for uploaded recordings."""

from __future__ import annotations

import json
import os
import sys
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import onnxruntime as ort

sys.path.insert(0, os.environ.get("PORTABLE_DETECTOR_DIR", "/ai_rocm"))
from portable_detector import SIZE, detect


MODEL = os.environ.get("VIDEO_ANALYSIS_MODEL", "/models/yolox_nano.onnx")
SAMPLE_FPS = max(0.1, float(os.environ.get("VIDEO_ANALYSIS_FPS", "2")))
FRAME_BYTES = SIZE * SIZE * 3


def _probe(path: Path) -> tuple[float, int, int, int]:
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
    return fps, frame_count, int(stream.get("width") or 0), int(stream.get("height") or 0)


def analyze(path: Path, recording_id: str, recording_at: str, progress) -> tuple[list[dict], int]:
    if not path.is_file():
        raise FileNotFoundError("recording file not found")
    if not Path(MODEL).is_file():
        raise FileNotFoundError(f"analysis model not found: {MODEL}")
    fps, frame_count, _, _ = _probe(path)
    session = ort.InferenceSession(MODEL, providers=["CPUExecutionProvider"])
    command = [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path),
        "-vf", f"fps={SAMPLE_FPS},scale={SIZE}:{SIZE}",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if process.stdout is None:
        raise RuntimeError("could not open decoded video stream")
    start = datetime.fromisoformat(recording_at.replace("Z", "+00:00"))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    rows: list[dict] = []
    sampled = 0
    try:
        while True:
            data = process.stdout.read(FRAME_BYTES)
            if not data:
                break
            if len(data) != FRAME_BYTES:
                raise ValueError("video ended with an incomplete decoded frame")
            sampled += 1
            frame = np.frombuffer(data, dtype=np.uint8).reshape(SIZE, SIZE, 3)
            timestamp_ms = int((start + timedelta(seconds=(sampled - 1) / SAMPLE_FPS)).timestamp() * 1000)
            for item in detect(session, frame):
                box = item["bounding_box"]
                rows.append({
                    "recording_id": recording_id,
                    "timestamp_ms": timestamp_ms,
                    "frame_number": max(1, int(round((sampled - 1) * fps / SAMPLE_FPS)) + 1),
                    "class_name": item["class_name"],
                    "confidence": item["confidence"],
                    "track_id": None,
                    "x": box["x"], "y": box["y"], "w": box["w"], "h": box["h"],
                    "frame_width": SIZE, "frame_height": SIZE,
                })
            if frame_count:
                progress(min(99, int(sampled * SAMPLE_FPS / max(fps * frame_count, 1) * 100)))
        exit_code = process.wait()
        if exit_code:
            stderr = process.stderr.read().decode(errors="replace") if process.stderr else ""
            raise RuntimeError(f"video decoding failed: {stderr[:300] or f'ffmpeg exit {exit_code}'}")
        return rows, sampled
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
