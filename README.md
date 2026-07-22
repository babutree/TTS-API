<h1 align="center">TTS API</h1>

<p align="center">
  <a href="#"><img src="https://img.shields.io/badge/version-v0.1-blue" alt="Version" /></a>
  <a href="#"><img src="https://img.shields.io/badge/license-MIT-green" alt="License" /></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/python-3.10+-blue" alt="Python" /></a>
  <a href="https://fastapi.tiangolo.com"><img src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white" alt="FastAPI" /></a>
  <a href="https://linux.do" target="_blank" rel="noopener noreferrer"><img src="https://img.shields.io/badge/LinuxDo-论坛-F90?logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxNiIgaGVpZ2h0PSIxNiIgdmlld0JveD0iMCAwIDI0IDI0IiBmaWxsPSJub25lIiBzdHJva2U9IiNmZmYiIHN0cm9rZS13aWR0aD0iMiIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBzdHJva2UtbGluZWpvaW49InJvdW5kIj48cG9seWdvbiBwb2ludHM9IjEyIDIgMTUgOSAyMiA5IDE2LjUgMTQuNSAxOSAyMiAxMiAxNyA1IDIyIDcuNSAxNC41IDIgOSA5IDkiLz48L3N2Zz4=" alt="LinuxDo" /></a>
</p>

<p align="center">
  <a href="README.md">English</a> |
  <a href="README_CN.md">中文</a>
</p>

A streaming TTS service built on FastAPI. Uses Kokoro for CPU-only on-device synthesis and Microsoft Edge TTS for cloud synthesis. Comes with a web UI.

## Features

| Capability | Detail |
|------------|--------|
| Dual engines | Kokoro on-device (free, offline) + Edge Microsoft cloud (natural, multi-lingual) |
| Real-time streaming | WebSocket binary frames, gapless playback via Web Audio API |
| Speed control | 0.5x–2.0x (UI) / 0.5x–3.0x (API), sentence-level switching during playback |
| Seek & pause | ±10s seek, pause/resume, stop — all with buffer retention |
| Language auto-routing | Auto engine mode per-sentence language detection: Chinese → Chinese voice, English → English voice |
| REST API | `POST /api/tts` returns streaming MP3 |
| Dark mode | Persistent theme toggle with glassmorphism design |
| i18n UI | Chinese/English interface with hot-switch |

## Architecture

```text
Browser (index.html)
  |  WebSocket /ws/tts  (JSON req + PCM binary res)
  |  REST  POST /api/tts (MP3 stream)
  v
FastAPI (app.py)
  |-- Static files: /index.html, /static/style.css
  |-- Health: GET /
  |-- Auth probe: GET /api/auth
  |-- Voices: GET /api/voices (kokoro + edge)
  |-- TTS:    POST /api/tts (MP3 stream)
  |-- Docs:   GET /api (interactive API tester)
  |-- WS:     /ws/tts (interactive streaming)
       |
        +-- Kokoro (local, asyncio.to_thread + threading locks)
        +-- Edge   (cloud, ffmpeg subprocess for audio transcoding)
```

## Project files

```text
tts-api
├── app.py              # FastAPI backend: WebSocket, REST, TTS engines
├── index.html          # Web frontend: Web Audio playback, viz, i18n
├── api.html            # API docs page with interactive tester
├── style.css           # Glassmorphism styling, dark mode support
├── API.md              # REST & WebSocket API reference
├── Dockerfile          # Python 3.10-slim container image
├── docker-compose.yml  # Production service with health checks
```

## Quick Start

### Local

Requires Python 3.10+, `ffmpeg`, and `espeak-ng` installed system-wide.

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8880
```

Open `http://localhost:8880/index.html`.

### Docker (recommended)

The image installs `espeak-ng`, `ffmpeg`, `libsndfile1`, CPU-only torch and the Python deps, then runs `uvicorn app:app` on port `8880`.

