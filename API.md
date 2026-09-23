# TTS-API Reference

Base URL: `http://localhost:8880`

## Authentication

The service has **built-in API Key authentication** so external programmatic clients (browser extensions, scripts, other backends) can call it directly.

- **Where the key comes from**: set `TTS_API_KEY` in `docker-compose.yml` (or the environment). Change it to your own strong random value.
- **Empty (unset) = fully open**: any request is allowed. This keeps local direct-connect usage working.
- **Same-origin pages are exempt**: the bundled UI (`/index.html`) and this docs page (`/api`) are served from the same origin and need no key when used in a browser.
- **External clients must send the key**:
  - REST — header `X-API-Key: <key>` (preferred, especially behind Caddy Basic Auth) or `Authorization: Bearer <key>`.
  - WebSocket — query parameter `/ws/tts?key=<key>` (browsers cannot set custom headers on the WS handshake).

Protected endpoints: `GET /api/voices`, `GET /api/voices/preview`, `GET /api/logs`, `POST /api/tts`, `WebSocket /ws/tts`, and all `/v1/*` routes.
Always-exempt endpoints: `GET /` (health check), `/index.html`, `/api`, `/static/style.css`, `/favicon.ico`.

`/v1/*` is **never** same-origin exempt. When `TTS_API_KEY` is set, OpenAI-compatible clients must send a valid key even if they forge `Origin`/`Referer`.

> Honest boundary: same-origin exemption for eligible UI API and WebSocket routes relies on the `Origin`/`Referer` header, which non-browser clients can forge. `/api/auth` and `/api/logs` never use this exemption, and neither does `/v1/*`. For real network isolation still put a reverse proxy (Caddy) in front; the key provides controlled access for external integrations.

Rejected requests return `401` (REST) or WebSocket close code `1008` (handshake).

---

