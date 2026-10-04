"""Web server: upload a video, get it back with subtitles, edit them later."""

import json
import shutil
import threading
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import subtitles
from .subtitles import Cue

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Subtitles")
# One job at a time: Whisper and ffmpeg already use all CPU cores.
_worker_lock = threading.Lock()
_jobs_lock = threading.Lock()

# Every job lives in data/<job id>/:
#   input.<ext>     the uploaded video
#   job.json        status and metadata
#   cues.json       the subtitles, the source of truth you edit
#   subtitles.srt / subtitles.vtt / output.mp4   generated from cues.json


def _job_dir(job_id: str) -> Path:
    path = DATA_DIR / job_id
    # Job ids are hex uuids; reject anything else so ids can't escape DATA_DIR.
    if not (len(job_id) == 32 and all(c in "0123456789abcdef" for c in job_id)) or not path.is_dir():
        raise HTTPException(404, "Job not found")
    return path


def _read_job(job_id: str) -> dict:
    return json.loads((_job_dir(job_id) / "job.json").read_text(encoding="utf-8"))


def _update_job(job_id: str, **changes) -> dict:
    with _jobs_lock:
        path = DATA_DIR / job_id / "job.json"
        job = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        job.update(changes, updated=datetime.now(timezone.utc).isoformat())
        path.write_text(json.dumps(job, indent=2), encoding="utf-8")
        return job


def _read_cues(job_dir: Path) -> list[Cue]:
    path = job_dir / "cues.json"
    if not path.exists():
        return []
    return [Cue(**c) for c in json.loads(path.read_text(encoding="utf-8"))]


