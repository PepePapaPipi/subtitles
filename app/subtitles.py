"""Transcription, subtitle formatting and burning subtitles into video."""

import difflib
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from faster_whisper import WhisperModel

MODEL_SIZE = os.environ.get("WHISPER_MODEL", "small")
MAX_CHARS = 42      # max characters per subtitle cue
MAX_DURATION = 6.0  # max seconds per subtitle cue

# Called with a fraction between 0 and 1 while a long step runs.
Progress = Callable[[float], None]

_model = None


def get_model() -> WhisperModel:
    global _model
    if _model is None:
        _model = WhisperModel(MODEL_SIZE, device="auto", compute_type="int8")
    return _model


@dataclass
class Cue:
    start: float
    end: float
    text: str


def _whisper_words(video_path: Path, language: str | None, on_progress: Progress | None = None):
    """Run Whisper and return its segments (with word timings) and language.

    Whisper yields segments in order as it works through the audio, so the end
    time of the latest segment divided by the audio length is the progress.
    """
    segments, info = get_model().transcribe(
        str(video_path),
        language=language or None,
        word_timestamps=True,
        vad_filter=True,
    )
    result = []
    for segment in segments:
        result.append(segment)
        if on_progress and info.duration:
            on_progress(min(1.0, segment.end / info.duration))
    if on_progress:
        on_progress(1.0)
    return result, info.language


def transcribe(
    video_path: Path, language: str | None = None, on_progress: Progress | None = None
) -> tuple[list[Cue], str]:
    """Transcribe the audio of a video into short subtitle cues.

    Returns the cues and the detected (or given) language code.
    """
    segments, detected = _whisper_words(video_path, language, on_progress)
    cues: list[Cue] = []
    for segment in segments:
        if segment.words:
            words = [Word(w.word.strip(), w.start, w.end) for w in segment.words if w.word.strip()]
            cues.extend(group_words(words))
        elif segment.text.strip():
            cues.append(Cue(segment.start, segment.end, segment.text.strip()))
    return cues, detected


@dataclass
class Word:
    text: str
    start: float
    end: float
    line_end: bool = False  # last word of a line in the user's text


def group_words(words: list[Word]) -> list[Cue]:
    """Group timed words into cues short enough to read on screen.

    A cue ends at the end of a sentence, at a line break in the user's text,
    or when it would get too long to read.
    """
    cues: list[Cue] = []
    current: list[Word] = []
    for word in words:
        if current:
            text = " ".join(w.text for w in current + [word])
            too_long = len(text) > MAX_CHARS
            too_slow = word.end - current[0].start > MAX_DURATION
            if too_long or too_slow:
                cues.append(_cue_from_words(current))
                current = []
        current.append(word)
        if word.line_end or word.text[-1:] in ".?!":
            cues.append(_cue_from_words(current))
            current = []
    if current:
        cues.append(_cue_from_words(current))
    return cues


def _cue_from_words(words: list[Word]) -> Cue:
    return Cue(words[0].start, words[-1].end, " ".join(w.text for w in words))


# ---------- Subtitles from the user's own text ----------

def _normalize(word: str) -> str:
    return "".join(ch for ch in word.lower() if ch.isalnum())


def align_text(
    video_path: Path, text: str, language: str | None = None, on_progress: Progress | None = None
) -> tuple[list[Cue], str]:
    """Time the user's own text against the speech in the video.

    Whisper transcribes the audio with word timings, then each word of the
    user's text is matched to the transcribed words. Words Whisper got wrong
    or missed get times spread evenly between the matched words around them.
    The user's spelling, punctuation and line breaks are kept.
    """
    script: list[Word] = []
    for line in text.splitlines():
        tokens = line.split()
        for i, token in enumerate(tokens):
            script.append(Word(token, 0.0, 0.0, line_end=i == len(tokens) - 1))
    if not script:
        raise ValueError("The text file is empty.")

    segments, detected = _whisper_words(video_path, language, on_progress)
    heard = [w for s in segments for w in (s.words or []) if _normalize(w.word)]
    if not heard:
        raise ValueError("No speech was found in this video, so the text can't be timed.")

    a = [_normalize(w.text) for w in script]
    b = [_normalize(w.word) for w in heard]
    matched = 0
    for block in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        for k in range(block.size):
            word, h = script[block.a + k], heard[block.b + k]
            word.start, word.end = h.start, h.end
            matched += 1
    if matched < max(1, len(script) // 5):
        raise ValueError(
            "The text doesn't seem to match what is said in the video "
            f"(only {matched} of {len(script)} words were recognised). "
            "Check that it's the right file and language."
        )

    _fill_gaps(script, audio_end=heard[-1].end)
    return group_words(script), detected


def _fill_gaps(words: list[Word], audio_end: float) -> None:
    """Give unmatched words (end == 0) times between their matched neighbours."""
    i = 0
    while i < len(words):
        if words[i].end > 0:
            i += 1
            continue
        j = i
        while j < len(words) and words[j].end == 0:
            j += 1
        gap_start = words[i - 1].end if i > 0 else 0.0
        gap_end = words[j].start if j < len(words) else audio_end
        if gap_end <= gap_start:  # no room: squeeze them in right after
            gap_end = gap_start + 0.3 * (j - i)
        step = (gap_end - gap_start) / (j - i)
        for k in range(i, j):
            words[k].start = gap_start + step * (k - i)
            words[k].end = words[k].start + step
        i = j


# ---------- Ready-made subtitle files ----------

_TIME = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")


def _parse_time(value: str) -> float:
    m = _TIME.fullmatch(value.strip())
    if not m:
        raise ValueError(f"Can't read the time \"{value.strip()}\".")
    h, mnt, sec, ms = m.groups()
    return int(h or 0) * 3600 + int(mnt) * 60 + int(sec) + int(ms.ljust(3, "0")) / 1000


def parse_subtitle_file(text: str) -> list[Cue]:
    """Read an .srt or .vtt file into cues."""
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = block.strip().split("\n")
        for i, line in enumerate(lines):
            if "-->" in line:
                start, end = line.split("-->")
                end = end.strip().split()[0]  # drop VTT cue settings
                body = " ".join(l.strip() for l in lines[i + 1:] if l.strip())
                body = re.sub(r"<[^>]+>", "", body)  # drop <i>, <b>, VTT tags
                if body:
                    cues.append(Cue(_parse_time(start), _parse_time(end), body))
                break
    if not cues:
        raise ValueError("No subtitles were found in this file.")
    return cues


def _timestamp(seconds: float, sep: str) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02}{sep}{ms:03}"


