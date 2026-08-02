"""Which moments of a video are worth paying to look at.

Every frame sent to the vision model is a billed call, so the question is not
"how do we cover the video" but "what is the smallest set of stills that lets an
AI say what this file *is*". Filing a document does not need to understand a
video; it needs to recognise it.

Three rules, in order of authority.

**The ends always count.** A video is very often identifiable from its opening and
its last shot alone — a slate, a subject walking into frame, a signed document
held up at the end. Those two are reserved before anything else, and they are the
whole budget on a short clip. Neither sits exactly at 0 or at the duration: the
first instants are frequently black or a fade, and seeking to the very last frame
is unreliable across containers.

**The middle goes to what was said.** The transcript's timestamps say where the
content is, so the remaining budget is spent there rather than on a blind grid.
This is the whole point of transcribing first — speech is cheap per minute, vision
is not.

**Nothing clusters.** Highlights arrive in the order the model ranked them, and
the best three moments of a thirty-minute video are frequently ten seconds apart
in the same passage. A minimum spacing rejects a candidate too close to one already
taken, so the budget buys coverage instead of three views of one wall.

With no transcript at all — a silent video, music only, a failed transcription —
the middle is filled by an even spread. That is the honest fallback: no
information about where to look means no reason to prefer one moment over another.
"""

from __future__ import annotations

from dataclasses import dataclass

# How many stills a video earns, by length. A table rather than a formula: the
# thresholds are a judgement about diminishing returns, and a table can be read,
# argued with and adjusted without re-deriving anything.
#
# The ceiling is what keeps a four-hour recording from becoming an expensive
# mistake — past a point, more stills of the same scene add nothing a filing
# decision can use.
_FRAME_BUDGET: tuple[tuple[float, int], ...] = (
    (30, 3),        # under 30 s — the ends plus one look inside
    (2 * 60, 4),
    (5 * 60, 5),
    (15 * 60, 7),
    (30 * 60, 9),
    (float("inf"), 12),
)

# The spacing floor scales with the video: on a thirty-minute recording, two
# stills eight seconds apart are the same shot twice. Half the even spacing
# (duration / budget / 2) is the natural scale — it lets a genuinely distinct
# moment through while rejecting a second look at the same passage. A fixed
# number of seconds cannot do this: 2 s is far too tight for a long video and too
# wide for a ten-second clip.
_MIN_GAP_FLOOR = 0.4

# How far in from each end the bookend stills are taken. Small ABSOLUTE offsets,
# not fractions of the duration — and that distinction was learned the hard way.
#
# These were originally 2% and 3% of the duration, capped at 5 seconds. On a
# 36-minute interview that put the closing still at 2164.6 s; the end card naming
# the organisation that produced the film appeared at ~2166 s. The one frame in the
# whole recording that identified the publisher was missed by under two seconds,
# and nothing else in 36 minutes said who it was.
#
# The offsets exist to clear a fade-in and to survive a container whose very last
# frames are not seekable. Both of those are measured in fractions of a second, so
# they do not scale with the length of the film — title cards and end cards do not
# get later because the video is longer.
_HEAD_INSET = 1.0
_TAIL_INSET = 1.5
# On a clip too short to afford those offsets, fall back to a fraction of it.
_EDGE_RATIO_CAP = 0.05


# Below this many differing bits out of 64, two frames are the same shot and the
# second one buys nothing. Measured on a filmed interview: the eight near-identical
# library shots sat at 1–5 bits from one another, while the four genuinely distinct
# scenes were 16–39 apart. The threshold sits in a wide empty gap, which is what
# makes it a safe number rather than a tuned one.
MIN_DISTINCT_BITS = 8


def select_distinct(hashes: list[int | None], *, min_bits: int = MIN_DISTINCT_BITS) -> list[int]:
    """Indices of the frames worth paying to look at, in order.

    A talking-head interview is visually static: sampling it every three minutes
    yields the same shot a dozen times, and each one is a billed vision call
    describing the same man in the same chair. Spacing frames in TIME does not
    prevent that — only comparing what they show does.

    Compared against every frame already kept, not just the previous one, so a
    scene that alternates A-B-A-B does not slip through. A frame whose hash could
    not be computed is KEPT: failing to hash it is not evidence that it is a
    duplicate, and dropping a frame we know nothing about would silently lose
    content.
    """
    kept: list[int] = []
    for index, value in enumerate(hashes):
        if value is None:
            kept.append(index)
            continue
        if all(
            hashes[other] is None
            or bin(value ^ (hashes[other] or 0)).count("1") >= min_bits
            for other in kept
        ):
            kept.append(index)
    return kept


@dataclass(frozen=True)
class Highlight:
    """A moment the transcript pass judged worth looking at."""

    start: float
    end: float = 0.0
    reason: str = ""

    @property
    def midpoint(self) -> float:
        """The middle of a spoken passage, not its first instant: someone starts
        describing a thing slightly before showing it."""
        if self.end > self.start:
            return (self.start + self.end) / 2.0
        return self.start


def frame_budget(duration_seconds: float) -> int:
    """How many stills a video of this length is worth."""
    if duration_seconds <= 0:
        return 0
    for threshold, count in _FRAME_BUDGET:
        if duration_seconds < threshold:
            return count
    return _FRAME_BUDGET[-1][1]  # pragma: no cover - the last threshold is inf


def _edges(duration: float) -> tuple[float, float]:
    """The two bookend timestamps: just inside the opening, just inside the close."""
    head = min(_HEAD_INSET, duration * _EDGE_RATIO_CAP)
    tail = max(duration - _TAIL_INSET, duration * (1 - _EDGE_RATIO_CAP))
    return (round(head, 3), round(min(tail, max(duration - 0.05, 0.0)), 3))


def _accept(candidate: float, taken: list[float], min_gap: float, duration: float) -> bool:
    if candidate < 0 or candidate > duration:
        return False
    return all(abs(candidate - already) >= min_gap for already in taken)


def plan_frame_timestamps(
    duration_seconds: float,
    highlights: list[Highlight] | None = None,
    *,
    budget: int | None = None,
) -> list[float]:
    """The moments to extract, in chronological order.

    Deterministic: the same inputs always give the same plan, so a run can be
    reasoned about and a test can pin it.
    """
    duration = max(0.0, float(duration_seconds))
    if duration <= 0:
        return []
    total = budget if budget is not None else frame_budget(duration)
    if total <= 0:
        return []

    min_gap = max(_MIN_GAP_FLOOR, duration / total / 2)

    head, tail = _edges(duration)
    taken: list[float] = [head]
    if total > 1 and _accept(tail, taken, min_gap, duration):
        taken.append(tail)

    # The middle, from what was actually said — in the order the model ranked them,
    # so the budget is spent on the best moments first.
    for highlight in highlights or []:
        if len(taken) >= total:
            break
        candidate = round(highlight.midpoint, 3)
        if _accept(candidate, taken, min_gap, duration):
            taken.append(candidate)

    # Whatever the transcript could not fill, spread evenly. This is the entire
    # middle for a silent video, and the tail end of the budget for a talkative
    # one whose highlights all sat in the same passage.
    if len(taken) < total:
        slots = total + 1
        for index in range(1, slots):
            if len(taken) >= total:
                break
            candidate = round(duration * index / slots, 3)
            if _accept(candidate, taken, min_gap, duration):
                taken.append(candidate)

    return sorted(taken)
