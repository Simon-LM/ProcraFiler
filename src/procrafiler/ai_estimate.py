"""How many AI calls a run will cost — answered before spending anything.

The app asks the user to trust it with their documents and their API budget, then
starts calling. A batch of a few dozen photos is tens of paid calls, and until now
nothing said so in advance: the only warning was "this may take a while and use
AI" past a threshold, with no number.

**Almost no file is opened.** The estimate comes from extensions and the
configured task chains alone, so it is instant even on a large Inbox and can be
printed before a run starts.

The **one exception is audio and video**, which are probed with ffprobe — a local,
free, millisecond read of the container header. The exception is worth making: a
five-second clip and a two-hour recording differ by a factor of a thousand in what
they cost, and an estimate that could not tell them apart would be useless exactly
where the money is. Everything else is still costed from its extension alone.

That precision limit is real and stated rather than hidden: a PDF may
carry a text layer (free, read locally) or be a scan (one OCR call), and a photo
costs one vision call plus a second OCR call **only if** it turns out to be a
photographed document. So the answer is a RANGE, and its two ends are honest.

**A call is not a cost.** The count says how much work the run is, not how much
money it is: an Ollama call runs on the user's own machine and is worth nothing,
while a Mistral one is billed. Counting them alike — as this module first did —
shows someone running fully locally a number that reads like a bill and is in fact
zero. So each task is attributed to the provider that will actually serve it, and
the billable share is stated separately.

What this module still does NOT do is turn calls into money. Prices are per
million tokens, not per call, and the token weight of an image depends on its
resolution; converting here would mean inventing a figure. Real consumption is
measured instead, per call, by `usage_meter`.

Two things it deliberately does not deduct, both of which make the real cost
lower, never higher:

- duplicates already in the library are trashed before any AI call;
- a task with no provider chain configured is a no-op, which IS accounted for —
  a run with no NAMING chain is not charged for a naming pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from procrafiler.ai_naming import task_chain_from_env  # type: ignore[reportMissingImports]
from procrafiler.av_reader import MAX_FRAMES_HARD_CAP, max_transcribe_seconds  # type: ignore[reportMissingImports]
from procrafiler.frame_sampling import frame_budget  # type: ignore[reportMissingImports]
from procrafiler.media_tools import probe_media  # type: ignore[reportMissingImports]
from procrafiler.taxonomy import dispatch_for_filename  # type: ignore[reportMissingImports]

# Providers that charge for a call. Everything else runs on the user's own
# hardware; kept as a set so a second paid provider is one entry, not a rewrite.
_BILLED_PROVIDERS = {"mistral"}

# Media types whose text is extracted locally, with no AI call at all.
_LOCAL_MEDIA = {"text", "office"}
# A PDF is free when it has a text layer and costs one OCR call when it is a scan.
# Which one it is cannot be known without opening it, hence the range.
_MAYBE_OCR_MEDIA = {"pdf"}
# An image always costs a vision call, and a second (OCR) one when the vision
# model reports it is a photographed document.
_VISION_MEDIA = {"image"}
# Audio and video are the one case where the file MUST be opened to be costed —
# see the note on probing in `estimate_ai_calls`.
_AV_MEDIA = {"video", "audio"}
# Every task that can produce a call during a run, so each one's provider is looked
# up exactly once instead of re-reading the environment per property access.
_TASKS = ("IMAGE", "OCR", "ANALYSIS", "NAMING", "ORGANIZE", "TRANSCRIBE", "VIDEO")


@dataclass(frozen=True)
class AICallEstimate:
    files: int = 0
    folder_sets: int = 0
    local_reads: int = 0
    maybe_ocr_reads: int = 0
    vision_reads: int = 0
    unreadable: int = 0
    analyses: int = 0
    naming_passes: int = 0
    organize_passes: int = 0
    # A photographed document is re-read with OCR — which cannot happen at all
    # when no OCR chain is configured, so the upper bound must not charge for it.
    ocr_available: bool = True
    # Audio and video. `audio_seconds` is the one quantity in this whole estimate
    # that is EXACT rather than inferred: ffprobe reports the true duration, and
    # transcription is billed by duration. Frames and passes are counts of calls
    # like any other.
    av_files: int = 0
    audio_seconds: int = 0
    transcribe_calls: int = 0
    highlight_calls: int = 0
    av_frames: int = 0
    # Task names whose PRIMARY provider bills. The primary rather than the whole
    # chain: a paid fallback behind a local primary only fires when the local one
    # fails, and quoting a run at the price of its worst case would cry wolf on
    # every run. When it does fire, `usage_meter` reports the truth afterwards.
    billed_tasks: frozenset[str] = field(default_factory=frozenset)

    def _billed(self, task: str, count: int) -> int:
        return count if task in self.billed_tasks else 0

    @property
    def minimum(self) -> int:
        """Every PDF has a text layer, and no photo is a document."""
        return (
            self.vision_reads
            + self.analyses
            + self.naming_passes
            + self.organize_passes
            + self.transcribe_calls
            + self.highlight_calls
            + self.av_frames
        )

    @property
    def maximum(self) -> int:
        """Every PDF is a scan, and every photo turns out to be a document."""
        confirms = self.vision_reads if self.ocr_available else 0
        return self.minimum + self.maybe_ocr_reads + confirms

    @property
    def billed_minimum(self) -> int:
        return (
            self._billed("IMAGE", self.vision_reads)
            + self._billed("IMAGE", self.av_frames)
            + self._billed("TRANSCRIBE", self.transcribe_calls)
            + self._billed("VIDEO", self.highlight_calls)
            + self._billed("ANALYSIS", self.analyses)
            + self._billed("NAMING", self.naming_passes)
            + self._billed("ORGANIZE", self.organize_passes)
        )

    @property
    def billed_maximum(self) -> int:
        confirms = self.vision_reads if self.ocr_available else 0
        return (
            self.billed_minimum
            + self._billed("OCR", self.maybe_ocr_reads)
            + self._billed("OCR", confirms)
        )

    def calls_by_task(self) -> dict[str, tuple[int, int]]:
        """Per task, the (fewest, most) calls this run can make. The single place
        that knows which uncertainty belongs to which task — a PDF's OCR call and a
        photo's OCR confirmation are both "maybe", and both land on OCR."""
        confirms = self.vision_reads if self.ocr_available else 0
        return {
            "IMAGE": (self.vision_reads + self.av_frames, self.vision_reads + self.av_frames),
            "OCR": (0, self.maybe_ocr_reads + confirms),
            "TRANSCRIBE": (self.transcribe_calls, self.transcribe_calls),
            "VIDEO": (self.highlight_calls, self.highlight_calls),
            "ANALYSIS": (self.analyses, self.analyses),
            "NAMING": (self.naming_passes, self.naming_passes),
            "ORGANIZE": (self.organize_passes, self.organize_passes),
        }

    @property
    def is_free(self) -> bool:
        """True when nothing in this run can be billed — every configured task is
        served locally. The one case where a call count must not be shown as if it
        were a budget."""
        return self.billed_maximum == 0


