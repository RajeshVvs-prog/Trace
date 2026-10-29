# TRACE

**Ask what happened in your CCTV footage, and get answers built only from what the camera recorded.**

Upload a recording, and TRACE finds every person and vehicle, follows each one
with a persistent ID, reads their body pose, and lets you investigate in plain
English: *"Did anyone run?"*, *"When did he raise his hands?"*, *"Is anyone
wearing a mask?"*. Every answer comes with timestamps you can click to jump
the video to that moment.

It runs on an ordinary laptop: no GPU required, no cloud, no API key, and the
footage never leaves the machine.

All code lives in [`rocm-cctv-analysis-main/`](rocm-cctv-analysis-main/).

## Two ways to run it

| | Laptop mode | GPU server |
|---|---|---|
| For | Investigating uploaded recordings | A building's live cameras, 24/7 |
| Hardware | Any recent laptop (tested: 4-core i5, 8 GB RAM) | AMD Instinct MI300X with ROCm |
| Start | `run_local.ps1`, or Docker | Docker Compose |
| Guide | [Laptop mode](#quick-start-laptop) below | [Full server documentation](rocm-cctv-analysis-main/README.md) |

## Quick start (laptop)

You need Python 3.10 or newer and ffmpeg on your `PATH`. From `rocm-cctv-analysis-main/`:

```powershell
powershell -ExecutionPolicy Bypass -File run_local.ps1
```

Open <http://localhost:8080>, upload a video on **Camera Sources**, and click
**Investigate** when analysis finishes. The first run creates a virtual
environment and downloads about 170 MB of models.

**With Docker instead** (needs about 4 GB of free RAM):

```bash
python scripts/get_models.py
docker compose -f docker-compose.public-test.yml up -d --build
```

**Sign-in.** Laptop mode asks for an account. On the first visit you create
the administrator; from **Account and people** (top-right menu) an admin can add
people, reset their passwords or remove them. Accounts are stored hashed in
`output/users.json` on this machine only. To run without sign-in, set
`$env:TRACE_AUTH = "off"` before `run_local.ps1`. Forgot the admin password?
Stop TRACE, delete `output/users.json`, start it again and set up a new admin;
recordings are not affected. The GPU server keeps its own Google sign-in.

To run the end-to-end test with sign-in on, pass an account:
`python rocm-cctv-analysis-main/scripts/e2e_local.py clip.mp4 --user NAME --password PW`.

**Optional captions:** install [Ollama](https://ollama.com) and run
`ollama pull moondream`. TRACE uses it automatically when it is running, on
the GPU if the NVIDIA driver is recent (531 or newer), otherwise on the CPU.

## How it works

```
UPLOAD ─► ANALYSIS (once per video, in the background)
            ffmpeg ─────── 5 frames per second
            YOLOX-Nano ─── people, cars, trucks, motorcycles, bicycles
            tracker ────── one persistent ID per object
            RTMPose ────── 17 body joints per person
            rules ──────── colours, speed, path, actions, events
            CLIP ───────── appearance search ("red jacket", "white bag")
            Moondream ──── optional one-line descriptions
         ─► SQLite
         ─► QUESTION ─► investigator: picks the matching search, fills an
                        answer from stored facts, attaches timestamps
```

No language model writes the answers, so TRACE cannot invent a person or a
moment. Every sentence traces back to something measured in the video.

## What you can ask

| Kind | Examples |
|---|---|
| Overview | "Summarize what happened", "How many people appear?" |
| Time | "What is visible at 0:30?", "Who appeared first?" |
| A person | "Describe person 3" |
| Appearance | "Anyone in a red top?", "Is anyone wearing a mask?", "Anyone carrying a bag?" |
| Movement | "Did anyone run?", "Did anyone stop?", "Did anyone follow someone?", "Did anyone walk together?" |
| Body actions | "When did he raise his hands?", "Did anyone wave, bend down, sit or fall?" |
| Vehicles | "Were any vehicles parked?" |

Questions about identity, age, gender or ethnicity are declined.

## Pages

| Page | What it does |
|---|---|
| **Camera Sources** | Upload videos; analysis starts automatically; re-analyse or delete |
| **Investigator** | The chat, with the video beside it; evidence jumps the video |
| **Playback** | The video with boxes, track numbers and skeletons, plus a timeline of tracks and actions |
| **Tracking** | A card per person or vehicle, with a replay of their path |

Every page shares one link format, `?recording=<id>&t=<seconds>&track=<n>`,
so you can move between pages without losing your place.

## Models

| Model | Size | Licence | Job |
|---|---|---|---|
| YOLOX-Nano | 3.5 MB | Apache-2.0 | Detect people and vehicles |
| RTMPose-tiny | 13 MB | Apache-2.0 | Body joints for actions |
| CLIP ViT-B/32, quantized | ~150 MB | MIT | Text-to-image appearance search |
| Moondream (via Ollama) | 1.7 GB | Apache-2.0 | Optional descriptions |

The tracker and action rules are plain code, not models. Measured on the test
laptop: an 80-second clip with 20 people analyses in about 35 to 40 seconds;
pose costs about 10 ms per person; Moondream takes about 17 s per image on CPU.

## Testing

With the app running:

```bash
python rocm-cctv-analysis-main/scripts/e2e_local.py path/to/clip.mp4
```

It uploads the clip and runs 37 checks across analysis, tracks, pose, events,
search, chat, history, pages and deletion, then deletes the test recording.

## Limits

- The small detector misses very distant or heavily hidden people.
- Track numbers can swap when people cross closely, and hold only within one video.
- Colours are approximate; lighting changes them.
- Body actions need people at least about 60 px tall and are estimated from joint positions.
- Night and infrared footage has not been tested.

## Licence

PolyForm Noncommercial License 1.0.0. See
[`rocm-cctv-analysis-main/LICENSE.md`](rocm-cctv-analysis-main/LICENSE.md).
