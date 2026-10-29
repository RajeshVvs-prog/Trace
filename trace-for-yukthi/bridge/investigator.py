"""Evidence-grounded Q&A over one analysed recording (no LLM, no API key).

The question is routed to a small set of deterministic "fat tools" that query
what analysis persisted: tracks (identity within this video, time span, path,
entry/exit, approximate colours, CLIP embedding, optional VLM caption), derived
events, scene captions and raw detections. Every sentence in an answer is
backed by an evidence item carrying ``offset_ms`` so the UI can seek the video.
When the evidence cannot support an answer, the reply says so and offers the
nearest thing it can show.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import numpy as np

import appearance

COLOR_WORDS = set(appearance.COLOR_NAMES) | {"grey", "dark", "light", "navy", "beige", "maroon"}
COLOR_ALIASES = {"grey": "gray", "navy": "blue", "maroon": "red", "beige": "white"}
UPPER_WORDS = {"shirt", "jacket", "top", "coat", "hoodie", "sweater", "jumper", "t-shirt", "tshirt", "blouse", "vest", "upper"}
LOWER_WORDS = {"pants", "trousers", "jeans", "shorts", "skirt", "leggings", "lower"}
OBJECT_WORDS = {"bag", "bags", "backpack", "umbrella", "carrying", "holding", "hat", "cap", "helmet",
                "suitcase", "phone", "box", "parcel", "stroller", "pram"}
# face coverings are visible clothing, not identity: answer them from captions
COVER_WORDS = {"mask", "masked", "balaclava", "hood", "hooded", "hoodie", "covered", "covering",
               "obscured", "hiding", "hidden", "disguise", "disguised", "face"}
APPEARANCE_WORDS = UPPER_WORDS | LOWER_WORDS | OBJECT_WORDS | COVER_WORDS | {
    "wearing", "dressed", "clothes", "clothing", "outfit"}
VEHICLE_WORDS = {"car", "cars", "vehicle", "vehicles", "truck", "trucks", "van", "vans",
                 "motorcycle", "motorbike", "bike", "bicycle", "bus"}
PERSON_WORDS = {"person", "people", "persons", "man", "men", "woman", "women", "someone",
                "anyone", "pedestrian", "pedestrians", "individual", "individuals", "human", "humans", "guy", "girl", "boy"}
VEHICLE_CLASSES = {"car", "truck", "motorcycle", "bus", "bicycle"}
GREETING_WORDS = {"hi", "hii", "hello", "hey", "heyy", "yo", "thanks", "thank", "you", "ok", "okay", "cool",
                  "good", "morning", "evening", "great", "nice"}

# action question words -> stored event kinds (see actions.py)
ACTION_VOCAB = (
    ({"run", "runs", "running", "ran", "sprint", "sprinting", "rush", "rushing", "jog", "jogging", "hurry",
      "hurrying", "fast", "speed"}, {"running"}),
    ({"raise", "raised", "raises", "raising", "lift", "lifted", "surrender", "surrendered"}, {"hands_raised", "hand_raised", "waving"}),
    ({"wave", "waves", "waved", "waving"}, {"waving", "hand_raised"}),
    ({"bend", "bends", "bent", "bending", "stoop", "stooped", "stooping", "bow", "bowed", "pick", "picked", "picking"}, {"bending"}),
    ({"sit", "sits", "sat", "sitting", "seated", "crouch", "crouched", "crouching", "squat", "squatting",
      "kneel", "kneeling", "knelt"}, {"sitting"}),
    ({"fall", "falls", "fell", "fallen", "falling", "collapse", "collapsed", "lying", "lie", "lay", "laying"}, {"fall", "lying"}),
    ({"stop", "stops", "stopped", "stopping", "pause", "paused", "halt", "halted", "froze"}, {"stopped"}),
    ({"follow", "follows", "followed", "following", "chase", "chased", "chasing", "tail", "tailed", "tailing"}, {"following"}),
)
ACTION_NAMES = {"running": "running", "hands_raised": "raising both hands", "hand_raised": "raising one hand",
                "waving": "waving", "bending": "bending forward", "sitting": "sitting or crouching",
                "fall": "falling", "lying": "lying down", "stopped": "stopping", "following": "following someone"}
POSE_KINDS = {"hands_raised", "hand_raised", "waving", "bending", "sitting", "fall", "lying"}


def action_kinds(q: str, words: set) -> set:
    kinds = set()
    for vocab, mapped in ACTION_VOCAB:
        if words & vocab:
            kinds |= mapped
    if re.search(r"\b(hands?|arms?) (up|in the air|above)", q) or "hands up" in q:
        kinds |= {"hands_raised", "hand_raised", "waving"}
    return kinds


SENSITIVE = re.compile(r"\b(who is (he|she|this|that|it)|identify|identity|name of|recogni[sz]e|facial recognition|"
                       r"age|how old|gender|race|ethnic|religion)\b")


def _tz(recording: dict):
    try:
        return datetime.fromisoformat(recording["recording_at"]).tzinfo or timezone.utc
    except (KeyError, ValueError):
        return timezone.utc


def clock(offset_ms: int) -> str:
    seconds = max(0, int(round(offset_ms / 1000)))
    return f"{seconds // 60}:{seconds % 60:02d}"


class Answerer:
    def __init__(self, recording: dict, tracks: list[dict], events: list[dict],
                 captions: list[dict], detections: list[dict]):
        self.rec = recording
        self.tracks = tracks
        self.by_id = {t["track_id"]: t for t in tracks}
        self.events = events
        self.captions = captions
        self.detections = detections
        self.tz = _tz(recording)
        self.start = datetime.fromisoformat(recording["recording_at"])
        if self.start.tzinfo is None:
            self.start = self.start.replace(tzinfo=timezone.utc)
        self.people = [t for t in tracks if t["class_name"] == "person"]
        self.vehicles = [t for t in tracks if t["class_name"] in VEHICLE_CLASSES]

    # ---- formatting helpers ------------------------------------------------

    def when(self, offset_ms: int) -> str:
        wall = (self.start + timedelta(milliseconds=offset_ms)).astimezone(self.tz)
        return f"{clock(offset_ms)} into the video ({wall.strftime('%H:%M:%S')})"

    def label(self, t: dict) -> str:
        noun = "Person" if t["class_name"] == "person" else t["class_name"].capitalize()
        return f"{noun} {t['track_id']}"

    def looks(self, t: dict) -> str:
        c = t.get("colors") or {}
        if t["class_name"] == "person":
            parts = []
            if c.get("upper"):
                parts.append(f"{'/'.join(c['upper'])} upper clothing")
            if c.get("lower"):
                parts.append(f"{'/'.join(c['lower'])} lower clothing")
            return ", ".join(parts)
        return f"{'/'.join(c.get('body', []))} body" if c.get("body") else ""

    def ev_track(self, t: dict, note: str = "", offset: int | None = None) -> dict:
        off = t["best_offset_ms"] if offset is None else offset
        return {
            "kind": "track", "track_id": t["track_id"], "class_name": t["class_name"],
            "offset_ms": off, "timestamp_ms": t["first_ms"] - t["first_offset_ms"] + off,
            "start_offset_ms": t["first_offset_ms"], "end_offset_ms": t["last_offset_ms"],
            "label": note or f"{self.label(t)} · {clock(t['first_offset_ms'])}–{clock(t['last_offset_ms'])}",
            "thumb_url": f"/api/uploads/{self.rec['id']}/tracks/{t['track_id']}/thumb" if t.get("thumb") else None,
        }

    def ev_event(self, e: dict) -> dict:
        return {
            "kind": "event", "event": e["kind"], "track_ids": e["track_ids"],
            "offset_ms": e["start_offset_ms"], "timestamp_ms": e["start_ms"],
            "start_offset_ms": e["start_offset_ms"], "end_offset_ms": e["end_offset_ms"],
            "label": f"{clock(e['start_offset_ms'])} · {e['description']}",
        }

    def ev_moment(self, offset_ms: int, text: str) -> dict:
        return {"kind": "moment", "offset_ms": offset_ms,
                "timestamp_ms": int(self.start.timestamp() * 1000) + offset_ms,
                "label": f"{clock(offset_ms)} · {text}"}

    def describe(self, t: dict) -> str:
        bits = [f"{self.label(t)} is visible from {self.when(t['first_offset_ms'])} "
                f"to {clock(t['last_offset_ms'])} ({(t['last_offset_ms'] - t['first_offset_ms']) / 1000:.0f} s)"]
        look = self.looks(t)
        if look:
            bits.append(f"approximate colours: {look}")
        if t["stationary"] or t["direction"] == "stayed roughly in place":
            where = t["entry_region"] if t["entry_region"] == t["exit_region"] else \
                f"{t['entry_region']} and {t['exit_region']}"
            bits.append(f"they stay roughly in place, in the {where} of the frame")
        else:
            entry = t["entry_edge"] or f"the {t['entry_region']}"
            exit_ = t["exit_edge"] or f"the {t['exit_region']}"
            bits.append(f"they first appear at {entry}, move {t['direction']}, and are last seen at {exit_}")
        if t.get("caption"):
            bits.append(f'local vision model description: "{t["caption"]}"')
        return "; ".join(bits) + "."

    # ---- routing -------------------------------------------------------------

    def answer(self, question: str) -> tuple[str, list[dict]]:
        q = question.lower().strip()
        words = set(re.findall(r"[a-z0-9\-]+", q))
        if not self.detections:
            return ("The analysis found no people or vehicles in this recording, so there is nothing I can "
                    "report as evidence. The detector only covers people, cars, trucks, motorcycles and bicycles.", [])
        if SENSITIVE.search(q):
            return ("I can't identify people or infer age, gender or other personal traits. I can describe what "
                    "the footage shows: when each tracked person appears, where they move and their approximate "
                    "clothing colours. Ask about a person number, for example \"describe person 3\".",
                    [self.ev_track(t) for t in self._top_people(3)])
        if words and words <= GREETING_WORDS:
            return self.greeting()
        ref = re.search(r"\b(?:person|track|id|#|car|truck|vehicle|object)\s*#?\s*(\d+)\b", q)
        ref_track = self.by_id.get(int(ref.group(1))) if ref else None
        kinds = action_kinds(q, words)
        if kinds and not (words & {"together", "group", "groups"}):
            return self.actions_answer(kinds, ref_track, asks_when=bool(
                words & {"when", "time", "which", "what"} or "at what" in q))
        if ref_track:
            return self.track_story(ref_track, q)
        moment = self._parse_time(q)
        if moment is not None and ("at " in q or "around" in q or "what" in q or "visible" in q):
            return self.at_time(moment)
        if words & {"together", "group", "groups", "meet", "met", "meeting", "interact", "interaction", "with"} \
                and not (words & APPEARANCE_WORDS):
            return self.together()
        if words & {"loiter", "loitering", "linger", "lingering", "waiting", "wait", "standing", "stood", "idle", "suspicious"}:
            return self.lingering()
        if words & {"busiest", "crowd", "crowded", "busy", "peak", "most"}:
            return self.busiest()
        if (words & COLOR_WORDS) or (words & APPEARANCE_WORDS) or "look like" in q or "looks like" in q                 or words & {"find", "search", "spot"}:
            return self.appearance(q, words)
        if words & {"how", "many", "count", "number"} and (words & {"many", "count", "number"}):
            return self.count(words)
        if words & {"first", "earliest", "last", "latest", "final"}:
            return self.first_last(words)
        if words & VEHICLE_WORDS or "parked" in words:
            return self.vehicles_answer()
        if words & {"enter", "entered", "enters", "exit", "exited", "leave", "left", "came", "come", "went", "go", "direction", "route", "path", "where"}:
            return self.movement()
        if words & {"summary", "summarize", "summarise", "happened", "happen", "overview", "describe", "events", "important", "going"}:
            return self.summary()
        return self.summary(prefix="I matched your question to an overview of the recording. ")

    # ---- tools ---------------------------------------------------------------

    def _top_people(self, n):
        return sorted(self.people, key=lambda t: t["observations"], reverse=True)[:n]

    def _parse_time(self, q: str) -> int | None:
        m = re.search(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b", q)
        if m:
            a, b, c = int(m.group(1)), int(m.group(2)), m.group(3)
            if c is not None:                      # wall clock hh:mm:ss
                wall = self.start.astimezone(self.tz).replace(hour=a, minute=b, second=int(c), microsecond=0)
                off = int((wall - self.start).total_seconds() * 1000)
                return off if off >= 0 else None
            return (a * 60 + b) * 1000             # m:ss offset into the video
        m = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:s|sec|secs|second|seconds)\b", q)
        if m:
            return int(float(m.group(1)) * 1000)
        return None

    def track_story(self, t: dict, q: str) -> tuple[str, list[dict]]:
        text = self.describe(t)
        evidence = [self.ev_track(t, f"{self.label(t)} first seen · {clock(t['first_offset_ms'])}", t["first_offset_ms"]),
                    self.ev_track(t, f"{self.label(t)} clearest view · {clock(t['best_offset_ms'])}"),
                    self.ev_track(t, f"{self.label(t)} last seen · {clock(t['last_offset_ms'])}", t["last_offset_ms"])]
        related = [e for e in self.events if t["track_id"] in e["track_ids"]]
        if related:
            text += " Related events: " + "; ".join(f"{e['description']} at {clock(e['start_offset_ms'])}" for e in related[:4]) + "."
            evidence += [self.ev_event(e) for e in related[:4]]
        text += (" Track numbers are only consistent within this recording; if the person leaves and returns "
                 "they may receive a new number.")
        return text, evidence

    def greeting(self) -> tuple[str, list[dict]]:
        dur = (self.rec.get("analysis_meta") or {}).get("duration_s")
        return (f"Hi! I'm looking at {self.rec['camera_name']} ({self.rec['original_name']}"
                + (f", {dur:.0f} s" if dur else "") + f"): {len(self.people)} tracked "
                + ("person" if len(self.people) == 1 else "people") + f" and {len(self.vehicles)} "
                + ("vehicle" if len(self.vehicles) == 1 else "vehicles") + ". Try \"summarize what happened\", "
                "\"did anyone run?\", \"when did someone raise their hands?\" or \"is anyone wearing a mask?\".", [])

    def actions_answer(self, kinds: set, track: dict | None = None,
                       asks_when: bool = False) -> tuple[str, list[dict]]:
        found = [e for e in self.events if e["kind"] in kinds
                 and (track is None or track["track_id"] in e["track_ids"])]
        order = list(ACTION_NAMES)
        main = [k for k in order if k in kinds and k not in {"hand_raised", "lying"}] or \
            [k for k in order if k in kinds]
        asked = " or ".join(ACTION_NAMES[k] for k in main)
        who = self.label(track) if track else "anyone"
        if found:
            found.sort(key=lambda e: e["start_offset_ms"])
            lines = [e["description"][:1].upper() + e["description"][1:] for e in found[:8]]
            if asks_when:
                lead = f"At {clock(found[0]['start_offset_ms'])}. " if len(found) == 1 else                     f"{len(found)} times, first at {clock(found[0]['start_offset_ms'])}. "
            else:
                lead = "Yes. " if len(found) == 1 else f"Yes, {len(found)} times. "
            text = lead + "; ".join(lines) + "."
            if kinds & POSE_KINDS:
                text += " Body actions are estimated from the person's joint positions; click a time to check it."
            evidence = [self.ev_event(e) for e in found[:8]]
            if len(found) > 8:
                text += f" {len(found) - 8} more are listed on the Playback timeline."
            return text, evidence

        meta = self.rec.get("analysis_meta") or {}
        text = f"I found no sign of {who} {asked} in this recording."
        evidence = []
        if kinds & POSE_KINDS:
            pose = meta.get("pose") or {}
            if not meta.get("pose_model"):
                text += (" Body pose was not analysed for this recording, so body actions cannot be checked. "
                         "Re-analyse it to add pose.")
            elif pose.get("people"):
                text += (f" Body pose was measured for {pose.get('with_pose', 0)} of {pose['people']} tracked "
                         f"{'person' if pose['people'] == 1 else 'people'}; people shorter than "
                         f"{pose.get('min_person_height_px', 60)} pixels are too small to judge.")
        if "running" in kinds:
            group = [track] if track else self.people
            speeds = [t for t in group if t.get("max_speed") is not None]
            if speeds:
                fastest = max(speeds, key=lambda t: t["max_speed"])
                text += (f" The fastest typical speed was {self.label(fastest)} at about {fastest['max_speed']:.1f} "
                         "body heights per second; walking is roughly 0.5 to 1.2 and running is 1.6 or more, "
                         "sustained for about a second.")
                evidence.append(self.ev_track(fastest))
        return text, evidence

    def at_time(self, offset_ms: int) -> tuple[str, list[dict]]:
        duration = int((self.rec.get("analysis_meta") or {}).get("duration_s", 0) * 1000)
        if duration and offset_ms > duration:
            return (f"The recording is only {clock(duration)} long, so there is no footage at {clock(offset_ms)}.", [])
        offsets = sorted({d["offset_ms"] for d in self.detections if d["offset_ms"] is not None})
        if not offsets:
            return "No detections are stored with video offsets; re-run analysis for this recording.", []
        nearest = min(offsets, key=lambda o: abs(o - offset_ms))
        here = [d for d in self.detections if d["offset_ms"] == nearest]
        tracked = sorted({d["track_id"] for d in here if d["track_id"] is not None})
        counts: dict[str, int] = {}
        for d in here:
            counts[d["class_name"]] = counts.get(d["class_name"], 0) + 1
        summary = ", ".join(f"{n} {c}{'s' if n > 1 and c != 'person' else ''}" if c != "person" else f"{n} {'people' if n > 1 else 'person'}"
                            for c, n in counts.items())
        text = f"At {self.when(nearest)} the detector sees {summary}."
        if abs(nearest - offset_ms) > 1500:
            text = f"There is no sampled frame exactly at {clock(offset_ms)}; the nearest is {clock(nearest)}. " + text
        if tracked:
            text += " Tracked: " + "; ".join(
                f"{self.label(self.by_id[i])}" + (f" ({self.looks(self.by_id[i])})" if self.looks(self.by_id[i]) else "")
                for i in tracked if i in self.by_id) + "."
        cap = min(self.captions, key=lambda c: abs(c["offset_ms"] - nearest), default=None)
        if cap and abs(cap["offset_ms"] - nearest) <= 6000:
            text += f' Scene caption near {clock(cap["offset_ms"])}: "{cap["caption"]}"'
        evidence = [self.ev_moment(nearest, summary)] + [self.ev_track(self.by_id[i], offset=nearest) for i in tracked[:6] if i in self.by_id]
        return text, evidence

    def count(self, words: set) -> tuple[str, list[dict]]:
        if words & VEHICLE_WORDS:
            group, noun = self.vehicles, "vehicles"
        else:
            group, noun = self.people, "people"
        if not group:
            return f"No {noun} were tracked in this recording.", []
        moving = [t for t in group if not t["stationary"]]
        text = (f"I tracked {len(group)} distinct {noun} in this recording"
                + (f" ({len(group) - len(moving)} stayed in place, {len(moving)} moved)" if len(moving) != len(group) else "")
                + ". This counts tracks, so a person who leaves view and comes back, or is hidden for several seconds, "
                  "can be counted twice.")
        busiest = next((e for e in self.events if e["kind"] == "busiest"), None)
        evidence = [self.ev_track(t) for t in group[:10]]
        if busiest and noun == "people":
            text += f" The most visible at once was {len(busiest['track_ids'])}, at {self.when(busiest['start_offset_ms'])}."
            evidence.insert(0, self.ev_event(busiest))
        return text, evidence

    def first_last(self, words: set) -> tuple[str, list[dict]]:
        group = self.vehicles if words & VEHICLE_WORDS else self.people
        noun = "vehicle" if group is self.vehicles else "person"
        if not group:
            return f"No {noun} was tracked in this recording.", []
        last = bool(words & {"last", "latest", "final"})
        if last:
            t = max(group, key=lambda t: t["first_offset_ms"])
            text = f"The last {noun} to appear is {self.label(t)}, first seen at {self.when(t['first_offset_ms'])}. " + self.describe(t)
        else:
            first = min(t["first_offset_ms"] for t in group)
            at_start = [t for t in group if t["first_offset_ms"] == first]
            t = at_start[0]
            if first == 0 and len(at_start) > 1:
                text = (f"{len(at_start)} {noun}s are already in view when the recording starts: "
                        + ", ".join(self.label(x) for x in at_start) + ". ")
                late = [x for x in group if x["first_offset_ms"] > 0]
                if late:
                    n = min(late, key=lambda x: x["first_offset_ms"])
                    text += f"The first {noun} to arrive after that is {self.label(n)} at {self.when(n['first_offset_ms'])}."
                    return text, [self.ev_track(x, offset=0) for x in at_start[:5]] + [self.ev_track(n, offset=n["first_offset_ms"])]
                return text, [self.ev_track(x, offset=0) for x in at_start[:5]]
            text = f"The first {noun} appears at {self.when(first)}: " + self.describe(t)
        return text, [self.ev_track(t, offset=t["first_offset_ms"])]

    def appearance(self, q: str, words: set) -> tuple[str, list[dict]]:
        wants_vehicle = bool(words & VEHICLE_WORDS) and not (words & PERSON_WORDS)
        pool = self.vehicles if wants_vehicle else self.people
        if not pool:
            return f"No {'vehicles' if wants_vehicle else 'people'} were tracked, so I can't search by appearance.", []
        colors = [COLOR_ALIASES.get(w, w) for w in re.findall(r"[a-z]+", q) if w in COLOR_WORDS and w not in {"dark", "light"}]
        part = "upper" if words & UPPER_WORDS else "lower" if words & LOWER_WORDS else None
        if words & OBJECT_WORDS and not part:
            colors = []      # "white bag": the colour belongs to the object, not the clothing
        scores = {t["track_id"]: 0.0 for t in pool}
        reasons: dict[int, list[str]] = {t["track_id"]: [] for t in pool}

        # 1) approximate colour evidence from pixels
        for t in pool:
            c = t.get("colors") or {}
            regions = ([c.get(part, [])] if part else [c.get("upper", []), c.get("lower", []), c.get("body", [])])
            for color in colors:
                for idx, names in enumerate(regions):
                    if color in names:
                        rank_bonus = 1.0 if names and names[0] == color else 0.6
                        scores[t["track_id"]] += rank_bonus
                        where = part or ("upper" if idx == 0 else "lower" if idx == 1 else "body")
                        reasons[t["track_id"]].append(f"{color} {where} colour")
                        break

        # 2) CLIP text-to-image similarity on track crops
        clip = appearance.Clip.get()
        used_clip = False
        query = re.sub(r"\b(find|search|look|looking|for|anyone|someone|is there|are there|was there|were there|"
                       r"any|a|an|the|me|show|please|who|which|in|video|recording|clip|footage)\b", " ", q)
        query = " ".join(re.sub(r"[^a-z0-9 \-]", " ", query).split())
        embedded = [t for t in pool if t.get("embedding") is not None]
        if clip is not None and query and embedded:
            subject = "vehicle" if wants_vehicle else "person"
            text_vec = clip.embed_text(f"a cctv photo of a {subject} {query}" if subject not in query else f"a cctv photo of {query}")
            base_vec = clip.embed_text(f"a cctv photo of a {subject}")
            sims = np.array([float(t["embedding"] @ text_vec - t["embedding"] @ base_vec) for t in embedded])
            if sims.size > 1 and sims.std() > 1e-6:
                z = (sims - sims.mean()) / sims.std()
                for t, zi in zip(embedded, z):
                    scores[t["track_id"]] += 0.6 * float(zi)
                    if zi > 1.0:
                        reasons[t["track_id"]].append("strong visual match")
                used_clip = True

        # 3) local vision-model captions, when present
        used_captions = False
        for t in pool:
            cap = set(re.findall(r"[a-z\-]+", (t.get("caption") or "").lower()))   # whole words only
            hits = [w for w in words if len(w) > 2 and w in cap and w not in PERSON_WORDS
                    and w not in {"their", "they", "the", "and", "with", "wearing", "anyone", "someone"}]
            if hits:
                used_captions = True
                scores[t["track_id"]] += 0.75 * len(hits)     # one specific word ("mask") is enough
                reasons[t["track_id"]].append("caption mentions " + ", ".join(sorted(hits)))

        ranked = sorted(pool, key=lambda t: scores[t["track_id"]], reverse=True)
        top = [t for t in ranked if scores[t["track_id"]] > 0.5][:4]
        if not top:
            text = ("I found no tracked " + ("vehicle" if wants_vehicle else "person") + " whose stored appearance clearly "
                    "matches that description. Colours are measured from small CCTV crops and lighting changes them, "
                    "so check the closest candidates below.")
            return text, [self.ev_track(t) for t in ranked[:3]]
        lines = []
        for t in top:
            why = ", ".join(dict.fromkeys(reasons[t["track_id"]])) or "visual similarity"
            lines.append(f"{self.label(t)} ({clock(t['first_offset_ms'])}–{clock(t['last_offset_ms'])}; {why}"
                         + (f'; described as "{t["caption"]}"' if t.get("caption") else "") + ")")
        parts = (["pixel colour statistics"] if colors else []) + (["CLIP visual similarity"] if used_clip else []) \
            + (["local vision-model descriptions"] if used_captions else [])
        method = " and ".join(parts) or "visual similarity"
        text = (f"Best candidates, ranked by {method}: " + "; ".join(lines) + ". "
                "These are likely matches to verify in the clip, not confirmed identifications.")
        return text, [self.ev_track(t) for t in top]

    def together(self) -> tuple[str, list[dict]]:
        groups = [e for e in self.events if e["kind"] == "together"]
        if not groups:
            return ("I found no pair of tracked people who stayed close together while moving for at least 4 seconds. "
                    "Brief passes and people standing still are not counted as being together.", [])
        groups.sort(key=lambda e: e["end_offset_ms"] - e["start_offset_ms"], reverse=True)
        text = f"I found {len(groups)} case(s) of people walking close together: " + "; ".join(
            f"{e['description']} from {clock(e['start_offset_ms'])}" for e in groups[:6]) + ". "
        text += "Being close in the image suggests, but does not prove, that they were together."
        return text, [self.ev_event(e) for e in groups[:6]]

    def lingering(self) -> tuple[str, list[dict]]:
        found = [e for e in self.events if e["kind"] == "lingering"]
        still = [t for t in self.people if t["stationary"] and not any(t["track_id"] in e["track_ids"] for e in found)]
        if not found and not still:
            return "No tracked person stayed in one area for 15 seconds or more.", []
        parts = [e["description"] + f" (from {clock(e['start_offset_ms'])})" for e in found]
        parts += [f"{self.label(t)} stayed in the {t['entry_region']} for "
                  f"{(t['last_offset_ms'] - t['first_offset_ms']) / 1000:.0f} s" for t in still]
        text = "People who stayed in place: " + "; ".join(parts[:6]) + ". Staying in place is not by itself suspicious."
        return text, [self.ev_event(e) for e in found[:4]] + [self.ev_track(t) for t in still[:3]]

    def busiest(self) -> tuple[str, list[dict]]:
        e = next((e for e in self.events if e["kind"] == "busiest"), None)
        if not e:
            return "At no point were two or more tracked people visible at the same time.", []
        text = (f"The busiest moment is {self.when(e['start_offset_ms'])}, with {len(e['track_ids'])} people in view: "
                + ", ".join(f"Person {i}" for i in e["track_ids"]) + ".")
        return text, [self.ev_event(e)] + [self.ev_track(self.by_id[i], offset=e["start_offset_ms"]) for i in e["track_ids"][:5] if i in self.by_id]

    def vehicles_answer(self) -> tuple[str, list[dict]]:
        if not self.vehicles:
            return "No vehicles were tracked in this recording.", []
        parked = [t for t in self.vehicles if t["stationary"]]
        moving = [t for t in self.vehicles if not t["stationary"]]
        parts = []
        if parked:
            parts.append(f"{len(parked)} stayed in place: " + ", ".join(
                f"{self.label(t)}" + (f" ({self.looks(t)})" if self.looks(t) else "") + f" in the {t['entry_region']}" for t in parked[:6]))
        if moving:
            parts.append(f"{len(moving)} moved: " + ", ".join(
                f"{self.label(t)} {t['direction']} at {clock(t['first_offset_ms'])}" for t in moving[:6]))
        return (f"I tracked {len(self.vehicles)} vehicle(s). " + "; ".join(parts) + ".",
                [self.ev_track(t) for t in (moving + parked)[:8]])

    def movement(self) -> tuple[str, list[dict]]:
        movers = [t for t in self.people if not t["stationary"]]
        if not movers:
            return "No tracked person moved a noticeable distance.", []
        movers.sort(key=lambda t: t["first_offset_ms"])
        lines = [f"{self.label(t)} at {clock(t['first_offset_ms'])}: from {t['entry_edge'] or 'the ' + t['entry_region']} "
                 f"to {t['exit_edge'] or 'the ' + t['exit_region']}, {t['direction']}" for t in movers[:8]]
        text = "Movement of tracked people (image directions, not compass directions): " + "; ".join(lines) + "."
        if len(movers) > 8:
            text += f" {len(movers) - 8} more are listed on the Tracking page."
        return text, [self.ev_track(t, offset=t["first_offset_ms"]) for t in movers[:8]]

    def summary(self, prefix: str = "") -> tuple[str, list[dict]]:
        meta = self.rec.get("analysis_meta") or {}
        dur = meta.get("duration_s")
        text = prefix + (f"This {dur:.0f}-second recording from {self.rec['camera_name']} " if dur else
                         f"This recording from {self.rec['camera_name']} ")
        np_, nv = len(self.people), len(self.vehicles)
        text += (f"contains {np_} tracked {'person' if np_ == 1 else 'people'} and "
                 f"{nv} tracked {'vehicle' if nv == 1 else 'vehicles'}. ")
        evidence = []
        busiest = next((e for e in self.events if e["kind"] == "busiest"), None)
        if busiest:
            text += f"The busiest moment is {clock(busiest['start_offset_ms'])} with {len(busiest['track_ids'])} people in view. "
            evidence.append(self.ev_event(busiest))
        notable = [e for e in self.events if e["kind"] in {"together", "lingering"}]
        if notable:
            text += "Notable: " + "; ".join(f"{e['description']} at {clock(e['start_offset_ms'])}" for e in notable[:4]) + ". "
            evidence += [self.ev_event(e) for e in notable[:4]]
        parked = [e for e in self.events if e["kind"] == "parked"]
        if parked:
            text += f"{len(parked)} vehicle(s) stay parked throughout. "
        if self.captions:
            text += "Scene captions from the local vision model: " + " ".join(
                f'[{clock(c["offset_ms"])}] {c["caption"]}' for c in self.captions[:3]) + " "
            evidence += [self.ev_moment(c["offset_ms"], "scene caption") for c in self.captions[:3]]
        evidence += [self.ev_track(t) for t in self._top_people(3)]
        text += ("You can ask about a specific person (\"describe person 2\"), a time (\"what is visible at 0:30\"), "
                 "clothing (\"anyone in a red top\"), groups, vehicles or people who stayed in place.")
        return text.strip(), evidence
