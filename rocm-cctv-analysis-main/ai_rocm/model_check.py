"""Validate a detector model without running video inference."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def inspect_model(path: Path, backend: str = "cpu") -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"model file not found: {path}")
    if path.stat().st_size < 1024:
        raise ValueError(f"model file is unexpectedly small: {path}")

    import onnxruntime as ort

    providers = ["CPUExecutionProvider"]
    if backend == "rocm":
        providers = ["MIGraphXExecutionProvider", "CPUExecutionProvider"]
    session = ort.InferenceSession(str(path), providers=providers)
    inputs = [
        {"name": item.name, "shape": item.shape, "type": item.type}
        for item in session.get_inputs()
    ]
    outputs = [
        {"name": item.name, "shape": item.shape, "type": item.type}
        for item in session.get_outputs()
    ]
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
        "providers": session.get_providers(),
        "inputs": inputs,
        "outputs": outputs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("--backend", choices=("cpu", "rocm"), default="cpu")
    args = parser.parse_args()
    try:
        print(json.dumps(inspect_model(args.model, args.backend), indent=2))
    except (FileNotFoundError, ImportError, RuntimeError, ValueError) as exc:
        print(f"model validation failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
