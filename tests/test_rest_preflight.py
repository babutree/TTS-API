# -*- coding: utf-8 -*-
"""REST 预检合成 (_start_synthesis) 的资源生命周期与竞态测试。

聚焦点：HTTP 200 一旦提交即锁死状态码，故失败判定必须前置于流式之前。
本文件验证预检期取消/竞态下 feed_task 与 ffmpeg 子进程被正确回收，且
已 done 且带异常的 feed_task 异常被取出(避免 GC "never retrieved" 告警)。
"""
import asyncio
import os
import unittest
import warnings

from _support import FakeProc, disable_asyncio_debug, import_app_with_fakes

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

        with self.assertRaises(self.app.HTTPException) as ctx:
            await self.app._start_synthesis("hello", "kokoro", "af_heart", 1.0)

        self.assertEqual(ctx.exception.status_code, 504)
        self.assertTrue(feed_cancelled.is_set())
        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)


if __name__ == "__main__":
    unittest.main()
