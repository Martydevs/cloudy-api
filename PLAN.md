# Plan: sequential batch lyrics → PPTX (max 3 per request)

## Context
Today `POST /v1/lyrics/pptx` (`src/cloudy_api/routers/lyrics.py:19`) turns **one** Letras HTML page into **one** `.pptx`.
The frontend (Nuxt) wants to stack several songs and get the generated files back in one request. Decisions confirmed with the user:

- **Input:** the frontend keeps scraping and sends **HTML items** (the same fields as `LyricsRequest`). The server does not fetch URLs, so there is no SSRF surface and no host allow-list.
- **Limit:** at most **3 items per request**.
- **Processing:** strictly **sequential**. One item at a time, which also suits the 512 MB Render free plan and the single shared LLM arranger.
- **Response:** one **ZIP** containing the `.pptx` files plus a `manifest.json`.
- **Failures:** **partial success**. Good items are returned and bad ones are reported per item. If every item fails, return 422.

The existing single endpoint stays as is, with the same behavior and response.

## Design

**Endpoint:** `POST /v1/lyrics/pptx/batch` in `routers/lyrics.py`. It sits under the existing router, so it inherits `require_api_key`.

**Request** (`schemas.py`, next to `LyricsRequest`, line 97):
```python
class LyricsBatchRequest(BaseModel):
    # Upper bound comes from MAX_LYRICS_BATCH and is enforced in the route (same pattern as JobCreate/MAX_BATCH).
    items: Annotated[list[LyricsRequest], Field(min_length=1)]
```
Each item keeps its own `html`, `title` and `font_color`.

**Limit:** new `Settings.max_lyrics_batch: int = Field(default=3, ge=1)` in `config.py:50`, next to `max_batch`.
- The route checks `len(body.items) > settings.max_lyrics_batch`.
- On failure it raises 422 with `detail={"code": "batch_too_large", "message": "At most 3 lyrics per request"}`. This mirrors `routers/jobs.py:108-111`.
- Wire it into `.env.example`, `render.yaml` (`MAX_LYRICS_BATCH`, `"3"`) and the README config table.

**Shared per-item pipeline:** pull the body of `create_lyrics_pptx` into a helper that both endpoints use. Put it in a new `services/lyrics_pptx.py`:
```python
@dataclass(frozen=True)
class LyricsPptx:
    content: bytes; title: str; ascii_name: str; full_name: str
    arranged_by: str; slide_count: int

async def generate_lyrics_pptx(item: LyricsRequest, arranger: LyricsArranger) -> LyricsPptx
```
It reuses what is already there:
- `parse_lyrics_html` via `asyncio.to_thread`.
- `arranger.arrange`.
- `build_presentation` via `asyncio.to_thread`.
- `presentation_filename`.

It raises `LyricsError` for bad input. The single endpoint calls it and keeps its `LyricsError` → 422 mapping and its headers. `arranger.arrange` never raises (it falls back internally), so `LyricsError` is the only per-item failure to catch.

**Batch route flow:**
1. Enforce the limit.
2. `for i, item in enumerate(body.items)` runs sequentially with no `gather`.
3. Call `generate_lyrics_pptx` and catch `LyricsError`. Record `ItemResult(index, status="done"|"failed", ...)`.
4. If nothing succeeded, raise 422 `{"code": "all_items_failed", "message": ..., "items": [...per-item errors]}`.
5. Build the ZIP in memory with `zipfile.ZIP_STORED` (PPTX is already compressed) and return it.

**ZIP contents:**
- `<ascii title>.pptx` per successful item. Duplicate names get ` (2)`, ` (3)` suffixes so entries never collide.
- `manifest.json`: `{"items": [{index, status, filename, title, arranged_by, slide_count, error}]}`. `error` reuses `ErrorInfo{code,message}` (`schemas.py:55`), the same per-item pattern as `ItemOut`. Define `LyricsBatchItemOut` and `LyricsBatchManifest` in `schemas.py`.
- Response: `application/zip`, `Content-Disposition: attachment; filename="lyrics.zip"`, plus `X-Batch-Succeeded` and `X-Batch-Failed` counts. Document the 200 response in the OpenAPI `responses=`, like the single endpoint does.

CORS needs no change. The browser doesn't call this directly, since `allow_methods` is GET only and the Nuxt server proxies it with `X-API-Key`.

## Files
| File | Change |
|---|---|
| `src/cloudy_api/routers/lyrics.py` | add batch route, slim the single route onto the shared helper |
| `src/cloudy_api/services/lyrics_pptx.py` | **new**: `LyricsPptx`, `generate_lyrics_pptx`, ZIP builder with filename dedupe |
| `src/cloudy_api/schemas.py` | `LyricsBatchRequest`, `LyricsBatchItemOut`, `LyricsBatchManifest` |
| `src/cloudy_api/config.py` | `max_lyrics_batch` |
| `.env.example`, `render.yaml`, `README.md` | `MAX_LYRICS_BATCH=3` plus an endpoint description |
| `tests/unit/test_routes_lyrics_batch.py` | **new** |

## Risk to flag
Render's proxy times out around 100 s (general platform knowledge, not in the repo). Worst case is 3 sequential songs × (30 s LLM timeout + 1 retry), which can exceed that.
- Typical runs are well under, because the LLM call takes a few seconds.
- I'm not adding a deadline mechanism in this change, to keep it small.
- If it bites in practice, the follow-up is a total-batch deadline that switches the remaining items to `fallback_plan`, or lowering `LLM_TIMEOUT_SECONDS`.

## Verification
- **Tests** (reuse `tests/conftest.py`: `client`, `AUTH`, `FakeChain`, `make_settings`; read ZIP entries with `zipfile` and PPTX with `Presentation(BytesIO(...))`, as `test_routes_lyrics.py` does):
  - 3 valid items → ZIP with 3 `.pptx` plus a manifest (all `done`).
  - Sequential order: the `FakeChain.calls` order matches the item order.
  - Mixed: 1 bad HTML → 200, 2 PPTX files, manifest shows the `failed` item with code `no_lyrics`.
  - All bad → 422 `all_items_failed` with per-item errors.
  - 4 items → 422 `batch_too_large`. Empty list → 422 (pydantic).
  - Duplicate titles → distinct entry names.
  - Per-item `title` and `font_color` overrides are honored.
  - 401 without an API key.
  - LLM failure → fallback, with `arranged_by="fallback"` in the manifest.
  - The existing `test_routes_lyrics.py` still passes unchanged (regression check for the refactor).
- **Run:** `uv run pytest` (coverage gate `fail_under = 90`).
- **Manual:** `bun run dev`, then `curl -X POST localhost:<port>/v1/lyrics/pptx/batch -H "X-API-Key: ..." -H "Content-Type: application/json" -d '{"items":[...]}' -o out.zip && unzip -l out.zip`. Open one PPTX to check the slides.
