#!/usr/bin/env python3
"""Download the CPU models the laptop/Docker upload workflow uses (standard library only).

    python scripts/get_models.py            # everything missing
    python scripts/get_models.py --check    # only report what is present

Files land under ai/models/ (mounted at /models in Docker):
  yolox_nano.onnx                         detector, 3.5 MB      Apache-2.0 (Megvii YOLOX)
  clip/vision_model_quantized.onnx        CLIP image side, 89 MB  MIT (OpenAI CLIP, Xenova export)
  clip/text_model_quantized.onnx          CLIP text side, 65 MB
  clip/tokenizer.json                     CLIP tokenizer, 2 MB
  pose/rtmpose_t_body7_256x192.onnx       body pose, 13 MB      Apache-2.0 (OpenMMLab RTMPose)

Moondream captions are optional and come from Ollama, not from here:
  install https://ollama.com, then run:  ollama pull moondream
"""

import argparse
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "ai" / "models"
CLIP = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/"
POSE_ZIP = ("https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/onnx_sdk/"
            "rtmpose-t_simcc-body7_pt-body7_420e-256x192-026a1439_20230504.zip")

MODELS = [
    ("yolox_nano.onnx", "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_nano.onnx", None),
    ("clip/vision_model_quantized.onnx", CLIP + "onnx/vision_model_quantized.onnx", None),
    ("clip/text_model_quantized.onnx", CLIP + "onnx/text_model_quantized.onnx", None),
    ("clip/tokenizer.json", CLIP + "tokenizer.json", None),
    ("pose/rtmpose_t_body7_256x192.onnx", POSE_ZIP, "end2end.onnx"),
]


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "trace-get-models"})
    with urllib.request.urlopen(request, timeout=600) as response:
        total = int(response.headers.get("Content-Length") or 0)
        chunks, done = [], 0
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
            done += len(chunk)
            if total:
                print(f"\r    {done / 1e6:6.1f} / {total / 1e6:.1f} MB", end="", flush=True)
        print()
        return b"".join(chunks)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="report only, download nothing")
    args = parser.parse_args()
    missing = 0
    for rel, url, member in MODELS:
        target = ROOT / rel
        if target.is_file() and target.stat().st_size > 0:
            print(f"ok       {rel} ({target.stat().st_size / 1e6:.1f} MB)")
            continue
        missing += 1
        if args.check:
            print(f"missing  {rel}")
            continue
        print(f"download {rel}")
        data = fetch(url)
        if member:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                name = next(n for n in archive.namelist() if n.endswith("/" + member) or n == member)
                data = archive.read(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(target)
        print(f"ok       {rel} ({len(data) / 1e6:.1f} MB)")
    if args.check and missing:
        print(f"\n{missing} model file(s) missing: run  python scripts/get_models.py")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
