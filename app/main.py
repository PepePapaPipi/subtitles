"""Web server: upload a video, get it back with subtitles."""

import shutil
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import subtitles

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Subtitles")
jobs: dict[str, dict] = {}
# One job at a time: Whisper already uses all CPU cores.
_worker_lock = threading.Lock()


def _process(job_id: str, video: Path, language: str | None) -> None:
    job = jobs[job_id]
    with _worker_lock:
        try:
            job["status"] = "transcribing"
            cues, detected = subtitles.transcribe(video, language)
            job["language"] = detected
            job["cues"] = len(cues)
            if not cues:
                raise RuntimeError("No speech was found in this video.")
            (video.parent / "subtitles.srt").write_text(subtitles.to_srt(cues), encoding="utf-8")
            (video.parent / "subtitles.vtt").write_text(subtitles.to_vtt(cues), encoding="utf-8")

            job["status"] = "burning"
            subtitles.burn_subtitles(video, video.parent / "subtitles.srt", video.parent / "output.mp4")
            job["status"] = "done"
        except Exception as exc:  # report any failure to the page
            job["status"] = "error"
            job["error"] = getattr(exc, "stderr", None) or str(exc)


@app.post("/api/jobs")
def create_job(file: UploadFile = File(...), language: str = Form("")):
    job_id = uuid.uuid4().hex
    job_dir = DATA_DIR / job_id
    job_dir.mkdir(parents=True)
    suffix = Path(file.filename or "video.mp4").suffix or ".mp4"
    video = job_dir / f"input{suffix}"
    with video.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    jobs[job_id] = {"id": job_id, "status": "queued", "filename": file.filename}
    threading.Thread(target=_process, args=(job_id, video, language.strip() or None), daemon=True).start()
    return jobs[job_id]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    return jobs[job_id]


DOWNLOADS = {
    "video": ("output.mp4", "video/mp4"),
    "srt": ("subtitles.srt", "application/x-subrip"),
    "vtt": ("subtitles.vtt", "text/vtt"),
}


@app.get("/api/jobs/{job_id}/{kind}")
def download(job_id: str, kind: str):
    if job_id not in jobs or kind not in DOWNLOADS:
        raise HTTPException(404, "Not found")
    name, media_type = DOWNLOADS[kind]
    path = DATA_DIR / job_id / name
    if not path.exists():
        raise HTTPException(404, "Not ready yet")
    stem = Path(jobs[job_id]["filename"] or "video").stem
    download_name = f"{stem}.subtitled.mp4" if kind == "video" else f"{stem}.{kind}"
    return FileResponse(path, media_type=media_type, filename=download_name)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
