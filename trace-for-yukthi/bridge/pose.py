"""Body keypoints for tracked people (CPU, no API key).

RTMPose-tiny (OpenMMLab, Apache-2.0; SimCC head, COCO-17 keypoints, 256x192
input) exported to ONNX. Top-down: each person box is expanded by 1.25,
warped to 192x256, normalised in RGB, and the SimCC x/y distributions are
decoded back to frame pixels. Fails soft: no model file -> no poses, and the
rest of the analysis runs unchanged.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("pose")

MODELS_DIR = Path(os.environ.get("TRACE_MODELS_DIR", "/models"))
POSE_MODEL = Path(os.environ.get("POSE_MODEL", str(MODELS_DIR / "pose" / "rtmpose_t_body7_256x192.onnx")))
MIN_PERSON_HEIGHT = int(os.environ.get("POSE_MIN_HEIGHT", "60"))   # px in the analysis frame

KEYPOINTS = ("nose", "left_eye", "right_eye", "left_ear", "right_ear",
             "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
             "left_wrist", "right_wrist", "left_hip", "right_hip",
             "left_knee", "right_knee", "left_ankle", "right_ankle")
SKELETON = ((5, 7), (7, 9), (6, 8), (8, 10), (5, 6), (5, 11), (6, 12), (11, 12),
            (11, 13), (13, 15), (12, 14), (14, 16), (0, 5), (0, 6))

_W, _H = 192, 256
_MEAN = np.array([123.675, 116.28, 103.53], np.float32)
_STD = np.array([58.395, 57.12, 57.375], np.float32)
_SPLIT = 2.0          # SimCC bins per input pixel


class PoseModel:
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        self.session = ort.InferenceSession(str(POSE_MODEL), opts, providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name

    @classmethod
    def get(cls) -> "PoseModel | None":
        with cls._lock:
            if cls._instance is None:
                if not POSE_MODEL.is_file():
                    log.warning("pose model not found at %s; action detection from poses disabled", POSE_MODEL)
                    cls._instance = False
                else:
                    try:
                        cls._instance = cls()
                        log.info("pose model loaded from %s", POSE_MODEL)
                    except Exception:
                        log.exception("pose model failed to load")
                        cls._instance = False
            return cls._instance or None

    @staticmethod
    def _affine(box):
        """Center/scale with 1.25 padding and the model's 3:4 aspect; returns (M, inverse M)."""
        x1, y1, x2, y2 = box
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        w, h = (x2 - x1) * 1.25, (y2 - y1) * 1.25
        if w / h > _W / _H:
            h = w * _H / _W
        else:
            w = h * _W / _H
        src = np.float32([[cx - w / 2, cy - h / 2], [cx + w / 2, cy - h / 2], [cx - w / 2, cy + h / 2]])
        dst = np.float32([[0, 0], [_W, 0], [0, _H]])
        return cv2.getAffineTransform(src, dst), cv2.getAffineTransform(dst, src)

    def estimate(self, frame_bgr: np.ndarray, boxes: list) -> list[np.ndarray]:
        """boxes: [(x1,y1,x2,y2)] in frame pixels -> [(17,3) array of x, y, score] per box."""
        if not boxes:
            return []
        crops, inverses = [], []
        for box in boxes:
            m, inv = self._affine(box)
            crop = cv2.warpAffine(frame_bgr, m, (_W, _H), flags=cv2.INTER_LINEAR)
            rgb = crop[:, :, ::-1].astype(np.float32)
            crops.append(((rgb - _MEAN) / _STD).transpose(2, 0, 1))
            inverses.append(inv)
        simcc_x, simcc_y = self.session.run(None, {self.input: np.stack(crops)})
        xs = simcc_x.argmax(axis=2) / _SPLIT
        ys = simcc_y.argmax(axis=2) / _SPLIT
        scores = np.minimum(simcc_x.max(axis=2), simcc_y.max(axis=2))
        out = []
        for i, inv in enumerate(inverses):
            pts = np.stack([xs[i], ys[i], np.ones_like(xs[i])], axis=1) @ inv.T
            out.append(np.column_stack([pts, scores[i]]).astype(np.float32))
        return out


def pose_available() -> bool:
    return PoseModel.get() is not None
