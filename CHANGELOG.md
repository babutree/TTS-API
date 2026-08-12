# Changelog

## v0.12 - Unreleased

### Added

- OpenAI-compatible layer: `POST /v1/audio/speech`, `GET /v1/models`, `GET /v1/audio/voices`.
- `/v1` strong auth before body/route validation; OpenAI-shaped pre-stream errors with `X-Request-ID`; `Retry-After` on `/v1` `429`.
- Field mapping `input`→text, `model`→engine; default engine `edge`; seven engine-aware aliases including OpenClaw's default `coral`.
- OpenAI binary output formats `mp3`, `opus`, `aac`, `flac`, `wav`, and raw 24 kHz mono s16le `pcm`, with matching MIME and filename extensions.
- Explicit request boundaries: binary `stream_format=audio` only; unsupported SSE and non-empty `instructions`, `lang_code`, `lang`, `language`, or guide-only `format` return `400` instead of being silently ignored.
- Offline contract tests in `tests/test_openai_compat.py`.

### Fixed

- Bound declared and chunked HTTP request bodies, while preserving `/v1` authentication precedence: missing or invalid credentials return `401` before an oversized authenticated request can return `413`.
- Cancel and await a pending HTTP response-body child write when its request owner is cancelled, preventing orphaned ASGI send tasks while preserving downstream exceptions and write-timeout errors.
- Close the same-tick Kokoro waiter-admission race and keep speculative Kokoro prefetch out of the normal waiter budget by failing it fast when no inference slot is immediately available.
- Use one static Unicode 15.0 Han table in the Python backend and browser router, so Python 3.10/3.12 Unicode database differences cannot route assigned Han differently.
- Reject Kokoro voice/text language mismatches on `/v1/audio/speech` before encoder startup, preventing silent partial success when monolingual filtering would otherwise drop CJK or Latin content.
- Reject Unicode letter scripts outside the selected Kokoro voice's Han/Latin script before `/v1` synthesis; neutral punctuation and numbers remain accepted.
- Preserve the primary Edge synthesis/queue exception when stream cleanup also fails, while still reaping the decoder and releasing its ffmpeg slot.
- Preserve Unicode word-internal multiplication stars and exponent expressions during Markdown cleanup while retaining bounded bold/italic removal.
- Return stable non-null `error.code` values for authenticated `/v1` route failures: `not_found` for `404` and `method_not_allowed` for `405`.
- Treat the Bearer authentication scheme case-insensitively and include the required nullable `param` member in OpenAI-shaped errors.
- Require both submitted source audio and non-empty encoder output before committing `200`; preserve the prefetched output exactly once and reject encoder EOF/read errors or observed non-zero exits before handoff.
- Stop waiting indefinitely when encoder stdout has already failed, even if the global synthesis timeout is disabled; keep same-tick feed/upstream errors correctly classified.
- Distinguish local ffmpeg stdin failures (`500`) from Edge upstream failures (`502`).
- Abort an already-started transfer when a late feed failure, premature EOF, observed non-zero encoder exit, or stream timeout is detected, instead of normally completing a known partial body.
- Keep a cancelled in-flight Kokoro worker inside the synthesis limit until the real thread exits, and wake queued Kokoro requests directly on WebSocket cancellation without dispatching the pipeline or leaking a permit.
- Continue controlled process reaping and release the ffmpeg limiter exactly once when `kill()` raises a non-race `OSError`, instead of skipping `wait()` and the release path; preserve the original stream-timeout or owner-cancellation exception when an earlier termination attempt fails.
- Correct the Caddy Basic Auth coexistence examples so programmatic `/v1/*` Bearer requests bypass Basic Auth and remain protected by `TTS_API_KEY`.

### Known limits (honest)

- `speed` max remains `3.0` (OpenAI cloud allows up to `4.0`).
- SSE, speech instructions, language hints, the full official voice catalog, and custom voices are not implemented; known unsupported fields fail explicitly.
- Markdown is cleaned before synthesis (differs from raw OpenAI cloud text).
- Edge voice pre-check only trusts a fresh, non-empty cached catalog. Cold, empty, or stale cache states allow synthesis through without refreshing and may still fail later as `502`.
- HTTP streaming cannot replace `200` after response headers are committed. Detected late failures now interrupt the transfer, but cannot be converted to an OpenAI JSON error; real proxy/client behavior still needs end-to-end validation.
- Real ffmpeg output for all six formats and Hermes/OpenClaw end-to-end playback have not yet been verified in this offline test environment.

## v0.11

### Added

- Added fixed runtime and test dependency files for reproducible local, Docker, and CI installs.
- Added Gitea Actions CI for Python compile checks and the `unittest` regression suite.
- Added API key auth for REST and WebSocket clients, including a key probe endpoint and same-origin UI exemption.
- Added `GET /api/logs` with API-key enforcement and response redaction.
- Added REST MP3 synthesis improvements: download mode, request IDs, preflight failure handling, short voice previews, and explicit error status codes.
- Added runtime controls for synthesis timeout, CORS allowlist, Edge voice cache TTL, and concurrent `ffmpeg` process limits.
- Added `TTS_MAX_SYNTHESIS_CONCURRENCY` (default `2`) to cap concurrent Kokoro inference for both REST and WebSocket, closing a WebSocket-only path that previously had no gate.

### Changed

- The main UI now pipelines cross-engine and cross-voice runs through a two-slot prefetch window, preserving PCM/timeline order and using a bounded fallback when the current prefetched run stalls.
- Unified the synthesis speed/engine bounds into shared constants (`SPEED_MIN`/`SPEED_MAX`/`VALID_ENGINES`) across the REST model, WebSocket parser, and voice-preview query, removing the duplicated `0.5`/`3.0`/`("kokoro","edge")` literals. REST still rejects out-of-range speed with `422`; WebSocket still clamps (each keeps its protocol semantics).
- Widened cross-language stripping and speakable-content detection to cover CJK extension A, compatibility ideographs, and the supplementary planes, not just the basic block.
- Documented that a `POST /api/tts` stream can end truncated/empty after the `200` is committed if transcoding fails mid-stream.

- Replaced FastAPI startup events with lifespan startup while preserving the existing startup initialization path.
- Disabled FastAPI's default `/docs`, `/redoc`, and `/openapi.json` surfaces in favor of the bundled `/api` tester.
- Health checks now require both Kokoro pipelines and `ffmpeg` availability before reporting ready.
- Documentation now calls out the current CPU-only Kokoro/PyTorch deployment path and public deployment limits.

### Fixed

- Kept speculative Edge prefetch below normal REST/WebSocket priority by reserving decoder capacity and waiting for main-request decoder admission before opening the Edge prefetch window.
- Prevented WebSocket synthesis failures from emitting a false `end` message.
- Fixed the main UI so later synthesis segments that disconnect before `end` are shown as errors instead of completed playback.
- Applied the `ffmpeg` process limit to WebSocket Edge synthesis and documented that voice previews are API-key protected.
- Avoided caching failed Edge voice-list refreshes; stale successful cache is served when refresh fails.
- Kept auth failure responses readable by browser clients through CORS headers.
- Rejected raw SSML explicitly instead of pretending to pass XML through `edge-tts`.
