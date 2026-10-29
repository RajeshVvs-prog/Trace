"""Optional local vision-language captions via Ollama (free, runs offline).

If an Ollama server is reachable and has the configured vision model pulled,
uploaded-recording analysis adds:
  * a one-sentence description of each prominent person track's best crop
  * a short scene caption for evenly spaced keyframes

Nothing here is required: without Ollama the investigator answers from the
detector, tracker and CLIP evidence alone and says captions are unavailable.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
from pathlib import Path

import cv2
import numpy as np
import requests

log = logging.getLogger("vlm")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
VLM_MODEL = os.environ.get("VLM_MODEL", "moondream")
MAX_TRACK_CAPTIONS = int(os.environ.get("VLM_MAX_TRACKS", "8"))
MAX_KEYFRAMES = int(os.environ.get("VLM_MAX_KEYFRAMES", "4"))
MIN_CROP_HEIGHT = int(os.environ.get("VLM_MIN_CROP_HEIGHT", "60"))   # smaller crops make the model babble
TIMEOUT = float(os.environ.get("VLM_TIMEOUT", "180"))

PERSON_PROMPT = (
    "Describe this person's visible clothing colours, anything they carry, and what they "
    "are doing, in one short sentence. Do not guess identity, age, gender or ethnicity."
)
SCENE_PROMPT = (
    "Describe what is happening in this CCTV frame in one or two short sentences. "
    "Only mention things that are clearly visible."
)


def available() -> bool:
    try:
        tags = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2).json()
    except (requests.RequestException, ValueError):
        return False
    names = {m.get("name", "") for m in tags.get("models", [])}
    return any(n == VLM_MODEL or n.startswith(f"{VLM_MODEL}:") for n in names)


def caption(image_bgr: np.ndarray, prompt: str) -> str:
    ok, jpeg = cv2.imencode(".jpg", image_bgr, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise ValueError("could not encode image")
    response = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": VLM_MODEL, "prompt": prompt, "stream": False,
            "images": [base64.b64encode(jpeg.tobytes()).decode()],
            "options": {"temperature": 0, "num_predict": 70, "repeat_penalty": 1.3},
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return clean_caption(response.json().get("response", ""))


def clean_caption(text: str) -> str:
    """Drop formatting noise and degenerate output; '' means 'no usable caption'."""
    import re
    text = " ".join(text.split())
    text = re.sub(r"^[^A-Za-z]*(important|note|answer|caption)?[^A-Za-z]*", "", text, flags=re.I).strip()
    text = re.sub(r"^urn\w*[\s,:;-]*", "", text).strip()          # moondream-on-ollama artefact
    text = re.sub(r"^([A-Z][A-Z' -]{2,}[!:.]+\s*)+", "", text).strip()  # "BAD GUESSING!!! ..." shouting tags
    text = re.sub(r"^(on|in|at|of|with)\s+(?=a |an |the )", "", text, flags=re.I).strip()  # "On a table…" fragments
    if re.match(r"(i cannot|i can't|i'm sorry|i am sorry|sorry|please provide|as an ai)", text, re.I):
        return ""                                                  # a refusal, not a description
    # the product never infers gender: neutralise what the model adds anyway
    text = re.sub(r"[;,]?\s*\b(she|he|they) (has|have) [\w\s-]*hair[^.;]*", "", text, flags=re.I)
    for pattern, repl in ((r"\b(she|he) is\b", "they are"), (r"\b(she|he) was\b", "they were"),
                          (r"\b(she|he) has\b", "they have"), (r"\b(she|he)\b", "they"),
                          (r"\b(her|his)\b", "their"),
                          (r"\b(man|woman|lady|guy|girl|boy)\b", "person"),
                          (r"\b(men|women)\b", "people")):
        text = re.sub(pattern, repl, text, flags=re.I)
    text = re.sub(r"^of\s+", "", text, flags=re.I)                # "of a person wearing..."
    if re.match(r"(is|was|wearing|walking|standing|carrying)\b", text, re.I):
        text = "Person " + text[0].lower() + text[1:]
    text = text[:1].upper() + text[1:]
    words = re.findall(r"[a-z']+", text.lower())
    if len(words) < 4 or len(set(words)) / len(words) < 0.5:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out = " ".join(sentences[:2])
    return out if len(out) <= 220 else out[:220].rsplit(" ", 1)[0] + "…"


def _frame_at(path: Path, offset_ms: int) -> np.ndarray | None:
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-ss", f"{offset_ms / 1000:.3f}", "-i", str(path),
         "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
        capture_output=True, timeout=30,
    )
    if result.returncode or not result.stdout:
        return None
    return cv2.imdecode(np.frombuffer(result.stdout, np.uint8), cv2.IMREAD_COLOR)


def caption_recording(store, recording_id: str, video: Path, track_dir: Path,
                      duration_s: float, progress=None) -> dict:
    """Caption prominent tracks and keyframes; returns a status summary."""
    tracks = [t for t in store.list_recording_tracks(recording_id) if t["class_name"] == "person" and t["thumb"]]
    for t in tracks:                       # a re-run replaces old captions
        store.set_track_caption(recording_id, t["track_id"], "")
    store.clear_recording_captions(recording_id)
    tracks.sort(key=lambda t: t["observations"], reverse=True)
    tracks = tracks[:MAX_TRACK_CAPTIONS]
    step_ms = max(10000, int(duration_s * 1000 / max(MAX_KEYFRAMES, 1)))
    keyframes = list(range(step_ms // 2, int(duration_s * 1000), step_ms))[:MAX_KEYFRAMES]
    total = max(1, len(tracks) + len(keyframes))
    done = failures = 0
    for t in tracks:
        if store.get_recording(recording_id) is None:      # deleted while captioning
            return {"status": "cancelled", "model": VLM_MODEL}
        crop = cv2.imread(str(track_dir / t["thumb"]))
        if crop is not None and crop.shape[0] >= MIN_CROP_HEIGHT:
            if crop.shape[0] < 160:        # tiny CCTV crops: upscale for the model
                factor = 160 / crop.shape[0]
                crop = cv2.resize(crop, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
            try:
                text = caption(crop, PERSON_PROMPT)
                if text:
                    store.set_track_caption(recording_id, t["track_id"], text)
            except Exception as exc:
                failures += 1
                log.warning("track caption failed recording=%s track=%s: %s", recording_id, t["track_id"], exc)
        done += 1
        if progress:
            progress(int(done / total * 100))
    for offset in keyframes:
        if store.get_recording(recording_id) is None or not video.is_file():
            return {"status": "cancelled", "model": VLM_MODEL}
        frame = _frame_at(video, offset)
        if frame is not None:
            try:
                text = caption(frame, SCENE_PROMPT)
                if text:
                    store.add_recording_caption(recording_id, offset, text, VLM_MODEL)
            except Exception as exc:
                failures += 1
                log.warning("scene caption failed recording=%s offset=%s: %s", recording_id, offset, exc)
        done += 1
        if progress:
            progress(int(done / total * 100))
    return {"status": "completed" if failures < total else "failed", "model": VLM_MODEL,
            "tracks": len(tracks), "keyframes": len(keyframes), "failures": failures}