def _write_subtitle_files(job_dir: Path, cues: list[Cue]) -> None:
    (job_dir / "cues.json").write_text(
        json.dumps([asdict(c) for c in cues], indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (job_dir / "subtitles.srt").write_text(subtitles.to_srt(cues), encoding="utf-8")
    (job_dir / "subtitles.vtt").write_text(subtitles.to_vtt(cues), encoding="utf-8")


def _burn(job_id: str) -> None:
    job_dir = DATA_DIR / job_id
    _update_job(job_id, status="burning")
    subtitles.burn_subtitles(_input_video(job_dir), job_dir / "subtitles.srt", job_dir / "output.mp4")
    _update_job(job_id, status="done", error=None)


def _input_video(job_dir: Path) -> Path:
    return next(job_dir.glob("input.*"))


def _fail(job_id: str, exc: Exception) -> None:
    _update_job(job_id, status="error", error=getattr(exc, "stderr", None) or str(exc))


def _make_subtitles(job_dir: Path, language: str | None) -> tuple[list[Cue], str | None]:
    """Build the cues from whatever was uploaded with the video."""
    video = _input_video(job_dir)
    text_file = next(job_dir.glob("text.*"), None)
    if text_file is None:
        return subtitles.transcribe(video, language)
    text = text_file.read_text(encoding="utf-8-sig", errors="replace")
    if text_file.suffix in (".srt", ".vtt"):
        return subtitles.parse_subtitle_file(text), language
    return subtitles.align_text(video, text, language)


def _transcribe_and_burn(job_id: str, language: str | None) -> None:
    job_dir = DATA_DIR / job_id
    with _worker_lock:
        try:
            _update_job(job_id, status="transcribing")
            cues, detected = _make_subtitles(job_dir, language)
            if not cues:
                raise RuntimeError("No speech was found in this video.")
            _write_subtitle_files(job_dir, cues)
            _update_job(job_id, language=detected, cues=len(cues))
            _burn(job_id)
        except Exception as exc:  # report any failure to the page
            _fail(job_id, exc)


def _reburn(job_id: str) -> None:
    with _worker_lock:
        try:
            _burn(job_id)
        except Exception as exc:
            _fail(job_id, exc)


@app.on_event("startup")
def _recover_interrupted_jobs() -> None:
    """Jobs that were running when the server stopped will never finish; mark them."""
    if not DATA_DIR.exists():
        return
    for path in DATA_DIR.glob("*/job.json"):
        job = json.loads(path.read_text(encoding="utf-8"))
        if job.get("status") in ("queued", "transcribing", "burning"):
            has_cues = (path.parent / "cues.json").exists()
            _update_job(
                path.parent.name,
                status="error",
                error="The server restarted while this video was being processed."
                + (" Your subtitles are saved: open the editor and click Save and update video." if has_cues else ""),
            )


@app.get("/api/jobs")
def list_jobs():
    if not DATA_DIR.exists():
        return []
    jobs = [json.loads(p.read_text(encoding="utf-8")) for p in DATA_DIR.glob("*/job.json")]
    return sorted(jobs, key=lambda j: j.get("created", ""), reverse=True)


@app.post("/api/jobs")
def create_job(
    file: UploadFile = File(...),
    language: str = Form(""),
    text: UploadFile | None = File(None),
):
    """Upload a video, optionally with its text.

    The text can be a plain .txt script (timed against the speech) or a
    ready-made .srt / .vtt file (used as is). Without it, the speech is
    transcribed automatically.
    """
    text_suffix = None
    if text is not None and text.filename:
        text_suffix = Path(text.filename).suffix.lower()
        if text_suffix not in (".srt", ".vtt"):
            text_suffix = ".txt"

    job_id = uuid.uuid4().hex
    job_dir = DATA_DIR / job_id
    job_dir.mkdir(parents=True)
    suffix = Path(file.filename or "video.mp4").suffix or ".mp4"
    with (job_dir / f"input{suffix}").open("wb") as out:
        shutil.copyfileobj(file.file, out)
    if text_suffix:
        with (job_dir / f"text{text_suffix}").open("wb") as out:
            shutil.copyfileobj(text.file, out)

    job = _update_job(
        job_id, id=job_id, status="queued", filename=file.filename,
        created=datetime.now(timezone.utc).isoformat(),
        source={".srt": "subtitle file", ".vtt": "subtitle file", ".txt": "your text"}.get(text_suffix, "automatic"),
    )
    threading.Thread(target=_transcribe_and_burn, args=(job_id, language.strip() or None), daemon=True).start()
    return job


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return _read_job(job_id)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    job_dir = _job_dir(job_id)
    if _read_job(job_id).get("status") in ("queued", "transcribing", "burning"):
        raise HTTPException(409, "This video is still being processed.")
    shutil.rmtree(job_dir)
    return {"deleted": job_id}


class CueIn(BaseModel):
    start: float
    end: float
    text: str


@app.get("/api/jobs/{job_id}/cues")
def get_cues(job_id: str):
    return [asdict(c) for c in _read_cues(_job_dir(job_id))]


@app.put("/api/jobs/{job_id}/cues")
def save_cues(job_id: str, cues: list[CueIn], burn: bool = False):
    """Save edited subtitles. With ?burn=true, also remake the subtitled video."""
    job_dir = _job_dir(job_id)
    if _read_job(job_id).get("status") in ("queued", "transcribing", "burning"):
        raise HTTPException(409, "This video is still being processed.")
    for c in cues:
        if c.start < 0 or c.end <= c.start:
            raise HTTPException(422, f"Subtitle \"{c.text[:30]}\" ends before it starts.")
    cleaned = [Cue(c.start, c.end, c.text.strip()) for c in sorted(cues, key=lambda c: c.start) if c.text.strip()]
    _write_subtitle_files(job_dir, cleaned)
    job = _update_job(job_id, cues=len(cleaned), edited=True)
    if burn:
        job = _update_job(job_id, status="queued")
        threading.Thread(target=_reburn, args=(job_id,), daemon=True).start()
    return job


DOWNLOADS = {
    "video": ("output.mp4", "video/mp4"),
    "srt": ("subtitles.srt", "application/x-subrip"),
    "vtt": ("subtitles.vtt", "text/vtt"),
}


@app.get("/api/jobs/{job_id}/source")
def source_video(job_id: str):
    """The original upload, used as the editor's preview."""
    return FileResponse(_input_video(_job_dir(job_id)))


@app.get("/api/jobs/{job_id}/{kind}")
def download(job_id: str, kind: str):
    job_dir = _job_dir(job_id)
    if kind not in DOWNLOADS:
        raise HTTPException(404, "Not found")
    name, media_type = DOWNLOADS[kind]
    path = job_dir / name
    if not path.exists():
        raise HTTPException(404, "Not ready yet")
    stem = Path(_read_job(job_id).get("filename") or "video").stem
    download_name = f"{stem}.subtitled.mp4" if kind == "video" else f"{stem}.{kind}"
    return FileResponse(path, media_type=media_type, filename=download_name)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
