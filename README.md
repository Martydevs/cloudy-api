# Cloudy API

On-demand YouTube download service (MP4 720p, MP4 1080p, MP3) and a
lyrics-to-PowerPoint converter for the Cloudy Nuxt app on Vercel. Built with FastAPI + [yt-dlp](https://github.com/yt-dlp/yt-dlp),
deployed as a Docker web service on Render Free.

> Downloading from YouTube conflicts with YouTube's Terms of Service. Running
> this service is the owner's decision and responsibility.

## How it works

- **yt-dlp** is the only YouTube library. It fetches metadata, picks streams and
  runs its postprocessors (merge to MP4, extract MP3).
- **ffmpeg / ffprobe** are required system tools; yt-dlp drives them.
- **A JavaScript runtime** (+ `yt-dlp-ejs`, pulled by `yt-dlp[default]`) solves
  YouTube's JS challenges. Without one, some formats disappear. The image ships
  **deno** and **Node.js 26 LTS**. yt-dlp uses the best one installed (deno → node
  → quickjs → bun), so Node takes over automatically if deno is removed. See
  [JS runtimes](#js-runtimes).
- Jobs are async: create → poll → the browser downloads the file directly from
  Render through a short-lived HMAC-signed URL.
- Render Free constraints: one instance, one worker, in-memory job store, files
  in a temp dir with a TTL. A restart loses jobs; clients get `404` and should
  treat that as "expired, retry".

## API (v1)

All `/v1/*` routes need the `X-API-Key` header, except the signed file route.

| Method | Path | Notes |
|---|---|---|
| GET | `/healthz` | `{status, yt_dlp_version, yt_dlp_ejs_version, ffmpeg, js_runtime, js_runtimes: {name: found}}`. No auth. `503` when ffmpeg/ffprobe or yt-dlp-ejs is missing, or none of the enabled JS runtimes is installed. |
| GET | `/v1/videos/{video_id}` | Title, duration, thumbnail, channel, `available_heights`, `downloadable`, `reason` (`too_long`, `live`, `unknown_duration`). |
| POST | `/v1/jobs` | `{video_ids: [1..10], format: "mp4_720" \| "mp4_1080" \| "mp3"}` → `202 {job_id, status}`. `422` for bad/too-long/unavailable videos, `429` when the queue is full. |
| GET | `/v1/jobs/{job_id}` | `status` (`queued\|running\|zipping\|done\|failed\|expired`), `progress`, `items[]`, and once done `download_url`, `filename`, `size_bytes`, `expires_at`. |
| GET | `/v1/jobs/{job_id}/file?exp=…&sig=…` | Streams the file as an attachment. Signature-verified, no API key. `403` bad/expired link, `410` file expired. |
| DELETE | `/v1/jobs/{job_id}` | Cancel / clean up early. `204`. |
| POST | `/v1/lyrics/pptx` | `{html, title?, font_color?}` → the `.pptx` itself (synchronous). See [Lyrics to PowerPoint](#lyrics-to-powerpoint). |
| POST | `/v1/lyrics/pptx/batch` | `{items: [1..3 × {html, title?, font_color?}]}` → a `.zip` of `.pptx` files + `manifest.json` (synchronous, sequential). See [Batch](#batch-several-songs-in-one-zip). |

Behaviour:

- Video IDs must match `^[A-Za-z0-9_-]{11}$`; the YouTube URL is built server-side.
- Batches are de-duplicated and processed sequentially. A single-video job returns
  the file itself (`My Title [id].mp4`); a batch returns `cloudy-batch-<id8>.zip`
  with the successful items. A job is `failed` only when every item failed.
- `mp4_1080` falls back to the best height ≤ 1080; check `items[].actual_height`.
- Error bodies look like `{"detail": {"code": "...", "message": "..."}}` (e.g.
  `unauthorized`, `not_found`, `queue_full`, `batch_too_large`, `videos_rejected`
  with a `videos[]` list, `invalid_signature`, `link_expired`, `expired`). Request
  validation errors (bad ID pattern, unknown format) use FastAPI's standard
  `{"detail": [ ... ]}` list. Item error codes: `private`, `unavailable`, `age_restricted`, `members_only`,
  `geo_restricted`, `live`, `format_unavailable`, `bot_check`, `ffmpeg_missing`,
  `download_failed`, `cancelled`, `internal`.

## Configuration

Copy `.env.example` to `.env`.

| Var | Default | |
|---|---|---|
| `API_KEY` | — | Required. Only the Nuxt server sends it. |
| `SIGNING_SECRET` | — | Required. HMAC key for download links. |
| `ALLOWED_ORIGINS` | empty | Comma-separated CORS origins (the Vercel domain). |
| `MAX_CONCURRENT_JOBS` | `1` | |
| `MAX_QUEUE` | `20` | Waiting jobs before `429`. |
| `MAX_BATCH` | `10` | |
| `MAX_LYRICS_BATCH` | `3` | Songs per `POST /v1/lyrics/pptx/batch`; more → `422 batch_too_large`. |
| `MAX_DURATION_SECONDS` | `1800` | |
| `FILE_TTL_MINUTES` | `30` | Files deleted after this; the record answers `expired` for another TTL. |
| `SIGNED_URL_TTL_SECONDS` | `600` | |
| `PROBE_CONCURRENCY` | `3` | Parallel metadata probes on job creation. Lower to `1` if Render runs out of memory. |
| `OPENROUTER_API_KEY` | — | Enables the LLM slide arrangement (`OPENROUTER_APIKEY` also works). Unset → fallback grouping. |
| `OPENROUTER_MODEL` | `@preset/lyrics-generator` | OpenRouter preset (or model) that supports tool calling. The preset holds the system prompt; the API sends only the numbered lyrics. |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | |
| `LLM_TIMEOUT_SECONDS` | `30` | Per LLM request; on timeout the fallback grouping is used. |
| `WORK_DIR` | `/tmp/cloudy` | |
| `PUBLIC_BASE_URL` | `RENDER_EXTERNAL_URL` | Base for `download_url`; falls back to the request URL. |
| `YTDLP_COOKIES_FILE` | — | Optional cookies.txt for bot checks. |
| `YTDLP_PROXY` | — | Optional proxy URL. |
| `YTDLP_JS_RUNTIMES` | `deno,node` | Enabled JS runtimes, comma-separated `name[:path]` (`deno`, `node`, `bun`, `quickjs`). |
| `YTDLP_EXTRACTOR_ARGS` | — | yt-dlp `--extractor-args`, space-separated `IE_KEY:ARGS` entries. See [Extractor arguments](#extractor-arguments). |

## Local development

Requires [uv](https://docs.astral.sh/uv/) and [bun](https://bun.sh) (scripts only).

**Option A — native tools**

```bash
brew install ffmpeg node        # Node >= 22 (or deno >= 2.3); or your distro's packages
cp .env.example .env            # set API_KEY and SIGNING_SECRET
uv sync
bun run dev                     # API docs (Scalar): http://localhost:8000/docs
```

**Option B — Docker** (ffmpeg, deno and Node included)

```bash
docker build -t cloudy-api .
docker run -p 8000:8000 --env-file .env cloudy-api
```

Smoke test:

```bash
H=http://localhost:8000; K="X-API-Key: change-me"
curl $H/healthz
curl -H "$K" $H/v1/videos/dQw4w9WgXcQ
curl -H "$K" -H 'content-type: application/json' \
  -d '{"video_ids":["dQw4w9WgXcQ"],"format":"mp4_720"}' $H/v1/jobs
curl -H "$K" $H/v1/jobs/<job_id>          # repeat until "done"
curl -OJ "<download_url>"
ffprobe -v error -show_entries stream=codec_name,height <file>
```

## API docs

Interactive docs are served by [Scalar](https://scalar.com) at `/docs` (Swagger UI
and ReDoc are disabled). The raw spec is at `/openapi.json`. Use the
**Authentication** panel to set `X-API-Key` before trying requests.

## Testing

```bash
bun run test            # uv run pytest: unit suite + coverage gate (fail_under = 90)
bun run coverage:html   # same, plus an HTML report in htmlcov/
```

- Unit tests live in `tests/unit/` and never touch the network, YouTube, ffmpeg,
  any JS runtime or OpenRouter. `tests/conftest.py` provides a `FakeDownloader` (wired
  in via `create_app(settings, downloader=...)`), a `FakeChain` standing in for the
  LLM (`LyricsArranger(settings, chain=...)`), and `test_downloader.py` uses a fake
  `YoutubeDL` through the `ydl_factory` argument.
- Every test gets its own `tmp_path` as `WORK_DIR`; clocks and the cleanup sleep
  are injected, so no test waits on real time.
- Coverage is line + branch, configured in `pyproject.toml`. The run fails below 90%.
- CI (`.github/workflows/ci.yml`) runs `uv sync --locked` and `uv run pytest` on every
  push and PR and uploads `coverage.xml`.

## Lyrics to PowerPoint

`POST /v1/lyrics/pptx` turns the lyrics HTML scraped from a Letras.com song page
into a presentation.

```bash
curl -H "X-API-Key: change-me" -H 'content-type: application/json'   -d '{"html": "<div class=\"lyric-original\"><p>Line 1<br>Line 2</p><p>Line 3</p></div>", "title": "My Song"}'   -OJ http://localhost:8000/v1/lyrics/pptx
```

Request body (JSON):

| Field | | |
|---|---|---|
| `html` | required, ≤ 500,000 chars | The lyrics container, a whole Letras page, or plain text. |
| `title` | optional, ≤ 200 chars | Names the file and the document. Defaults to the page's `<h1>`, then `lyrics`. |
| `font_color` | optional, `RRGGBB` or `#RRGGBB` | Text color. Default `000000`. |

Response: `200` with the `.pptx` bytes (`Content-Type:
application/vnd.openxmlformats-officedocument.presentationml.presentation`,
`Content-Disposition: attachment; filename="…"; filename*=UTF-8''…`) plus:

- `X-Slide-Count`: number of slides.
- `X-Lyrics-Arranged-By`: `llm` or `fallback` (see below).

Errors: `401 unauthorized`; `422 {"detail": {"code": "no_lyrics" | "lyrics_too_long", …}}`
when the HTML has no lyrics or more than 300 lines; FastAPI's standard `422` list for
a malformed body.

How it works:

1. **Parse** (`services/lyrics_parser.py`, beautifulsoup4): finds `.lyric-original`
   (or `.cnt-letra`, `.lyric`, else the whole document), turns `<br>` into line
   breaks and each `<p>` / blank-line run into a stanza.
2. **Arrange** (`services/lyrics_arranger.py`, LangChain `ChatOpenAI` on OpenRouter):
   **2 lines per slide**, each line its own paragraph, and every stanza starts on a new
   slide (a stanza with an odd number of lines ends on a 1-line slide). The instructions
   live in the OpenRouter preset `@preset/lyrics-generator` (`OPENROUTER_MODEL`); the
   request carries no system prompt or title, only the numbered lyrics (blank lines
   between stanzas). The model returns slides as inclusive line ranges. It never writes
   text: the ranges are validated (every line exactly once, in order, one line per
   paragraph, two per slide, no slide spanning two stanzas) and mapped back to the
   original lines. If there's no `OPENROUTER_API_KEY`, the call fails or times out, or
   the plan is invalid, the service uses a deterministic grouping instead (each stanza
   chunked in twos) and reports `X-Lyrics-Arranged-By: fallback`. A request never
   fails because of the LLM.
3. **Render** (`services/pptx_builder.py`, python-pptx): 16:9, one slide per group,
   in a compact **top-center box** (10.5 × 2.6 in, horizontally centered, 0.4 in from
   the top edge) with centered Arial text at 40 pt, shrinking toward 24 pt only when a
   line is long enough to wrap. The text box and slide background have no fill. Open it in
   PowerPoint and the slide shows white; the "transparent" part matters for tools that
   import the text over a background (or when exporting with transparency).

The route is synchronous (an LLM call plus a tiny file build, a few seconds at most),
so there's no job or signed URL: the browser-facing Nuxt route receives the bytes.

### Batch: several songs in one ZIP

`POST /v1/lyrics/pptx/batch` takes `{"items": [...]}`, where each item has the same
fields as the single route (`html`, `title?`, `font_color?`). It accepts 1 to
`MAX_LYRICS_BATCH` (default 3) items. Items are converted **one at a time, in order**,
through the same parse → arrange → render pipeline (`services/lyrics_pptx.py`).

```bash
curl -H "X-API-Key: change-me" -H 'content-type: application/json' \
  -d '{"items": [{"html": "<p>A 1<br>A 2</p>", "title": "Song A"}, {"html": "<p>B 1</p>", "title": "Song B", "font_color": "#FFFFFF"}]}' \
  -o lyrics.zip http://localhost:8000/v1/lyrics/pptx/batch && unzip -l lyrics.zip
```

Response: `200 application/zip` (`Content-Disposition: attachment; filename="lyrics.zip"`)
with `X-Batch-Succeeded` and `X-Batch-Failed` counts. The ZIP holds:

- One `<ASCII title>.pptx` per converted item. Repeated names get ` (2)`, ` (3)`, … suffixes;
  the comparison ignores case.
- `manifest.json`: `{"items": [{index, status, filename, title, arranged_by, slide_count, error}]}`,
  in request order. `status` is `done` or `failed`. A failed item has only `index`,
  `status` and `error: {code, message}` (`no_lyrics`, `lyrics_too_long`).

Partial success is the norm. Bad items are reported in the manifest and the rest are still
returned. Errors:

- `422 {"detail": {"code": "all_items_failed", "message": …, "items": [...]}}` when no item
  converts. `items` has the same shape as the manifest entries.
- `422 batch_too_large` for more than `MAX_LYRICS_BATCH` items.
- FastAPI's standard `422` list for an empty `items` or a malformed item.

Timing: each item can take up to `LLM_TIMEOUT_SECONDS` (plus one retry) before it falls
back, and Render's proxy cuts requests at about 100 s. Typical runs take a few seconds per
song. If timeouts show up, lower `LLM_TIMEOUT_SECONDS`.

## JS runtimes

yt-dlp needs a JavaScript runtime to solve YouTube's challenges. `YTDLP_JS_RUNTIMES`
controls which ones it may use. Entries follow yt-dlp's `--js-runtimes` syntax:
`name` or `name:/path/to/executable-or-dir`.

| Runtime | Executable | Minimum version |
|---|---|---|
| `deno` | `deno` | 2.3 |
| `node` | `node` | 22 |
| `quickjs` | `qjs` | 2023-12-09 (any quickjs-ng); versions before 2025-04-26 / ng 0.12 are very slow |
| `bun` | `bun` | 1.2.11–1.3.14. **Deprecated** by yt-dlp and runs without sandboxing |

The order in the variable doesn't matter. yt-dlp always tries installed runtimes in
the order deno → node → quickjs → bun. The default `deno,node` means Node is the
fallback.

**Retiring deno:** remove the `deno` stage and its `COPY` line from the `Dockerfile`,
then set `YTDLP_JS_RUNTIMES=node` (or just leave the default; a missing deno is
skipped). `/healthz` stays `ok` as long as one enabled runtime is installed.
Upgrading Node is a `NODE_VERSION` build arg bump. Keep it on an LTS release.

## yt-dlp dependencies

What the [yt-dlp README](https://github.com/yt-dlp/yt-dlp#dependencies) and
[EJS wiki](https://github.com/yt-dlp/yt-dlp/wiki/EJS) require, and how this repo
covers it:

| Dependency | Why | Provided by |
|---|---|---|
| ffmpeg + ffprobe (binaries) | Merge DASH video+audio, extract MP3 | `apt-get install ffmpeg` in the image |
| yt-dlp-ejs | YouTube JS challenge solver ("required for full YouTube support") | `yt-dlp[default]`, locked in `uv.lock` at the exact version yt-dlp pins |
| JS runtime | Runs yt-dlp-ejs | deno + node in the image ([JS runtimes](#js-runtimes)) |
| certifi, brotli, websockets, requests/urllib3, pycryptodomex, mutagen | TLS roots, compression, HTTPS proxies, AES-HLS | `yt-dlp[default]` |

Deliberately left out:

- **curl_cffi (impersonation):** yt-dlp only uses it when an extractor or
  `--impersonate` asks for it, and YouTube doesn't. The README also warns that
  forcing impersonation hurts speed and stability.
- **AtomicParsley, phantomjs, rtmpdump, secretstorage:** they cover thumbnail
  embedding, legacy non-YouTube JS, RTMP and browser-cookie extraction, none of
  which this service uses.
- **`remote_components`:** not needed because yt-dlp-ejs is installed as a package.
  The service never fetches solver code at runtime.

`scripts/check_deps.py` (`bun run check:deps`) checks all of this: ffmpeg/ffprobe,
minimum runtime versions, and that yt-dlp-ejs matches yt-dlp's pin. The Docker build
runs it, so a broken image fails to build. `/healthz` also reports `yt_dlp_ejs_version`
and returns `503` if it's missing.

### Extractor arguments

`YTDLP_EXTRACTOR_ARGS` passes yt-dlp `--extractor-args` values and is parsed by
yt-dlp's own option parser (config files ignored). Separate entries with spaces and
quote values that contain spaces. Examples:

```bash
# Try other YouTube player clients when the defaults are blocked or missing formats
YTDLP_EXTRACTOR_ARGS="youtube:player_client=default,mweb"
# Manually supplied PO token (see the yt-dlp PO Token Guide)
YTDLP_EXTRACTOR_ARGS="youtube:player_client=default,mweb;po_token=mweb.gvs+TOKEN"
```

Leave it unset unless downloads fail. yt-dlp's defaults are tuned per release.

## Deploying to Render

1. New → Blueprint, point at this repo (`render.yaml`): Docker runtime, Free plan,
   health check `/healthz`. In the service settings, set auto-deploy to
   "After CI checks pass" so a red build never ships.
2. Set `API_KEY` and `ALLOWED_ORIGINS`; `SIGNING_SECRET` is generated.
3. Run the smoke test above against `https://<service>.onrender.com` for each format
   and a 3-video batch.

Free instances sleep after 15 minutes idle (≈1 min cold start). Have the Nuxt app
call `/healthz` when the YouTube page opens to wake it up.

## Deploying to Railway

Railway builds the same `Dockerfile` (selected by `railway.json`, which also sets the
`/healthz` health check and an on-failure restart policy). Railway injects `PORT`,
which the image's `CMD` already uses.

1. New Project → Deploy from GitHub repo (or `railway up` from this directory).
2. In the service **Variables**, set:

   | Variable | Value |
   | --- | --- |
   | `API_KEY` | a long random secret (shared with the Nuxt app) |
   | `SIGNING_SECRET` | a long random secret (`openssl rand -hex 32`) |
   | `ALLOWED_ORIGINS` | the Vercel origin(s), comma-separated |
   | `PUBLIC_BASE_URL` | `https://${{RAILWAY_PUBLIC_DOMAIN}}` |
   | `OPENROUTER_API_KEY` | optional (lyrics LLM arrangement) |

   The limits in `.env.example` all have defaults; override only what you need.
3. Settings → Networking → **Generate Domain**.
4. Run the smoke test above against `https://<service>.up.railway.app`.

Railway's filesystem is ephemeral, so `WORK_DIR` (`/tmp/cloudy`) is cleared on every
deploy/restart; that's fine because files only live for `FILE_TTL_MINUTES`. Unlike
Render's Free plan the service doesn't sleep when idle, but watch memory: each
download runs ffmpeg plus a JS runtime, so keep `MAX_CONCURRENT_JOBS=1` on small plans.

### When downloads start failing

1. **Bump yt-dlp first.** YouTube changes often. Run
   `uv lock --upgrade-package yt-dlp --upgrade-package yt-dlp-ejs` (yt-dlp-ejs must
   move with yt-dlp), commit `uv.lock`, and redeploy. The Docker build fails if the
   two don't match.
2. "Sign in to confirm you're not a bot" (`bot_check`): Render's datacenter IPs get
   flagged. Add a cookies file as a Render Secret File and set
   `YTDLP_COOKIES_FILE=/etc/secrets/cookies.txt`, set `YTDLP_PROXY`, or try other
   player clients / a PO token via `YTDLP_EXTRACTOR_ARGS`.
3. `/healthz` returning `503` means ffmpeg, yt-dlp-ejs, or every enabled JS runtime
   is missing from the image. Check `js_runtimes` and `yt_dlp_ejs_version` in the response.

If the Python 3.15 image breaks (missing wheels), switch the Dockerfile to
`python:3.14-slim-trixie` and lower `.python-version` / `requires-python` to 3.14.

## Nuxt integration

(YouTube; for lyrics see [Lyrics to PowerPoint](#lyrics-to-powerpoint). The API key
must also stay on the Nuxt server there: proxy the POST and stream the bytes back.)

1. A Nuxt server route (`/api/yt/*`) reads `NUXT_CLOUDY_API_URL` and
   `NUXT_CLOUDY_API_KEY` and proxies only the JSON calls (metadata, create, poll),
   adding `X-API-Key`. The key never reaches the browser.
2. The client polls `GET /v1/jobs/{id}` every 2–3 s and shows `progress` / `items`.
3. When `status` is `done`, the browser navigates to `download_url`, which goes
   straight to Render. The link expires in about 10 minutes; poll again to get a fresh one.
4. Treat `404` on a job as "expired, retry", and `429` as "busy, retry later".
