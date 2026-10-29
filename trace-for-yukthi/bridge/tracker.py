"""Persistent object tracker for uploaded-video analysis (CPU, numpy only).

A port of the production BoT-SORT-style tracker in ``ai_rocm/airocm.py``
(constant-velocity Kalman on cx/cy/w/h + ByteTrack two-stage association + a
lost-track buffer), without the GPU OSNet embedder. Uploaded videos are sampled
at a few frames per second, so boxes move further between samples than in the
live 8 fps pipeline; a third association stage therefore re-finds lost tracks
by predicted-centre distance, gated by a cheap colour-histogram similarity so
two people crossing do not swap identities.
"""

from __future__ import annotations

import numpy as np


class _KalmanBox:
    SP, SV = 1 / 20, 1 / 160

    def __init__(self, cx, cy, w, h):
        self.x = np.array([cx, cy, w, h, 0, 0, 0, 0], np.float64)
        sp, sv = self.SP, self.SV
        std = [2 * sp * w, 2 * sp * h, 2 * sp * w, 2 * sp * h,
               10 * sv * w, 10 * sv * h, 10 * sv * w, 10 * sv * h]
        self.P = np.diag(np.square(std))
        self.F = np.eye(8)
        self.F[:4, 4:] = np.eye(4)
        self.H = np.eye(4, 8)

    def predict(self, damp=False):
        if damp:
            self.x[4:] *= 0.8
        w, h = max(self.x[2], 1.0), max(self.x[3], 1.0)
        sp, sv = self.SP, self.SV
        q = [sp * w, sp * h, sp * w, sp * h, sv * w, sv * h, sv * w, sv * h]
        self.x = self.F @ self.x
        self.P = self.F @ self.P @ self.F.T + np.diag(np.square(q))

    def correct(self, cx, cy, w, h):
        sp = self.SP
        mw, mh = max(self.x[2], 1.0), max(self.x[3], 1.0)
        R = np.diag(np.square([sp * mw, sp * mh, sp * mw, sp * mh]))
        z = np.array([cx, cy, w, h], np.float64)
        S = self.H @ self.P @ self.H.T + R
        K = self.P @ self.H.T @ np.linalg.inv(S)
        self.x = self.x + K @ (z - self.H @ self.x)
        self.P = (np.eye(8) - K @ self.H) @ self.P

    def tlbr(self):
        cx, cy, w, h = self.x[:4]
        return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


class _Track:
    __slots__ = ("id", "kf", "cls", "score", "lost", "hits", "hist")

    def __init__(self, tid, box, cls, score, hist):
        x1, y1, x2, y2 = box
        self.kf = _KalmanBox((x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1)
        self.id, self.cls, self.score = tid, cls, score
        self.lost, self.hits = 0, 1
        self.hist = hist


def color_hist(frame: np.ndarray | None, box) -> np.ndarray | None:
    """Normalised 8x4 hue/saturation histogram of a box (BGR uint8 frame)."""
    if frame is None:
        return None
    h, w = frame.shape[:2]
    x1, y1 = max(0, int(box[0])), max(0, int(box[1]))
    x2, y2 = min(w, int(box[2])), min(h, int(box[3]))
    if x2 - x1 < 4 or y2 - y1 < 8:
        return None
    import cv2
    hsv = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [8, 4], [0, 180, 0, 256]).flatten()
    total = hist.sum()
    return hist / total if total else None


def _hist_sim(a, b) -> float:
    if a is None or b is None:
        return 0.5
    return float(np.minimum(a, b).sum())


