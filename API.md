# TTS API Reference

Base URL: `http://localhost:8880`

## Authentication

The service has **built-in API Key authentication** so external programmatic clients (browser extensions, scripts, other backends) can call it directly.

- **Where the key comes from**: set `TTS_API_KEY` in `docker-compose.yml` (or the environment). Change it to your own strong random value.
- **Empty (unset) = fully open**: any request is allowed. This keeps local direct-connect usage working.
- **Same-origin pages are exempt**: the bundled UI (`/index.html`) and this docs page (`/api`) are served from the same origin and need no key when used in a browser.
- **External clients must send the key**:
  - REST — header `X-API-Key: <key>` (preferred, especially behind Caddy Basic Auth) or `Authorization: Bearer <key>`.
  - WebSocket — query parameter `/ws/tts?key=<key>` (browsers cannot set custom headers on the WS handshake).

Protected endpoints: `GET /api/voices`, `GET /api/voices/preview`, `GET /api/logs`, `POST /api/tts`, `WebSocket /ws/tts`.
Always-exempt endpoints: `GET /` (health check), `/index.html`, `/api`, `/static/style.css`, `/favicon.ico`.

> Honest boundary: same-origin exemption relies on the `Origin`/`Referer` header, which non-browser clients can forge. For real network isolation still put a reverse proxy (Caddy) in front; the key provides controlled access for external integrations.

Rejected requests return `401` (REST) or WebSocket close code `1008` (handshake).

---

## Runtime configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `TTS_API_KEY` | empty | Enables API key auth when set. |
| `MAX_TEXT_LENGTH` | `100000` | Max input characters accepted by REST and WebSocket requests. |
| `TTS_CORS_ALLOW_ORIGINS` | `*` | Comma-separated CORS allowed origins. Keep `*` for local/open use; set explicit origins for public deployments. |
| `EDGE_VOICES_CACHE_TTL_SECONDS` | `86400` | Edge voice-list cache TTL. Refresh failures do not poison a successful stale cache. |
| `TTS_SYNTHESIS_TIMEOUT_SECONDS` | `0` | REST/WebSocket synthesis timeout. `0` disables it. REST returns `504` before audio starts; after streaming starts, timeout stops the stream and reaps resources. WebSocket returns `error`. |
| `TTS_MAX_FFMPEG_PROCESSES` | `2` | Maximum concurrent `ffmpeg` subprocesses. Excess REST requests fail fast with `429`; WebSocket synthesis returns `error`. |
| `TTS_MAX_SYNTHESIS_CONCURRENCY` | `2` | Maximum concurrent Kokoro inferences (shared by REST and WebSocket). Excess requests queue (block) rather than fail. WebSocket Kokoro produces no `ffmpeg` process, so this is its only concurrency guard. |

Kokoro currently runs with CPU-only PyTorch wheels in this project. The Docker image and dependency lock do not enable GPU acceleration.

---

## `GET /api/logs`

Diagnostic endpoint for the in-memory ring buffer. Returns the latest log lines retained by the process.

This endpoint is **not** same-origin exempt. When `TTS_API_KEY` is configured, clients must send a valid key using `X-API-Key` or `Authorization: Bearer`, even from the bundled pages. Returned lines redact obvious API key forms such as `Authorization: Bearer ...`, `X-API-Key: ...`, `?key=...`, and `TTS_API_KEY=...`.

### Query

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | integer | `100` | Number of recent lines to return, range `1`–`1000` |

### Response `200`

```json
{
  "limit": 100,
  "total_buffered": 287,
  "lines": ["2026-... INFO tts-api: ..."]
}
```

### Response `401`

```json
{ "detail": "缺少或错误的 API Key" }
```

---

## `GET /api/auth`

Key-probe endpoint. Verifies whether the supplied key is valid, so clients can check the key before making real calls. This endpoint is **not** same-origin exempt — even the bundled pages must send a valid key to get `authorized: true`.

Send the key the same way as any REST call (`X-API-Key: <key>` preferred, or `Authorization: Bearer <key>`).

### Response `200` (authorized, or auth disabled)

```json
{ "auth": "enabled", "authorized": true }
```

```json
{ "auth": "disabled", "authorized": true }
```

### Response `401` (missing or wrong key)

```json
{ "auth": "enabled", "authorized": false, "detail": "缺少或错误的 API Key" }
```

---

## `GET /`

Health check. Returns the engine and `ffmpeg` readiness status.

### Response `200`

```json
{
  "status": "v0.1 engine running",
  "ready": true
}
```

### Response `503` (engines not ready)

```json
{
  "status": "starting",
  "ready": false
}
```

### Response `503` (`ffmpeg` missing)

