# Nuxt integration report

What the Cloudy Nuxt app (Vercel) needs to call this FastAPI service (Railway)
correctly after the move to **unversioned `/api` routes** and the **pytubefix-only**
download engine.

The interactive docs (Scalar) with every schema and error code are at
`https://<cloudy-api>/docs`; the raw spec is at `/openapi.json`.

---

## 1. What changed (migration checklist)

| Before | Now |
|---|---|
| `/v1/videos/{id}`, `/v2/videos/{id}` | `/api/videos/{id}` |
| `/v1/jobs`, `/v2/jobs` | `/api/jobs` |
| `/v1/jobs/{id}`, `/v2/jobs/{id}` | `/api/jobs/{id}` |
| `/v1/jobs/{id}/file?exp&sig` | `/api/jobs/{id}/file?exp&sig` (comes ready-made in `download_url`) |
| `/v1/lyrics/pptx`, `/v1/lyrics/pptx/batch` | `/api/lyrics/pptx`, `/api/lyrics/pptx/batch` |
| `/healthz` | `/healthz` (unchanged, root) |

- Any `/v1/*` or `/v2/*` call now returns **`404`**. Update every path in the Nuxt
  server routes before (or together with) deploying this API.
- Request and response **bodies are unchanged**. Only the paths moved.
- There's a single download engine (pytubefix). Any "switch to v2 when bot-checked"
  logic in Nuxt can be deleted.
- `download_url` now points at `/api/jobs/{id}/file`. Always use it as returned;
  never build it yourself.

To-do in the Nuxt repo:

- [ ] Replace `/v1/` and `/v2/` with `/api/` in every upstream URL.
- [ ] Remove the v1/v2 engine switch (if any).
- [ ] Check `NUXT_CLOUDY_API_URL` has **no** trailing `/v1` (it's just the origin,
      e.g. `https://cloudy-api.up.railway.app`).
- [ ] Optional: regenerate types from `/openapi.json` (section 7).

---

## 2. Configuration

### Nuxt (`nuxt.config.ts`)

Both values are **server-only** (not under `public`), so the API key never reaches
the browser:

```ts
export default defineNuxtConfig({
  runtimeConfig: {
    cloudyApiUrl: '', // NUXT_CLOUDY_API_URL  e.g. https://cloudy-api.up.railway.app
    cloudyApiKey: '', // NUXT_CLOUDY_API_KEY  same value as API_KEY on Railway
  },
})
```

Vercel environment variables: `NUXT_CLOUDY_API_URL`, `NUXT_CLOUDY_API_KEY`.

### This API (Railway variables) that matter to Nuxt

