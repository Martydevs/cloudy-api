# Project overview
UV init starter python template. This has been modified to be a FastAPI web service.

This FastAPI webservice is part of the Cloudy app product.

Cloudy app is a webapp to automating and improving workflows for a christian multimedia workteam for producing and managing media files.

# Running project
- Dev server: `bun run dev` (runs `uv run fastapi dev src/cloudy_api/main.py`)
- Tests: `bun run test` (`uv run pytest`, coverage gate 90%)

### Notes
This project is deployed as a Docker web service on Railway (`railway.json`) to download videos on demand for a Nuxt Fullstack Webapp hosted on Vercel.

- Routes are unversioned under `/api` (`main.API_PREFIX`: `/api/videos`, `/api/jobs`, `/api/lyrics`);
  `/healthz`, `/docs` (Scalar) and `/openapi.json` stay at the root. `/v1` and `/v2` were removed.
- Downloads use pytubefix (`services/pytubefix_downloader.py`); yt-dlp was removed.
- Nuxt-side contract: `NUXT_INTEGRATION.md`.
- Video metadata (`GET /api/videos/{id}` and the job preflight) comes from the YouTube Data API v3
  (`services/youtube_api.py`) when `YOUTUBE_DATA_API_KEY` is set; otherwise pytubefix probes it itself.
