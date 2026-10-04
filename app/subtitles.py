"""Transcription, subtitle formatting and burning subtitles into video."""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from faster_whisper import WhisperModel

MODEL_SIZE = os.environ.get("WHISPER_MODEL", "small")
MAX_CHARS = 42      # max characters per subtitle cue
MAX_DURATION = 6.0  # max seconds per subtitle cue

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


def transcribe(video_path: Path, language: str | None = None) -> tuple[list[Cue], str]:
    """Transcribe the audio of a video into short subtitle cues.

    Returns the cues and the detected (or given) language code.
    """
    segments, info = get_model().transcribe(
        str(video_path),
        language=language or None,
        word_timestamps=True,
        vad_filter=True,
    )
    cues: list[Cue] = []
    for segment in segments:
        cues.extend(_split_segment(segment))
    return cues, info.language


def _split_segment(segment) -> list[Cue]:
    """Split a Whisper segment into cues short enough to read on screen."""
    words = segment.words or []
    if not words:
        text = segment.text.strip()
        return [Cue(segment.start, segment.end, text)] if text else []

    cues: list[Cue] = []
    current: list = []
    for word in words:
        if current:
            text = "".join(w.word for w in current + [word]).strip()
            too_long = len(text) > MAX_CHARS
            too_slow = word.end - current[0].start > MAX_DURATION
            if too_long or too_slow:
                cues.append(_cue_from_words(current))
                current = []
        current.append(word)
        if word.word.strip()[-1:] in ".?!":
            cues.append(_cue_from_words(current))
            current = []
    if current:
        cues.append(_cue_from_words(current))
    return cues


def _cue_from_words(words) -> Cue:
    return Cue(words[0].start, words[-1].end, "".join(w.word for w in words).strip())


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


def burn_subtitles(video_path: Path, srt_path: Path, output_path: Path) -> None:
    """Render the SRT onto the video frames with ffmpeg."""
    # Run inside the SRT's folder so the subtitles filter gets a plain file name
    # and we avoid ffmpeg's filter-argument escaping rules.
    style = "FontSize=22,Outline=2,Shadow=0,MarginV=24"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(video_path.resolve()),
            "-vf", f"subtitles={srt_path.name}:force_style='{style}'",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "copy",
            str(output_path.resolve()),
        ],
        cwd=srt_path.parent,
        check=True,
        capture_output=True,
        text=True,
    )
