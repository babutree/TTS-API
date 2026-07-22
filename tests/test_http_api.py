# -*- coding: utf-8 -*-
"""HTTP API 集成测试：健康检查、音色目录、REST TTS 状态码。"""
import unittest
import os
import shutil

from starlette.testclient import TestClient

from _support import FakeProc, ScriptedStdout, import_app_with_fakes


def _load_with_cors(origins):
    old = os.environ.get("TTS_CORS_ALLOW_ORIGINS")
    os.environ["TTS_CORS_ALLOW_ORIGINS"] = origins
    try:
        app = import_app_with_fakes()
    finally:
        if old is None:
            os.environ.pop("TTS_CORS_ALLOW_ORIGINS", None)
        else:
            os.environ["TTS_CORS_ALLOW_ORIGINS"] = old
    return app


class HttpApiTests(unittest.TestCase):
    def setUp(self):
        self.app = import_app_with_fakes()
        self.client = TestClient(self.app.app)
        self.app.logger.disabled = True

    def tearDown(self):
        self.app.logger.disabled = False

    def _mark_ready(self):
        self.app.pipeline_zh = object()
        self.app.pipeline_en = object()

    def _install_mp3_success(self, content=b"MP3DATA"):
        async def fake_encoder(engine):
            return FakeProc(stdout=ScriptedStdout([content]))

        async def fake_run_kokoro(text, voice, speed, cancel_event=None):
            return b"\x01\x02\x03\x04"

        self.app._create_mp3_encoder = fake_encoder
        self.app.run_kokoro = fake_run_kokoro

    def test_health_returns_503_until_both_pipelines_ready(self):
        resp = self.client.get("/")

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json(), {"status": "starting", "ready": False})

    def test_health_returns_200_when_ready(self):
        self._mark_ready()

        resp = self.client.get("/")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "v0.1 engine running", "ready": True})

    def test_health_returns_503_when_ffmpeg_missing(self):
        self._mark_ready()
        old_which = shutil.which
        self.app.shutil.which = lambda name: None if name == "ffmpeg" else old_which(name)

        resp = self.client.get("/")

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["status"], "ffmpeg missing")
        self.assertFalse(resp.json()["ready"])

    def test_health_reports_ready_when_ffmpeg_available(self):
        self._mark_ready()
        self.app.shutil.which = lambda name: "C:/ffmpeg/bin/ffmpeg.exe" if name == "ffmpeg" else None

        resp = self.client.get("/")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "v0.1 engine running", "ready": True})

    def test_static_html_responses_declare_utf8_charset(self):
        for path in ("/index.html", "/api"):
            with self.subTest(path=path):
                resp = self.client.get(path)
                self.assertEqual(resp.status_code, 200)
                self.assertIn("charset=utf-8", resp.headers["content-type"].lower())

    def test_static_css_and_favicon_are_served_without_engine_readiness(self):
        css = self.client.get("/static/style.css")
        favicon = self.client.get("/favicon.ico")

        self.assertEqual(css.status_code, 200)
        self.assertIn("text/css", css.headers["content-type"])
        self.assertEqual(favicon.status_code, 200)
        self.assertGreater(len(favicon.content), 0)
        self.assertIn("image", favicon.headers["content-type"].lower())

    def test_401_auth_response_keeps_cors_headers(self):
        self.app.TTS_API_KEY = "secret"
        resp = self.client.get("/api/voices", headers={"Origin": "http://evil.example"})

        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.headers["access-control-allow-origin"], "*")

    def test_configured_cors_allows_matching_origin_on_401(self):
        self.app = _load_with_cors("http://allowed.example")
        self.app.TTS_API_KEY = "secret"
        self.client = TestClient(self.app.app)

        resp = self.client.get("/api/voices", headers={"Origin": "http://allowed.example"})

        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.headers["access-control-allow-origin"], "http://allowed.example")

    def test_configured_cors_rejects_non_matching_origin_on_401(self):
        self.app = _load_with_cors("http://allowed.example")
        self.app.TTS_API_KEY = "secret"
        self.client = TestClient(self.app.app)

        resp = self.client.get("/api/voices", headers={"Origin": "http://evil.example"})

        self.assertEqual(resp.status_code, 401)
        self.assertNotIn("access-control-allow-origin", resp.headers)

    def test_logs_endpoint_requires_real_key_even_for_same_origin(self):
        self.app.TTS_API_KEY = "secret"

        resp = self.client.get("/api/logs", headers={"Origin": "http://testserver", "Host": "testserver"})

        self.assertEqual(resp.status_code, 401)

    def test_logs_endpoint_returns_limited_redacted_lines(self):
        self.app.TTS_API_KEY = "secret"
        self.app._ring_handler.buffer.clear()
        self.app._ring_handler.buffer.append(
            "Authorization: Bearer secret /ws/tts?key=secret X-API-Key: secret"
        )
        self.app._ring_handler.buffer.append("second line")

        resp = self.client.get("/api/logs?limit=2", headers={"X-API-Key": "secret"})

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["limit"], 2)
        self.assertEqual(body["total_buffered"], 2)
        self.assertEqual(len(body["lines"]), 2)
        rendered = "\n".join(body["lines"])
        self.assertIn("second line", rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertNotIn("secret", rendered)

    def test_logs_endpoint_rejects_invalid_limit(self):
        for limit in ("0", str(self.app.LOG_MAX_LINES + 1)):
            with self.subTest(limit=limit):
                resp = self.client.get(f"/api/logs?limit={limit}")
                self.assertEqual(resp.status_code, 422)

    def test_voices_maps_edge_voice_metadata_and_includes_kokoro_catalog(self):
        async def fake_list_voices():
            return [
                {
                    "ShortName": "en-US-AriaNeural",
                    "FriendlyName": "Microsoft Aria Online",
                    "Gender": "Female",
                    "Locale": "en-US",
                }
            ]

        self.app.edge_tts.list_voices = fake_list_voices
        resp = self.client.get("/api/voices")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body["kokoro"]), 28)
        self.assertEqual(
            body["edge"],
            [{"id": "en-US-AriaNeural", "name": "Microsoft Aria Online", "gender": "Female", "locale": "en-US"}],
        )

    def test_voices_falls_back_to_empty_edge_list_when_fetch_fails(self):
        async def failing_list_voices():
            raise RuntimeError("edge network unavailable")

        self.app.edge_tts.list_voices = failing_list_voices
        resp = self.client.get("/api/voices")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["edge"], [])

    def test_tts_returns_503_when_engine_not_ready(self):
        resp = self.client.post(
            "/api/tts",
            json={"text": "hi", "engine": "kokoro", "voice": "af_heart", "speed": 1.0},
        )

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["detail"], "TTS engine not ready")

    def test_tts_rejects_blank_and_cleaned_empty_text(self):
        self._mark_ready()
        blank = self.client.post(
            "/api/tts",
            json={"text": "   ", "engine": "kokoro", "voice": "af_heart", "speed": 1.0},
        )
        cleaned = self.client.post(
            "/api/tts",
            json={"text": "```python\nprint(1)\n```", "engine": "kokoro", "voice": "af_heart", "speed": 1.0},
        )

        self.assertEqual(blank.status_code, 400)
        self.assertEqual(blank.json()["detail"], "text must not be empty")
        self.assertEqual(cleaned.status_code, 400)
        self.assertEqual(cleaned.json()["detail"], "text is empty after cleaning")

    def test_tts_validation_errors_return_422(self):
        self._mark_ready()
        payloads = [
            {"text": "hi", "engine": "kokoro", "voice": "missing", "speed": 1.0},
            {"text": "hi", "engine": "bad", "voice": "af_heart", "speed": 1.0},
            {"text": "hi", "engine": "kokoro", "voice": "af_heart", "speed": 9.0},
        ]

        for payload in payloads:
            with self.subTest(payload=payload):
                resp = self.client.post("/api/tts", json=payload)
                self.assertEqual(resp.status_code, 422)

    def test_tts_success_streams_mp3(self):
        self._mark_ready()
        self._install_mp3_success(b"MP3DATA")

        resp = self.client.post(
            "/api/tts",
            json={"text": "hello world.", "engine": "kokoro", "voice": "af_heart", "speed": 1.0},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "audio/mpeg")
        self.assertIn("x-request-id", resp.headers)
        self.assertEqual(resp.content, b"MP3DATA")

    def test_tts_uses_supplied_request_id_header(self):
        self._mark_ready()
        self._install_mp3_success(b"MP3DATA")

        resp = self.client.post(
            "/api/tts",
            json={"text": "hello world.", "engine": "kokoro", "voice": "af_heart", "speed": 1.0},
            headers={"X-Request-ID": "client-id-123"},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["x-request-id"], "client-id-123")

    def test_tts_failure_log_includes_request_id_engine_and_voice(self):
        self._mark_ready()
        self.app._ring_handler.buffer.clear()
        self.app.logger.disabled = False

        async def fake_encoder(engine):
            return FakeProc(stdout=ScriptedStdout([]))

        async def fake_run_kokoro(text, voice, speed, cancel_event=None):
            raise RuntimeError("kokoro boom")

        self.app._create_mp3_encoder = fake_encoder
        self.app.run_kokoro = fake_run_kokoro

        resp = self.client.post(
            "/api/tts",
            json={"text": "hello.", "engine": "kokoro", "voice": "af_heart", "speed": 1.0},
            headers={"X-Request-ID": "rid-123"},
        )

        self.assertEqual(resp.status_code, 500)
        rendered = "\n".join(self.app._ring_handler.buffer)
        self.assertIn("request_id=rid-123", rendered)
        self.assertIn("engine=kokoro", rendered)
        self.assertIn("voice=af_heart", rendered)

    def test_tts_download_uses_attachment_disposition(self):
        self._mark_ready()
        self._install_mp3_success(b"MP3DATA")

        resp = self.client.post(
            "/api/tts?download=true",
            json={"text": "hello world.", "engine": "kokoro", "voice": "af_heart", "speed": 1.0},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "audio/mpeg")
        self.assertIn("attachment", resp.headers["content-disposition"])
        self.assertIn("tts-output.mp3", resp.headers["content-disposition"])
        self.assertEqual(resp.content, b"MP3DATA")

    def test_voice_preview_synthesizes_short_sample(self):
        self._mark_ready()
        self._install_mp3_success(b"PREVIEW")

        resp = self.client.get("/api/voices/preview?engine=kokoro&voice=af_heart")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "audio/mpeg")
        self.assertIn("inline", resp.headers["content-disposition"])
        self.assertEqual(resp.content, b"PREVIEW")

    def test_voice_preview_rejects_unknown_kokoro_voice(self):
        self._mark_ready()

        resp = self.client.get("/api/voices/preview?engine=kokoro&voice=missing")

        self.assertEqual(resp.status_code, 422)

    def test_voice_preview_rejects_speed_outside_shared_range(self):
        # preview 的 speed 边界应与 REST/WS 共用同一组常量(SPEED_MIN/MAX)，
        # 越界 422。此前 preview 硬编码 0.5/3.0，未接统一常量(B1 漏网)。
        self._mark_ready()

        for speed in (0.49, 3.01):
            with self.subTest(speed=speed):
                resp = self.client.get(f"/api/voices/preview?engine=kokoro&voice=af_heart&speed={speed}")
                self.assertEqual(resp.status_code, 422)

    def test_edge_rejects_raw_ssml(self):
        self._mark_ready()

        resp = self.client.post(
            "/api/tts",
            json={
                "text": "<speak>Hello</speak>",
                "engine": "edge",
                "voice": "en-US-AriaNeural",
                "speed": 1.0,
                "ssml": True,
            },
        )

        self.assertEqual(resp.status_code, 422)

    def test_kokoro_rejects_ssml(self):
        self._mark_ready()

        resp = self.client.post(
            "/api/tts",
            json={"text": "<speak>Hello</speak>", "engine": "kokoro", "voice": "af_heart", "ssml": True},
        )

        self.assertEqual(resp.status_code, 422)

    def test_default_fastapi_docs_and_openapi_are_disabled(self):
        docs = self.client.get("/docs")
        openapi = self.client.get("/openapi.json")

        self.assertEqual(docs.status_code, 404)
        self.assertEqual(openapi.status_code, 404)

    def test_tts_rejects_when_ffmpeg_process_limit_is_exhausted(self):
        self._mark_ready()

        class ExhaustedLimiter:
            async def acquire(self):
                return False

            def release(self):
                raise AssertionError("release should not run when acquire fails")

        self.app._ffmpeg_limiter = ExhaustedLimiter()

        resp = self.client.post(
            "/api/tts",
            json={"text": "hello world.", "engine": "kokoro", "voice": "af_heart", "speed": 1.0},
        )

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["detail"], "ffmpeg process limit reached")

    def test_tts_no_speakable_content_returns_400(self):
        self._mark_ready()

        async def fake_encoder(engine):
            return FakeProc(stdout=ScriptedStdout([b"SHOULD_NOT_MATTER"]))

        async def fake_run_kokoro(text, voice, speed, cancel_event=None):
            return b""

        self.app._create_mp3_encoder = fake_encoder
        self.app.run_kokoro = fake_run_kokoro
        resp = self.client.post(
            "/api/tts",
            json={"text": "....", "engine": "kokoro", "voice": "af_heart", "speed": 1.0},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"], "no speakable content for the given voice")

    def test_tts_kokoro_preflight_failure_returns_500(self):
        self._mark_ready()

        async def fake_encoder(engine):
            return FakeProc(stdout=ScriptedStdout([]))

        async def fake_run_kokoro(text, voice, speed, cancel_event=None):
            raise RuntimeError("kokoro boom")

        self.app._create_mp3_encoder = fake_encoder
        self.app.run_kokoro = fake_run_kokoro
        resp = self.client.post(
            "/api/tts",
            json={"text": "hello.", "engine": "kokoro", "voice": "af_heart", "speed": 1.0},
        )

        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.json()["detail"], "synthesis failed")

    def test_tts_edge_preflight_failure_returns_502(self):
        self._mark_ready()

        async def fake_encoder(engine):
            return FakeProc(stdout=ScriptedStdout([]))

        class BoomCommunicate:
            def __init__(self, *args, **kwargs):
                pass

            async def stream(self):
                raise RuntimeError("edge boom")
                yield

        self.app._create_mp3_encoder = fake_encoder
        self.app.edge_tts.Communicate = BoomCommunicate
        resp = self.client.post(
            "/api/tts",
            json={"text": "hello.", "engine": "edge", "voice": "en-US-AriaNeural", "speed": 1.0},
        )

        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.json()["detail"], "synthesis failed")


if __name__ == "__main__":
    unittest.main()
