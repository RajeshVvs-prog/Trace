"""Action events for uploaded recordings, from measured movement and body pose.

Movement (from the tracker, no extra model): running, stopping after moving,
one person following another. Speeds are in body heights per second, so a
person far from the camera and one close to it are judged the same way.

Pose (from pose.py keypoints, COCO-17): hands raised, one hand raised, waving,
bending, sitting/crouching, lying down, falling. Each rule must hold for a
minimum duration with small gaps bridged, so one bad frame never makes an
event. Every event is a fact with a time span that the UI can seek to.
"""

from __future__ import annotations

import math

import numpy as np

KP_MIN = 0.3            # keypoint score below this is treated as not visible
GAP_MS = 450            # bridge gaps up to this inside a segment

# COCO-17 indices
NOSE, L_SH, R_SH, L_EL, R_EL, L_WR, R_WR, L_HIP, R_HIP, L_KN, R_KN, L_AN, R_AN = 0, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16


def _segments(times: list[int], flags: list[bool], min_ms: int) -> list[tuple[int, int]]:
    """Runs of True (gaps <= GAP_MS bridged) lasting at least min_ms."""
    out, start, last = [], None, None
    for t, f in zip(times, flags):
        if f:
            if start is None or t - last > GAP_MS:
                if start is not None and last - start >= min_ms:
                    out.append((start, last))
                start = t
            last = t
    if start is not None and last - start >= min_ms:
        out.append((start, last))
    return out


def _fmt(ms: int) -> str:
    s = max(0, int(round(ms / 1000)))
    return f"{s // 60}:{s % 60:02d}"


# ---- movement ------------------------------------------------------------------


def _speed_series(points: list, width: int, height: int):
    """points: (offset_ms, cx, cy, w, h) normalised -> (times, speeds in body heights/s)."""
    if len(points) < 4:
        return [], []
    t = np.array([p[0] for p in points], float)
    xy = np.array([[p[1] * width, p[2] * height] for p in points], float)
    h = np.array([p[4] * height for p in points], float)
    times, speeds = [], []
    j = 0
    for i in range(len(t)):
        while j < len(t) - 1 and t[j] - t[i] < 1000:      # ~1 s window
            j += 1
        if t[j] - t[i] < 600:
            break
        body = max(float(np.median(h[i:j + 1])), 1.0)
        dist = float(np.linalg.norm(xy[j] - xy[i]))
        times.append(int(t[i]))
        speeds.append(dist / body / ((t[j] - t[i]) / 1000))
    return times, speeds


