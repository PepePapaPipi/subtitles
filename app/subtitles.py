"""Transcription, subtitle formatting and burning subtitles into video."""

import difflib
import os
import re
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


def _whisper_words(video_path: Path, language: str | None):
    """Run Whisper and return its segments (with word timings) and language."""
    segments, info = get_model().transcribe(
        str(video_path),
        language=language or None,
        word_timestamps=True,
        vad_filter=True,
    )
    return list(segments), info.language


def transcribe(video_path: Path, language: str | None = None) -> tuple[list[Cue], str]:
    """Transcribe the audio of a video into short subtitle cues.

    Returns the cues and the detected (or given) language code.
    """
    segments, detected = _whisper_words(video_path, language)
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


def align_text(video_path: Path, text: str, language: str | None = None) -> tuple[list[Cue], str]:
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

    segments, detected = _whisper_words(video_path, language)
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


def burn_subtitles(video_path: Path, srt_path: Path, output_path: Path) -> None:
    """Render the SRT onto the video frames with ffmpeg.

    Writes to a temporary file first so the previous video stays downloadable
    until the new one is complete.
    """
    tmp_path = output_path.with_name(output_path.stem + ".tmp" + output_path.suffix)
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
            str(tmp_path.resolve()),
        ],
        cwd=srt_path.parent,
        check=True,
        capture_output=True,
        text=True,
    )
    tmp_path.replace(output_path)