1. Adjust `docker-compose.yml` before the first run:
   - `TTS_API_KEY` — set your own strong random value (e.g. `openssl rand -hex 32`), or leave empty for fully-open local use.
   - `MAX_TEXT_LENGTH` — max characters per synthesis (default `100000`).
   - `TTS_CORS_ALLOW_ORIGINS` — comma-separated allowed browser origins (default `*`).
   - `EDGE_VOICES_CACHE_TTL_SECONDS` — Edge voice-list cache TTL (default `86400`). A failed refresh keeps the last successful cache when available.
   - `TTS_SYNTHESIS_TIMEOUT_SECONDS` — synthesis timeout for REST and WebSocket; `0` disables it (default `0`).
   - `TTS_MAX_FFMPEG_PROCESSES` — fail-fast limit for concurrent `ffmpeg` subprocesses (default `2`).
   - `TTS_MAX_SYNTHESIS_CONCURRENCY` — cap on concurrent Kokoro inference tasks, shared by REST and WebSocket (default `2`). Blocks (queues) rather than rejecting; guards the thread pool from being exhausted by requests waiting on the per-language lock.
   - `volumes` — `./models:/app/models` caches the Kokoro weights so containers rebuild without re-downloading.
   - `ports` — publishes `8880` for direct access. To put the service on an external reverse-proxy network (e.g. Caddy's `caddy_net`), uncomment the optional `networks` block in `docker-compose.yml` after `docker network create caddy_net`.

2. Build and start:

   ```bash
   docker compose up --build -d
   ```

3. First boot downloads the Kokoro model (a few hundred MB) into `./models` and warms up both pipelines. `GET /` returns `503` until warmup finishes, then `200`. The compose `healthcheck` has a `60s` start period to cover this.

4. Open `http://<host>:8880/index.html` (or route it through your proxy).

Update / restart / logs:

```bash
docker compose pull        # if using a prebuilt image
docker compose up --build -d
docker compose logs -f tts-api
docker compose down
```

## API

See [API.md](API.md) for the full REST and WebSocket API reference, or open `/api` in a browser for an interactive docs page with a built-in tester. REST `/api/tts` supports inline playback or `?download=true`, and `/api/voices/preview` returns a short voice audition sample.

## Testing

Run the backend regression suite with:

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -p "test_*.py" -v
```

The tests use fake Kokoro and Edge modules so they can run without model weights, `ffmpeg`, `espeak-ng`, or network access. They cover text preprocessing, sentence splitting, voice-language filtering, PCM encoding, request validation, auth middleware, CORS on auth failures, REST status codes, REST preflight cleanup, synthesis timeout, request-id headers, readiness checks, startup warmup wiring, Edge voice-cache semantics, ffmpeg command construction and process limiting, WebSocket handshake/auth/reuse behavior, synthesis-unit plumbing, and frontend HTML/JS contracts for voice routing plus the API tester. Real audio quality and full engine integration still require a runtime environment with the actual models, system binaries, and Edge TTS network access.

## Authentication

The service has built-in API Key authentication so external clients (browser extensions, scripts, other backends) can call it directly.

- Set `TTS_API_KEY` in `docker-compose.yml` and change it to your own strong random value.
- Empty (unset) = fully open, which keeps local direct-connect usage working.
- The bundled UI (`/index.html`) and docs page (`/api`) are same-origin and need no key in a browser.
- External clients send the key via `X-API-Key: <key>` for REST (preferred, especially behind Caddy Basic Auth; `Authorization: Bearer <key>` also works), and `/ws/tts?key=<key>` for WebSocket.

For real network isolation still run behind a reverse proxy (e.g. Caddy); the key provides controlled access for external integrations.

## Reverse proxy (Caddy)

The service ships no TLS or login of its own. A typical Caddy front-end terminates HTTPS and (optionally) adds a login gate. It must forward WebSocket upgrades and use long timeouts so streaming synthesis is not cut off.

```caddyfile
tts.example.com {
    encode gzip zstd

    # Optional: gate the human-facing UI behind a login.
    # basic_auth generates the hash with:  caddy hash-password
    basic_auth {
        alice $2a$14$REPLACE_WITH_YOUR_OWN_BCRYPT_HASH
    }

    reverse_proxy tts-api:8880 {
        transport http {
            read_timeout 1h
            write_timeout 1h
        }
    }
}
```

`reverse_proxy` upstream `tts-api:8880` is the compose `container_name`. Join Caddy and this service on the same Docker network (uncomment the optional `caddy_net` block in `docker-compose.yml`, or attach both to any shared network). Caddy forwards the `Upgrade`/`Connection` headers automatically, so `/ws/tts` works without extra config.

### Basic Auth + API key together

If you enable Caddy `basic_auth` for the **whole site**, every request (including programmatic ones) must first pass Basic Auth, and the browser sends `Authorization: Basic …`. Because the built-in key also lives in the `Authorization` header when sent as `Bearer`, the two collide. Two clean options:

- **Human UI only** — keep `basic_auth` site-wide and leave `TTS_API_KEY` empty. The bundled pages send the key via `X-API-Key`, which does not clash with Basic Auth, but you don't need the key at all in this mode.
- **Programmatic clients (CRX/scripts)** — exempt the API/WS paths from Basic Auth so they can authenticate with `TTS_API_KEY` instead:

  ```caddyfile
  tts.example.com {
      encode gzip zstd

      @api path /api/* /ws/tts
      handle @api {
          reverse_proxy tts-api:8880 {
              transport http {
                  read_timeout 1h
                  write_timeout 1h
              }
          }
      }

      handle {
          basic_auth {
              alice $2a$14$REPLACE_WITH_YOUR_OWN_BCRYPT_HASH
          }
          reverse_proxy tts-api:8880 {
              transport http {
                  read_timeout 1h
                  write_timeout 1h
              }
          }
      }
  }
  ```

  Under `@api` the backend enforces `TTS_API_KEY` (REST `X-API-Key`/`Bearer`, WS `?key=`), while the UI stays behind the login gate.

## Limits

- Edge TTS requires internet access. The first request will be slower.
- Kokoro currently runs through CPU-only PyTorch wheels in this project. GPU acceleration is not wired into the Docker image or dependency lock yet.
- Text length is limited by `MAX_TEXT_LENGTH` (default: `100000`).
- For public or multi-user deployments, lower `MAX_TEXT_LENGTH`, set `TTS_SYNTHESIS_TIMEOUT_SECONDS`, and keep `TTS_MAX_FFMPEG_PROCESSES` small enough for the host CPU/memory budget.
- Playback buffer grows with text length. Long sessions may use significant memory on the client side.

## License

MIT