class Tracker:
    TRACK_HIGH = 0.35      # dets >= this join stage 1
    TRACK_LOW = 0.15       # dets in [low, high) may rescue an active track
    NEW_TRACK = 0.45       # unmatched dets >= this start a track
    MATCH_FUSED = 0.3      # stage 1 floor on iou * score (low fps => looser)
    MATCH_IOU2 = 0.3
    REFIND_DIST = 0.8      # stage 3: centre distance in units of box height
    REFIND_HIST = 0.55     # stage 3: minimum colour-histogram overlap

    def __init__(self, fps: float):
        self.buffer = max(4, int(round(fps * 6)))     # keep lost tracks ~6 s
        self.active: list[_Track] = []
        self.lost: list[_Track] = []
        self.next_id = 1

    @staticmethod
    def _iou_matrix(tracks, boxes):
        if not tracks or not boxes:
            return np.zeros((len(tracks), len(boxes)))
        t = np.array([tr.kf.tlbr() for tr in tracks])
        d = np.array(boxes, np.float64)
        x1 = np.maximum(t[:, None, 0], d[None, :, 0])
        y1 = np.maximum(t[:, None, 1], d[None, :, 1])
        x2 = np.minimum(t[:, None, 2], d[None, :, 2])
        y2 = np.minimum(t[:, None, 3], d[None, :, 3])
        inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
        at = (t[:, 2] - t[:, 0]) * (t[:, 3] - t[:, 1])
        ad = (d[:, 2] - d[:, 0]) * (d[:, 3] - d[:, 1])
        union = at[:, None] + ad[None, :] - inter
        return np.where(union > 0, inter / union, 0.0)

    @staticmethod
    def _greedy(cost, floor):
        pairs = []
        if cost.size:
            order = np.dstack(np.unravel_index(np.argsort(cost, axis=None)[::-1], cost.shape))[0]
            used_t, used_d = set(), set()
            for ti, di in order:
                if cost[ti, di] < floor:
                    break
                if ti in used_t or di in used_d:
                    continue
                used_t.add(ti)
                used_d.add(di)
                pairs.append((int(ti), int(di)))
        return pairs

    def _commit(self, tr, det, hist, out, i):
        box = det[0]
        tr.kf.correct((box[0] + box[2]) / 2, (box[1] + box[3]) / 2, box[2] - box[0], box[3] - box[1])
        tr.score, tr.lost, tr.hits = det[2], 0, tr.hits + 1
        if hist is not None:
            tr.hist = hist if tr.hist is None else 0.8 * tr.hist + 0.2 * hist
        out[i] = tr.id

    def _assign(self, tracks, idx, dets, hists, out, fused):
        boxes = [dets[i][0] for i in idx]
        iou = self._iou_matrix(tracks, boxes)
        for r, tr in enumerate(tracks):
            for c, i in enumerate(idx):
                if dets[i][1] != tr.cls:
                    iou[r, c] = 0.0
                elif fused:
                    # appearance keeps crossing people apart
                    iou[r, c] *= 0.6 + 0.4 * _hist_sim(tr.hist, hists[i])
        if fused:
            cost = iou * np.array([dets[i][2] for i in idx])[None, :] if idx else iou
            floor = self.MATCH_FUSED
        else:
            cost, floor = iou, self.MATCH_IOU2
        matched_t, matched_d = set(), set()
        for ti, ci in self._greedy(cost, floor):
            self._commit(tracks[ti], dets[idx[ci]], hists[idx[ci]], out, idx[ci])
            matched_t.add(ti)
            matched_d.add(ci)
        return ([t for k, t in enumerate(tracks) if k not in matched_t],
                [i for k, i in enumerate(idx) if k not in matched_d])

    def _refind(self, tracks, idx, dets, hists, out):
        """Distance + appearance match for tracks whose IoU fell to zero."""
        if not tracks or not idx:
            return tracks, idx
        cost = np.zeros((len(tracks), len(idx)))
        for r, tr in enumerate(tracks):
            tx1, ty1, tx2, ty2 = tr.kf.tlbr()
            tcx, tcy, th = (tx1 + tx2) / 2, (ty1 + ty2) / 2, max(ty2 - ty1, 1.0)
            for c, i in enumerate(idx):
                if dets[i][1] != tr.cls:
                    continue
                b = dets[i][0]
                dist = np.hypot((b[0] + b[2]) / 2 - tcx, (b[1] + b[3]) / 2 - tcy) / th
                reach = self.REFIND_DIST * (1 + 0.15 * min(tr.lost, 10))
                sim = _hist_sim(tr.hist, hists[i])
                bh = max(b[3] - b[1], 1.0)
                if dist > reach or sim < self.REFIND_HIST or not 0.5 < bh / th < 2.0:
                    continue
                cost[r, c] = (1 - dist / reach) * 0.5 + sim * 0.5
        matched_t, matched_d = set(), set()
        for ti, ci in self._greedy(cost, 0.3):
            self._commit(tracks[ti], dets[idx[ci]], hists[idx[ci]], out, idx[ci])
            matched_t.add(ti)
            matched_d.add(ci)
        return ([t for k, t in enumerate(tracks) if k not in matched_t],
                [i for k, i in enumerate(idx) if k not in matched_d])

    def update(self, dets, frame=None):
        """dets: [(box(x1,y1,x2,y2), cls, score)] -> {det_index: track_id}"""
        for tr in self.active + self.lost:
            tr.kf.predict(damp=tr.lost > 0)
        hists = [color_hist(frame, d[0]) for d in dets]
        out: dict[int, int] = {}
        high = [i for i, d in enumerate(dets) if d[2] >= self.TRACK_HIGH]
        low = [i for i, d in enumerate(dets) if self.TRACK_LOW <= d[2] < self.TRACK_HIGH]
        pool = self.active + self.lost
        un_tracks, un_high = self._assign(pool, high, dets, hists, out, True)
        un_active = [t for t in un_tracks if t in self.active]
        un_active, _ = self._assign(un_active, low, dets, hists, out, False)
        un_tracks = [t for t in un_tracks if t.id not in set(out.values())]
        _, un_high = self._refind(un_tracks, un_high, dets, hists, out)

        matched = set(out.values())
        survivors, still_lost = [], []
        for tr in pool:
            if tr.id in matched:
                survivors.append(tr)
            else:
                tr.lost += 1
                if tr.lost <= self.buffer:
                    still_lost.append(tr)
        for i in un_high:
            if dets[i][2] >= self.NEW_TRACK:
                tr = _Track(self.next_id, dets[i][0], dets[i][1], dets[i][2], hists[i])
                self.next_id += 1
                survivors.append(tr)
                out[i] = tr.id
        self.active, self.lost = survivors, still_lost
        return out