```json
{
  "status": "ffmpeg missing",
  "ready": false
}
```

---

## `GET /api/voices`

List all available voices for both engines.

### Response `200`

```json
{
  "kokoro": [
    { "id": "zf_xiaoxiao", "name": "晓晓", "gender": "female", "language": "zh" },
    { "id": "am_michael",  "name": "Michael", "gender": "male", "language": "en" }
  ],
  "edge": [
    { "id": "zh-CN-XiaoxiaoNeural", "name": "Microsoft Xiaoxiao Online (Natural) - Chinese (Mainland)", "gender": "Female", "locale": "zh-CN" },
    { "id": "en-US-AvaNeural",      "name": "Microsoft Ava Online (Natural) - English (United States)", "gender": "Female", "locale": "en-US" }
  ]
}
```

Edge voices are fetched live from Microsoft and cached for `EDGE_VOICES_CACHE_TTL_SECONDS` seconds. If a refresh fails after a successful fetch, the endpoint returns the stale cache instead of replacing it with an empty list.

---

## `GET /api/voices/preview`

Synthesize a short built-in preview phrase for one voice. This endpoint is intended for UI voice audition and uses the same synthesis pipeline and limits as `/api/tts`.

### Query

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `engine` | `string` | `"kokoro"` | `"kokoro"` or `"edge"` |
| `voice` | `string` | `"af_heart"` | Voice ID |
| `speed` | `number` | `1.0` | Playback speed, range `0.5`–`3.0` |

Returns `audio/mpeg` with `Content-Disposition: inline`.

---

## `POST /api/tts`

Stream MP3 audio for the given text.

### Request Body

