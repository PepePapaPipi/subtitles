# Subtitles

Upload a video in your browser and get it back with subtitles. The speech is
transcribed automatically with [Whisper](https://github.com/SYSTRAN/faster-whisper)
and the subtitles are burned into the video with ffmpeg. You can also download
the subtitles as `.srt` or `.vtt`.

## Run it with Docker (easiest)

You only need [Docker Desktop](https://www.docker.com/products/docker-desktop/). Python, ffmpeg and Whisper are all inside the container.

```bash
docker compose up --build
```

Open http://localhost:8000, pick a video and click **Add subtitles**. Stop it with `Ctrl+C`.

The first video takes longer because the Whisper model is downloaded (about 500 MB for `small`). It is kept in a Docker volume, so later runs start right away. Results are saved in the `data/` folder.

To use a different model size, change `WHISPER_MODEL` in `docker-compose.yml` and run `docker compose up --build` again.

## Run it without Docker

You need Python 3.10+ and [ffmpeg](https://ffmpeg.org/download.html) installed.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

Open http://localhost:8000, pick a video and click **Add subtitles**.

The first run downloads the Whisper model (about 500 MB for `small`).

## Settings

| Variable | Default | What it does |
|---|---|---|
| `WHISPER_MODEL` | `small` | Model size: `tiny`, `base`, `small`, `medium`, `large-v3`. Bigger is more accurate but slower. |

## How it works

1. `POST /api/jobs` saves the upload under `data/<job id>/` and starts processing in the background.
2. `app/subtitles.py` transcribes the audio, splits it into short cues (max 42 characters or 6 seconds) and writes `subtitles.srt` and `subtitles.vtt`.
3. ffmpeg renders the subtitles onto the video as `output.mp4`.
4. The page polls `GET /api/jobs/<id>` and shows download links when it is done.
