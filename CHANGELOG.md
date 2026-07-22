# Changelog

## v0.1 - Unreleased

### Added

- Added fixed runtime and test dependency files for reproducible local, Docker, and CI installs.
- Added Gitea Actions CI for Python compile checks and the `unittest` regression suite.
- Added API key auth for REST and WebSocket clients, including a key probe endpoint and same-origin UI exemption.
- Added `GET /api/logs` with API-key enforcement and response redaction.
- Added REST MP3 synthesis improvements: download mode, request IDs, preflight failure handling, short voice previews, and explicit error status codes.
- Added runtime controls for synthesis timeout, CORS allowlist, Edge voice cache TTL, and concurrent `ffmpeg` process limits.
- Added `TTS_MAX_SYNTHESIS_CONCURRENCY` (default `2`) to cap concurrent Kokoro inference for both REST and WebSocket, closing a WebSocket-only path that previously had no gate.

### Changed

- Unified the synthesis speed/engine bounds into shared constants (`SPEED_MIN`/`SPEED_MAX`/`VALID_ENGINES`) across the REST model, WebSocket parser, and voice-preview query, removing the duplicated `0.5`/`3.0`/`("kokoro","edge")` literals. REST still rejects out-of-range speed with `422`; WebSocket still clamps (each keeps its protocol semantics).
- Widened cross-language stripping and speakable-content detection to cover CJK extension A, compatibility ideographs, and the supplementary planes, not just the basic block.
- Documented that a `POST /api/tts` stream can end truncated/empty after the `200` is committed if transcoding fails mid-stream.

- Replaced FastAPI startup events with lifespan startup while preserving the existing startup initialization path.
- Disabled FastAPI's default `/docs`, `/redoc`, and `/openapi.json` surfaces in favor of the bundled `/api` tester.
- Health checks now require both Kokoro pipelines and `ffmpeg` availability before reporting ready.
- Documentation now calls out the current CPU-only Kokoro/PyTorch deployment path and public deployment limits.

### Fixed

- Prevented WebSocket synthesis failures from emitting a false `end` message.
- Fixed the main UI so later synthesis segments that disconnect before `end` are shown as errors instead of completed playback.
- Applied the `ffmpeg` process limit to WebSocket Edge synthesis and documented that voice previews are API-key protected.
- Avoided caching failed Edge voice-list refreshes; stale successful cache is served when refresh fails.
- Kept auth failure responses readable by browser clients through CORS headers.
- Rejected raw SSML explicitly instead of pretending to pass XML through `edge-tts`.
