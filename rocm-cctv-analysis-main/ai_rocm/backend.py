"""Runtime selection for ONNX inference backends."""

from __future__ import annotations

import os
from typing import Any


ROCM_PROVIDER = "MIGraphXExecutionProvider"
CPU_PROVIDER = "CPUExecutionProvider"


def available_providers(ort: Any) -> list[str]:
    return list(ort.get_available_providers())


def select_providers(ort: Any, requested: str | None = None) -> tuple[list[Any], str]:
    """Return an ORT provider list and the selected provider name.

    ``auto`` prefers MIGraphX when it is installed and usable, then CPU.
    Explicit GPU selection fails with a useful message instead of silently
    degrading to CPU.
    """
    requested = (requested or os.environ.get("INFERENCE_BACKEND", "auto")).lower()
    available = available_providers(ort)
    if requested in {"cpu", CPU_PROVIDER.lower()}:
        return [CPU_PROVIDER], CPU_PROVIDER
    if requested in {"rocm", "migraphx", ROCM_PROVIDER.lower()}:
        if ROCM_PROVIDER not in available:
            raise RuntimeError(
                "INFERENCE_BACKEND=rocm was requested, but "
                "MIGraphXExecutionProvider is unavailable. "
                f"Available providers: {', '.join(available) or 'none'}."
            )
        return [(ROCM_PROVIDER, {"migraphx_fp16_enable": True}), CPU_PROVIDER], ROCM_PROVIDER
    if requested != "auto":
        raise ValueError(
            f"Unsupported INFERENCE_BACKEND={requested!r}; use auto, cpu, or rocm."
        )
    if ROCM_PROVIDER in available:
        return [(ROCM_PROVIDER, {"migraphx_fp16_enable": True}), CPU_PROVIDER], ROCM_PROVIDER
    if CPU_PROVIDER in available:
        return [CPU_PROVIDER], CPU_PROVIDER
    raise RuntimeError(
        "No usable ONNX Runtime provider is installed. Install onnxruntime "
        "for CPU inference or the ROCm ONNX Runtime build for AMD inference."
    )


def create_session(ort: Any, model_path: str, requested: str | None = None) -> tuple[Any, str]:
    if not os.path.isfile(model_path):
        raise FileNotFoundError(
            f"model file not found: {model_path}. "
            "Place the validated weight at that path; model downloads are explicit."
        )
    if os.path.getsize(model_path) < 1024:
        raise ValueError(f"model file is unexpectedly small or incomplete: {model_path}")
    providers, selected = select_providers(ort, requested)
    try:
        session = ort.InferenceSession(model_path, providers=providers)
    except Exception as exc:
        raise RuntimeError(
            f"Could not load model {model_path!r} with {selected}. "
            "Verify that the model format is supported by the selected runtime "
            "and that the model file is complete."
        ) from exc
    actual = session.get_providers()[0]
    return session, actual
