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
#   job.json        status, metadata and the list of versions
#   cues.json       the subtitles you are editing (working copy)
#   subtitles.srt / subtitles.vtt   generated from cues.json
#   versions/<n>/   one folder per finished video: version 1 is the original,
#                   every "Save as new version" adds the next one. Each holds
#                   cues.json, subtitles.srt, subtitles.vtt and output.mp4.


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
        # Write a temporary file and swap it in, so readers never see a half-written file.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(job, indent=2), encoding="utf-8")
        tmp.replace(path)
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


BUSY = ("queued", "transcribing", "burning", "saving")


def _set_stage(job_id: str, status: str) -> None:
    """Start a processing step; the page shows its progress bar and time left."""
    _update_job(job_id, status=status, progress=0.0, stage_started=datetime.now(timezone.utc).isoformat())


def _progress_reporter(job_id: str):
    """Save a step's progress to job.json, at most once per whole percent."""
    last = -1

    def report(fraction: float) -> None:
        nonlocal last
        percent = int(fraction * 100)
        if percent != last:
            last = percent
            _update_job(job_id, progress=round(fraction, 3))

    return report


VERSION_FILES = ("cues.json", "subtitles.srt", "subtitles.vtt")


def _version_dir(job_dir: Path, n: int) -> Path:
    return job_dir / "versions" / str(n)


def _burn_new_version(job_id: str, source: str, original: bool = False) -> None:
    """Freeze the current subtitles as a new version and burn its video.

    The other versions are never touched, so the original video stays
    available next to every edited one.
    """
    job_dir = DATA_DIR / job_id
    job = _read_job(job_id)
    n = max((v["n"] for v in job.get("versions", [])), default=0) + 1
    if original:
        name = "Original"
    else:
        name = f"Edit {n - 1 if job.get('has_original') else n}"
    version_dir = _version_dir(job_dir, n)
    shutil.rmtree(version_dir, ignore_errors=True)  # leftovers of a failed attempt
    version_dir.mkdir(parents=True)
    for file_name in VERSION_FILES:
        shutil.copy2(job_dir / file_name, version_dir / file_name)
    _update_job(job_id, making_version=name)
    _set_stage(job_id, "burning")
    try:
        subtitles.burn_subtitles(
            _input_video(job_dir), version_dir / "subtitles.srt", version_dir / "output.mp4",
            on_progress=_progress_reporter(job_id),
            on_saving=lambda: _set_stage(job_id, "saving"),
            on_saving_progress=_progress_reporter(job_id),
        )
    except Exception:
        shutil.rmtree(version_dir, ignore_errors=True)
        raise
    version = {
        "n": n, "name": name, "source": source, "cues": len(_read_cues(version_dir)),
        "created": datetime.now(timezone.utc).isoformat(),
    }
    with _jobs_lock:
        versions = _read_job(job_id).get("versions", []) + [version]
    changes = {"has_original": True} if original else {}
    _update_job(job_id, versions=versions, status="done", progress=1.0, error=None,
                making_version=None, step_source=None, **changes)


def _input_video(job_dir: Path) -> Path:
    return next(job_dir.glob("input.*"))


def _fail(job_id: str, exc: Exception) -> None:
    _update_job(job_id, status="error", error=getattr(exc, "stderr", None) or str(exc))


def _make_subtitles(job_id: str, language: str | None, text_file: Path | None) -> tuple[list[Cue], str | None]:
    """Build the cues from the video, plus the text file uploaded with it if any."""
    video = _input_video(DATA_DIR / job_id)
    report = _progress_reporter(job_id)
    if text_file is None:
        return subtitles.transcribe(video, language, report)
    text = text_file.read_text(encoding="utf-8-sig", errors="replace")
    if text_file.suffix in (".srt", ".vtt"):
        cues = subtitles.parse_subtitle_file(text)
        report(1.0)
        return cues, language
    return subtitles.align_text(video, text, language, report)


def _text_source(suffix: str | None) -> str:
    return {".srt": "subtitle file", ".vtt": "subtitle file", ".txt": "your text"}.get(suffix, "automatic")


