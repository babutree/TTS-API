# -*- coding: utf-8 -*-
"""OpenAI 兼容层契约：/v1/audio/speech、/v1/models、/v1/audio/voices。

严格对应 v0.12 CSV：强鉴权、字段映射、默认 edge、错误形状、Retry-After、response_format。
"""
import asyncio
import json
import os
import unittest
from unittest import mock

from starlette.requests import ClientDisconnect
from starlette.testclient import TestClient

from _support import FakeProc, HangingStdout, ScriptedStdout, import_app_with_fakes


def _load_with_key(key: str):
    old = os.environ.get("TTS_API_KEY")
    os.environ["TTS_API_KEY"] = key
    try:
        app = import_app_with_fakes()
    finally:
        if old is None:
            os.environ.pop("TTS_API_KEY", None)
        else:
            os.environ["TTS_API_KEY"] = old
    app.pipeline_zh = object()
    app.pipeline_en = object()
    app.logger.disabled = True
    return app


class OpenAISpeechAuthTests(unittest.TestCase):
    def setUp(self):
        self.app = _load_with_key("secret-v1")
        self.client = TestClient(self.app.app)
        self._install_mp3_success()
        self.app._edge_voices_cache = []
        self.app._edge_voices_cache_expires_at = float("inf")

    def tearDown(self):
        self.app.logger.disabled = False

    def _install_mp3_success(self, content=b"MP3V1"):
        async def fake_encoder(engine):
            return FakeProc(stdout=ScriptedStdout([content]))

        async def fake_run_kokoro(
            text, voice, speed, cancel_event=None, permit=None
        ):
            return b"\x01\x02\x03\x04"

        class OkEdge:
            def __init__(self, *a, **k):
                pass

            async def stream(self):
                yield {"type": "audio", "data": b"\xff\xfb\x90\x00"}

        self.app._create_mp3_encoder = fake_encoder
        self.app.run_kokoro = fake_run_kokoro
        self.app.edge_tts.Communicate = OkEdge

    def test_missing_key_returns_401_openai_shape(self):
        request_id = "rid-auth"
        resp = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "model": "edge", "voice": "en-US-AriaNeural"},
            headers={"X-Request-ID": request_id},
        )
        self.assertEqual(resp.status_code, 401)
        body = resp.json()
        self.assertIn("error", body)
        self.assertIn("message", body["error"])
        self.assertIn("type", body["error"])
        self.assertIn("code", body["error"])
        self.assertEqual(resp.headers.get("x-request-id"), request_id)

    def test_forged_origin_still_401(self):
        # 核心：/v1 不吃同源豁免；对照 /api/tts 同条件下可 200。
        forged = {
            "Origin": "http://testserver",
            "Host": "testserver",
        }
        v1 = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "model": "edge", "voice": "en-US-AriaNeural"},
            headers=forged,
        )
        self.assertEqual(v1.status_code, 401)

        tts = self.client.post(
            "/api/tts",
            json={"text": "hello", "engine": "edge", "voice": "en-US-AriaNeural"},
            headers=forged,
        )
        self.assertEqual(tts.status_code, 200)

    def test_bearer_and_x_api_key_succeed(self):
        for headers in (
            {"Authorization": "Bearer secret-v1"},
            {"X-API-Key": "secret-v1"},
        ):
            with self.subTest(headers=headers):
                resp = self.client.post(
                    "/v1/audio/speech",
                    json={"input": "hello world", "model": "edge", "voice": "en-US-AriaNeural"},
                    headers=headers,
                )
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.headers["content-type"], "audio/mpeg")
                self.assertEqual(resp.content, b"MP3V1")

    def test_bearer_scheme_is_case_insensitive(self):
        for scheme in ("bearer", "BEARER", "BeArEr"):
            with self.subTest(scheme=scheme):
                resp = self.client.post(
                    "/v1/audio/speech",
                    json={"input": "hello"},
                    headers={"Authorization": f"{scheme} secret-v1"},
                )
                self.assertEqual(resp.status_code, 200, resp.text)

    def test_ui_exempt_paths_still_open(self):
        self.assertEqual(self.client.get("/index.html").status_code, 200)
        self.assertEqual(self.client.get("/api").status_code, 200)

    def test_401_includes_cors_headers(self):
        resp = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello"},
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(resp.status_code, 401)
        self.assertIn("access-control-allow-origin", resp.headers)

    def test_cors_preflight_is_not_blocked_by_v1_auth(self):
        resp = self.client.options(
            "/v1/audio/speech",
            headers={
                "Origin": "http://client.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("access-control-allow-origin", resp.headers)
        self.assertIn("POST", resp.headers["access-control-allow-methods"])

    def test_v10_is_not_misclassified_as_openai_namespace(self):
        unauthenticated = self.client.get(
            "/v10/not-a-route", headers={"X-Request-ID": "rid-v10"}
        )
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertIn("detail", unauthenticated.json())
        self.assertNotIn("error", unauthenticated.json())

        authenticated = self.client.get(
            "/v10/not-a-route", headers={"X-API-Key": "secret-v1"}
        )
        self.assertEqual(authenticated.status_code, 404)
        self.assertIn("detail", authenticated.json())
        self.assertNotIn("error", authenticated.json())

    def test_auth_precedes_body_validation(self):
        headers = {
            "Origin": "http://evil.example",
            "X-Request-ID": "rid-before-body",
        }
        responses = (
            self.client.post("/v1/audio/speech", json={}, headers=headers),
            self.client.post(
                "/v1/audio/speech",
                json={"input": "hello", "speed": 99},
                headers=headers,
            ),
            self.client.post(
                "/v1/audio/speech",
                content="{",
                headers={**headers, "Content-Type": "application/json"},
            ),
        )
        for resp in responses:
            with self.subTest(body=resp.text):
                self.assertEqual(resp.status_code, 401)
                self.assertEqual(resp.json()["error"]["code"], "invalid_api_key")
                self.assertEqual(
                    resp.headers.get("x-request-id"), "rid-before-body"
                )
                self.assertIn("access-control-allow-origin", resp.headers)

    def test_auth_precedes_oversized_content_length(self):
        self.app.TTS_MAX_REQUEST_BODY_BYTES = 64
        body = json.dumps(
            {"input": "hello", "padding": "x" * 256},
            separators=(",", ":"),
        ).encode("utf-8")
        for credentials in ({}, {"X-API-Key": "wrong-key"}):
            headers = {
                "Content-Type": "application/json",
                "Origin": "http://evil.example",
                "X-Request-ID": "rid-auth-before-size",
                **credentials,
            }
            with self.subTest(credentials=credentials):
                resp = self.client.post(
                    "/v1/audio/speech", content=body, headers=headers
                )
                self.assertEqual(resp.status_code, 401)
                self.assertEqual(
                    resp.json()["error"]["code"], "invalid_api_key"
                )
                self.assertEqual(
                    resp.headers.get("x-request-id"),
                    "rid-auth-before-size",
                )
                self.assertIn("access-control-allow-origin", resp.headers)

    def test_oversized_content_length_is_rejected_after_valid_auth(self):
        self.app.TTS_MAX_REQUEST_BODY_BYTES = 64
        body = json.dumps(
            {"input": "hello", "padding": "x" * 256},
            separators=(",", ":"),
        ).encode("utf-8")
        resp = self.client.post(
            "/v1/audio/speech",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": "secret-v1",
                "X-Request-ID": "rid-size-after-auth",
            },
        )
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(resp.json()["error"]["code"], "request_too_large")
        self.assertEqual(
            resp.headers.get("x-request-id"), "rid-size-after-auth"
        )

    def test_auth_precedes_route_resolution(self):
        headers = {"X-Request-ID": "rid-before-route"}
        for method, path in (
            ("get", "/v1/not-a-route"),
            ("get", "/v1/audio/speech"),
        ):
            with self.subTest(method=method, path=path):
                resp = self.client.request(method, path, headers=headers)
                self.assertEqual(resp.status_code, 401)
                self.assertIn("error", resp.json())
                self.assertEqual(
                    resp.headers.get("x-request-id"), "rid-before-route"
                )

    def test_authenticated_route_errors_use_stable_code_and_request_id(self):
        headers = {
            "Authorization": "Bearer secret-v1",
            "X-Request-ID": "rid-route-error",
        }
        for method, path, expected_status, expected_code in (
            ("get", "/v1/not-a-route", 404, "not_found"),
            ("get", "/v1/audio/speech", 405, "method_not_allowed"),
        ):
            with self.subTest(method=method, path=path):
                resp = self.client.request(method, path, headers=headers)
                self.assertEqual(resp.status_code, expected_status)
                error = resp.json()["error"]
                self.assertEqual(error["type"], "invalid_request_error")
                self.assertEqual(error["code"], expected_code)
                self.assertTrue(error["message"])
                self.assertEqual(
                    resp.headers.get("x-request-id"), "rid-route-error"
                )

    def test_validation_errors_preserve_request_id(self):
        headers = {
            "Authorization": "Bearer secret-v1",
            "X-Request-ID": "rid-validation",
        }
        missing_input = self.client.post(
            "/v1/audio/speech", json={}, headers=headers
        )
        invalid_voice = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "model": "kokoro", "voice": "missing"},
            headers=headers,
        )
        for resp in (missing_input, invalid_voice):
            with self.subTest(body=resp.text):
                self.assertEqual(resp.status_code, 422)
                self.assertIn("error", resp.json())
                self.assertEqual(
                    resp.headers.get("x-request-id"), "rid-validation"
                )


class OpenAISpeechDisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_preflight_disconnect_does_not_forge_openai_500(self):
        app = _load_with_key("")
        app.logger.disabled = True
        synthesis_started = asyncio.Event()
        synthesis_cancelled = asyncio.Event()
        messages = asyncio.Queue()
        sent = []
        body = json.dumps(
            {
                "input": "hello",
                "model": "kokoro",
                "voice": "af_heart",
            }
        ).encode("utf-8")
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/audio/speech",
            "raw_path": b"/v1/audio/speech",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
            "state": {},
        }

        async def hanging_start(*_args, **_kwargs):
            synthesis_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                synthesis_cancelled.set()
                raise

        async def receive():
            return await messages.get()

        async def send(message):
            sent.append(dict(message))

        app._start_synthesis = hanging_start
        await messages.put(
            {"type": "http.request", "body": body, "more_body": False}
        )
        owner = asyncio.create_task(app.app(scope, receive, send))
        try:
            await asyncio.wait_for(synthesis_started.wait(), 0.5)
            await messages.put({"type": "http.disconnect"})
            await asyncio.wait_for(owner, 0.5)
            self.assertTrue(synthesis_cancelled.is_set())
            self.assertFalse(
                any(message["type"] == "http.response.start" for message in sent),
                f"disconnected request must not receive a forged response: {sent!r}",
            )
        finally:
            app.logger.disabled = False
            if not owner.done():
                owner.cancel()
                try:
                    await owner
                except BaseException:
                    pass


class OpenAIUnhandledErrorMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = import_app_with_fakes()
        self.scope = {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/audio/speech",
            "raw_path": b"/v1/audio/speech",
            "query_string": b"",
            "headers": [],
            "state": {},
        }

    async def test_exception_after_response_start_is_not_rewritten(self):
        sent = []

        async def downstream(_scope, _receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                }
            )
            raise RuntimeError("stream failed")

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(dict(message))

        middleware = self.app.OpenAIUnhandledErrorMiddleware(downstream)
        with self.assertRaisesRegex(RuntimeError, "stream failed"):
            await middleware(self.scope, receive, send)
        self.assertEqual(
            [message["type"] for message in sent], ["http.response.start"]
        )

    async def test_send_failure_during_response_start_is_not_retried(self):
        send_calls = 0

        async def downstream(_scope, _receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                }
            )

        async def receive():
            return {"type": "http.disconnect"}

        async def failing_send(_message):
            nonlocal send_calls
            send_calls += 1
            raise RuntimeError("client send failed")

        middleware = self.app.OpenAIUnhandledErrorMiddleware(downstream)
        with self.assertRaisesRegex(RuntimeError, "client send failed"):
            await middleware(self.scope, receive, failing_send)
        self.assertEqual(send_calls, 1)

    async def test_cancellation_and_client_disconnect_are_not_rewritten(self):
        async def receive():
            return {"type": "http.disconnect"}

        for exception in (asyncio.CancelledError(), ClientDisconnect()):
            with self.subTest(exception=type(exception).__name__):
                sent = []

                async def downstream(_scope, _receive, _send):
                    raise exception

                async def send(message):
                    sent.append(dict(message))

                middleware = self.app.OpenAIUnhandledErrorMiddleware(downstream)
                with self.assertRaises(type(exception)):
                    await middleware(self.scope, receive, send)
                self.assertEqual(sent, [])

    async def test_non_v1_exception_is_not_rewritten(self):
        sent = []
        scope = {**self.scope, "path": "/api/tts", "raw_path": b"/api/tts"}

        async def downstream(_scope, _receive, _send):
            raise RuntimeError("legacy failure")

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(dict(message))

        middleware = self.app.OpenAIUnhandledErrorMiddleware(downstream)
        with self.assertRaisesRegex(RuntimeError, "legacy failure"):
            await middleware(scope, receive, send)

    async def test_response_write_timeout_cancels_downstream_and_runs_cleanup(self):
        body_send_entered = asyncio.Event()
        cleanup_ran = asyncio.Event()

        async def downstream(_scope, _receive, send):
            try:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b"audio",
                        "more_body": True,
                    }
                )
            finally:
                cleanup_ran.set()

        async def receive():
            return {"type": "http.disconnect"}

        async def hanging_send(message):
            if message["type"] == "http.response.body":
                body_send_entered.set()
                await asyncio.Event().wait()

        middleware = self.app.ResponseWriteTimeoutMiddleware(
            downstream, timeout_seconds=0.01
        )
        with self.assertRaises(self.app._HttpResponseWriteTimeout):
            await middleware(self.scope, receive, hanging_send)

        self.assertTrue(body_send_entered.is_set())
        self.assertTrue(cleanup_ran.is_set())

    async def test_response_write_owner_cancellation_cleans_child_send(self):
        body_send_entered = asyncio.Event()
        child_send_cancelled = asyncio.Event()
        child_send_task = None

        async def downstream(_scope, _receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"audio",
                    "more_body": True,
                }
            )

        async def receive():
            return {"type": "http.disconnect"}

        async def hanging_send(message):
            nonlocal child_send_task
            if message["type"] != "http.response.body":
                return
            child_send_task = asyncio.current_task()
            body_send_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                child_send_cancelled.set()
                raise

        middleware = self.app.ResponseWriteTimeoutMiddleware(
            downstream, timeout_seconds=60
        )
        owner = asyncio.create_task(
            middleware(self.scope, receive, hanging_send)
        )
        try:
            await asyncio.wait_for(body_send_entered.wait(), 0.5)
            owner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await owner

            self.assertTrue(
                child_send_cancelled.is_set(),
                "owner cancellation must cancel the pending child send",
            )
            self.assertIsNotNone(child_send_task)
            self.assertTrue(child_send_task.done())
        finally:
            if not owner.done():
                owner.cancel()
                await asyncio.gather(owner, return_exceptions=True)
            if child_send_task is not None and not child_send_task.done():
                child_send_task.cancel()
                await asyncio.gather(child_send_task, return_exceptions=True)


class RequestBodyLimitMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunked_body_is_counted_without_content_length(self):
        app = import_app_with_fakes()
        app.TTS_MAX_REQUEST_BODY_BYTES = 5
        downstream_completed = False
        messages = iter(
            (
                {"type": "http.request", "body": b"123", "more_body": True},
                {"type": "http.request", "body": b"456", "more_body": False},
            )
        )
        sent = []
        scope = {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/audio/speech",
            "raw_path": b"/v1/audio/speech",
            "query_string": b"",
            "headers": [(b"x-request-id", b"rid-chunked")],
            "state": {},
        }

        async def downstream(_scope, receive, _send):
            nonlocal downstream_completed
            while True:
                message = await receive()
                if not message.get("more_body"):
                    break
            downstream_completed = True

        async def receive():
            return next(messages)

        async def send(message):
            sent.append(dict(message))

        middleware = app.RequestBodyLimitMiddleware(downstream)
        await middleware(scope, receive, send)

        self.assertFalse(downstream_completed)
        self.assertEqual(sent[0]["status"], 413)
        headers = dict(sent[0]["headers"])
        self.assertEqual(headers[b"x-request-id"], b"rid-chunked")
        body = json.loads(sent[1]["body"])
        self.assertEqual(body["error"]["code"], "request_too_large")


