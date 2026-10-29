"""Appearance evidence for uploaded-video tracks, CPU only, no API key.

* ``dominant_colors`` names the approximate colours of a crop region from HSV
  pixel statistics (used for upper/lower clothing of people, body of vehicles).
* ``Clip`` wraps a quantized CLIP ViT-B/32 ONNX export so a free-text query
  ("red jacket", "person with a white bag") can be ranked against track crops.
  It is a ranking signal, not a classifier: answers present the top matches as
  candidates with their score, never as confirmed facts.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("appearance")

MODELS_DIR = Path(os.environ.get("TRACE_MODELS_DIR", "/models"))
CLIP_DIR = Path(os.environ.get("CLIP_MODEL_DIR", str(MODELS_DIR / "clip")))

COLOR_NAMES = ("black", "white", "gray", "red", "orange", "yellow", "green",
               "blue", "purple", "pink", "brown")


def _pixel_colors(hsv: np.ndarray) -> np.ndarray:
    """Map every HSV pixel (OpenCV ranges) to an index into COLOR_NAMES."""
    h = hsv[..., 0].astype(np.int32)
    s = hsv[..., 1].astype(np.int32)
    v = hsv[..., 2].astype(np.int32)
    out = np.full(h.shape, COLOR_NAMES.index("gray"), np.int32)
    chromatic = (s >= 60) & (v >= 60)
    hue_name = np.select(
        [(h < 8) | (h >= 170), h < 20, h < 33, h < 85, h < 128, h < 150, h < 170],
        [3, 4, 5, 6, 7, 8, 9], default=3,
    )
    out = np.where(chromatic, hue_name, out)
    brown = chromatic & (h >= 5) & (h < 22) & (v < 150)
    out = np.where(brown, COLOR_NAMES.index("brown"), out)
    out = np.where(~chromatic & (v > 185) & (s < 40), COLOR_NAMES.index("white"), out)
    out = np.where(v < 55, COLOR_NAMES.index("black"), out)
    return out


def color_votes(crop: np.ndarray) -> np.ndarray:
    """Share of each colour name in a BGR crop (central 60% of its width)."""
    if crop is None or crop.size == 0 or min(crop.shape[:2]) < 3:
        return np.zeros(len(COLOR_NAMES))
    w = crop.shape[1]
    core = crop[:, int(w * 0.2): max(int(w * 0.8), int(w * 0.2) + 1)]
    hsv = cv2.cvtColor(core, cv2.COLOR_BGR2HSV)
    counts = np.bincount(_pixel_colors(hsv).ravel(), minlength=len(COLOR_NAMES)).astype(float)
    return counts / max(counts.sum(), 1.0)


def dominant_colors(votes: np.ndarray, min_share: float = 0.22) -> list[str]:
    """Up to two colour names that cover a meaningful share of the region."""
    if votes is None or not votes.any():
        return []
    order = np.argsort(votes)[::-1]
    names = [COLOR_NAMES[i] for i in order[:2] if votes[i] >= min_share]
    return names or [COLOR_NAMES[order[0]]]


def person_regions(crop: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Upper-body and lower-body slices of a person crop."""
    h = crop.shape[0]
    return crop[int(h * 0.18): int(h * 0.5)], crop[int(h * 0.55): int(h * 0.9)]


class Clip:
    """Quantized CLIP ViT-B/32 (Xenova ONNX export). Loaded lazily, thread-safe."""

    MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32).reshape(3, 1, 1)
    STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32).reshape(3, 1, 1)
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        providers = ["CPUExecutionProvider"]
        self.vision = ort.InferenceSession(str(CLIP_DIR / "vision_model_quantized.onnx"), opts, providers=providers)
        self.text = ort.InferenceSession(str(CLIP_DIR / "text_model_quantized.onnx"), opts, providers=providers)
        self.tokenizer = Tokenizer.from_file(str(CLIP_DIR / "tokenizer.json"))
        self._text_cache: dict[str, np.ndarray] = {}

    @classmethod
    def get(cls) -> "Clip | None":
        with cls._lock:
            if cls._instance is None:
                needed = ("vision_model_quantized.onnx", "text_model_quantized.onnx", "tokenizer.json")
                if not all((CLIP_DIR / name).is_file() for name in needed):
                    log.warning("CLIP model not found in %s; appearance search disabled", CLIP_DIR)
                    cls._instance = False
                else:
                    try:
                        cls._instance = cls()
                        log.info("CLIP appearance model loaded from %s", CLIP_DIR)
                    except Exception:
                        log.exception("CLIP model failed to load; appearance search disabled")
                        cls._instance = False
            return cls._instance or None

    def _prep(self, crop: np.ndarray) -> np.ndarray:
        h, w = crop.shape[:2]
        side = max(h, w)
        canvas = np.full((side, side, 3), 127, np.uint8)
        y0, x0 = (side - h) // 2, (side - w) // 2
        canvas[y0:y0 + h, x0:x0 + w] = crop
        rgb = cv2.cvtColor(cv2.resize(canvas, (224, 224), interpolation=cv2.INTER_CUBIC), cv2.COLOR_BGR2RGB)
        arr = rgb.astype(np.float32).transpose(2, 0, 1) / 255.0
        return (arr - self.MEAN) / self.STD

    def embed_images(self, crops: list[np.ndarray]) -> np.ndarray | None:
        crops = [c for c in crops if c is not None and min(c.shape[:2]) >= 8]
        if not crops:
            return None
        batch = np.stack([self._prep(c) for c in crops])
        feats = self.vision.run(None, {"pixel_values": batch})[0]
        return feats / np.maximum(np.linalg.norm(feats, axis=1, keepdims=True), 1e-6)

    def embed_text(self, text: str) -> np.ndarray:
        key = text.strip().lower()
        if key not in self._text_cache:
            ids = self.tokenizer.encode(key).ids[:77]
            feats = self.text.run(None, {"input_ids": np.array([ids], np.int64)})[0][0]
            self._text_cache[key] = feats / max(np.linalg.norm(feats), 1e-6)
        return self._text_cache[key]


def clip_available() -> bool:
    return Clip.get() is not None