def movement_events(tracks: list[dict], accs: dict, width: int, height: int) -> list[dict]:
    events = []
    people = [t for t in tracks if t["class_name"] == "person"]
    speed_of: dict[int, tuple[list, list]] = {}
    for t in people:
        tid = t["track_id"]
        times, speeds = _speed_series(accs[tid].points, width, height)
        speed_of[tid] = (times, speeds)
        if not times:
            continue
        # typical top speed: ignores one-window spikes from a box jumping
        t["max_speed"] = round(float(np.percentile(speeds, 90)), 2) if len(speeds) >= 3 else None
        for a, b in _segments(times, [s >= 1.6 for s in speeds], 800):
            peak = max(s for tt, s in zip(times, speeds) if a <= tt <= b)
            events.append({"kind": "running", "track_ids": [tid], "start_offset_ms": a, "end_offset_ms": b + 1000,
                           "description": f"person {tid} moved at running speed from {_fmt(a)} to {_fmt(b + 1000)} "
                                          f"(up to {peak:.1f} body heights per second)"})
        moving = [s >= 0.35 for s in speeds]
        still = _segments(times, [s < 0.12 for s in speeds], 2000)
        for a, b in still:
            before = any(m for tt, m in zip(times, moving) if a - 3000 <= tt < a)
            if before:
                events.append({"kind": "stopped", "track_ids": [tid], "start_offset_ms": a, "end_offset_ms": b + 1000,
                               "description": f"person {tid} stopped walking at {_fmt(a)} and stayed still for "
                                              f"{(b + 1000 - a) / 1000:.0f} s"})

    # following: B is where A was a few seconds earlier, while not side by side
    pos = {t["track_id"]: {p[0]: p for p in accs[t["track_id"]].points} for t in people}
    movers = [t for t in people if (t.get("max_speed") or 0) >= 0.35]
    for a in movers:
        for b in movers:
            if a is b:
                continue
            pa, pb = pos[a["track_id"]], pos[b["track_id"]]
            best = None
            for lag in (1000, 2000, 3000, 4000, 5000):
                hits = []
                for off, p in sorted(pb.items()):
                    q, now = pa.get(off - lag), pa.get(off)
                    if q is None or now is None:
                        continue
                    body = max(p[4] * height, 1.0)                     # B's height in pixels

                    def gap(u, v):
                        return math.hypot((u[1] - v[1]) * width, (u[2] - v[2]) * height) / body

                    hits.append((off, gap(p, q) < 0.6 and gap(p, now) > 1.0))
                segs = _segments([h[0] for h in hits], [h[1] for h in hits], 4000)
                if segs:
                    longest = max(segs, key=lambda s: s[1] - s[0])
                    if best is None or longest[1] - longest[0] > best[1][1] - best[1][0]:
                        best = (lag, longest)
            if best:
                lag, (s, e) = best
                events.append({"kind": "following", "track_ids": [b["track_id"], a["track_id"]],
                               "start_offset_ms": s, "end_offset_ms": e,
                               "description": f"person {b['track_id']} followed the path of person {a['track_id']} "
                                              f"about {lag // 1000} s behind, from {_fmt(s)} to {_fmt(e)}"})
    return events


# ---- pose ----------------------------------------------------------------------


def _vis(k, *idx) -> bool:
    return all(k[i, 2] >= KP_MIN for i in idx)


def _frame_flags(k: np.ndarray, box_w: float, box_h: float) -> dict:
    """Per-frame booleans from one (17,3) keypoint array in pixels (y grows down)."""
    f = {}
    if not _vis(k, L_SH, R_SH, L_HIP, R_HIP):
        return f
    sh = (k[L_SH, :2] + k[R_SH, :2]) / 2
    hip = (k[L_HIP, :2] + k[R_HIP, :2]) / 2
    torso = float(np.linalg.norm(sh - hip))
    if torso < 8:
        return f
    tilt = math.degrees(math.atan2(abs(sh[0] - hip[0]), max(hip[1] - sh[1], 1e-6)))  # 0 = upright
    if hip[1] <= sh[1]:
        tilt = max(tilt, 90.0)
    f["tilt"] = tilt
    shoulder_w = float(abs(k[L_SH, 0] - k[R_SH, 0])) or torso * 0.5
    head_x = float(k[NOSE, 0]) if _vis(k, NOSE) else float(sh[0])
    raised, overhead = [], []
    for wr, el, s in ((L_WR, L_EL, L_SH), (R_WR, R_EL, R_SH)):
        if _vis(k, wr, el):
            forearm_up = k[wr, 1] < k[el, 1] - 0.04 * torso    # high cameras foreshorten raised forearms
            high = k[wr, 1] < k[s, 1] + 0.45 * torso          # chest height or higher
            raised.append(forearm_up and high)
            overhead.append(k[wr, 1] < k[s, 1])
        else:
            raised.append(False)
            overhead.append(False)
    # raised hands sit apart, out from the body; holding a bag or crossed arms keep them together
    spread = _vis(k, L_WR, R_WR) and abs(k[L_WR, 0] - k[R_WR, 0]) >= 1.3 * shoulder_w
    f["both_up"] = all(raised) and spread
    # one hand: clearly above the shoulder and away from the head (not a phone at the ear)
    lone = [r and o and abs(float(k[w, 0]) - head_x) > 0.5 * shoulder_w
            for r, o, w in zip(raised, overhead, (L_WR, R_WR))]
    f["one_up"] = sum(lone) == 1 and not f["both_up"]
    f["overhead"] = any(o and r for o, r in zip(overhead, raised))
    f["wrist_x"] = [float(k[w, 0]) for w, r in zip((L_WR, R_WR), lone) if r]
    f["shoulder_w"] = shoulder_w
    f["lying"] = tilt > 65 and box_w > 0.8 * box_h
    f["bending"] = 40 <= tilt <= 75 and not f["lying"]
    if _vis(k, L_KN, R_KN):
        knee_y = (k[L_KN, 1] + k[R_KN, 1]) / 2
        thigh_drop = knee_y - hip[1]
        f["low"] = tilt < 40 and thigh_drop < 0.45 * torso
    return f