## Runtime configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `TTS_API_KEY` | empty | Enables API key auth when set. |
| `MAX_TEXT_LENGTH` | `100000` | Max input characters accepted by REST and WebSocket requests. |
| `TTS_CORS_ALLOW_ORIGINS` | `*` | Comma-separated CORS allowed origins. Keep `*` for local/open use; set explicit origins for public deployments. |
| `EDGE_VOICES_CACHE_TTL_SECONDS` | `86400` | Non-negative finite Edge voice-list cache TTL. `0` disables retention between refresh waves; overlapping calls still share one in-flight refresh. |
| `EDGE_RETRY_MAX_ATTEMPTS` | `2` | Positive integer total attempts for Edge voice-list calls and Edge synthesis before the first audio chunk. `1` disables application-level retries. |
| `EDGE_RETRY_BASE_DELAY_SECONDS` | `0.25` | Non-negative finite base delay in seconds for exponential Edge retry backoff. `0` removes the wait between attempts. |
| `EDGE_VOICES_FAILURE_COOLDOWN_SECONDS` | `5` | Non-negative finite cooldown after an exhausted Edge voice-list refresh. `0` disables post-wave cooldown; overlapping calls still share the failed refresh. |
| `EDGE_VOICES_REQUEST_TIMEOUT_SECONDS` | `5` | Non-negative finite timeout in seconds per attempt to fetch the Edge voice catalog. `0` disables this timeout. |
| `TTS_SYNTHESIS_TIMEOUT_SECONDS` | `0` | REST pre-stream total deadline, REST post-start idle timeout, and WebSocket synthesis deadline. `0` disables these guards. Public deployments should set a non-zero value. |
| `TTS_MAX_FFMPEG_PROCESSES` | `2` | Maximum concurrent `ffmpeg` subprocesses. Excess REST requests fail fast with `429`; WebSocket synthesis returns `error`. |
| `TTS_MAX_SYNTHESIS_CONCURRENCY` | `2` | Maximum concurrent Kokoro inferences (shared by REST and WebSocket). Excess requests queue (block) rather than fail. WebSocket Kokoro produces no `ffmpeg` process, so this is its only concurrency guard. |
| `TTS_MAX_SYNTHESIS_WAITERS` | `16` | Maximum queued normal Kokoro requests, excluding active inference. Excess normal requests fail explicitly; speculative UI prefetch fails fast when no inference slot is immediately available and does not consume this queue. |
| `TTS_MAX_REQUEST_BODY_BYTES` | `1048576` | Maximum HTTP request-body bytes, enforced for both `Content-Length` and chunked bodies before JSON validation completes. |
| `TTS_RESPONSE_WRITE_TIMEOUT_SECONDS` | `30` | Timeout for each HTTP response-body write to a slow client, and for each WebSocket control/data frame send. `0` disables this guard. Request-owner cancellation also cancels and awaits any pending child write before synthesis cleanup unwinds. With `0`, a half-open WebSocket client (alive but never reading) can stall the sender and the full output queue indefinitely, pinning the handler and its held synthesis slot — keep this non-zero on untrusted networks. |
| `KOKORO_MAX_UNIT_CHARS` | `2000` | Maximum characters in one internal Kokoro inference fragment. Longer accepted units are split in order without adding WebSocket `seg` events; hard splits may affect prosody. |

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
  "status": "v0.12 engine running",
  "ready": true,
  "max_text_length": 100000
}
```

`max_text_length` mirrors `MAX_TEXT_LENGTH` so clients (including the bundled UI) can align the input limit without a separate config endpoint. It is present on both `200` and `503` responses.

### Response `503` (engines not ready)

```json
{
  "status": "starting",
  "ready": false,
  "max_text_length": 100000
}
```

### Response `503` (`ffmpeg` missing)

```json
{
  "status": "ffmpeg missing",
  "ready": false,
  "max_text_length": 100000
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
    { "id": "zh-CN-liaoning-XiaobeiNeural", "name": "Microsoft Xiaobei Online (Natural) - Chinese (Northeastern Mandarin)", "gender": "Female", "locale": "zh-CN-liaoning" },
    { "id": "zh-CN-shaanxi-XiaoniNeural", "name": "Microsoft Xiaoni Online (Natural) - Chinese (Zhongyuan Mandarin Shaanxi)", "gender": "Female", "locale": "zh-CN-shaanxi" },
    { "id": "zh-HK-HiuGaaiNeural", "name": "Microsoft HiuGaai Online (Natural) - Chinese (Cantonese Traditional)", "gender": "Female", "locale": "zh-HK" },
    { "id": "zh-TW-HsiaoChenNeural", "name": "Microsoft HsiaoChen Online (Natural) - Chinese (Taiwanese Mandarin)", "gender": "Female", "locale": "zh-TW" },
    { "id": "en-US-AvaNeural",      "name": "Microsoft Ava Online (Natural) - English (United States)", "gender": "Female", "locale": "en-US" }
  ]
}
```

Edge voices are fetched live from Microsoft and cached for `EDGE_VOICES_CACHE_TTL_SECONDS` seconds. Concurrent expired-cache requests share one refresh. Each upstream request has an `EDGE_VOICES_REQUEST_TIMEOUT_SECONDS` timeout per attempt; `0` disables that timeout. A timeout, failed request, empty response, or malformed response is retried up to `EDGE_RETRY_MAX_ATTEMPTS` and never replaces a successful cache; after the final failure, the endpoint enters `EDGE_VOICES_FAILURE_COOLDOWN_SECONDS` cooldown and returns the last successful cache, or an empty Edge catalog when no successful cache exists.

This endpoint returns the **full** Microsoft Edge catalog (no server-side locale filter). The bundled Web UI (`index.html`) further filters Edge voices to these locales only: `zh-CN`, `zh-CN-liaoning`, `zh-CN-shaanxi`, `zh-HK`, `zh-TW`, `en-US`, `en-GB`. Chinese dialect/regional locales are shown in the Chinese dropdown with a dialect tag (Cantonese / Taiwan / Northeastern / Shaanxi); Mandarin `zh-CN` has no tag. ShortNames and locales are owned by Microsoft — if a voice is renamed or removed upstream, it disappears from `/api/voices` and the UI without a local fallback catalog.

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

Edge upstream failures, including a stream that ends without non-empty audio, are retried only while no non-empty Edge audio chunk has been observed. If all attempts fail before audio, REST returns `502` and WebSocket sends `error`. Once any non-empty audio chunk has arrived, automatic retry is permanently disabled for that request so a reconnect cannot duplicate already-produced speech.

> Streaming edge case: once response headers are committed, the `200` status cannot be changed to a JSON error. Before returning `200`, the server now requires both a successfully submitted source-audio chunk and a non-empty encoder-output chunk, while rejecting observed encoder EOF/read failures and non-zero exits. After streaming starts, a detected feed failure, premature encoder EOF, non-zero exit, or idle timeout aborts the transfer instead of ending a partial body normally. Clients must still treat a transport error or truncated audio as failure and correlate `X-Request-ID`; exact behavior through Uvicorn, reverse proxies, and each HTTP client needs end-to-end verification.

---

## OpenAI-compatible API (`/v1`)

Drop-in style endpoints for agents and OpenAI SDK clients that call speech synthesis. These routes reuse the same dual-engine encoder pipeline as `POST /api/tts`, but accept OpenAI field names, support six binary output formats, and return OpenAI-shaped errors.

### Differences from OpenAI official TTS

| Topic | This service | OpenAI official (typical) |
|-------|--------------|---------------------------|
| `speed` | `0.5`–`3.0` | `0.25`–`4.0` |
| `response_format` | `mp3` / `opus` / `aac` / `flac` / `wav` / `pcm` | Same format names |
| `stream_format` | `audio` only; `sse` → `400` | `audio` / `sse` |
| Semantic hints | Non-empty `instructions`, `lang_code`, `lang`, or `language` → `400` | `instructions` is supported by selected models; language names differ across client/guide sources |
| Guide format alias | Non-empty `format` → `400`; use `response_format` | The live custom-voice guide uses `format`, while the generated API reference uses `response_format` |
| Input limit | `MAX_TEXT_LENGTH` (default `100000`) | `4096` characters |
| Required fields | `input` only; `model` and `voice` may be omitted | `input`, `model`, and `voice` |
| Voice names | Real engine IDs plus seven engine-aware aliases; custom voice objects → `422` | Larger built-in catalog plus custom voice objects |
| Markdown | Input is cleaned (headings, lists, bold, links, code) before speak | not applicable |
| Auth | No same-origin bypass; Bearer or `X-API-Key` | Bearer API key |
| Models | `kokoro` / `edge` (engines), not vendor model SKUs | `tts-1`, `tts-1-hd`, … |
| Default engine | `edge` when `model` is omitted or unknown | n/a |

### Authentication

When `TTS_API_KEY` is set:

- `Authorization: Bearer <key>` (preferred for OpenAI SDK) or `X-API-Key: <key>`
- Forged browser `Origin` does **not** unlock `/v1`

Malformed requests are authenticated before body validation. With auth enabled, a missing or invalid key therefore returns `401` even for bad JSON, missing fields, unknown `/v1/*` routes, or a wrong HTTP method. CORS preflight `OPTIONS` requests remain handled by the outer CORS middleware.

When the key is empty/unset, `/v1` is open (local use).

### `POST /v1/audio/speech`

```json
{
  "input": "Hello world",
  "model": "edge",
  "voice": "en-US-AvaNeural",
  "speed": 1.0,
  "response_format": "mp3"
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `input` | `string` | — | Required text (OpenAI name; mapped to internal `text`). Max `MAX_TEXT_LENGTH`. |
| `model` | `string` or `null` | `edge` | `kokoro` or `edge`. Unknown values (e.g. `tts-1`) fall back to `edge`; the name does not provide the corresponding OpenAI model semantics. |
| `voice` | `string` or `null` | selected-engine, language-aware default | A real ID for the selected engine, or one of the seven compatibility aliases below. Edge defaults: Chinese → `zh-CN-XiaoxiaoNeural`, English → `en-US-AvaNeural`. Kokoro defaults: Chinese → `zf_xiaoxiao`, English → `af_heart`. Empty values, custom voice objects, and IDs from the other engine return `422`. |
| `speed` | `number` | `1.0` | `0.5`–`3.0` |
| `response_format` | `string` | `mp3` | `mp3`, `opus`, `aac`, `flac`, `wav`, or `pcm`. Unsupported values return `400`. |
| `stream_format` | `string` | `audio` | Only binary `audio` is supported. `sse` and other values return `400`. |
| `instructions` | `string` or `null` | `null` | Non-empty values return `400`; local Edge/Kokoro engines do not implement OpenAI instruction semantics. |
| `lang_code` / `lang` | `string` or `null` | `null` | Known Hermes/OpenClaw language hints. Non-empty values return `400`; select engine and voice explicitly. |
| `language` / `format` | `string` or `null` | `null` | Names present in the live official custom-voice guide example but not the generated core request schema. Non-empty values return `400`; use `response_format` and select a supported engine/voice explicitly. |

Unknown extra JSON fields are ignored (not `422`) so SDK payloads with unrelated metadata keep working. This is deliberately more permissive than the official `additionalProperties:false` schema: a misspelled field such as `response_formt` is ignored and may fall back to a default, so clients should still validate their payloads. Known semantic names (`instructions`, `stream_format`, `lang_code`, `lang`, `language`, and `format`) are modeled and fail explicitly when non-empty. The official Python SDK method signature also requires explicit `model` and `voice`; the local omission defaults are available only to raw HTTP or other permissive clients.

Output metadata:

| Format | `Content-Type` | `Content-Disposition` filename | Notes |
|--------|----------------|--------------------------------|-------|
| `mp3` | `audio/mpeg` | `tts-output.mp3` | MP3 |
| `opus` | `audio/ogg` | `tts-output.opus` | Ogg Opus, mono 48 kHz |
| `aac` | `audio/aac` | `tts-output.aac` | AAC/ADTS |
| `flac` | `audio/flac` | `tts-output.flac` | mono 24 kHz |
| `wav` | `audio/wav` | `tts-output.wav` | PCM s16le WAV, mono 24 kHz |
| `pcm` | `application/octet-stream` | `tts-output.pcm` | raw s16le, mono 24 kHz |

Voice aliases never override an explicit `model`. They resolve inside the selected engine:

| Alias | Edge | Kokoro |
|-------|------|--------|
| `alloy` | `en-US-AvaNeural` | `af_alloy` |
| `coral` | `en-US-AvaNeural` | `af_heart` |
| `echo` | `en-US-AndrewNeural` | `am_echo` |
| `fable` | `en-US-GuyNeural` | `af_bella` |
| `onyx` | `en-US-BrianNeural` | `am_onyx` |
| `nova` | `en-US-EmmaNeural` | `af_nova` |
| `shimmer` | `en-US-JennyNeural` | `af_sarah` |

These seven names are a compatibility subset, not the complete current OpenAI voice catalog, and the mapped voices are not claimed to be acoustically equivalent to OpenAI-hosted voices. Names such as `ash`, `ballad`, `sage`, `verse`, `marin`, and `cedar` are not locally aliased. Explicit `alloy` / `coral` always maps to the English Ava voice; only an omitted voice is selected by text language. This does not prevent callers from passing a real Edge ID such as `en-US-AnaNeural` directly.

**Kokoro language mismatch:** after alias/default resolution, English Kokoro voices (`af_*`/`am_*`) allow Latin letters and Chinese Kokoro voices (`zf_*`/`zm_*`) allow Han letters. Any other Unicode letter script (including pure Cyrillic, kana, Hangul, Greek, or Arabic) returns `422` before synthesis and before an encoder slot is acquired. Digits, punctuation, whitespace, and combining marks are neutral; non-ASCII Latin remains valid for English voices. This explicit script gate prevents silent partial success after monolingual filtering would drop part of the input. Pure Japanese text made only of Han characters cannot be distinguished from Chinese by Unicode and is treated as Han. This applies only when `model` resolves to `kokoro`; default/Edge requests (for example `voice: "alloy"` with Chinese text) still use Edge and preserve the complete cleaned input. Split mixed-language text or use Edge.

Only a fresh, non-empty cached Edge catalog may reject a voice. Speech requests never refresh the catalog: cold, empty, or stale cache states are allowed through so a catalog outage cannot reject all synthesis. `GET /v1/audio/voices` remains the discovery/refresh path.

**Response**

- `200` — binary stream with the format-specific MIME and filename above, plus `X-Request-ID`
- Errors use OpenAI shape: `{ "error": { "message", "type", "param", "code" } }`; current `param` is `null`
- Status mapping: `400` empty/no speakable content, `401` bad key, `422` invalid voice/fields or Kokoro language mismatch, `429` ffmpeg limit (`Retry-After` present), `500` Kokoro failure, `502` Edge upstream, `503` not ready, `504` timeout
- Route-level classification codes are stable and non-null after authentication: 404 → `not_found`; 405 → `method_not_allowed`. Both retain `type=invalid_request_error`, the original HTTP status/message, and `X-Request-ID`. With auth enabled, unauthenticated requests still fail as `401 invalid_api_key` before route resolution.

Every pre-stream `/v1` error includes `X-Request-ID`. This covers authentication, body validation, route `404/405`, voice validation, limiter failures, timeouts, and unexpected preflight failures. Once response headers are committed, HTTP status is fixed at `200`; detected late failures abort the transfer but cannot become an OpenAI JSON error. Clients must treat transport errors or truncated audio as failure and correlate them with `X-Request-ID`.

**Defaults rationale:** `/v1` does **not** inherit `/api/tts` default `voice=zf_xiaoxiao` + Kokoro. That path runs Chinese Kokoro filtering and can strip pure English to empty audio. Default engine is therefore `edge` (Edge path does not run `filter_for_voice`).

### `GET /v1/models`

Returns only the two engines (`kokoro`, `edge`) in `{ "object": "list", "data": [...] }`. Voices are **not** listed here. The local model objects omit the official schema's required `created` field, and `GET /v1/models/{model}` is not implemented; this endpoint is a discovery subset, not a complete Models API.

### `GET /v1/audio/voices`

Local GET discovery extension. Shape `{ "object": "list", "data": [ { "id", "name", "gender", "locale", "engine" }, ... ] }` — all Kokoro voices plus the cached Edge catalog; the seven request aliases are not added as synthetic rows. If an Edge refresh fails, the last successful stale Edge catalog is returned; when no successful Edge cache exists, the endpoint returns Kokoro-only with `200` (not `500`).

The current OpenAI schema does define **POST** `/v1/audio/voices` for custom-voice creation, but no standard GET voice-list at this path. This service does not implement that POST, voice-consent lifecycle, or custom-voice synthesis; authenticated POST currently returns `405 method_not_allowed`.

Same auth as other `/v1` routes. Successful discovery responses currently do not include `X-Request-ID`; this is a low-risk observability difference from speech success and all `/v1` errors.

---

## `WebSocket /ws/tts`

Interactive TTS session. Send a JSON request, receive binary PCM frames interleaved with JSON control messages.

When `TTS_API_KEY` is set, the handshake is authenticated: same-origin pages (the built-in UI) connect without a key, while external clients must pass the key as a query parameter — `/ws/tts?key=<key>`. Browsers cannot set custom headers on a WebSocket handshake, hence the query parameter. A failed check closes the handshake with code `1008` (Policy Violation) before `accept`.

The default Docker image starts Uvicorn with `--no-access-log` so its built-in access logger does not write this query string to container stdout. That mitigates only the image default: a custom Uvicorn command, reverse proxy, load balancer, or observability agent can still record the raw URI. Configure every such layer to omit or redact query strings, never treat raw operational logs as already redacted, and rotate the key if exposure is suspected.

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

While the server is synthesizing, the client can send any message (or close the connection) to cancel the current request. The signal immediately wakes Edge I/O and queued Kokoro work; a queued Kokoro request does not wait to acquire synthesis-slot capacity. This does not make resource cleanup instantaneous: before listening for the next request, the server waits for the Edge decoder process to exit or for an already-running Kokoro inference to stop cooperatively at the next generated chunk, then completes cleanup.

### Auto Engine

`"engine":"auto"` is a **client-side UI option only** — it is never sent to the server. When Auto is selected, the browser splits text by sentence and language boundary: Chinese (including Chinese punctuation) routes to the Chinese voice, and English (including single terms and all-caps abbreviations like `DNS` or `OpenWrt`) always routes to the English voice. Each Kokoro pipeline is monolingual — the Chinese pipeline cannot even read isolated English abbreviations (official issues #95/#238) — so English is never merged into a Chinese segment.

### Text Preprocessing

The server automatically strips Markdown formatting (code blocks, inline code, images, links, headings, blockquotes, lists, bold/italic/strikethrough, horizontal rules) and removes quotation marks (straight double, curly, CJK corner brackets, and Chinese book-title brackets `《》`) before synthesis. This prevents markup from being read aloud and avoids phoneme artifacts caused by quotes in Kokoro.

An asterisk is inherently ambiguous in plain text, so the server uses a narrow, deterministic rule rather than attempting full Markdown parsing: complete ASCII `**` power chains (for example `2**8**2`) and complete Unicode letter/number `*` multiplication chains (for example `α*β*γ`, including `中文*加粗*中文`) remain literal. CJK-adjacent `**` is treated as Markdown and unwrapped (for example `中文**加粗**中文` becomes `中文加粗中文`). A mixed or incomplete `**` chain is also handled as Markdown as a whole; the service never preserves only one delimiter from that chain.

On the legacy `/api/tts` and `/ws/tts` surfaces, Kokoro applies per-voice language filtering as a fallback: Chinese voices (`zf_*`/`zm_*`) strip all English letters before synthesis, while English voices (`af_*`/`am_*`) strip all CJK characters. Because each Kokoro pipeline is monolingual and mispronounces the other language, this fallback avoids garbled output. For mixed Chinese/English text, use Auto mode in the UI to route each segment to the matching voice. `/v1/audio/speech` is stricter: it rejects a Kokoro language mismatch with `422` before synthesis instead of silently returning partial audio.

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

Covered contracts include auth enforcement, CORS on auth failures, `/api/auth`, `/api/logs`, `/api/voices`, REST `/api/tts` status codes, OpenAI-compatible `/v1/audio/speech` / `/v1/models` / `/v1/audio/voices` (strong auth, field mapping, error shape, `Retry-After`, `response_format`), REST preflight cleanup, synthesis timeout, request-id headers, readiness checks, startup warmup wiring, Edge voice-cache semantics, ffmpeg command construction and process limiting, WebSocket handshake/auth/error/end/reuse behavior, request validation, text preprocessing, sentence splitting, voice-language filtering, PCM encoding, synthesis-unit plumbing, and frontend HTML/JS contracts for voice routing plus the API tester.