```json
{
  "text": "Hello world",
  "engine": "kokoro",
  "voice": "zf_xiaoxiao",
  "speed": 1.0,
  "ssml": false
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `text` | `string` | — | Input text (required, non-empty, max length from `MAX_TEXT_LENGTH`, default `100000`) |
| `engine` | `string` | `"kokoro"` | `"kokoro"` (local) or `"edge"` (Microsoft cloud) |
| `voice` | `string` | `"zf_xiaoxiao"` | Voice ID from `/api/voices` |
| `speed` | `number` | `1.0` | Playback speed, range `0.5`–`3.0` |
| `ssml` | `boolean` | `false` | Reserved flag. Raw SSML is currently rejected with `422` because `edge-tts` escapes input text before building its own SSML request. |

### Query

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `download` | `boolean` | `false` | When `true`, response uses `Content-Disposition: attachment; filename=tts-output.mp3`. |

### Response

- Status `200` — `audio/mpeg` streaming response with `Content-Disposition: inline`.
- Status `400` — Empty text, text empty after Markdown cleaning, or no speakable content for the selected voice.
- Status `401` — Missing or wrong API key (only when `TTS_API_KEY` is set and the request is neither same-origin nor authenticated).
- Status `429` — `ffmpeg` process limit reached.
- Status `422` — Invalid field value, text longer than `MAX_TEXT_LENGTH`, or `ssml=true`.
- Status `500` — Kokoro/local synthesis failed before any audio was produced.
- Status `502` — Edge/upstream synthesis failed before any audio was produced.
- Status `503` — Engines not ready.
- Status `504` — Synthesis timed out before any audio was ready.

Every REST `/api/tts` response includes `X-Request-ID`. A client may provide `X-Request-ID`; otherwise the server generates one. The same id is included in REST failure logs.

> Streaming edge case: once the first audio byte is committed the `200` status is locked and cannot be downgraded to an error code. The server preflights synthesis (waiting for the first source bytes before returning `200`), so failures that occur *before* streaming starts still surface as the correct `4xx`/`5xx`. But if transcoding fails *after* the first byte has been sent (e.g. an `ffmpeg` crash mid-stream, or a post-start timeout), the client receives a truncated or empty-tail `200` body rather than an error status. Clients should treat an unexpectedly short/empty `200` as a failure and check the correlating `X-Request-ID` in the server logs. This is an inherent limitation of HTTP streaming, not a recoverable status.

---

## `WebSocket /ws/tts`

Interactive TTS session. Send a JSON request, receive binary PCM frames interleaved with JSON control messages.

When `TTS_API_KEY` is set, the handshake is authenticated: same-origin pages (the built-in UI) connect without a key, while external clients must pass the key as a query parameter — `/ws/tts?key=<key>`. Browsers cannot set custom headers on a WebSocket handshake, hence the query parameter. A failed check closes the handshake with code `1008` (Policy Violation) before `accept`.

### Audio Format

- Sample rate: **24000 Hz**
- Channels: **1 (mono)**
- Bit depth: **16-bit signed little-endian PCM**

### Client → Server

Send a JSON message:

```json
{
  "text": "Hello world.\nNice to meet you.",
  "engine": "kokoro",
  "voice": "zf_xiaoxiao",
  "speed": 1.0
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `text` | `string` | — | Input text (required, non-empty, max length from `MAX_TEXT_LENGTH`, default `100000`) |
| `engine` | `string` | `"kokoro"` | `"kokoro"` or `"edge"` |
| `voice` | `string` | `"zf_xiaoxiao"` | Voice ID |
| `speed` | `number` | `1.0` | Playback speed, range `0.5`–`3.0` (clamped server-side) |

Separate sentences with `\n` — the server uses these as synthesis units and aligns segment boundaries 1:1 for gapless streaming.

### Server → Client

| Message | Type | Description |
|---------|------|-------------|
| `{"type":"start"}` | JSON | Begin processing a request |
| `{"type":"seg","text":"..."}` | JSON | Sentence boundary marker; marks the start of one synthesis unit in PCM order |
| Binary frame | `ArrayBuffer` | 24kHz 16-bit mono PCM chunk (typically 2048 bytes) |
| `{"type":"end"}` | JSON | All audio sent; connection stays open for the next request |
| `{"type":"error","message":"..."}` | JSON | Request rejected or synthesis failed; connection stays open for the next request |

The `error` message covers: invalid JSON, missing/empty `text`, text over `MAX_TEXT_LENGTH`, unknown `engine`, unknown Kokoro `voice`, synthesis timeout, and synthesis failure (e.g. an invalid Edge voice or engine fault). On synthesis failure the server sends `error` instead of `end`, so the client leaves the "synthesizing" state without a false success.

### Cancel / Interrupt

While the server is synthesizing, the client can send any message (or close the connection) to cancel the current request. The server immediately stops synthesis and starts listening for the next request.

### Auto Engine

`"engine":"auto"` is a **client-side UI option only** — it is never sent to the server. When Auto is selected, the browser splits text by sentence and language boundary: Chinese (including Chinese punctuation) routes to the Chinese voice, and English (including single terms and all-caps abbreviations like `DNS` or `OpenWrt`) always routes to the English voice. Each Kokoro pipeline is monolingual — the Chinese pipeline cannot even read isolated English abbreviations (official issues #95/#238) — so English is never merged into a Chinese segment.

### Text Preprocessing

The server automatically strips Markdown formatting (code blocks, inline code, images, links, headings, blockquotes, lists, bold/italic/strikethrough, horizontal rules) and removes quotation marks (straight double, curly, CJK corner brackets, guillemets) before synthesis. This prevents markup from being read aloud and avoids phoneme artifacts caused by quotes in Kokoro.

Additionally, Kokoro applies per-voice language filtering as a fallback: Chinese voices (`zf_*`/`zm_*`) strip all English letters before synthesis, while English voices (`af_*`/`am_*`) strip all CJK characters. Because each Kokoro pipeline is monolingual and mispronounces the other language, stripping is preferable to reading garbled output. For mixed Chinese/English text, use Auto mode in the UI to route each segment to the matching voice.

---

## Static Files

| Path | Content |
|------|---------|
| `/index.html` | Single-page browser UI |
| `/api` | API documentation page (with interactive tester) |
| `/static/style.css` | Stylesheet |
| `/favicon.ico` | Site favicon (200) |

`/index.html` and `/api` are served as `text/html; charset=utf-8`. The HTML files also declare `<meta charset="UTF-8">`, so Chinese and English UI text do not depend on browser charset guessing.

FastAPI's default `/docs`, `/redoc`, and `/openapi.json` surfaces are disabled to avoid conflicting with the authenticated custom `/api` page.

Exempt paths (`/`, `/index.html`, `/api`, `/static/style.css`, `/favicon.ico`) are always reachable without a key so the pages and the Docker health check keep working even when `TTS_API_KEY` is set.

---

## Validation

The repository includes a `unittest` regression suite under `tests/`. It uses fake Kokoro/Edge dependencies to validate protocol and boundary behavior without requiring model weights, `ffmpeg`, `espeak-ng`, or network access:

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

Covered contracts include auth enforcement, CORS on auth failures, `/api/auth`, `/api/logs`, `/api/voices`, REST `/api/tts` status codes, REST preflight cleanup, synthesis timeout, request-id headers, readiness checks, startup warmup wiring, Edge voice-cache semantics, ffmpeg command construction and process limiting, WebSocket handshake/auth/error/end/reuse behavior, request validation, text preprocessing, sentence splitting, voice-language filtering, PCM encoding, synthesis-unit plumbing, and frontend HTML/JS contracts for voice routing plus the API tester.