def estimate_ai_calls(work_sets: list[tuple[str, list[Path]]]) -> AICallEstimate:
    """Cost of processing `work_sets`, in AI calls, without opening anything.

    `work_sets` is what the pipeline builds: `(top_level_folder, members)`, where
    an empty folder name means a loose file at the Inbox root. Naming and organize
    run **once per dropped folder** — never for loose files, which are not a set —
    and only when their chain is configured.
    """
    files = local = maybe_ocr = vision = unreadable = 0
    folder_sets = 0
    av_files = audio_seconds = transcribe_calls = highlight_calls = av_frames = 0

    for set_top, members in work_sets:
        if set_top:
            folder_sets += 1
        for member in members:
            files += 1
            dispatch = dispatch_for_filename(member.name)
            if not dispatch.can_dispatch:
                unreadable += 1
            elif dispatch.media_type in _VISION_MEDIA:
                vision += 1
            elif dispatch.media_type in _MAYBE_OCR_MEDIA:
                maybe_ocr += 1
            elif dispatch.media_type in _LOCAL_MEDIA:
                local += 1
            elif dispatch.media_type in _AV_MEDIA:
                probe = probe_media(member)
                if not probe.ok or probe.duration_seconds <= 0:
                    unreadable += 1
                    continue
                av_files += 1
                if probe.has_audio:
                    transcribe_calls += 1
                    audio_seconds += int(min(probe.duration_seconds, max_transcribe_seconds()))
                if probe.has_video:
                    frames = min(frame_budget(probe.duration_seconds), MAX_FRAMES_HARD_CAP)
                    av_frames += frames
                    # The passage-selection pass only happens when there is speech
                    # to read, and whether there IS speech cannot be known without
                    # transcribing. Counted whenever there is an audio track: it is
                    # one cheap text call, and over-stating by one beats a surprise.
                    if probe.has_audio and frames > 2:
                        highlight_calls += 1
            else:
                # archive and anything else: no reader, so no call.
                unreadable += 1

    # Every task is charged only when its own chain is configured: with no chain
    # the call simply never happens, and the file lands in manual review for free.
    # Gating each one separately matters — an install with vision but no OCR pays
    # for the image read and never for the confirmation.
    chains = {task: task_chain_from_env(task) for task in _TASKS}
    if not chains["IMAGE"]:
        vision = 0
    ocr_available = bool(chains["OCR"])
    if not ocr_available:
        maybe_ocr = 0
    # A file that cannot be read never reaches the analysis: it goes to manual
    # review, free of charge.
    if not chains["TRANSCRIBE"]:
        transcribe_calls = audio_seconds = 0
        highlight_calls = 0  # nothing to select passages from
    if not chains["VIDEO"]:
        highlight_calls = 0
    if not chains["IMAGE"]:
        av_frames = 0
    analyses = files - unreadable if chains["ANALYSIS"] else 0
    naming = folder_sets if chains["NAMING"] else 0
    organize = folder_sets if chains["ORGANIZE"] else 0

    return AICallEstimate(
        billed_tasks=frozenset(
            task
            for task, entries in chains.items()
            if entries and entries[0].provider in _BILLED_PROVIDERS
        ),
        av_files=av_files,
        audio_seconds=audio_seconds,
        transcribe_calls=transcribe_calls,
        highlight_calls=highlight_calls,
        av_frames=av_frames,
        files=files,
        folder_sets=folder_sets,
        local_reads=local,
        maybe_ocr_reads=maybe_ocr,
        vision_reads=vision,
        unreadable=unreadable,
        analyses=analyses,
        naming_passes=naming,
        organize_passes=organize,
        ocr_available=ocr_available,
    )


