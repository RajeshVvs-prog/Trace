# Run Trace on a laptop without Docker, GPU or API keys.
# Usage (from this folder):  powershell -ExecutionPolicy Bypass -File run_local.ps1
# Then open http://localhost:8080
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    python -m venv .venv
    .\.venv\Scripts\python -m pip install -r requirements.local.txt
}
# detector, CLIP and pose models (~170 MB the first time; skipped when present)
& .\.venv\Scripts\python.exe scripts\get_models.py
if ($LASTEXITCODE -ne 0) { throw "model download failed; run: python scripts\get_models.py" }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    throw "ffmpeg is required on PATH (winget install Gyan.FFmpeg)"
}

$env:BRIDGE_PORT = if ($env:BRIDGE_PORT) { $env:BRIDGE_PORT } else { "8080" }
$env:BRIDGE_OUTPUT = Join-Path $root "output"
$env:WEBAPP_DIR = Join-Path $root "webapp"
$env:CCVT_STORAGE = "sqlite"
$env:CCVT_SQLITE_PATH = Join-Path $root "output\trace.db"
$env:VIDEO_ANALYSIS_MODEL = Join-Path $root "ai\models\yolox_nano.onnx"
$env:PORTABLE_DETECTOR_DIR = Join-Path $root "ai_rocm"
$env:TRACE_MODELS_DIR = Join-Path $root "ai\models"
$env:TRACE_WEB_CONFIG = Join-Path $root "webapp\local-config.json"
$env:PYTHONIOENCODING = "utf-8"

Set-Location (Join-Path $root "bridge")
& (Join-Path $root ".venv\Scripts\python.exe") bridge.py