def to_srt(cues: list[Cue]) -> str:
    blocks = [
        f"{i}\n{_timestamp(c.start, ',')} --> {_timestamp(c.end, ',')}\n{c.text}\n"
        for i, c in enumerate(cues, 1)
    ]
    return "\n".join(blocks)


def to_vtt(cues: list[Cue]) -> str:
    blocks = [
        f"{_timestamp(c.start, '.')} --> {_timestamp(c.end, '.')}\n{c.text}\n"
        for c in cues
    ]
    return "WEBVTT\n\n" + "\n".join(blocks)


def video_duration(video_path: Path) -> float:
    """Length of a video in seconds, read with ffprobe (0 if unknown)."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video_path)],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def _run_ffmpeg(args: list[str], cwd: Path, duration: float, on_progress: Progress | None) -> None:
    """Run ffmpeg, reporting how much of the video it has processed so far."""
    command = ["ffmpeg", "-y", "-loglevel", "error", "-nostats", "-progress", "pipe:1", *args]
    # Errors go to a temporary file so a full stderr pipe can never block ffmpeg.
    with tempfile.TemporaryFile(mode="w+") as errors:
        process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=errors, text=True)
        for line in process.stdout:
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and duration and on_progress and value.isdigit():
                on_progress(min(1.0, int(value) / 1_000_000 / duration))
        process.wait()
        if process.returncode != 0:
            errors.seek(0)
            raise subprocess.CalledProcessError(process.returncode, command, stderr=errors.read())
    if on_progress:
        on_progress(1.0)


def burn_subtitles(
    video_path: Path,
    srt_path: Path,
    output_path: Path,
    on_progress: Progress | None = None,
    on_saving: Callable[[], None] | None = None,
    on_saving_progress: Progress | None = None,
) -> None:
    """Render the SRT onto the video frames with ffmpeg, in two steps.

    1. Draw the subtitles onto every frame and re-encode the video
       (on_progress reports the share of the video rendered).
    2. Save the final file: copy it without re-encoding and move its index to
       the front (+faststart), so browsers can start playing before the whole
       file has downloaded (on_saving starts this step, on_saving_progress
       reports it).

    The finished file replaces output_path only at the end, so the previous
    video stays downloadable until the new one is complete.
    """
    rendered = output_path.with_name(output_path.stem + ".render" + output_path.suffix)
    saved = output_path.with_name(output_path.stem + ".tmp" + output_path.suffix)
    duration = video_duration(video_path)
    # Run inside the SRT's folder so the subtitles filter gets a plain file name
    # and we avoid ffmpeg's filter-argument escaping rules.
    style = "FontSize=22,Outline=2,Shadow=0,MarginV=24"
    try:
        _run_ffmpeg(
            [
                "-i", str(video_path.resolve()),
                "-vf", f"subtitles={srt_path.name}:force_style='{style}'",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "copy",
                str(rendered.resolve()),
            ],
            cwd=srt_path.parent, duration=duration, on_progress=on_progress,
        )
        if on_saving:
            on_saving()
        _run_ffmpeg(
            ["-i", str(rendered.resolve()), "-c", "copy", "-movflags", "+faststart", str(saved.resolve())],
            cwd=srt_path.parent, duration=duration, on_progress=on_saving_progress,
        )
        saved.replace(output_path)
    finally:
        rendered.unlink(missing_ok=True)
