# -*- coding: utf-8 -*-
"""REST 预检合成 (_start_synthesis) 的资源生命周期与竞态测试。

聚焦点：HTTP 200 一旦提交即锁死状态码，故失败判定必须前置于流式之前。
本文件验证预检期取消/竞态下 feed_task 与 ffmpeg 子进程被正确回收，且
已 done 且带异常的 feed_task 异常被取出(避免 GC "never retrieved" 告警)。
"""
import asyncio
import json
import os
import unittest
import warnings

from _support import FakeProc, disable_asyncio_debug, import_app_with_fakes
from uvicorn.protocols.http.h11_impl import RequestResponseCycle

warnings.filterwarnings(
    "ignore", message=".*on_event is deprecated.*", category=DeprecationWarning
)


class ConfigParsingTests(unittest.TestCase):
    def test_empty_max_text_length_env_uses_default(self):
        # 空字符串环境变量必须回落到默认值，而非抛 ValueError 或取 0。
        old_value = os.environ.get("MAX_TEXT_LENGTH")
        os.environ["MAX_TEXT_LENGTH"] = ""
        try:
            app = import_app_with_fakes()
        finally:
            if old_value is None:
                os.environ.pop("MAX_TEXT_LENGTH", None)
            else:
                os.environ["MAX_TEXT_LENGTH"] = old_value

        self.assertEqual(app.MAX_TEXT_LENGTH, app.DEFAULT_MAX_TEXT_LENGTH)
        self.assertEqual(app.MAX_TEXT_LENGTH, 100000)

    def test_explicit_max_text_length_env_is_honored(self):
        old_value = os.environ.get("MAX_TEXT_LENGTH")
        os.environ["MAX_TEXT_LENGTH"] = "42"
        try:
            app = import_app_with_fakes()
        finally:
            if old_value is None:
                os.environ.pop("MAX_TEXT_LENGTH", None)
            else:
                os.environ["MAX_TEXT_LENGTH"] = old_value

        self.assertEqual(app.MAX_TEXT_LENGTH, 42)

    def test_cors_allow_origins_defaults_to_wildcard(self):
        app = import_app_with_fakes()

        self.assertEqual(app.parse_cors_allow_origins(None), ["*"])
        self.assertEqual(app.parse_cors_allow_origins(""), ["*"])

    def test_cors_allow_origins_parses_comma_separated_values(self):
        app = import_app_with_fakes()

        self.assertEqual(
            app.parse_cors_allow_origins(" https://a.example , http://localhost:3000 "),
            ["https://a.example", "http://localhost:3000"],
        )

    def test_cors_allow_origins_rejects_empty_config(self):
        app = import_app_with_fakes()

        with self.assertRaises(ValueError):
            app.parse_cors_allow_origins(" , ")

    def test_empty_edge_voices_ttl_env_uses_default(self):
        old_value = os.environ.get("EDGE_VOICES_CACHE_TTL_SECONDS")
        os.environ["EDGE_VOICES_CACHE_TTL_SECONDS"] = ""
        try:
            app = import_app_with_fakes()
        finally:
            if old_value is None:
                os.environ.pop("EDGE_VOICES_CACHE_TTL_SECONDS", None)
            else:
                os.environ["EDGE_VOICES_CACHE_TTL_SECONDS"] = old_value

        self.assertEqual(app.EDGE_VOICES_CACHE_TTL_SECONDS, app.DEFAULT_EDGE_VOICES_CACHE_TTL_SECONDS)

    def test_invalid_edge_voices_ttl_env_raises(self):
        old_value = os.environ.get("EDGE_VOICES_CACHE_TTL_SECONDS")
        os.environ["EDGE_VOICES_CACHE_TTL_SECONDS"] = "-1"
        try:
            with self.assertRaises(ValueError):
                import_app_with_fakes()
        finally:
            if old_value is None:
                os.environ.pop("EDGE_VOICES_CACHE_TTL_SECONDS", None)
            else:
                os.environ["EDGE_VOICES_CACHE_TTL_SECONDS"] = old_value

    def test_non_finite_duration_values_are_rejected(self):
        app = import_app_with_fakes()

        for value in ("nan", "inf", "-inf"):
            with self.subTest(parser="edge voices ttl", value=value):
                with self.assertRaises(ValueError):
                    app.parse_edge_voices_cache_ttl(value)
            with self.subTest(parser="optional duration", value=value):
                with self.assertRaises(ValueError):
                    app.parse_optional_positive_float(value, "TEST_DURATION")

    def test_edge_retry_env_values_are_honored(self):
        values = {
            "EDGE_RETRY_MAX_ATTEMPTS": "3",
            "EDGE_RETRY_BASE_DELAY_SECONDS": "0.5",
            "EDGE_VOICES_FAILURE_COOLDOWN_SECONDS": "7.5",
        }
        old_values = {name: os.environ.get(name) for name in values}
        os.environ.update(values)
        try:
            app = import_app_with_fakes()
        finally:
            for name, old_value in old_values.items():
                if old_value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = old_value

        self.assertEqual(app.EDGE_RETRY_MAX_ATTEMPTS, 3)
        self.assertEqual(app.EDGE_RETRY_BASE_DELAY_SECONDS, 0.5)
        self.assertEqual(app.EDGE_VOICES_FAILURE_COOLDOWN_SECONDS, 7.5)

    def test_edge_voices_request_timeout_defaults_to_five_seconds(self):
        name = "EDGE_VOICES_REQUEST_TIMEOUT_SECONDS"
        old_value = os.environ.pop(name, None)
        try:
            app = import_app_with_fakes()
        finally:
            if old_value is not None:
                os.environ[name] = old_value

        self.assertEqual(app.DEFAULT_EDGE_VOICES_REQUEST_TIMEOUT_SECONDS, 5.0)
        self.assertEqual(app.EDGE_VOICES_REQUEST_TIMEOUT_SECONDS, 5.0)

    def test_edge_voices_request_timeout_env_is_validated(self):
        name = "EDGE_VOICES_REQUEST_TIMEOUT_SECONDS"
        old_value = os.environ.get(name)
        try:
            os.environ[name] = "1.5"
            app = import_app_with_fakes()
            self.assertEqual(app.EDGE_VOICES_REQUEST_TIMEOUT_SECONDS, 1.5)

            os.environ[name] = "-1"
            with self.assertRaises(ValueError):
                import_app_with_fakes()
        finally:
            if old_value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old_value

    def test_synthesis_timeout_env_defaults_to_disabled(self):
        app = import_app_with_fakes()

        self.assertEqual(app.parse_optional_positive_float(None, "TTS_SYNTHESIS_TIMEOUT_SECONDS"), 0.0)
        self.assertEqual(app.parse_optional_positive_float("", "TTS_SYNTHESIS_TIMEOUT_SECONDS"), 0.0)

    def test_invalid_synthesis_timeout_env_raises(self):
        app = import_app_with_fakes()

        with self.assertRaises(ValueError):
            app.parse_optional_positive_float("-1", "TTS_SYNTHESIS_TIMEOUT_SECONDS")

    def test_ffmpeg_max_processes_env_defaults_to_positive_limit(self):
        app = import_app_with_fakes()

        self.assertEqual(app.parse_positive_int(None, "TTS_MAX_FFMPEG_PROCESSES", 2), 2)
        self.assertEqual(app.parse_positive_int("", "TTS_MAX_FFMPEG_PROCESSES", 2), 2)

    def test_invalid_ffmpeg_max_processes_env_raises(self):
        app = import_app_with_fakes()

        with self.assertRaises(ValueError):
            app.parse_positive_int("0", "TTS_MAX_FFMPEG_PROCESSES", 2)


class AwaitCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        disable_asyncio_debug()
        self.app = import_app_with_fakes()

    async def test_cleanup_failure_does_not_override_external_cancellation(self):
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()

        async def failing_cleanup():
            cleanup_started.set()
            await release_cleanup.wait()
            raise RuntimeError("cleanup failed")

        owner = asyncio.create_task(self.app._await_cleanup(failing_cleanup()))
        try:
            await asyncio.wait_for(cleanup_started.wait(), 0.2)
            owner.cancel()
            await asyncio.sleep(0)
            self.assertFalse(owner.done(), "外部取消后仍须等待 cleanup 收尾")

            release_cleanup.set()
            with self.assertRaises(BaseException) as caught:
                await asyncio.wait_for(owner, 0.2)
            self.assertIsInstance(caught.exception, asyncio.CancelledError)
            self.assertIsInstance(caught.exception.__cause__, RuntimeError)
            self.assertEqual(str(caught.exception.__cause__), "cleanup failed")
        finally:
            release_cleanup.set()
            if not owner.done():
                owner.cancel()
                try:
                    await owner
                except BaseException:
                    pass

    async def test_cleanup_failure_does_not_override_pending_cancellation(self):
        owner_started = asyncio.Event()
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()

        async def failing_cleanup():
            cleanup_started.set()
            await release_cleanup.wait()
            raise RuntimeError("cleanup failed in finally")

        async def owner_body():
            try:
                owner_started.set()
                await asyncio.Event().wait()
            finally:
                await self.app._await_cleanup(failing_cleanup())

        owner = asyncio.create_task(owner_body())
        try:
            await asyncio.wait_for(owner_started.wait(), 0.2)
            owner.cancel()
            await asyncio.wait_for(cleanup_started.wait(), 0.2)
            release_cleanup.set()

            with self.assertRaises(BaseException) as caught:
                await asyncio.wait_for(owner, 0.2)
            self.assertIsInstance(caught.exception, asyncio.CancelledError)
            self.assertIsInstance(caught.exception.__cause__, RuntimeError)
            self.assertEqual(
                str(caught.exception.__cause__), "cleanup failed in finally"
            )
        finally:
            release_cleanup.set()
            if not owner.done():
                owner.cancel()
                try:
                    await owner
                except BaseException:
                    pass


class StartSynthesisLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        disable_asyncio_debug()
        self.app = import_app_with_fakes()
        self.app.logger.disabled = True

    async def asyncTearDown(self):
        self.app.logger.disabled = False

    async def test_no_audio_no_exception_raises_400(self):
        # feed 正常结束但零音频(纯标点/跨语言过滤后为空)：诚实回 400，不伪装成功。
        proc = FakeProc()

        async def fake_encoder(engine):
            return proc

        async def fake_feed(proc_arg, text, engine, voice, speed, first_audio):
            return  # 不置位 first_audio，正常结束

        self.app._create_mp3_encoder = fake_encoder
        self.app._feed_mp3 = fake_feed

        with self.assertRaises(self.app.HTTPException) as ctx:
            await self.app._start_synthesis("....", "kokoro", "af_heart", 1.0)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)

    async def test_kokoro_feed_exception_before_audio_raises_500(self):
        # kokoro(本机引擎)预检期失败归 500；子进程被回收。
        proc = FakeProc()

        async def fake_encoder(engine):
            return proc

        async def fake_feed(proc_arg, text, engine, voice, speed, first_audio):
            raise RuntimeError("kokoro boom")

        self.app._create_mp3_encoder = fake_encoder
        self.app._feed_mp3 = fake_feed

        with self.assertRaises(self.app.HTTPException) as ctx:
            await self.app._start_synthesis("hello", "kokoro", "af_heart", 1.0)

        self.assertEqual(ctx.exception.status_code, 500)
        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)

    async def test_edge_feed_exception_before_audio_raises_502(self):
        # edge(上游微软)预检期失败归 502，与本机故障区分。
        proc = FakeProc()

        async def fake_encoder(engine):
            return proc

        async def fake_feed(proc_arg, text, engine, voice, speed, first_audio):
            raise RuntimeError("edge boom")

        self.app._create_mp3_encoder = fake_encoder
        self.app._feed_mp3 = fake_feed

        with self.assertRaises(self.app.HTTPException) as ctx:
            await self.app._start_synthesis("hello", "edge", "en-US-AriaNeural", 1.0)

        self.assertEqual(ctx.exception.status_code, 502)
        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)

    async def test_edge_first_audio_signal_and_feed_failure_same_tick_raises_502(self):
        proc = FakeProc()

        async def fake_encoder(engine):
            return proc

        async def fake_feed(proc_arg, text, engine, voice, speed, first_audio):
            first_audio.set()
            raise RuntimeError("edge failed with first audio signal")

        self.app._create_mp3_encoder = fake_encoder
        self.app._feed_mp3 = fake_feed

        with self.assertRaises(self.app.HTTPException) as ctx:
            await self.app._start_synthesis(
                "hello", "edge", "en-US-AriaNeural", 1.0
            )

        self.assertEqual(ctx.exception.status_code, 502)
        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)

    async def test_audio_produced_returns_proc_and_live_feed_task(self):
        # 首音频事件置位后：放行流式，返回 (proc, feed_task)，proc 不被回收。
        proc = FakeProc()

        async def fake_encoder(engine):
            return proc

        async def fake_feed(proc_arg, text, engine, voice, speed, first_audio):
            first_audio.set()
            # 继续存活，模拟后续内容仍在喂入
            await asyncio.Event().wait()

        self.app._create_mp3_encoder = fake_encoder
        self.app._feed_mp3 = fake_feed

        out_proc, feed_task = await self.app._start_synthesis(
            "hello", "kokoro", "af_heart", 1.0
        )
        try:
            self.assertIs(out_proc, proc)
            self.assertFalse(feed_task.done())
            self.assertFalse(proc.killed)  # 有音频：不回收，交由流式接管
        finally:
            feed_task.cancel()
            try:
                await feed_task
            except asyncio.CancelledError:
                pass

    async def test_cancellation_consumes_already_done_feed_exception(self):
        # 竞态：取消与 feed 结束同刻发生，feed_task 已 done 且带异常。
        # 断言异常被取出(CPython _log_traceback 调 .exception() 后转 False)。
        proc = FakeProc()

        async def fake_encoder(engine):
            return proc

        async def fake_feed(proc_arg, text, engine, voice, speed, first_audio):
            self.app._captured_feed_task = asyncio.current_task()
            raise RuntimeError("boom")

        self.app._create_mp3_encoder = fake_encoder
        self.app._feed_mp3 = fake_feed

        real_wait = asyncio.wait

        async def fake_wait(aws, return_when=None, timeout=None):
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            raise asyncio.CancelledError()

        asyncio.wait = fake_wait
        try:
            with self.assertRaises(asyncio.CancelledError):
                await self.app._start_synthesis("hi", "edge", "bad", 1.0)
        finally:
            asyncio.wait = real_wait

        feed_task = self.app._captured_feed_task
        self.assertTrue(feed_task.done())
        self.assertFalse(feed_task.cancelled())
        self.assertFalse(feed_task._log_traceback)
        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)

    async def test_cancellation_reaps_proc_and_pending_feed_task(self):
        # 预检期外部取消(客户端断开)：pending 的 feed_task 被 cancel 并 await，proc 回收。
        proc = FakeProc()
        feed_cancelled = asyncio.Event()

        async def fake_encoder(engine):
            return proc

        async def fake_feed(proc_arg, text, engine, voice, speed, first_audio):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                feed_cancelled.set()
                raise

        self.app._create_mp3_encoder = fake_encoder
        self.app._feed_mp3 = fake_feed

        task = asyncio.create_task(
            self.app._start_synthesis("hello", "edge", "bad", 1.0)
        )
        await asyncio.sleep(0)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertTrue(feed_cancelled.is_set())
        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)

    async def test_preflight_timeout_raises_504_and_reaps_resources(self):
        proc = FakeProc()
        self.app.TTS_SYNTHESIS_TIMEOUT_SECONDS = 0.01
        feed_cancelled = asyncio.Event()
        reap_calls = 0

        async def fake_encoder(engine):
            return proc

        async def fake_feed(proc_arg, text, engine, voice, speed, first_audio):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                feed_cancelled.set()
                raise

        real_reap = self.app._reap_proc

        async def counting_reap(proc_arg):
            nonlocal reap_calls
            reap_calls += 1
            await real_reap(proc_arg)

        self.app._create_mp3_encoder = fake_encoder
        self.app._feed_mp3 = fake_feed
        self.app._reap_proc = counting_reap

        with self.assertRaises(self.app.HTTPException) as ctx:
            await self.app._start_synthesis("hello", "kokoro", "af_heart", 1.0)

        self.assertEqual(ctx.exception.status_code, 504)
        self.assertTrue(feed_cancelled.is_set())
        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)
        self.assertEqual(reap_calls, 1)

    async def test_preflight_error_cleanup_preserves_external_cancellation(self):
        proc = FakeProc()
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()
        self.app.TTS_SYNTHESIS_TIMEOUT_SECONDS = 0.01

        async def fake_encoder(engine):
            return proc

        async def blocking_feed(
            proc_arg, text, engine, voice, speed, first_audio
        ):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_started.set()
                while not release_cleanup.is_set():
                    try:
                        await release_cleanup.wait()
                    except asyncio.CancelledError:
                        continue
                raise

        self.app._create_mp3_encoder = fake_encoder
        self.app._feed_mp3 = blocking_feed
        owner = asyncio.create_task(
            self.app._start_synthesis("hello", "edge", "voice", 1.0)
        )
        try:
            await asyncio.wait_for(cleanup_started.wait(), 0.2)
            owner.cancel()
            await asyncio.sleep(0)
            self.assertFalse(owner.done(), "外部取消仍须等待资源清理完成")

            release_cleanup.set()
            done, _ = await asyncio.wait({owner}, timeout=0.2)
            self.assertIn(owner, done)
            with self.assertRaises(asyncio.CancelledError):
                await owner
            self.assertTrue(proc.killed)
            self.assertTrue(proc.waited)
        finally:
            release_cleanup.set()
            if not owner.done():
                owner.cancel()
                try:
                    await owner
                except BaseException:
                    pass


class RequestDisconnectLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        disable_asyncio_debug()
        self.app = import_app_with_fakes()
        self.app.pipeline_zh = object()
        self.app.pipeline_en = object()
        self.app.logger.disabled = True

    async def asyncTearDown(self):
        self.app.logger.disabled = False

    def _scope(self, method: str, path: str, query_string: bytes = b""):
        return {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query_string,
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "root_path": "",
            "app": self.app.app,
        }

    def _request(self, method: str, path: str):
        messages = asyncio.Queue()
        messages.put_nowait(
            {"type": "http.request", "body": b"", "more_body": False}
        )
        scope = self._scope(method, path)
        return self.app.Request(scope, messages.get), messages

    async def _assert_preflight_disconnect_cancels(self, invoke, messages):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def hanging_start(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        self.app._start_synthesis = hanging_start
        task = asyncio.create_task(invoke())
        try:
            await asyncio.wait_for(started.wait(), 0.2)
            await messages.put({"type": "http.disconnect"})
            done, _ = await asyncio.wait({task}, timeout=0.2)
            self.assertIn(task, done, "真实 http.disconnect 必须结束 REST 预检")
            with self.assertRaises(BaseException) as caught:
                await task
            disconnect_error = getattr(
                self.app, "_HttpRequestDisconnected", None
            )
            self.assertIsNotNone(disconnect_error)
            self.assertIsInstance(caught.exception, disconnect_error)
            self.assertTrue(cancelled.is_set())
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def test_api_tts_http_disconnect_cancels_preflight(self):
        request, messages = self._request("POST", "/api/tts")
        req = self.app.TTSRequest(
            text="hello", engine="edge", voice="en-US-AvaNeural", speed=1.0
        )

        async def invoke():
            return await self.app.api_tts(req, request)

        await self._assert_preflight_disconnect_cancels(invoke, messages)

    async def test_voice_preview_http_disconnect_cancels_preflight(self):
        request, messages = self._request("GET", "/api/voices/preview")

        async def invoke():
            return await self.app.api_voice_preview(
                request, engine="edge", voice="en-US-AvaNeural", speed=1.0
            )

        await self._assert_preflight_disconnect_cancels(invoke, messages)

    async def _assert_full_asgi_disconnect_has_no_response(
        self, method, path, body=b"", query_string=b""
    ):
        messages = asyncio.Queue()
        messages.put_nowait(
            {"type": "http.request", "body": body, "more_body": False}
        )
        scope = self._scope(method, path, query_string)
        if body:
            scope["headers"] = [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ]
        sent = []
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def hanging_start(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        async def send(message):
            sent.append(dict(message))

        self.app._start_synthesis = hanging_start
        task = asyncio.create_task(self.app.app(scope, messages.get, send))
        try:
            await asyncio.wait_for(started.wait(), 0.2)
            messages.put_nowait({"type": "http.disconnect"})
            done, _ = await asyncio.wait({task}, timeout=0.2)
            self.assertIn(task, done, "完整 ASGI 链路必须在断连后结束")
            try:
                await task
            except asyncio.CancelledError:
                self.fail("已确认断连不得把自取消传播到 ASGI 服务器")
            self.assertTrue(cancelled.is_set())
            self.assertFalse(
                any(message["type"] == "http.response.start" for message in sent),
                f"已断连请求不得伪造 HTTP 响应: {sent!r}",
            )
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass

    async def test_full_asgi_tts_disconnect_returns_without_response(self):
        body = json.dumps(
            {
                "text": "hello",
                "engine": "edge",
                "voice": "en-US-AvaNeural",
                "speed": 1.0,
            }
        ).encode("utf-8")
        await self._assert_full_asgi_disconnect_has_no_response(
            "POST", "/api/tts", body=body
        )

    async def test_full_asgi_preview_disconnect_returns_without_response(self):
        await self._assert_full_asgi_disconnect_has_no_response(
            "GET",
            "/api/voices/preview",
            query_string=b"engine=edge&voice=en-US-AvaNeural&speed=1.0",
        )

    async def _assert_uvicorn_disconnect_is_quiet(
        self, method, path, body=b"", query_string=b""
    ):
        messages = asyncio.Queue()
        messages.put_nowait(
            {"type": "http.request", "body": body, "more_body": False}
        )
        scope = self._scope(method, path, query_string)
        if body:
            scope["headers"] = [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ]
        synthesis_started = asyncio.Event()
        synthesis_cancelled = asyncio.Event()

        async def hanging_start(*args, **kwargs):
            synthesis_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                synthesis_cancelled.set()
                raise

        class RecordingLogger:
            def __init__(self):
                self.errors = []

            def error(self, *args, **kwargs):
                self.errors.append((args, kwargs))

        class RecordingTransport:
            def __init__(self):
                self.close_calls = 0

            def close(self):
                self.close_calls += 1

        class RecordingCycle:
            def __init__(self):
                self.scope = scope
                self.response_started = False
                self.response_complete = False
                self.disconnected = False
                self.logger = RecordingLogger()
                self.transport = RecordingTransport()
                self.send_500_calls = 0
                self.sent = []
                self.on_response = lambda: None

            async def receive(self):
                message = await messages.get()
                if message.get("type") == "http.disconnect":
                    self.disconnected = True
                return message

            async def send(self, message):
                self.sent.append(dict(message))
                if message["type"] == "http.response.start":
                    self.response_started = True

            async def send_500_response(self):
                self.send_500_calls += 1

        self.app._start_synthesis = hanging_start
        cycle = RecordingCycle()
        task = asyncio.create_task(
            RequestResponseCycle.run_asgi(cycle, self.app.app)
        )
        try:
            await asyncio.wait_for(synthesis_started.wait(), 0.2)
            messages.put_nowait({"type": "http.disconnect"})
            await asyncio.wait_for(task, 0.2)

            self.assertTrue(synthesis_cancelled.is_set())
            self.assertEqual(cycle.logger.errors, [])
            self.assertEqual(cycle.send_500_calls, 0)
            self.assertFalse(cycle.response_started)
            self.assertEqual(cycle.sent, [])
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass

    async def test_uvicorn_tts_disconnect_is_quiet(self):
        body = json.dumps(
            {
                "text": "hello",
                "engine": "edge",
                "voice": "en-US-AvaNeural",
                "speed": 1.0,
            }
        ).encode("utf-8")
        await self._assert_uvicorn_disconnect_is_quiet(
            "POST", "/api/tts", body=body
        )

    async def test_uvicorn_preview_disconnect_is_quiet(self):
        await self._assert_uvicorn_disconnect_is_quiet(
            "GET",
            "/api/voices/preview",
            query_string=b"engine=edge&voice=en-US-AvaNeural&speed=1.0",
        )

    async def test_full_asgi_external_cancellation_still_propagates(self):
        messages = asyncio.Queue()
        body = json.dumps(
            {
                "text": "hello",
                "engine": "edge",
                "voice": "en-US-AvaNeural",
                "speed": 1.0,
            }
        ).encode("utf-8")
        messages.put_nowait(
            {"type": "http.request", "body": body, "more_body": False}
        )
        scope = self._scope("POST", "/api/tts")
        scope["headers"] = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        synthesis_started = asyncio.Event()
        synthesis_cancelled = asyncio.Event()
        sent = []

        async def hanging_start(*args, **kwargs):
            synthesis_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                synthesis_cancelled.set()
                raise

        async def send(message):
            sent.append(dict(message))

        self.app._start_synthesis = hanging_start
        task = asyncio.create_task(self.app.app(scope, messages.get, send))
        try:
            await asyncio.wait_for(synthesis_started.wait(), 0.2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 0.2)
            self.assertTrue(synthesis_cancelled.is_set())
            self.assertFalse(
                any(message["type"] == "http.response.start" for message in sent)
            )
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass

    async def test_disconnect_cleanup_preserves_external_cancellation(self):
        messages = asyncio.Queue()
        messages.put_nowait(
            {"type": "http.request", "body": b"", "more_body": False}
        )
        request = self.app.Request(
            self._scope("POST", "/api/tts"), messages.get
        )
        synthesis_started = asyncio.Event()
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()

        async def hanging_start(*args, **kwargs):
            synthesis_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_started.set()
                while not release_cleanup.is_set():
                    try:
                        await release_cleanup.wait()
                    except asyncio.CancelledError:
                        continue
                raise

        self.app._start_synthesis = hanging_start
        owner = asyncio.create_task(
            self.app._start_synthesis_for_request(
                request, "hello", "edge", "en-US-AvaNeural", 1.0
            )
        )
        try:
            await asyncio.wait_for(synthesis_started.wait(), 0.2)
            messages.put_nowait({"type": "http.disconnect"})
            await asyncio.wait_for(cleanup_started.wait(), 0.2)
            owner.cancel()
            await asyncio.sleep(0)
            self.assertFalse(owner.done(), "外部取消不得中断预检资源清理")

            release_cleanup.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(owner, 0.2)
        finally:
            release_cleanup.set()
            if not owner.done():
                owner.cancel()
                try:
                    await owner
                except BaseException:
                    pass

    async def test_receive_error_cancels_preflight_and_propagates(self):
        synthesis_started = asyncio.Event()
        synthesis_cancelled = asyncio.Event()
        receive_failed = asyncio.Event()

        async def receive():
            await synthesis_started.wait()
            receive_failed.set()
            raise RuntimeError("receive failed")

        async def hanging_start(*args, **kwargs):
            synthesis_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                synthesis_cancelled.set()
                raise

        request = self.app.Request(self._scope("POST", "/api/tts"), receive)
        self.app._start_synthesis = hanging_start
        owner = asyncio.create_task(
            self.app._start_synthesis_for_request(
                request, "hello", "edge", "en-US-AvaNeural", 1.0
            )
        )
        try:
            await asyncio.wait_for(receive_failed.wait(), 0.2)
            done, _ = await asyncio.wait({owner}, timeout=0.2)
            self.assertIn(
                owner,
                done,
                "receive 异常必须主动终止预检，不能等默认关闭的合成超时",
            )
            with self.assertRaisesRegex(RuntimeError, "receive failed"):
                await owner
            self.assertTrue(synthesis_cancelled.is_set())
        finally:
            if not owner.done():
                owner.cancel()
            try:
                await owner
            except BaseException:
                pass

    async def test_response_cleanup_preserves_external_cancellation(self):
        request, _ = self._request("POST", "/api/tts")
        req = self.app.TTSRequest(
            text="hello", engine="edge", voice="en-US-AvaNeural", speed=1.0
        )
        proc = FakeProc()
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()

        async def feed():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_started.set()
                while not release_cleanup.is_set():
                    try:
                        await release_cleanup.wait()
                    except asyncio.CancelledError:
                        continue
                raise

        feed_task = asyncio.create_task(feed())

        async def successful_start(*args, **kwargs):
            return proc, feed_task

        def failing_response(*args, **kwargs):
            raise ValueError("response construction failed")

        self.app._start_synthesis_for_request = successful_start
        self.app._Mp3StreamingResponse = failing_response
        owner = asyncio.create_task(self.app.api_tts(req, request))
        try:
            await asyncio.wait_for(cleanup_started.wait(), 0.2)
            owner.cancel()
            await asyncio.sleep(0)
            self.assertFalse(owner.done(), "外部取消仍须等待 session 清理完成")

            release_cleanup.set()
            done, _ = await asyncio.wait({owner}, timeout=0.2)
            self.assertIn(owner, done)
            with self.assertRaises(asyncio.CancelledError):
                await owner
            self.assertTrue(proc.killed)
            self.assertTrue(proc.waited)
        finally:
            release_cleanup.set()
            if not owner.done():
                owner.cancel()
            try:
                await owner
            except BaseException:
                pass
            if not feed_task.done():
                feed_task.cancel()
            try:
                await feed_task
            except asyncio.CancelledError:
                pass

    async def test_cancel_before_response_start_reaps_session_once(self):
        messages = asyncio.Queue()
        body = json.dumps(
            {
                "text": "hello",
                "engine": "edge",
                "voice": "en-US-AvaNeural",
                "speed": 1.0,
            }
        ).encode("utf-8")
        messages.put_nowait(
            {"type": "http.request", "body": body, "more_body": False}
        )
        scope = self._scope("POST", "/api/tts")
        scope["headers"] = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        proc = FakeProc()
        feed_started = asyncio.Event()
        feed_cancel_calls = 0
        limiter_release_calls = 0
        response_start_entered = asyncio.Event()

        async def feed():
            nonlocal feed_cancel_calls
            feed_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                feed_cancel_calls += 1
                raise

        feed_task = asyncio.create_task(feed())
        await feed_started.wait()

        async def successful_start(*args, **kwargs):
            return proc, feed_task

        def counting_release(*args, **kwargs):
            nonlocal limiter_release_calls
            limiter_release_calls += 1

        async def send(message):
            if message["type"] == "http.response.start":
                response_start_entered.set()
                await asyncio.Event().wait()

        self.app._start_synthesis_for_request = successful_start
        self.app._ffmpeg_limiter.release = counting_release
        owner = asyncio.create_task(self.app.app(scope, messages.get, send))
        try:
            await asyncio.wait_for(response_start_entered.wait(), 0.2)
            owner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(owner, 0.2)

            self.assertEqual(feed_cancel_calls, 1)
            self.assertTrue(proc.killed)
            self.assertTrue(proc.waited)
            self.assertEqual(limiter_release_calls, 1)
        finally:
            if not owner.done():
                owner.cancel()
                try:
                    await owner
                except BaseException:
                    pass
            if not feed_task.done():
                feed_task.cancel()
            try:
                await feed_task
            except asyncio.CancelledError:
                pass

    async def test_response_start_cancel_keeps_cancellation_when_reap_fails(self):
        class FailingWaitProc(FakeProc):
            async def wait(self):
                self.waited = True
                raise RuntimeError("proc.wait failed")

        proc = FailingWaitProc()
        feed_cancel_calls = 0
        limiter_release_calls = 0
        response_start_entered = asyncio.Event()

        async def feed():
            nonlocal feed_cancel_calls
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                feed_cancel_calls += 1
                raise

        feed_task = asyncio.create_task(feed())
        await asyncio.sleep(0)

        def counting_release(*args, **kwargs):
            nonlocal limiter_release_calls
            limiter_release_calls += 1

        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            if message["type"] == "http.response.start":
                response_start_entered.set()
                await asyncio.Event().wait()

        self.app._ffmpeg_limiter.release = counting_release
        response = self.app._Mp3StreamingResponse(
            proc,
            feed_task,
            "edge",
            "en-US-AvaNeural",
            media_type="audio/mpeg",
        )
        scope = self._scope("POST", "/api/tts")
        owner = asyncio.create_task(response(scope, receive, send))
        try:
            await asyncio.wait_for(response_start_entered.wait(), 0.2)
            owner.cancel()
            with self.assertRaises(BaseException) as caught:
                await asyncio.wait_for(owner, 0.2)

            self.assertIsInstance(caught.exception, asyncio.CancelledError)
            self.assertIsInstance(caught.exception.__cause__, RuntimeError)
            self.assertEqual(str(caught.exception.__cause__), "proc.wait failed")
            self.assertEqual(feed_cancel_calls, 1)
            self.assertTrue(proc.killed)
            self.assertTrue(proc.waited)
            self.assertEqual(limiter_release_calls, 1)
        finally:
            if not owner.done():
                owner.cancel()
                try:
                    await owner
                except BaseException:
                    pass
            if not feed_task.done():
                feed_task.cancel()
            try:
                await feed_task
            except asyncio.CancelledError:
                pass

    async def test_preflight_success_stops_watcher_before_stream_handoff(self):
        receive_entered = asyncio.Event()
        watcher_cancelled = asyncio.Event()
        allow_watcher_exit = asyncio.Event()
        messages = asyncio.Queue()
        active_receives = 0
        max_active_receives = 0

        async def receive():
            nonlocal active_receives, max_active_receives
            active_receives += 1
            max_active_receives = max(max_active_receives, active_receives)
            receive_entered.set()
            try:
                return await messages.get()
            except asyncio.CancelledError:
                watcher_cancelled.set()
                await allow_watcher_exit.wait()
                raise
            finally:
                active_receives -= 1

        request = self.app.Request(
            {"type": "http", "method": "POST", "path": "/api/tts", "headers": []},
            receive,
        )
        proc = FakeProc()
        feed_task = asyncio.create_task(asyncio.Event().wait())

        async def successful_start(*args, **kwargs):
            await receive_entered.wait()
            return proc, feed_task

        self.app._start_synthesis = successful_start
        owner = asyncio.create_task(
            self.app._start_synthesis_for_request(
                request, "hello", "edge", "en-US-AvaNeural", 1.0
            )
        )
        try:
            await asyncio.wait_for(watcher_cancelled.wait(), 0.2)
            self.assertFalse(
                owner.done(),
                "watcher 的 receive 清理完成前不得交接 StreamingResponse",
            )
            self.assertEqual(active_receives, 1)

            allow_watcher_exit.set()
            out_proc, out_feed = await asyncio.wait_for(owner, 0.2)
            self.assertIs(out_proc, proc)
            self.assertIs(out_feed, feed_task)
            self.assertEqual(active_receives, 0)

            messages.put_nowait({"type": "http.disconnect"})
            message = await asyncio.wait_for(request.receive(), 0.2)
            self.assertEqual(message["type"], "http.disconnect")
            self.assertEqual(max_active_receives, 1)
        finally:
            allow_watcher_exit.set()
            if not owner.done():
                owner.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await owner
            if not feed_task.done():
                feed_task.cancel()
            try:
                await feed_task
            except asyncio.CancelledError:
                pass

    async def test_preflight_success_stops_watcher_exactly_once(self):
        receive_entered = asyncio.Event()
        release_synthesis = asyncio.Event()
        second_stop_started = asyncio.Event()
        release_second_stop = asyncio.Event()

        async def receive():
            receive_entered.set()
            await asyncio.Event().wait()

        request = self.app.Request(
            {"type": "http", "method": "POST", "path": "/api/tts", "headers": []},
            receive,
        )
        proc = FakeProc()
        feed_task = asyncio.create_task(asyncio.Event().wait())

        async def successful_start(*args, **kwargs):
            await receive_entered.wait()
            await release_synthesis.wait()
            return proc, feed_task

        real_stop = self.app._stop_disconnect_watcher
        stop_calls = 0

        async def controlled_stop(watcher):
            nonlocal stop_calls
            stop_calls += 1
            if stop_calls == 1:
                return await real_stop(watcher)
            second_stop_started.set()
            await release_second_stop.wait()

        self.app._start_synthesis = successful_start
        self.app._stop_disconnect_watcher = controlled_stop
        owner = asyncio.create_task(
            self.app._start_synthesis_for_request(
                request, "hello", "edge", "en-US-AvaNeural", 1.0
            )
        )
        try:
            await asyncio.wait_for(receive_entered.wait(), 0.2)
            release_synthesis.set()
            done, _ = await asyncio.wait({owner}, timeout=0.2)
            self.assertIn(
                owner,
                done,
                "成功交接前已停止 watcher，finally 不得再次 await 并暴露资源丢失窗口",
            )
            out_proc, out_feed = await owner
            self.assertIs(out_proc, proc)
            self.assertIs(out_feed, feed_task)
            self.assertEqual(stop_calls, 1)
            self.assertFalse(second_stop_started.is_set())
        finally:
            release_synthesis.set()
            release_second_stop.set()
            if not owner.done():
                try:
                    await owner
                except BaseException:
                    pass
            if not feed_task.done():
                feed_task.cancel()
            try:
                await feed_task
            except asyncio.CancelledError:
                pass

    async def test_disconnect_during_watcher_teardown_disposes_session_once(self):
        class CountingProc:
            def __init__(self):
                self.returncode = None
                self.kill_calls = 0
                self.wait_calls = 0

            def kill(self):
                self.kill_calls += 1
                self.returncode = -9

            async def wait(self):
                self.wait_calls += 1

        receive_entered = asyncio.Event()
        watcher_cancelled = asyncio.Event()
        release_disconnect = asyncio.Event()
        feed_started = asyncio.Event()
        feed_cancel_calls = 0
        limiter_release_calls = 0

        async def receive():
            receive_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # 模拟取消 receive 的同时，已到达的 disconnect 被 ASGI 层交付。
                watcher_cancelled.set()
                await release_disconnect.wait()
                return {"type": "http.disconnect"}

        request = self.app.Request(
            {"type": "http", "method": "POST", "path": "/api/tts", "headers": []},
            receive,
        )
        proc = CountingProc()

        async def feed():
            nonlocal feed_cancel_calls
            feed_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                feed_cancel_calls += 1
                raise

        feed_task = asyncio.create_task(feed())
        await feed_started.wait()

        async def successful_start(*args, **kwargs):
            await receive_entered.wait()
            return proc, feed_task

        def counting_release(*args, **kwargs):
            nonlocal limiter_release_calls
            limiter_release_calls += 1

        self.app._start_synthesis = successful_start
        self.app._ffmpeg_limiter.release = counting_release
        owner = asyncio.create_task(
            self.app._start_synthesis_for_request(
                request, "hello", "edge", "en-US-AvaNeural", 1.0
            )
        )
        try:
            await asyncio.wait_for(watcher_cancelled.wait(), 0.2)
            release_disconnect.set()
            with self.assertRaises(BaseException) as caught:
                await asyncio.wait_for(owner, 0.2)
            disconnect_error = getattr(
                self.app, "_HttpRequestDisconnected", None
            )
            self.assertIsNotNone(disconnect_error)
            self.assertIsInstance(caught.exception, disconnect_error)

            self.assertEqual(feed_cancel_calls, 1)
            self.assertEqual(proc.kill_calls, 1)
            self.assertEqual(proc.wait_calls, 1)
            self.assertEqual(limiter_release_calls, 1)
        finally:
            release_disconnect.set()
            if not owner.done():
                owner.cancel()
                try:
                    await owner
                except asyncio.CancelledError:
                    pass
            if not feed_task.done():
                feed_task.cancel()
            try:
                await feed_task
            except asyncio.CancelledError:
                pass

    async def test_streaming_http_disconnect_cancels_feed_and_reaps_proc(self):
        class FirstChunkThenHang:
            def __init__(self):
                self.sent_first = False

            async def read(self, size):
                if not self.sent_first:
                    self.sent_first = True
                    return b"MP3"
                await asyncio.Event().wait()

        proc = FakeProc(stdout=FirstChunkThenHang())
        feed_task = asyncio.create_task(asyncio.Event().wait())
        messages = asyncio.Queue()
        messages.put_nowait(
            {"type": "http.request", "body": b"", "more_body": False}
        )

        async def receive():
            return await messages.get()

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body") == b"MP3":
                await messages.put({"type": "http.disconnect"})

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "path": "/api/tts",
            "headers": [],
        }
        response = self.app.StreamingResponse(
            self.app._stream_mp3(proc, feed_task, "edge", "en-US-AvaNeural"),
            media_type="audio/mpeg",
        )

        await asyncio.wait_for(response(scope, receive, send), 0.2)

        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)
        self.assertTrue(feed_task.cancelled())


if __name__ == "__main__":
    unittest.main()