def _transcribe_and_burn(job_id: str, language: str | None) -> None:
    job_dir = DATA_DIR / job_id
    text_file = next(job_dir.glob("text.*"), None)
    with _worker_lock:
        try:
            _set_stage(job_id, "transcribing")
            cues, detected = _make_subtitles(job_id, language, text_file)
            if not cues:
                raise RuntimeError("No speech was found in this video.")
            _write_subtitle_files(job_dir, cues)
            _update_job(job_id, language=detected, cues=len(cues))
            _burn_new_version(job_id, _text_source(text_file and text_file.suffix), original=True)
        except Exception as exc:  # report any failure to the page
            _fail(job_id, exc)


def _new_version_from_file(job_id: str, text_file: Path) -> None:
    """Make a new version from a subtitle or text file uploaded later."""
    job_dir = DATA_DIR / job_id
    with _worker_lock:
        try:
            _set_stage(job_id, "transcribing")
            cues, _ = _make_subtitles(job_id, _read_job(job_id).get("language"), text_file)
            if not cues:
                raise RuntimeError("No subtitles were found in this file.")
            _write_subtitle_files(job_dir, cues)
            _update_job(job_id, cues=len(cues))
            _burn_new_version(job_id, _text_source(text_file.suffix))
        except Exception as exc:
            _fail(job_id, exc)


def _reburn(job_id: str) -> None:
    with _worker_lock:
        try:
            _burn_new_version(job_id, "editor")
        except Exception as exc:
            _fail(job_id, exc)


def _migrate_to_versions(job_dir: Path, job: dict) -> None:
    """Jobs made before versions existed have one output.mp4 next to job.json.

    Move it into versions/1, named Original, or Edit 1 if it was remade from
    edited subtitles (in that case the original video no longer exists).
    """
    old_video = job_dir / "output.mp4"
    if "versions" in job or not old_video.exists() or not (job_dir / "cues.json").exists():
        return
    version_dir = _version_dir(job_dir, 1)
    version_dir.mkdir(parents=True, exist_ok=True)
    for file_name in VERSION_FILES:
        shutil.copy2(job_dir / file_name, version_dir / file_name)
    old_video.replace(version_dir / "output.mp4")
    edited = bool(job.get("edited"))
    _update_job(job_dir.name, has_original=not edited, versions=[{
        "n": 1, "name": "Edit 1" if edited else "Original",
        "source": "editor" if edited else job.get("source", "automatic"),
        "cues": job.get("cues"), "created": job.get("updated") or job.get("created"),
    }])


@app.on_event("startup")
def _startup() -> None:
    """Upgrade old jobs, and mark jobs that were running when the server stopped."""
    if not DATA_DIR.exists():
        return
    for path in DATA_DIR.glob("*/job.json"):
        job_dir = path.parent
        job = json.loads(path.read_text(encoding="utf-8"))
        _migrate_to_versions(job_dir, job)
        job = json.loads(path.read_text(encoding="utf-8"))
        if job.get("status") in BUSY:
            # A version that was being made when the server stopped is incomplete.
            finished = {str(v["n"]) for v in job.get("versions", [])}
            for version_dir in (job_dir / "versions").glob("*"):
                if version_dir.name not in finished:
                    shutil.rmtree(version_dir, ignore_errors=True)
            has_cues = (job_dir / "cues.json").exists()
            _update_job(
                job_dir.name,
                status="error", making_version=None, step_source=None,
                error="The server restarted while this video was being processed."
                + (" Your subtitles are saved: open the editor and click Save as new version." if has_cues else ""),
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
        source=_text_source(text_suffix), versions=[],
    )
    threading.Thread(target=_transcribe_and_burn, args=(job_id, language.strip() or None), daemon=True).start()
    return job


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return _read_job(job_id)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    job_dir = _job_dir(job_id)
    if _read_job(job_id).get("status") in BUSY:
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
    """Save edited subtitles. With ?burn=true, also make a new version of the video from them."""
    job_dir = _job_dir(job_id)
    if _read_job(job_id).get("status") in BUSY:
        raise HTTPException(409, "This video is still being processed.")
    for c in cues:
        if c.start < 0 or c.end <= c.start:
            raise HTTPException(422, f"Subtitle \"{c.text[:30]}\" ends before it starts.")
    cleaned = [Cue(c.start, c.end, c.text.strip()) for c in sorted(cues, key=lambda c: c.start) if c.text.strip()]
    _write_subtitle_files(job_dir, cleaned)
    job = _update_job(job_id, cues=len(cleaned), edited=True)
    if burn:
        job = _update_job(job_id, status="queued", progress=0.0, step_source=None)
        threading.Thread(target=_reburn, args=(job_id,), daemon=True).start()
    return job


