# Subtitles

Upload a video in your browser and get it back with subtitles. The speech is
transcribed automatically with [Whisper](https://github.com/SYSTRAN/faster-whisper)
and the subtitles are burned into the video with ffmpeg. You can also download
the subtitles as `.srt` or `.vtt`.

## Run it

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