def pose_events(tracks: list[dict], accs: dict) -> tuple[list[dict], dict]:
    events, coverage = [], {"people": 0, "with_pose": 0}
    for t in tracks:
        if t["class_name"] != "person":
            continue
        coverage["people"] += 1
        acc = accs[t["track_id"]]
        if len(acc.poses) < 3:
            continue
        coverage["with_pose"] += 1
        tid = t["track_id"]
        times = [p[0] for p in acc.poses]
        flags = [_frame_flags(p[1], p[2], p[3]) for p in acc.poses]

        def add(kind, segs, text):
            for a, b in segs:
                events.append({"kind": kind, "track_ids": [tid], "start_offset_ms": a, "end_offset_ms": b,
                               "description": text.format(tid=tid, a=_fmt(a), b=_fmt(b), d=(b - a) / 1000)})

        both = _segments(times, [f.get("both_up", False) for f in flags], 500)
        for a, b in both:
            over = any(f.get("overhead") for tt, f in zip(times, flags) if a <= tt <= b)
            add("hands_raised", [(a, b)], "person {tid} raised both hands" +
                (" above shoulder height" if over else " (forearms up, hands at chest height or higher)") +
                " from {a} to {b}")
        one = _segments(times, [f.get("one_up", False) for f in flags], 700)
        one = [s for s in one if not any(s[0] <= x[1] and x[0] <= s[1] for x in both)]
        for a, b in one:
            xs = [f["wrist_x"][0] for tt, f in zip(times, flags) if a <= tt <= b and f.get("wrist_x")]
            sw = np.median([f["shoulder_w"] for tt, f in zip(times, flags) if a <= tt <= b and "shoulder_w" in f])
            turns = sum(1 for i in range(1, len(xs) - 1) if (xs[i] - xs[i - 1]) * (xs[i + 1] - xs[i]) < 0)
            if len(xs) >= 4 and turns >= 2 and (max(xs) - min(xs)) > 0.25 * sw:
                add("waving", [(a, b)], "person {tid} appears to wave one hand from {a} to {b}")
            else:
                add("hand_raised", [(a, b)], "person {tid} raised one hand from {a} to {b}")
        add("bending", _segments(times, [f.get("bending", False) for f in flags], 600),
            "person {tid} bent forward from {a} to {b}")
        add("sitting", _segments(times, [f.get("low", False) for f in flags], 1000),
            "person {tid} was sitting or crouching from {a} to {b}")
        lying = _segments(times, [f.get("lying", False) for f in flags], 1000)
        for a, b in lying:
            upright_before = any(f.get("tilt", 90) < 30 for tt, f in zip(times, flags) if a - 2000 <= tt < a)
            if upright_before:
                add("fall", [(a, b)], "person {tid} went from upright to lying down at {a} (possible fall), until {b}")
            else:
                add("lying", [(a, b)], "person {tid} was lying down from {a} to {b}")
    return events, coverage
