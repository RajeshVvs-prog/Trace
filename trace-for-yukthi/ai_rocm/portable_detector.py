"""Small CPU YOLOX detector for the portable RTSP smoke-test stack."""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Iterable

import numpy as np
import onnxruntime as ort
import requests

MODEL = os.environ.get("YOLOX_ONNX", "/models/yolox_nano.onnx")
SOURCE = os.environ.get("SOURCE", "rtsp://gateway:8554/public_ch01_sub")
BRIDGE = os.environ.get("BRIDGE_URL", "http://bridge:8081/ingest")
CAMERA = os.environ.get("CAMERA_ID", "public_ch01")
FPS = max(0.2, float(os.environ.get("DETECT_FPS", "2")))
SIZE = 416
CONF = float(os.environ.get("CONF_FLOOR", "0.35"))
COCO = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag",
    "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon",
    "bowl", "banana", "apple", "sandwich", "orange", "broccoli", "carrot",
    "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]
KEEP = {"person", "bicycle", "car", "motorcycle", "truck"}
LAST_MAX_SCORE = 0.0


def grids() -> tuple[np.ndarray, np.ndarray]:
    parts = []
    strides = []
    for stride in (8, 16, 32):
        h = w = SIZE // stride
        y, x = np.meshgrid(np.arange(h), np.arange(w), indexing="ij")
        parts.append(np.stack((x, y), axis=-1).reshape(-1, 2))
        strides.append(np.full((h * w, 1), stride, dtype=np.float32))
    return np.concatenate(parts).astype(np.float32), np.concatenate(strides)


GRID, STRIDE = grids()


def nms(boxes: np.ndarray, scores: np.ndarray, threshold: float = 0.45) -> list[int]:
    order = scores.argsort()[::-1]
    kept: list[int] = []
    while order.size:
        i = int(order[0])
        kept.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(boxes[i, 0], boxes[rest, 0])
        yy1 = np.maximum(boxes[i, 1], boxes[rest, 1])
        xx2 = np.minimum(boxes[i, 2], boxes[rest, 2])
        yy2 = np.minimum(boxes[i, 3], boxes[rest, 3])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        area = (boxes[i, 2] - boxes[i, 0]) * (boxes[i, 3] - boxes[i, 1])
        other = (boxes[rest, 2] - boxes[rest, 0]) * (boxes[rest, 3] - boxes[rest, 1])
        order = rest[inter / np.maximum(area + other - inter, 1e-6) < threshold]
    return kept


def detect(session: ort.InferenceSession, frame: np.ndarray) -> list[dict]:
    global LAST_MAX_SCORE
    # This exported YOLOX graph contains the input normalization expected by
    # the original model; feeding 0..1 values suppresses objectness scores.
    tensor = frame.astype(np.float32).transpose(2, 0, 1)[None]
    raw = session.run(None, {"images": tensor})[0][0]
    xy = (raw[:, :2] + GRID) * STRIDE
    wh = np.exp(np.clip(raw[:, 2:4], -10, 10)) * STRIDE
    boxes = np.column_stack((xy - wh / 2, xy + wh / 2))
    scores = raw[:, 4:5] * raw[:, 5:]
    class_ids = scores.argmax(axis=1)
    confidence = scores[np.arange(len(scores)), class_ids]
    LAST_MAX_SCORE = float(confidence.max())
    result = []
    for cid in np.unique(class_ids):
        name = COCO[int(cid)]
        if name not in KEEP:
            continue
        selected = np.flatnonzero((class_ids == cid) & (confidence >= CONF))
        for index in nms(boxes[selected], confidence[selected]):
            box = boxes[selected[index]]
            result.append({
                "class_name": name,
                "confidence": round(float(confidence[selected[index]]), 4),
                "bounding_box": {
                    "x": round(float(max(0, min(SIZE, box[0]))), 1),
                    "y": round(float(max(0, min(SIZE, box[1]))), 1),
                    "w": round(float(max(0, min(SIZE, box[2]) - box[0])), 1),
                    "h": round(float(max(0, min(SIZE, box[3]) - box[1])), 1),
                },
            })
    return result


def frames() -> Iterable[np.ndarray]:
    command = [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-rtsp_transport", "tcp",
        "-i", SOURCE, "-vf", f"fps={FPS},scale={SIZE}:{SIZE}",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE)
    assert process.stdout is not None
    size = SIZE * SIZE * 3
    try:
        while True:
            data = process.stdout.read(size)
            if len(data) != size:
                break
            yield np.frombuffer(data, dtype=np.uint8).reshape(SIZE, SIZE, 3)
    finally:
        process.kill()
        process.wait()


def main() -> None:
    session = ort.InferenceSession(MODEL, providers=["CPUExecutionProvider"])
    print(f"[portable-detector] CPU model={MODEL} source={SOURCE}", flush=True)
    for frame_number, frame in enumerate(frames(), 1):
        records = detect(session, frame)
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        for record in records:
            record.update({
                "camera_id": CAMERA,
                "timestamp": timestamp,
                "frame": {"width": SIZE, "height": SIZE, "number": frame_number},
                "track_id": -1,
            })
        if records:
            try:
                requests.post(BRIDGE, json=records, timeout=3).raise_for_status()
            except requests.RequestException as exc:
                print(f"[portable-detector] bridge post failed: {exc}", flush=True)
        if frame_number % 20 == 0:
            print(
                f"[portable-detector] frames={frame_number} "
                f"detections={len(records)} max_score={LAST_MAX_SCORE:.3f}",
                flush=True,
            )


if __name__ == "__main__":
    main()