class OpenAISpeechContractTests(unittest.TestCase):
    def setUp(self):
        self.app = import_app_with_fakes()
        self.client = TestClient(self.app.app)
        self.app.pipeline_zh = object()
        self.app.pipeline_en = object()
        self.app.logger.disabled = True
        self.seen = {}
        self._real_create_mp3_encoder = self.app._create_mp3_encoder
        self._real_create_openai_audio_encoder = (
            self.app._create_openai_audio_encoder
        )
        self._install_capture_success()

    def tearDown(self):
        self.app.logger.disabled = False

    def _install_capture_success(self, content=b"MP3OK"):
        seen = self.seen

        async def fake_encoder(engine):
            seen["encoder_engine"] = engine
            seen["encoder_format"] = "mp3"
            return FakeProc(stdout=ScriptedStdout([content]))

        async def fake_openai_encoder(engine, response_format):
            seen["encoder_engine"] = engine
            seen["encoder_format"] = response_format
            return FakeProc(stdout=ScriptedStdout([content]))

        async def fake_run_kokoro(
            text, voice, speed, cancel_event=None, permit=None
        ):
            seen["kokoro"] = {"text": text, "voice": voice, "speed": speed}
            return b"\x01\x02\x03\x04"

        class CaptureEdge:
            def __init__(self, text, voice, rate=None):
                seen["edge"] = {"text": text, "voice": voice, "rate": rate}

            async def stream(self):
                yield {"type": "audio", "data": b"\xff\xfb\x90\x00"}

        self.app._create_mp3_encoder = fake_encoder
        self.app._create_openai_audio_encoder = fake_openai_encoder
        self.app.run_kokoro = fake_run_kokoro
        self.app.edge_tts.Communicate = CaptureEdge

    def test_standard_payload_returns_audio_mpeg(self):
        resp = self.client.post(
            "/v1/audio/speech",
            json={
                "input": "hello world",
                "model": "edge",
                "voice": "en-US-AriaNeural",
                "response_format": "mp3",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "audio/mpeg")
        self.assertIn("x-request-id", resp.headers)
        self.assertEqual(resp.content, b"MP3OK")
        self.assertEqual(self.seen["edge"]["text"], "hello world")
        self.assertEqual(self.seen["edge"]["voice"], "en-US-AriaNeural")

    def test_kokoro_rejects_letters_outside_voice_script_before_synthesis(self):
        for voice in ("af_heart", "zf_xiaoxiao"):
            for text in ("かな", "한글", "Привет"):
                with self.subTest(voice=voice, text=text):
                    self.seen.clear()
                    resp = self.client.post(
                        "/v1/audio/speech",
                        json={"input": text, "model": "kokoro", "voice": voice},
                    )
                    self.assertEqual(resp.status_code, 422, resp.text)
                    self.assertEqual(
                        resp.json()["error"]["code"], "invalid_request"
                    )
                    self.assertIn("use Edge", resp.json()["error"]["message"])
                    self.assertNotIn(
                        "English Kokoro voice or Edge",
                        resp.json()["error"]["message"],
                    )
                    self.assertNotIn("encoder_engine", self.seen)
                    self.assertNotIn("kokoro", self.seen)

        # 省略 voice 时，纯第三脚本文本默认选择英文 Kokoro 音色，也必须拒绝。
        self.seen.clear()
        resp = self.client.post(
            "/v1/audio/speech", json={"input": "かな", "model": "kokoro"}
        )
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertNotIn("encoder_engine", self.seen)
        self.assertNotIn("kokoro", self.seen)

    def test_kokoro_accepts_unicode_letters_in_matching_script(self):
        for voice, text in (
            ("af_heart", "éclair"),
            ("af_heart", "Ｈｅｌｌｏ"),
            ("zf_xiaoxiao", "你好"),
        ):
            with self.subTest(voice=voice, text=text):
                self.seen.clear()
                resp = self.client.post(
                    "/v1/audio/speech",
                    json={"input": text, "model": "kokoro", "voice": voice},
                )
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(self.seen["kokoro"]["text"], text)

    def test_default_edge_preserves_other_unicode_scripts(self):
        text = "かな"

        resp = self.client.post("/v1/audio/speech", json={"input": text})

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.seen["edge"]["text"], text)

    def test_encoder_eof_before_first_output_returns_500_not_empty_200(self):
        created = []
        limiter = self.app.FfmpegLimiter(1)
        self.app._ffmpeg_limiter = limiter

        async def eof_encoder(_engine):
            self.assertTrue(await limiter.acquire())
            proc = FakeProc(stdout=ScriptedStdout([]))
            created.append(proc)
            return proc

        async def eof_openai_encoder(_engine, _response_format):
            return await eof_encoder(_engine)

        self.app._create_mp3_encoder = eof_encoder
        self.app._create_openai_audio_encoder = eof_openai_encoder

        for engine, voice in (
            ("kokoro", "af_heart"),
            ("edge", "en-US-AriaNeural"),
        ):
            for response_format in ("mp3", "opus"):
                with self.subTest(engine=engine, response_format=response_format):
                    resp = self.client.post(
                        "/v1/audio/speech",
                        json={
                            "input": "hello",
                            "model": engine,
                            "voice": voice,
                            "response_format": response_format,
                        },
                    )
                    self.assertEqual(resp.status_code, 500, resp.text)
                    self.assertEqual(
                        resp.json()["error"]["code"], "synthesis_failed"
                    )
                    self.assertEqual(limiter.active, 0)
                    self.assertTrue(created[-1].killed)
                    self.assertTrue(created[-1].waited)

    def test_encoder_output_timeout_before_first_byte_returns_504(self):
        limiter = self.app.FfmpegLimiter(1)
        self.app._ffmpeg_limiter = limiter
        proc = FakeProc(stdout=HangingStdout())

        async def hanging_encoder(_engine):
            self.assertTrue(await limiter.acquire())
            return proc

        self.app._create_mp3_encoder = hanging_encoder
        self.app.TTS_SYNTHESIS_TIMEOUT_SECONDS = 0.01

        resp = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "model": "kokoro", "voice": "af_heart"},
        )

        self.assertEqual(resp.status_code, 504, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "timeout")
        self.assertEqual(limiter.active, 0)
        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)

    def test_preflight_first_encoded_chunk_is_sent_exactly_once(self):
        proc = FakeProc(stdout=ScriptedStdout([b"FIRST", b"SECOND"]))

        async def encoder(_engine):
            return proc

        self.app._create_mp3_encoder = encoder

        resp = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "model": "kokoro", "voice": "af_heart"},
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.content, b"FIRSTSECOND")

    def test_missing_input_returns_openai_error(self):
        resp = self.client.post(
            "/v1/audio/speech",
            json={"model": "edge", "voice": "en-US-AriaNeural"},
        )
        self.assertIn(resp.status_code, (400, 422))
        body = resp.json()
        self.assertIn("error", body)
        self.assertIn("message", body["error"])
        self.assertIn("type", body["error"])

    def test_error_object_includes_nullable_param(self):
        resp = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "instructions": "speak slowly"},
        )

        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("param", resp.json()["error"])
        self.assertIsNone(resp.json()["error"]["param"])

    def test_unknown_extra_fields_do_not_422(self):
        resp = self.client.post(
            "/v1/audio/speech",
            json={
                "input": "hello",
                "model": "edge",
                "voice": "en-US-AriaNeural",
                "response_format": "mp3",
                "user": "agent-1",
                "metadata": {"source": "compat-test"},
            },
        )
        self.assertEqual(resp.status_code, 200)

    def test_known_unsupported_semantic_fields_fail_explicitly(self):
        for field, value in (
            ("instructions", "speak slowly"),
            ("stream_format", "sse"),
            ("lang_code", "es"),
            ("lang", "zh"),
            ("language", "fr"),
            ("format", "wav"),
        ):
            with self.subTest(field=field):
                resp = self.client.post(
                    "/v1/audio/speech",
                    json={"input": "hello", field: value},
                )
                self.assertEqual(resp.status_code, 400, resp.text)
                error = resp.json()["error"]
                self.assertEqual(error["type"], "invalid_request_error")
                self.assertIn(field, error["message"])

    def test_stream_format_audio_keeps_binary_response(self):
        resp = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "stream_format": "audio"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.headers["content-type"], "audio/mpeg")

    def test_model_maps_to_engine_and_unknown_falls_back_to_edge(self):
        resp = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "model": "tts-1", "voice": "en-US-AriaNeural"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.seen.get("encoder_engine"), "edge")

        self.seen.clear()
        resp = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "model": "kokoro", "voice": "af_heart"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.seen.get("encoder_engine"), "kokoro")
        self.assertEqual(self.seen["kokoro"]["voice"], "af_heart")

    def test_kokoro_model_without_voice_uses_kokoro_default(self):
        for text, expected_voice in (
            ("hello", "af_heart"),
            ("你好", "zf_xiaoxiao"),
        ):
            with self.subTest(text=text):
                self.seen.clear()
                resp = self.client.post(
                    "/v1/audio/speech",
                    json={"input": text, "model": "kokoro"},
                )
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(self.seen.get("encoder_engine"), "kokoro")
                self.assertEqual(
                    self.seen["kokoro"]["voice"], expected_voice
                )

    def test_default_engine_is_edge_without_voice_en_and_zh(self):
        for model in (None, "edge"):
            for text, expected_voice in (
                ("hello world", "en-US-AvaNeural"),
                ("你好世界", "zh-CN-XiaoxiaoNeural"),
            ):
                with self.subTest(model=model, text=text):
                    self.seen.clear()
                    payload = {"input": text}
                    if model is not None:
                        payload["model"] = model
                    resp = self.client.post(
                        "/v1/audio/speech", json=payload
                    )
                    self.assertEqual(resp.status_code, 200, resp.text)
                    self.assertEqual(resp.content, b"MP3OK")
                    self.assertEqual(self.seen.get("encoder_engine"), "edge")
                    self.assertIn("edge", self.seen)
                    self.assertEqual(
                        self.seen["edge"]["voice"], expected_voice
                    )

    def test_kokoro_english_voice_rejects_cjk_before_synthesis(self):
        for text in ("你好，世界", "部署 Docker 容器"):
            with self.subTest(text=text):
                self.seen.clear()
                resp = self.client.post(
                    "/v1/audio/speech",
                    json={
                        "input": text,
                        "model": "kokoro",
                        "voice": "alloy",
                    },
                )
                self.assertEqual(resp.status_code, 422, resp.text)
                error = resp.json()["error"]
                self.assertEqual(error["code"], "invalid_request")
                self.assertEqual(error["type"], "invalid_request_error")
                self.assertIn("English Kokoro voice", error["message"])
                self.assertIn("CJK", error["message"])
                self.assertIn("x-request-id", resp.headers)
                self.assertNotIn("encoder_engine", self.seen)
                self.assertNotIn("kokoro", self.seen)

    def test_kokoro_chinese_voice_rejects_latin_before_synthesis(self):
        for text in ("hello world", "部署 Docker 容器"):
            with self.subTest(text=text):
                self.seen.clear()
                resp = self.client.post(
                    "/v1/audio/speech",
                    json={
                        "input": text,
                        "model": "kokoro",
                        "voice": "zf_xiaoxiao",
                    },
                )
                self.assertEqual(resp.status_code, 422, resp.text)
                error = resp.json()["error"]
                self.assertEqual(error["code"], "invalid_request")
                self.assertEqual(error["type"], "invalid_request_error")
                self.assertIn("Chinese Kokoro voice", error["message"])
                self.assertIn("Latin", error["message"])
                self.assertIn("x-request-id", resp.headers)
                self.assertNotIn("encoder_engine", self.seen)
                self.assertNotIn("kokoro", self.seen)

    def test_default_and_edge_alloy_preserve_mixed_chinese_text(self):
        text = "部署 Docker 容器"
        for model in (None, "edge", "tts-1"):
            with self.subTest(model=model):
                self.seen.clear()
                payload = {"input": text, "voice": "alloy"}
                if model is not None:
                    payload["model"] = model
                resp = self.client.post("/v1/audio/speech", json=payload)
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(self.seen.get("encoder_engine"), "edge")
                self.assertEqual(
                    self.seen["edge"]["voice"], "en-US-AvaNeural"
                )
                self.assertEqual(self.seen["edge"]["text"], text)

    def test_openai_voice_aliases_follow_selected_engine(self):
        edge_aliases = {
            "alloy": "en-US-AvaNeural",
            "coral": "en-US-AvaNeural",
            "echo": "en-US-AndrewNeural",
            "fable": "en-US-GuyNeural",
            "onyx": "en-US-BrianNeural",
            "nova": "en-US-EmmaNeural",
            "shimmer": "en-US-JennyNeural",
        }
        kokoro_aliases = {
            "alloy": "af_alloy",
            "coral": "af_heart",
            "echo": "am_echo",
            "fable": "af_bella",
            "onyx": "am_onyx",
            "nova": "af_nova",
            "shimmer": "af_sarah",
        }
        for model, expected_engine in (
            (None, "edge"),
            ("edge", "edge"),
            ("kokoro", "kokoro"),
        ):
            for name in (
                "alloy", "coral", "echo", "fable", "onyx", "nova", "shimmer"
            ):
                with self.subTest(model=model, voice=name):
                    self.seen.clear()
                    payload = {"input": "hello", "voice": name}
                    if model is not None:
                        payload["model"] = model
                    resp = self.client.post("/v1/audio/speech", json=payload)
                    self.assertEqual(resp.status_code, 200, resp.text)
                    self.assertEqual(
                        self.seen.get("encoder_engine"), expected_engine
                    )
                    expected_voice = (
                        edge_aliases[name]
                        if expected_engine == "edge"
                        else kokoro_aliases[name]
                    )
                    self.assertEqual(
                        self.seen[expected_engine]["voice"], expected_voice
                    )

    def test_padded_openai_alias_still_resolves(self):
        # 回归：两侧空白曾绕过别名表，冷缓存时把 "  alloy  " 原样交给 Edge。
        self.app._edge_voices_cache = [
            {
                "ShortName": "en-US-AvaNeural",
                "FriendlyName": "Ava",
                "Gender": "Female",
                "Locale": "en-US",
            }
        ]
        self.app._edge_voices_cache_expires_at = float("inf")
        self.seen.clear()
        resp = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "voice": "  alloy  "},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.seen.get("encoder_engine"), "edge")
        self.assertEqual(self.seen["edge"]["voice"], "en-US-AvaNeural")

    def test_explicit_edge_model_rejects_kokoro_voice_id(self):
        resp = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "model": "edge", "voice": "af_heart"},
        )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"]["code"], "invalid_request")
        self.assertNotIn("encoder_engine", self.seen)

    def test_real_voice_id_passthrough(self):
        resp = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "model": "kokoro", "voice": "am_adam"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.seen["kokoro"]["voice"], "am_adam")

    def test_explicit_empty_voice_is_rejected(self):
        for voice in ("", "   "):
            with self.subTest(voice=repr(voice)):
                resp = self.client.post(
                    "/v1/audio/speech",
                    json={"input": "hello", "voice": voice},
                )
                self.assertEqual(resp.status_code, 422)
                self.assertEqual(
                    resp.json()["error"]["code"], "invalid_request"
                )

    def test_invalid_kokoro_voice_is_4xx_not_502(self):
        resp = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "model": "kokoro", "voice": "not-a-voice"},
        )
        self.assertEqual(resp.status_code, 422)
        body = resp.json()
        self.assertEqual(body["error"]["code"], "invalid_request")

    def test_invalid_edge_voice_prechecked_when_catalog_present(self):
        calls = 0

        async def must_not_refresh():
            nonlocal calls
            calls += 1
            raise AssertionError("speech validation must not refresh voice catalog")

        self.app.edge_tts.list_voices = must_not_refresh
        self.app._edge_voices_cache = [
            {
                "ShortName": "en-US-AriaNeural",
                "FriendlyName": "Aria",
                "Gender": "Female",
                "Locale": "en-US",
            }
        ]
        self.app._edge_voices_cache_expires_at = float("inf")
        self.app._edge_voices_retry_after = 0.0

        resp = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "model": "edge", "voice": "en-US-NotRealNeural"},
        )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"]["code"], "invalid_request")
        self.assertEqual(calls, 0)

    def test_cold_edge_catalog_does_not_block_speech_validation(self):
        calls = 0

        async def must_not_refresh():
            nonlocal calls
            calls += 1
            return [{"ShortName": "en-US-OtherNeural"}]

        self.app.edge_tts.list_voices = must_not_refresh
        self.app._edge_voices_cache = None
        self.app._edge_voices_cache_expires_at = 0.0
        self.app._edge_voices_retry_after = 0.0

        resp = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "model": "edge", "voice": "en-US-NewNeural"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(calls, 0)

    def test_stale_edge_catalog_does_not_reject_new_voice(self):
        calls = 0

        async def must_not_refresh():
            nonlocal calls
            calls += 1
            raise AssertionError("stale validation must not refresh catalog")

        self.app.edge_tts.list_voices = must_not_refresh
        self.app._edge_voices_cache = [{"ShortName": "en-US-OldNeural"}]
        self.app._edge_voices_cache_expires_at = 0.0
        self.app._edge_voices_retry_after = float("inf")

        resp = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "model": "edge", "voice": "en-US-NewNeural"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(calls, 0)

    def test_empty_edge_catalog_allows_unknown_voice(self):
        calls = 0

        async def empty_list():
            nonlocal calls
            calls += 1
            return []

        self.app.edge_tts.list_voices = empty_list
        self.app._edge_voices_cache = []
        self.app._edge_voices_cache_expires_at = 1e18
        self.app._edge_voices_retry_after = 0.0

        resp = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "model": "edge", "voice": "en-US-UnknownNeural"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(calls, 0)

    def test_official_binary_response_formats_are_encoded_and_typed(self):
        expected_media_types = {
            "mp3": "audio/mpeg",
            "opus": "audio/ogg",
            "aac": "audio/aac",
            "flac": "audio/flac",
            "wav": "audio/wav",
            "pcm": "application/octet-stream",
        }
        for response_format, media_type in expected_media_types.items():
            with self.subTest(response_format=response_format):
                self.seen.clear()
                resp = self.client.post(
                    "/v1/audio/speech",
                    json={"input": "hello", "response_format": response_format},
                )
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(resp.headers["content-type"], media_type)
                self.assertEqual(
                    resp.headers["content-disposition"],
                    f"inline; filename=tts-output.{response_format}",
                )
                self.assertEqual(
                    self.seen.get("encoder_format"), response_format
                )

    def test_custom_voice_object_is_explicitly_rejected(self):
        self.seen.clear()
        resp = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "voice": {"id": "voice_1234"}},
        )
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "invalid_request")
        self.assertNotIn("encoder_engine", self.seen)

    def test_unknown_or_empty_response_format_errors(self):
        bad = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "response_format": "not-a-format"},
        )
        self.assertEqual(bad.status_code, 400)
        body = bad.json()
        self.assertIn("error", body)
        self.assertIn("not-a-format", body["error"]["message"].lower())

        empty = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "response_format": ""},
        )
        self.assertEqual(empty.status_code, 400)
        self.assertIn("error", empty.json())

        null = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "response_format": None},
        )
        self.assertEqual(null.status_code, 422)
        self.assertIn("error", null.json())

    def test_non_finite_speed_is_rejected_as_invalid_request(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                resp = self.client.post(
                    "/v1/audio/speech",
                    content=f'{{"input":"hello","speed":{value}}}',
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(resp.status_code, 422)
                self.assertEqual(
                    resp.json()["error"]["code"], "invalid_request"
                )

    def test_lone_surrogate_in_error_message_keeps_json_and_status(self):
        cases = (
            (
                {
                    "input": "hello",
                    "model": "kokoro",
                    "voice": chr(0xD800),
                },
                422,
            ),
            (
                {"input": "hello", "response_format": chr(0xD800)},
                400,
            ),
        )
        client = TestClient(self.app.app, raise_server_exceptions=False)
        for payload, expected_status in cases:
            with self.subTest(field=tuple(payload)):
                body = json.dumps(payload, ensure_ascii=True).encode("ascii")
                resp = client.post(
                    "/v1/audio/speech",
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Request-ID": "rid-surrogate",
                    },
                )
                self.assertEqual(resp.status_code, expected_status)
                self.assertEqual(
                    resp.headers.get("content-type"), "application/json"
                )
                self.assertEqual(
                    set(resp.json()["error"]),
                    {"message", "type", "param", "code"},
                )
                self.assertEqual(
                    resp.headers.get("x-request-id"), "rid-surrogate"
                )

    def test_edge_lone_surrogate_voice_does_not_break_logs_endpoint(self):
        self.app.logger.disabled = False
        self.app._ring_handler.buffer.clear()
        called = False

        class BoomEdge:
            def __init__(self, *args, **kwargs):
                nonlocal called
                called = True

            async def stream(self):
                raise RuntimeError("edge upstream failure")
                yield

        self.app.edge_tts.Communicate = BoomEdge

        async def fake_encoder(engine):
            return FakeProc(stdout=HangingStdout())

        self.app._create_mp3_encoder = fake_encoder
        body = json.dumps(
            {"input": "hello", "model": "edge", "voice": "bad\ud800voice"},
            ensure_ascii=True,
        ).encode("ascii")
        resp = self.client.post(
            "/v1/audio/speech",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 422)
        self.assertFalse(called)
        logs = self.client.get("/api/logs", headers={"X-API-Key": "secret-v1"})
        self.assertEqual(logs.status_code, 200)
        lines = logs.json()["lines"]
        self.assertTrue(all("\ud800" not in line for line in lines))

    def test_edge_voice_control_character_cannot_forge_log_records(self):
        self.app.logger.disabled = False
        self.app._ring_handler.buffer.clear()
        called = False

        class BoomEdge:
            def __init__(self, *args, **kwargs):
                nonlocal called
                called = True

            async def stream(self):
                raise RuntimeError("edge upstream failure")
                yield

        self.app.edge_tts.Communicate = BoomEdge

        async def fake_encoder(engine):
            return FakeProc(stdout=HangingStdout())

        self.app._create_mp3_encoder = fake_encoder
        voice = "bad\nFORGED"
        resp = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "model": "edge", "voice": voice},
        )
        self.assertEqual(resp.status_code, 422)
        self.assertFalse(called)
        logs = self.client.get("/api/logs", headers={"X-API-Key": "secret-v1"})
        self.assertEqual(logs.status_code, 200)
        lines = logs.json()["lines"]
        for line in lines:
            self.assertNotIn("\n", line)

    def test_edge_voice_has_bounded_length_before_synthesis(self):
        called = False

        async def fail_if_called(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("synthesis must not start")

        self.app._start_synthesis_for_request = fail_if_called
        voice = "v" * 4096
        resp = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "model": "edge", "voice": voice},
        )
        self.assertEqual(resp.status_code, 422)
        self.assertFalse(called)

    def test_openai_request_body_limit_returns_413_before_validation(self):
        self.app.TTS_MAX_REQUEST_BODY_BYTES = 128
        body = json.dumps(
            {"input": "hello", "padding": "x" * 512},
            separators=(",", ":"),
        ).encode("utf-8")
        resp = self.client.post(
            "/v1/audio/speech",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Request-ID": "rid-too-large",
            },
        )
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(resp.json()["error"]["code"], "request_too_large")
        self.assertEqual(resp.headers.get("x-request-id"), "rid-too-large")

    def _install_exhausted_ffmpeg(self):
        """走真实 _create_mp3_encoder + 耗尽 limiter，触发真正的 429 路径。"""
        limiter = self.app.FfmpegLimiter(1)
        self.assertTrue(asyncio.run(limiter.acquire()))
        self.app._create_mp3_encoder = self._real_create_mp3_encoder
        self.app._create_openai_audio_encoder = (
            self._real_create_openai_audio_encoder
        )
        self.app._ffmpeg_limiter = limiter

    def test_error_shape_for_common_status_codes(self):
        # 503 not ready
        self.app.pipeline_zh = None
        r503 = self.client.post(
            "/v1/audio/speech",
            json={"input": "hi"},
            headers={"X-Request-ID": "rid-503"},
        )
        self.app.pipeline_zh = object()
        self.app.pipeline_en = object()
        self.assertEqual(r503.status_code, 503)
        self.assertEqual(
            r503.json()["error"],
            {
                "message": "TTS engine not ready",
                "type": "server_error",
                "param": None,
                "code": "engine_not_ready",
            },
        )
        self.assertEqual(r503.headers.get("x-request-id"), "rid-503")

        # 400 empty after clean
        r400 = self.client.post(
            "/v1/audio/speech",
            json={"input": "```python\nprint(1)\n```"},
            headers={"X-Request-ID": "rid-400"},
        )
        self.assertEqual(r400.status_code, 400)
        self.assertEqual(
            r400.json()["error"],
            {
                "message": "input is empty after cleaning",
                "type": "invalid_request_error",
                "param": None,
                "code": "invalid_request",
            },
        )
        self.assertEqual(r400.headers.get("x-request-id"), "rid-400")

        self._install_exhausted_ffmpeg()
        r429 = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello"},
            headers={"X-Request-ID": "rid-429"},
        )
        self.assertEqual(r429.status_code, 429)
        self.assertEqual(
            r429.json()["error"],
            {
                "message": "ffmpeg process limit reached",
                "type": "rate_limit_error",
                "param": None,
                "code": "rate_limit_exceeded",
            },
        )
        self.assertEqual(r429.headers.get("x-request-id"), "rid-429")
        self.assertIn("retry-after", r429.headers)

    def test_429_includes_retry_after(self):
        self._install_exhausted_ffmpeg()
        resp = self.client.post("/v1/audio/speech", json={"input": "hello world"})
        self.assertEqual(resp.status_code, 429)
        self.assertIn("retry-after", resp.headers)
        self.assertTrue(int(resp.headers["retry-after"]) >= 1)
        self.assertIn("error", resp.json())

    def test_api_tts_429_still_detail_shape(self):
        self._install_exhausted_ffmpeg()
        resp = self.client.post(
            "/api/tts",
            json={"text": "hello", "engine": "kokoro", "voice": "af_heart"},
        )
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["detail"], "ffmpeg process limit reached")
        self.assertNotIn("error", resp.json())

    def test_api_validation_error_keeps_detail_shape(self):
        resp = self.client.post("/api/tts", json={})
        self.assertEqual(resp.status_code, 422)
        self.assertIn("detail", resp.json())
        self.assertNotIn("error", resp.json())

    def test_504_openai_shape_and_request_id(self):
        async def timeout(*args, **kwargs):
            raise self.app.HTTPException(
                status_code=504, detail="synthesis timed out"
            )

        self.app._start_synthesis_for_request = timeout
        resp = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "model": "kokoro", "voice": "af_heart"},
            headers={"X-Request-ID": "rid-timeout"},
        )
        self.assertEqual(resp.status_code, 504)
        self.assertEqual(
            resp.json()["error"],
            {
                "message": "synthesis timed out",
                "type": "server_error",
                "param": None,
                "code": "timeout",
            },
        )
        self.assertEqual(resp.headers.get("x-request-id"), "rid-timeout")

    def test_encoder_spawn_failure_returns_openai_500_and_releases_slot(self):
        self.app._create_mp3_encoder = self._real_create_mp3_encoder
        self.app._create_openai_audio_encoder = (
            self._real_create_openai_audio_encoder
        )
        self.app._ffmpeg_limiter = self.app.FfmpegLimiter(1)
        client = TestClient(self.app.app, raise_server_exceptions=False)

        with mock.patch.object(
            self.app.asyncio,
            "create_subprocess_exec",
            side_effect=FileNotFoundError("private executable path"),
        ):
            resp = client.post(
                "/v1/audio/speech",
                json={
                    "input": "hello",
                    "model": "kokoro",
                    "voice": "af_heart",
                },
                headers={"X-Request-ID": "rid-spawn"},
            )

        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.headers.get("content-type"), "application/json")
        self.assertEqual(
            resp.json()["error"],
            {
                "message": "synthesis failed",
                "type": "server_error",
                "param": None,
                "code": "synthesis_failed",
            },
        )
        self.assertEqual(resp.headers.get("x-request-id"), "rid-spawn")
        self.assertNotIn("private executable path", resp.text)
        self.assertEqual(self.app._ffmpeg_limiter.active, 0)

    def test_unexpected_pre_stream_exception_uses_openai_500(self):
        def fail_clean_text(_text):
            raise RuntimeError("private clean-text details")

        self.app.clean_text = fail_clean_text
        client = TestClient(self.app.app, raise_server_exceptions=False)
        resp = client.post(
            "/v1/audio/speech",
            json={"input": "hello"},
            headers={
                "Origin": "http://client.example",
                "X-Request-ID": "rid-unexpected",
            },
        )

        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.headers.get("content-type"), "application/json")
        self.assertEqual(
            resp.json()["error"],
            {
                "message": "Internal server error",
                "type": "server_error",
                "param": None,
                "code": "synthesis_failed",
            },
        )
        self.assertEqual(resp.headers.get("x-request-id"), "rid-unexpected")
        self.assertIn("access-control-allow-origin", resp.headers)
        self.assertNotIn("private clean-text details", resp.text)

    def test_response_construction_failure_reaps_session_and_uses_openai_500(self):
        proc = FakeProc(stdout=ScriptedStdout([b"MP3-CONSTRUCT"]))

        async def fake_encoder(_engine):
            return proc

        def fail_response(*_args, **_kwargs):
            raise RuntimeError("private response details")

        self.app._create_mp3_encoder = fake_encoder
        self.app._Mp3StreamingResponse = fail_response
        client = TestClient(self.app.app, raise_server_exceptions=False)
        resp = client.post(
            "/v1/audio/speech",
            json={"input": "hello", "model": "kokoro", "voice": "af_heart"},
            headers={"X-Request-ID": "rid-response-construction"},
        )

        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.headers.get("content-type"), "application/json")
        self.assertEqual(resp.json()["error"]["code"], "synthesis_failed")
        self.assertEqual(
            resp.headers.get("x-request-id"), "rid-response-construction"
        )
        self.assertNotIn("private response details", resp.text)
        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)

    def test_502_and_500_openai_shape(self):
        async def fake_encoder(engine):
            # 保持编码器为 pending，使本用例只验证引擎/上游异常分类；
            # 明确 EOF 属本地编码器故障，应由独立用例断言 500。
            return FakeProc(stdout=HangingStdout())

        async def boom_kokoro(
            text, voice, speed, cancel_event=None, permit=None
        ):
            raise RuntimeError("kokoro boom")

        class BoomEdge:
            def __init__(self, *a, **k):
                pass

            async def stream(self):
                raise RuntimeError("edge boom")
                yield

        self.app._create_mp3_encoder = fake_encoder
        self.app.run_kokoro = boom_kokoro
        r500 = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "model": "kokoro", "voice": "af_heart"},
            headers={"X-Request-ID": "rid-kokoro-failure"},
        )
        self.assertEqual(r500.status_code, 500)
        self.assertEqual(
            r500.json()["error"],
            {
                "message": "synthesis failed",
                "type": "server_error",
                "param": None,
                "code": "synthesis_failed",
            },
        )
        self.assertEqual(
            r500.headers.get("x-request-id"), "rid-kokoro-failure"
        )

        self.app.edge_tts.Communicate = BoomEdge
        r502 = self.client.post(
            "/v1/audio/speech",
            json={"input": "hello", "model": "edge", "voice": "en-US-AriaNeural"},
            headers={"X-Request-ID": "rid-edge-failure"},
        )
        self.assertEqual(r502.status_code, 502)
        self.assertEqual(
            r502.json()["error"],
            {
                "message": "synthesis failed",
                "type": "server_error",
                "param": None,
                "code": "upstream_error",
            },
        )
        self.assertEqual(
            r502.headers.get("x-request-id"), "rid-edge-failure"
        )

    def test_400_no_speakable_content_stays_4xx(self):
        async def fake_encoder(engine):
            return FakeProc(stdout=ScriptedStdout([b"x"]))

        async def empty_kokoro(
            text, voice, speed, cancel_event=None, permit=None
        ):
            return b""

        self.app._create_mp3_encoder = fake_encoder
        self.app.run_kokoro = empty_kokoro
        resp = self.client.post(
            "/v1/audio/speech",
            json={"input": "....", "model": "kokoro", "voice": "af_heart"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.json())


class OpenAIDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.app = _load_with_key("secret-v1")
        self.client = TestClient(self.app.app)

    def tearDown(self):
        self.app.logger.disabled = False

    def test_models_and_voices_require_key(self):
        self.assertEqual(self.client.get("/v1/models").status_code, 401)
        self.assertEqual(self.client.get("/v1/audio/voices").status_code, 401)

    def test_models_lists_engines_only(self):
        resp = self.client.get("/v1/models", headers={"X-API-Key": "secret-v1"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        ids = {m["id"] for m in body["data"]}
        self.assertEqual(ids, {"kokoro", "edge"})
        for m in body["data"]:
            self.assertNotIn("gender", m)
            self.assertNotIn("locale", m)

    def test_voices_lists_kokoro_and_edge(self):
        calls = 0

        async def fake_list_voices():
            nonlocal calls
            calls += 1
            return [
                {
                    "ShortName": "en-US-AriaNeural",
                    "FriendlyName": "Aria",
                    "Gender": "Female",
                    "Locale": "en-US",
                }
            ]

        self.app.edge_tts.list_voices = fake_list_voices
        self.app._edge_voices_cache = None
        self.app._edge_voices_cache_expires_at = 0.0
        self.app._edge_voices_retry_after = 0.0

        resp = self.client.get("/v1/audio/voices", headers={"X-API-Key": "secret-v1"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        kokoro_ids = {v["id"] for v in data if v.get("engine") == "kokoro"}
        edge_ids = {v["id"] for v in data if v.get("engine") == "edge"}
        self.assertEqual(len(kokoro_ids), len(self.app.KOKORO_VOICES))
        self.assertIn("en-US-AriaNeural", edge_ids)
        sample = next(v for v in data if v["id"] == "en-US-AriaNeural")
        self.assertEqual(sample["engine"], "edge")
        self.assertIn("name", sample)
        self.assertIn("gender", sample)
        self.assertIn("locale", sample)
        self.assertEqual(calls, 1)

    def test_voices_edge_failure_returns_kokoro_only(self):
        async def failing():
            raise RuntimeError("edge down")

        self.app.edge_tts.list_voices = failing
        self.app._edge_voices_cache = None
        self.app._edge_voices_cache_expires_at = 0.0
        self.app._edge_voices_retry_after = 0.0

        resp = self.client.get("/v1/audio/voices", headers={"X-API-Key": "secret-v1"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertTrue(all(v["engine"] == "kokoro" for v in data))
        self.assertEqual(len(data), len(self.app.KOKORO_VOICES))


if __name__ == "__main__":
    unittest.main()