def format_estimate(estimate: AICallEstimate) -> str:
    """One line when the answer is exact, a short breakdown when it is a range."""
    if estimate.files == 0:
        return "Nothing to process."
    if estimate.minimum == 0 and estimate.maximum == 0:
        return f"{estimate.files} file(s) — no AI call (no provider chain configured)."

    parts: list[str] = []
    if estimate.vision_reads:
        parts.append(f"{estimate.vision_reads} image read(s)")
    if estimate.av_files:
        # Never "0 min" for a real recording about to be paid for: a short clip
        # is shown in seconds, so the figure always matches something billable.
        seconds = estimate.audio_seconds
        length = f"{seconds}s" if seconds < 60 else f"{seconds // 60} min"
        detail = f"{estimate.av_files} audio/video"
        if estimate.transcribe_calls:
            detail += f" ({length} to transcribe"
            if estimate.av_frames:
                detail += f", {estimate.av_frames} frame(s) sampled"
            detail += ")"
        elif estimate.av_frames:
            detail += f" ({estimate.av_frames} frame(s) sampled)"
        parts.append(detail)
    if estimate.maybe_ocr_reads:
        parts.append(f"{estimate.maybe_ocr_reads} PDF(s), OCR only if scanned")
    if estimate.analyses:
        parts.append(f"{estimate.analyses} analysis")
    if estimate.naming_passes:
        parts.append(f"{estimate.naming_passes} naming")
    if estimate.organize_passes:
        parts.append(f"{estimate.organize_passes} organize")

    if estimate.minimum == estimate.maximum:
        head = f"≈ {estimate.minimum} AI call(s)"
    else:
        head = f"≈ {estimate.minimum} to {estimate.maximum} AI call(s)"

    line = f"{head} for {estimate.files} file(s): " + ", ".join(parts) + "."

    # The billable share, stated separately — a number of calls means something
    # very different depending on who serves them.
    if estimate.is_free:
        line += " All of them run locally: nothing is billed."
    elif estimate.billed_maximum < estimate.maximum:
        if estimate.billed_minimum == estimate.billed_maximum:
            line += f" Of these, {estimate.billed_maximum} are billed; the rest run locally."
        else:
            line += (
                f" Of these, {estimate.billed_minimum} to {estimate.billed_maximum} "
                "are billed; the rest run locally."
            )

    extras: list[str] = []
    if estimate.local_reads:
        extras.append(f"{estimate.local_reads} read locally, free")
    if estimate.unreadable:
        extras.append(f"{estimate.unreadable} to manual review, free")
    if extras:
        line += " (" + "; ".join(extras) + ")"
    if estimate.minimum != estimate.maximum:
        line += " Duplicates already filed cost nothing and are not deducted here."
    return line