@app.post("/api/jobs/{job_id}/versions")
def add_version(job_id: str, text: UploadFile = File(...)):
    """Make a new version from an edited .srt / .vtt, or a .txt timed against the speech."""
    job_dir = _job_dir(job_id)
    if _read_job(job_id).get("status") in BUSY:
        raise HTTPException(409, "This video is still being processed.")
    suffix = Path(text.filename or "").suffix.lower()
    if suffix not in (".srt", ".vtt"):
        suffix = ".txt"
    for old in job_dir.glob("version-text.*"):
        old.unlink()
    text_file = job_dir / f"version-text{suffix}"
    with text_file.open("wb") as out:
        shutil.copyfileobj(text.file, out)
    job = _update_job(job_id, status="queued", progress=0.0, step_source=_text_source(suffix), error=None)
    threading.Thread(target=_new_version_from_file, args=(job_id, text_file), daemon=True).start()
    return job


@app.delete("/api/jobs/{job_id}/versions/{n}")
def delete_version(job_id: str, n: int):
    """Delete one edited version. The original stays until the whole video is deleted."""
    job_dir = _job_dir(job_id)
    job = _read_job(job_id)
    if job.get("status") in BUSY:
        raise HTTPException(409, "This video is still being processed.")
    version = next((v for v in job.get("versions", []) if v["n"] == n), None)
    if version is None:
        raise HTTPException(404, "Version not found")
    if version["name"] == "Original":
        raise HTTPException(409, "The original can't be deleted on its own. Delete the whole video instead.")
    shutil.rmtree(_version_dir(job_dir, n), ignore_errors=True)
    with _jobs_lock:
        versions = [v for v in _read_job(job_id).get("versions", []) if v["n"] != n]
    return _update_job(job_id, versions=versions)


DOWNLOADS = {
    "video": ("output.mp4", "video/mp4"),
    "srt": ("subtitles.srt", "application/x-subrip"),
    "vtt": ("subtitles.vtt", "text/vtt"),
}


def _download(job_id: str, n: int | None, kind: str):
    job_dir = _job_dir(job_id)
    if kind not in DOWNLOADS:
        raise HTTPException(404, "Not found")
    job = _read_job(job_id)
    versions = job.get("versions", [])
    version = versions[-1] if n is None and versions else next((v for v in versions if v["n"] == n), None)
    if version is None:
        raise HTTPException(404, "Not ready yet")
    name, media_type = DOWNLOADS[kind]
    path = _version_dir(job_dir, version["n"]) / name
    if not path.exists():
        raise HTTPException(404, "Not ready yet")
    stem = Path(job.get("filename") or "video").stem
    suffix = "" if version["name"] == "Original" else "." + version["name"].lower().replace(" ", "")
    download_name = f"{stem}{suffix}.subtitled.mp4" if kind == "video" else f"{stem}{suffix}.{kind}"
    return FileResponse(path, media_type=media_type, filename=download_name)


@app.get("/api/jobs/{job_id}/source")
def source_video(job_id: str):
    """The original upload, used as the editor's preview."""
    return FileResponse(_input_video(_job_dir(job_id)))


@app.get("/api/jobs/{job_id}/versions/{n}/{kind}")
def download_version(job_id: str, n: int, kind: str):
    """The video, .srt or .vtt of one version."""
    return _download(job_id, n, kind)


@app.get("/api/jobs/{job_id}/{kind}")
def download_latest(job_id: str, kind: str):
    """The video, .srt or .vtt of the newest version."""
    return _download(job_id, None, kind)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