| Variable | Why Nuxt cares |
|---|---|
| `API_KEY` | Must equal `NUXT_CLOUDY_API_KEY`. |
| `ALLOWED_ORIGINS` | The Vercel origin(s), comma-separated, e.g. `https://cloudy-indol.vercel.app`. Only needed if the browser `fetch()`es the file URL (plain navigation doesn't need CORS). |
| `PUBLIC_BASE_URL` | `https://${{RAILWAY_PUBLIC_DOMAIN}}`. Makes `download_url` use the public domain. |
| `YOUTUBE_DATA_API_KEY` | Recommended. Fast metadata without YouTube's bot check. With it, `available_heights` is always `[]`. |
| `MAX_BATCH` (10), `MAX_LYRICS_BATCH` (3), `MAX_DURATION_SECONDS` (1800) | Limits to mirror in the UI. |
| `SIGNED_URL_TTL_SECONDS` (600), `FILE_TTL_MINUTES` (30) | How long a download link and the file live. |

### Naming clash to keep in mind

Nuxt server routes also live under `/api` (`server/api/**`). Keep them under their
own namespace, e.g. Nuxt `/api/cloudy/*` → upstream `${cloudyApiUrl}/api/*`, so it's
always clear which `/api` a path belongs to.

---

## 3. Endpoints

All `/api/*` routes need the header `X-API-Key: <key>`, **except** the signed file
route. Send `Content-Type: application/json` on POSTs.

| Method | Upstream path | Called from | Success |
|---|---|---|---|
| GET | `/healthz` | Nuxt server (optional) | `200` / `503` |
| GET | `/api/videos/{video_id}` | Nuxt server | `200 VideoInfo` |
| POST | `/api/jobs` | Nuxt server | `202 JobCreated` |
| GET | `/api/jobs/{job_id}` | Nuxt server (polled) | `200 JobOut` |
| DELETE | `/api/jobs/{job_id}` | Nuxt server | `204` |
| GET | `/api/jobs/{job_id}/file?exp=…&sig=…` | **Browser directly** | `200` file attachment |
| POST | `/api/lyrics/pptx` | Nuxt server | `200` `.pptx` bytes |
| POST | `/api/lyrics/pptx/batch` | Nuxt server | `200` `.zip` bytes |

### TypeScript types

```ts
export type Format = 'mp4_720' | 'mp4_1080' | 'mp3'
export type JobStatus = 'queued' | 'running' | 'zipping' | 'done' | 'failed' | 'expired'
export type ItemStatus = 'queued' | 'running' | 'done' | 'failed'

export interface ErrorInfo { code: string; message: string }
/** Body of every API error (except request validation, see section 5). */
export interface ApiError { detail: ErrorInfo & { videos?: RejectedVideo[]; items?: LyricsBatchItem[] } }
export interface RejectedVideo { video_id: string; code: string; message: string }

export interface VideoInfo {
  video_id: string
  title: string
  duration: number | null          // seconds
  thumbnail: string | null
  channel: string | null
  available_heights: number[]      // [] when YOUTUBE_DATA_API_KEY is set
  downloadable: boolean
  reason: 'too_long' | 'live' | 'unknown_duration' | null
}

export interface JobCreate { video_ids: string[]; format: Format }
export interface JobCreated { job_id: string; status: JobStatus }

export interface JobItem {
  video_id: string
  title: string | null
  status: ItemStatus
  progress: number                 // 0..1
  error: ErrorInfo | null
  actual_height: number | null     // real height of the MP4 (null for MP3)
}

export interface JobOut {
  job_id: string
  status: JobStatus
  format: Format
  progress: number                 // 0..1, stays ≤ 0.99 until done
  items: JobItem[]
  // Only when status === 'done':
  download_url: string | null      // signed, ~10 min
  filename: string | null          // "Title [id].mp4" or "cloudy-batch-xxxxxxxx.zip"
  size_bytes: number | null
  expires_at: number | null        // Unix seconds when the file is deleted
}

export interface LyricsRequest { html: string; title?: string; font_color?: string } // RRGGBB or #RRGGBB
export interface LyricsBatchRequest { items: LyricsRequest[] }
export interface LyricsBatchItem {
  index: number
  status: 'done' | 'failed'
  filename: string | null
  title: string | null
  arranged_by: 'llm' | 'fallback' | null
  slide_count: number | null
  error: ErrorInfo | null
}
```

### Request rules

- `video_id` must match `^[A-Za-z0-9_-]{11}$`. Extract it from YouTube URLs in Nuxt
  (`watch?v=`, `youtu.be/`, `shorts/`, `embed/`); the API never accepts URLs.
- `video_ids`: 1 to `MAX_BATCH` (10). Duplicates are dropped server-side.
- Videos longer than 30 min, live or upcoming are rejected at job creation.
- `mp4_1080` falls back to the best height ≤ 1080; show `items[].actual_height`.
- Lyrics: `html` ≤ 500,000 chars, ≤ 300 lyric lines; batch ≤ `MAX_LYRICS_BATCH` (3)
  items; whole request body ≤ 4 MiB (`413 request_too_large`).

---

## 4. Nuxt server routes (proxy)

### Shared helper — `server/utils/cloudy.ts`

```ts
import type { H3Event } from 'h3'
import { FetchError } from 'ofetch'

const VIDEO_ID = /^[A-Za-z0-9_-]{11}$/
const JOB_ID = /^[0-9a-f]{32}$/

type CloudyOptions = Parameters<typeof $fetch>[1]

function withCloudy(opts: CloudyOptions = {}): CloudyOptions {
  const { cloudyApiUrl, cloudyApiKey } = useRuntimeConfig()
  return {
    ...opts,
    baseURL: cloudyApiUrl,
    headers: { 'X-API-Key': cloudyApiKey, ...(opts.headers as Record<string, string>) },
    timeout: 60_000,
  }
}

/** JSON calls: returns the parsed body. */
export const cloudyFetch = <T>(path: string, opts?: CloudyOptions) =>
  $fetch<T>(path, withCloudy(opts))

/** Binary calls (lyrics): returns the raw response, headers included. */
export const cloudyFetchRaw = <T>(path: string, opts?: CloudyOptions) =>
  $fetch.raw<T>(path, withCloudy(opts))

/** Forward the API's status and `{detail: {code, message}}` body unchanged. */
export function forwardCloudyError(event: H3Event, err: unknown) {
  if (err instanceof FetchError && err.response) {
    setResponseStatus(event, err.response.status)
    return err.data ?? { detail: { code: 'upstream_error', message: err.message } }
  }
  setResponseStatus(event, 502)
  return { detail: { code: 'upstream_unreachable', message: 'Cloudy API unreachable' } }
}

export function assertVideoId(id: string | undefined): string {
  if (!id || !VIDEO_ID.test(id)) throw createError({ statusCode: 400, statusMessage: 'Invalid video id' })
  return id
}

export function assertJobId(id: string | undefined): string {
  if (!id || !JOB_ID.test(id)) throw createError({ statusCode: 404, statusMessage: 'Job not found' })
  return id
}
```

Validating ids before building the upstream path stops a crafted id (e.g.
`../lyrics/pptx`) from reaching another API route with your key.

### Routes

```ts
// server/api/cloudy/videos/[id].get.ts
export default defineEventHandler(async (event) => {
  const id = assertVideoId(getRouterParam(event, 'id'))
  try { return await cloudyFetch<VideoInfo>(`/api/videos/${id}`) }
  catch (err) { return forwardCloudyError(event, err) }
})

// server/api/cloudy/jobs/index.post.ts
export default defineEventHandler(async (event) => {
  const body = await readBody<JobCreate>(event)
  try {
    setResponseStatus(event, 202)
    return await cloudyFetch<JobCreated>('/api/jobs', { method: 'POST', body })
  } catch (err) { return forwardCloudyError(event, err) }
})

// server/api/cloudy/jobs/[id].get.ts
export default defineEventHandler(async (event) => {
  const id = assertJobId(getRouterParam(event, 'id'))
  try { return await cloudyFetch<JobOut>(`/api/jobs/${id}`) }
  catch (err) { return forwardCloudyError(event, err) }
})

// server/api/cloudy/jobs/[id].delete.ts
export default defineEventHandler(async (event) => {
  const id = assertJobId(getRouterParam(event, 'id'))
  try {
    await cloudyFetch(`/api/jobs/${id}`, { method: 'DELETE' })
    return sendNoContent(event)
  } catch (err) { return forwardCloudyError(event, err) }
})
```

Lyrics return **binary**, so read the raw response and pass the useful headers on:

```ts
// server/api/cloudy/lyrics/pptx.post.ts   (same for pptx/batch.post.ts → '/api/lyrics/pptx/batch')
export default defineEventHandler(async (event) => {
  const body = await readBody(event)
  try {
    const res = await cloudyFetchRaw<ArrayBuffer>('/api/lyrics/pptx', {
      method: 'POST', body, responseType: 'arrayBuffer',
    })
    for (const h of ['content-type', 'content-disposition', 'x-slide-count',
                     'x-lyrics-arranged-by', 'x-batch-succeeded', 'x-batch-failed']) {
      const v = res.headers.get(h)
      if (v) setResponseHeader(event, h, v)
    }
    return Buffer.from(res._data!)
  } catch (err) { return forwardCloudyError(event, err) }
})
```

Vercel notes:

- **Never proxy the video file through Vercel.** Function responses are capped at
  4.5 MB and the files are much bigger. The browser downloads from `download_url`
  directly (section 5).
- Lyrics batches can take up to ~45 s (`LYRICS_BATCH_DEADLINE_SECONDS`). Give those
  two routes enough function time, e.g. in `nuxt.config.ts`:
  `nitro: { vercel: { functions: { maxDuration: 60 } } }` (check your plan's limit).
- `POST /api/jobs` waits for the metadata check of every video (usually < 2 s with
  `YOUTUBE_DATA_API_KEY`, longer without it). Keep a ~60 s timeout.

---

## 5. Client flow

### Download a video (or a batch)

1. Optional preview: `GET /api/cloudy/videos/{id}` → show title, thumbnail,
   duration; disable the button when `downloadable` is `false` (`reason`).
2. `POST /api/cloudy/jobs` `{ video_ids, format }` → `202 { job_id }`.
3. Poll `GET /api/cloudy/jobs/{job_id}` every **2–3 s** while `status` is `queued`,
   `running` or `zipping`. Show `progress` (0..1) and per-item `status`/`error`.
4. On `done`: start the download with the returned `download_url`, as is:
   ```ts
   window.location.assign(job.download_url!) // or <a :href="job.download_url">
   ```
   The response is `Content-Disposition: attachment`, so the page stays put and
   the browser saves `filename`. If the user clicks later (the link may be older
   than ~10 min), fetch the job once more first and use the fresh `download_url`
   (see Expiry rules).
5. On `failed`: every item failed; show `items[].error.message`.
   A batch is `done` if at least one item worked. The ZIP only has the successful
   ones, so show the failed items' errors too.
6. Optional: `DELETE /api/cloudy/jobs/{job_id}` when the user cancels.

Minimal composable:

```ts
export function useCloudyJob() {
  const job = ref<JobOut | null>(null)
  const error = ref<ErrorInfo | null>(null)
  let timer: ReturnType<typeof setTimeout> | undefined

  async function start(video_ids: string[], format: Format) {
    error.value = null
    try {
      const { job_id } = await $fetch<JobCreated>('/api/cloudy/jobs', { method: 'POST', body: { video_ids, format } })
      await poll(job_id)
    } catch (e: any) { error.value = e?.data?.detail ?? { code: 'network', message: String(e) } }
  }

  async function poll(id: string) {
    try {
      job.value = await $fetch<JobOut>(`/api/cloudy/jobs/${id}`)
    } catch (e: any) {
      error.value = e?.data?.detail ?? { code: 'network', message: String(e) }
      return
    }
    if (['queued', 'running', 'zipping'].includes(job.value.status)) {
      timer = setTimeout(() => poll(id), 2500)
    }
  }

  onScopeDispose(() => clearTimeout(timer))
  return { job, error, start, poll }
}
```

### Expiry rules

- `download_url` is valid ~10 min (`SIGNED_URL_TTL_SECONDS`) and is re-signed on
  every poll. A stale link opens a `403 link_expired` JSON page in the browser, so
  re-fetch the job right before navigating whenever the last poll is more than a
  few minutes old.
- The file is deleted 30 min after the job finished (`expires_at`; the cleanup
  runs every 5 min, so it can take a little longer). After that the job reports
  `status: "expired"` and the file route answers `410 expired`: create a new job.
- Jobs live in memory. A redeploy/restart loses them, so **`404` on a job you
  created means "expired, create it again"**.

### Lyrics

`POST /api/cloudy/lyrics/pptx` with `{ html, title?, font_color? }` → blob. Save it
client-side:

```ts
const res = await $fetch.raw<Blob>('/api/cloudy/lyrics/pptx', { method: 'POST', body, responseType: 'blob' })
const name = /filename\*=UTF-8''([^;]+)/.exec(res.headers.get('content-disposition') ?? '')?.[1]
const url = URL.createObjectURL(res._data!)
Object.assign(document.createElement('a'), { href: url, download: decodeURIComponent(name ?? 'lyrics.pptx') }).click()
URL.revokeObjectURL(url)
```

`X-Lyrics-Arranged-By: fallback` means the LLM wasn't used (no key, timeout or an
invalid plan); the file is still valid. For batches, read `manifest.json` inside the
ZIP or the `X-Batch-Succeeded` / `X-Batch-Failed` headers for partial failures.

---

## 6. Errors

API errors are `{"detail": {"code": "...", "message": "..."}}`; branch on `code`,
not on `message`. Malformed requests (bad id pattern, unknown `format`, wrong JSON)
get FastAPI's standard `422` whose `detail` is a **list**:
`{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}`. Check
`Array.isArray(detail)`.

| Status | `code` | Where | Suggested UI |
|---|---|---|---|
| 401 | `unauthorized` | any `/api` | Config bug: key mismatch between Vercel and Railway. |
| 404 | `not_found` | jobs | "Expired, try again" → create a new job. |
| 422 | `videos_rejected` | create job | List `detail.videos[]` (`too_long`, `live`, `unavailable`, `private`, …) and let the user drop them. |
| 422 | `batch_too_large` | create job / lyrics batch | Cap selection at `MAX_BATCH` / `MAX_LYRICS_BATCH`. |
| 422 | `private`, `unavailable`, `age_restricted`, `members_only`, `geo_restricted`, `live` | video metadata | "This video can't be downloaded" + message. |
| 422 | `no_lyrics`, `lyrics_too_long` | lyrics | Ask for a different page/song. |
| 422 | `all_items_failed` | lyrics batch | Show `detail.items[].error`. |
| 429 | `queue_full` | create job | "Busy, try again in a minute". |
| 403 | `invalid_signature`, `link_expired` | file (browser) | Poll the job for a fresh `download_url`. |
| 410 | `expired` | file (browser) | Create the job again. |
| 413 | `request_too_large` | lyrics | Send less HTML / fewer songs. |
| 502 | `bot_check` | metadata, create job | "YouTube is blocking the server, try later". Report it: the operator may need `PYTUBEFIX_PROXY` or a pytubefix bump. |
| 502 | `quota_exceeded`, `metadata_unavailable` | metadata, create job | Retry later (YouTube Data API quota resets daily). |
| 502 | `download_failed`, `ffmpeg_missing` | metadata, create job | Generic "try again later". |

Per-item codes inside `JobOut.items[].error.code`: `private`, `unavailable`,
`age_restricted`, `members_only`, `geo_restricted`, `live`, `format_unavailable`,
`bot_check`, `ffmpeg_missing`, `download_failed`, `cancelled`, `internal`.

---

## 7. Optional: generated types

The spec has stable operation ids (`get_video`, `create_job`, `get_job`,
`delete_job`, `download_file`, `create_lyrics_pptx`, `create_lyrics_pptx_batch`,
`healthz`), so you can generate the types above instead of hand-writing them:

```bash
npx openapi-typescript https://<cloudy-api>/openapi.json -o types/cloudy-api.d.ts
```

---

## 8. Smoke test after deploying both sides

1. `curl https://<cloudy-api>/healthz` → `200`, `"status": "ok"`, `"pytubefix_node": true`.
2. `curl https://<cloudy-api>/v1/jobs` → `404` (old paths really gone).
3. From the Nuxt app: preview a video, create an `mp4_720` job, watch progress,
   download the file; repeat with `mp3` and a 3-video batch (ZIP).
4. Convert one lyrics page and a 2-song batch.
5. Wait > 10 min after a job finished, click download again → Nuxt should re-fetch
   the job and use the fresh link instead of landing on a 403 page.
