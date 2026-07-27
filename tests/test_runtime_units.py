# -*- coding: utf-8 -*-
"""运行时内部单元测试：startup、run_kokoro、Edge 音色缓存、ffmpeg 命令、进程回收。"""
import asyncio
import time
import unittest
from unittest import mock

import numpy as np

from _support import FakeProc, disable_asyncio_debug, import_app_with_fakes


REAL_MONOTONIC = time.monotonic


class _FakeAudio:
    def __init__(self, values):
        self._array = np.array(values, dtype=np.float32)

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self._array


class _FakeResult:
    def __init__(self, values):
        self.output = type("Output", (), {"audio": _FakeAudio(values)})()


class _RecordingPipeline:
    def __init__(self, chunks):
        self.chunks = chunks
        self.calls = []

    def __call__(self, text, voice, speed):
        self.calls.append({"text": text, "voice": voice, "speed": speed})
        for chunk in self.chunks:
            yield _FakeResult(chunk)


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        disable_asyncio_debug()

    def _install_warmup_pipeline(self, app):
        instances = []

        class WarmupPipeline:
            def __init__(self, lang_code):
                self.lang_code = lang_code
                self.calls = []
                instances.append(self)

            def __call__(self, text, voice, speed):
                self.calls.append({"text": text, "voice": voice, "speed": speed})
                yield object()

        app.KPipeline = WarmupPipeline
        return instances

    async def test_startup_initializes_both_pipelines_and_warms_them(self):
        app = import_app_with_fakes()
        instances = self._install_warmup_pipeline(app)
        await app.startup()

        self.assertEqual([item.lang_code for item in instances], ["z", "a"])
        self.assertIs(app.pipeline_zh, instances[0])
        self.assertIs(app.pipeline_en, instances[1])
        self.assertEqual(instances[0].calls, [{"text": "预热", "voice": "zf_xiaoxiao", "speed": 1.0}])
        self.assertEqual(instances[1].calls, [{"text": "warm up", "voice": "af_heart", "speed": 1.0}])

    async def test_lifespan_uses_startup_initialization_path(self):
        app = import_app_with_fakes()
        instances = self._install_warmup_pipeline(app)

        async with app.lifespan(app.app):
            self.assertEqual([item.lang_code for item in instances], ["z", "a"])
            self.assertIs(app.pipeline_zh, instances[0])
            self.assertIs(app.pipeline_en, instances[1])


class RunKokoroTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        disable_asyncio_debug()
        self.app = import_app_with_fakes()

    async def test_chinese_voice_routes_to_chinese_pipeline_after_filtering_english(self):
        zh = _RecordingPipeline([[0.0, 0.5], [-0.5]])
        en = _RecordingPipeline([[1.0]])
        self.app.pipeline_zh = zh
        self.app.pipeline_en = en

        pcm = await self.app.run_kokoro("你好 DNS", "zf_xiaoxiao", 1.25)

        self.assertEqual(zh.calls, [{"text": "你好", "voice": "zf_xiaoxiao", "speed": 1.25}])
        self.assertEqual(en.calls, [])
        expected = self.app.to_pcm(np.array([0.0, 0.5, -0.5], dtype=np.float32))
        self.assertEqual(pcm, expected)

    async def test_english_voice_routes_to_english_pipeline_after_filtering_chinese(self):
        zh = _RecordingPipeline([[1.0]])
        en = _RecordingPipeline([[0.25]])
        self.app.pipeline_zh = zh
        self.app.pipeline_en = en

        pcm = await self.app.run_kokoro("hello 你好", "af_heart", 0.75)

        self.assertEqual(zh.calls, [])
        self.assertEqual(en.calls, [{"text": "hello", "voice": "af_heart", "speed": 0.75}])
        self.assertEqual(pcm, self.app.to_pcm(np.array([0.25], dtype=np.float32)))

    async def test_filtered_empty_text_skips_pipeline(self):
        zh = _RecordingPipeline([[1.0]])
        self.app.pipeline_zh = zh
        self.app.pipeline_en = _RecordingPipeline([[1.0]])

        pcm = await self.app.run_kokoro("DNS", "zf_xiaoxiao", 1.0)

        self.assertEqual(pcm, b"")
        self.assertEqual(zh.calls, [])

    async def test_cancel_event_stops_before_appending_audio(self):
        pipeline = _RecordingPipeline([[1.0]])
        self.app.pipeline_en = pipeline
        self.app.pipeline_zh = _RecordingPipeline([[1.0]])
        cancel_event = asyncio.Event()
        cancel_event.set()

        pcm = await self.app.run_kokoro("hello", "af_heart", 1.0, cancel_event)

        self.assertEqual(pcm, b"")
        self.assertEqual(pipeline.calls, [{"text": "hello", "voice": "af_heart", "speed": 1.0}])

    async def test_empty_generator_returns_empty_pcm(self):
        pipeline = _RecordingPipeline([])
        self.app.pipeline_en = pipeline
        self.app.pipeline_zh = _RecordingPipeline([[1.0]])

        pcm = await self.app.run_kokoro("hello", "af_heart", 1.0)

        self.assertEqual(pcm, b"")

    async def test_extension_a_char_survives_chinese_voice(self):
        # A2：扩展 A 汉字(㐀 U+3400)属中文，中文音色不应把它当外语剥离，且不判为无可发音内容。
        zh = _RecordingPipeline([[0.5]])
        self.app.pipeline_zh = zh
        self.app.pipeline_en = _RecordingPipeline([[1.0]])

        pcm = await self.app.run_kokoro("㐀", "zf_xiaoxiao", 1.0)

        self.assertEqual(zh.calls, [{"text": "㐀", "voice": "zf_xiaoxiao", "speed": 1.0}])
        self.assertEqual(pcm, self.app.to_pcm(np.array([0.5], dtype=np.float32)))

    async def test_extension_a_char_stripped_by_english_voice(self):
        # A2：英文音色应剥离扩展区汉字(此前仅覆盖基本区会漏读)；剥离后无可发音内容返回空 PCM。
        en = _RecordingPipeline([[1.0]])
        self.app.pipeline_en = en
        self.app.pipeline_zh = _RecordingPipeline([[1.0]])

        pcm = await self.app.run_kokoro("㐀", "af_heart", 1.0)

        self.assertEqual(pcm, b"")
        self.assertEqual(en.calls, [])

    async def test_astral_cjk_char_stripped_by_english_voice(self):
        # A2：扩展 B 及以上(astral 平面，如 𠀀 U+20000)同样应被英文音色剥离。
        en = _RecordingPipeline([[1.0]])
        self.app.pipeline_en = en
        self.app.pipeline_zh = _RecordingPipeline([[1.0]])

        pcm = await self.app.run_kokoro("\U00020000", "af_heart", 1.0)

        self.assertEqual(pcm, b"")
        self.assertEqual(en.calls, [])

    async def test_synthesis_semaphore_blocks_when_exhausted(self):
        # A1：合成信号量是 REST/WS 共用的推理并发闸门(与 ffmpeg 配额解耦)。
        # 确定性验证(不依赖真实线程调度，杜绝死锁)：把上限设为 1 并预先耗尽信号量，
        # 则 run_kokoro 必须阻塞在 async with 处、拿不到信号量、无法进入 to_thread。
        # 让出几轮事件循环后断言推理仍未派发(pipeline 未被调用)，即闸门确实拦住了它；
        # 释放信号量后应能正常完成——证明这是"拦截"而非"永久拒绝"。
        self.app.TTS_MAX_SYNTHESIS_CONCURRENCY = 1
        sem = asyncio.Semaphore(1)
        self.app._synthesis_semaphore = sem

        pipeline = _RecordingPipeline([[0.5]])
        self.app.pipeline_en = pipeline
        self.app.pipeline_zh = _RecordingPipeline([[1.0]])

        # 预先占满信号量：此时任何 run_kokoro 都应卡在信号量外。
        await sem.acquire()
        task = asyncio.create_task(self.app.run_kokoro("hello", "af_heart", 1.0))
        try:
            # 让出若干轮事件循环，给 task 充分机会推进；被闸门拦住则推理不会派发。
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertEqual(pipeline.calls, [])

            # 释放信号量：run_kokoro 应拿到槽位、完成推理。
            sem.release()
            pcm = await asyncio.wait_for(task, timeout=2.0)
            self.assertEqual(pcm, self.app.to_pcm(np.array([0.5], dtype=np.float32)))
            self.assertEqual(pipeline.calls, [{"text": "hello", "voice": "af_heart", "speed": 1.0}])
        finally:
            # 兜底：任何断言失败都不能留下挂起 task 拖死 tearDown。
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


class EdgeVoiceCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # app.time 是全局 time 模块；上个用例若失败也不能把常量时钟泄漏给事件循环。
        time.monotonic = REAL_MONOTONIC
        disable_asyncio_debug()
        self.app = import_app_with_fakes()

    async def asyncTearDown(self):
        # asyncio 的 delay 同样依赖 time.monotonic；不恢复会让后续 retry sleep 永不到期。
        time.monotonic = REAL_MONOTONIC

    async def test_successful_edge_voice_fetch_is_cached(self):
        calls = 0
        voices = [{"ShortName": "en-US-AriaNeural"}]

        async def fake_list_voices():
            nonlocal calls
            calls += 1
            return voices

        self.app.edge_tts.list_voices = fake_list_voices

        first = await self.app._get_edge_voices()
        second = await self.app._get_edge_voices()

        self.assertIs(first, voices)
        self.assertIs(second, voices)
        self.assertEqual(calls, 1)

    async def test_transient_edge_voice_failure_retries_in_same_request(self):
        calls = 0
        self.app.EDGE_RETRY_MAX_ATTEMPTS = 2
        self.app.EDGE_RETRY_BASE_DELAY_SECONDS = 0

        async def failing_then_ok():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("network down")
            return [{"ShortName": "en-US-AriaNeural"}]

        self.app.edge_tts.list_voices = failing_then_ok

        first = await self.app._get_edge_voices()
        second = await self.app._get_edge_voices()

        self.assertEqual(first, [{"ShortName": "en-US-AriaNeural"}])
        self.assertIs(second, first)
        self.assertEqual(calls, 2)

    async def test_edge_voice_retries_use_exponential_backoff(self):
        calls = 0
        delays = []
        self.app.EDGE_RETRY_MAX_ATTEMPTS = 3
        self.app.EDGE_RETRY_BASE_DELAY_SECONDS = 0.25
        self.app.EDGE_VOICES_FAILURE_COOLDOWN_SECONDS = 0

        async def always_fails():
            nonlocal calls
            calls += 1
            raise RuntimeError("network down")

        async def record_sleep(delay):
            delays.append(delay)

        self.app.edge_tts.list_voices = always_fails
        with mock.patch.object(asyncio, "sleep", record_sleep):
            voices = await self.app._get_edge_voices()

        self.assertEqual(voices, [])
        self.assertEqual(calls, 3)
        self.assertEqual(delays, [0.25, 0.5])

    async def test_exhausted_cold_refresh_obeys_failure_cooldown(self):
        calls = 0
        now = 100.0
        self.app.EDGE_RETRY_MAX_ATTEMPTS = 2
        self.app.EDGE_RETRY_BASE_DELAY_SECONDS = 0
        self.app.EDGE_VOICES_FAILURE_COOLDOWN_SECONDS = 10.0
        self.app.time.monotonic = lambda: now

        async def always_fails():
            nonlocal calls
            calls += 1
            raise RuntimeError("network down")

        self.app.edge_tts.list_voices = always_fails

        self.assertEqual(await self.app._get_edge_voices(), [])
        self.assertEqual(calls, 2)
        self.assertEqual(await self.app._get_edge_voices(), [])
        self.assertEqual(calls, 2)

        now = 111.0
        self.assertEqual(await self.app._get_edge_voices(), [])
        self.assertEqual(calls, 4)

    async def test_concurrent_edge_voice_refresh_is_single_flight(self):
        calls = 0
        entered = asyncio.Event()
        release = asyncio.Event()
        voices = [{"ShortName": "en-US-AriaNeural"}]

        async def blocked_fetch():
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return voices

        self.app.edge_tts.list_voices = blocked_fetch

        first_task = asyncio.create_task(self.app._get_edge_voices())
        await entered.wait()
        second_task = asyncio.create_task(self.app._get_edge_voices())
        await asyncio.sleep(0)
        release.set()
        first, second = await asyncio.gather(first_task, second_task)

        self.assertIs(first, voices)
        self.assertIs(second, voices)
        self.assertEqual(calls, 1)

    async def test_concurrent_success_refresh_is_single_flight_when_ttl_is_zero(self):
        calls = 0
        entered = asyncio.Event()
        release = asyncio.Event()
        self.app.EDGE_VOICES_CACHE_TTL_SECONDS = 0

        async def blocked_fetch():
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return [{"ShortName": f"voice-{calls}"}]

        self.app.edge_tts.list_voices = blocked_fetch

        first_task = asyncio.create_task(self.app._get_edge_voices())
        await entered.wait()
        second_task = asyncio.create_task(self.app._get_edge_voices())
        await asyncio.sleep(0)
        release.set()
        first, second = await asyncio.gather(first_task, second_task)

        self.assertIs(first, second)
        self.assertEqual(first, [{"ShortName": "voice-1"}])
        self.assertEqual(calls, 1)

        self.assertEqual(
            await self.app._get_edge_voices(),
            [{"ShortName": "voice-2"}],
        )
        self.assertEqual(calls, 2)

    async def test_concurrent_failed_edge_voice_refresh_is_single_flight(self):
        calls = 0
        entered = asyncio.Event()
        release = asyncio.Event()
        self.app.EDGE_RETRY_MAX_ATTEMPTS = 1
        self.app.EDGE_VOICES_FAILURE_COOLDOWN_SECONDS = 10

        async def blocked_failure():
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            raise RuntimeError("network down")

        self.app.edge_tts.list_voices = blocked_failure

        first_task = asyncio.create_task(self.app._get_edge_voices())
        await entered.wait()
        second_task = asyncio.create_task(self.app._get_edge_voices())
        await asyncio.sleep(0)
        release.set()
        first, second = await asyncio.gather(first_task, second_task)

        self.assertEqual(first, [])
        self.assertEqual(second, [])
        self.assertEqual(calls, 1)
        self.assertEqual(await self.app._get_edge_voices(), [])
        self.assertEqual(calls, 1)

    async def test_concurrent_failed_refresh_is_single_flight_when_cooldown_is_zero(self):
        calls = 0
        entered = asyncio.Event()
        release = asyncio.Event()
        self.app.EDGE_RETRY_MAX_ATTEMPTS = 1
        self.app.EDGE_VOICES_FAILURE_COOLDOWN_SECONDS = 0

        async def blocked_failure():
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            raise RuntimeError("network down")

        self.app.edge_tts.list_voices = blocked_failure

        first_task = asyncio.create_task(self.app._get_edge_voices())
        await entered.wait()
        second_task = asyncio.create_task(self.app._get_edge_voices())
        await asyncio.sleep(0)
        release.set()
        first, second = await asyncio.gather(first_task, second_task)

        self.assertEqual(first, [])
        self.assertEqual(second, [])
        self.assertEqual(calls, 1)

        self.assertEqual(await self.app._get_edge_voices(), [])
        self.assertEqual(calls, 2)

    async def test_cancelled_voice_refresh_releases_lock_for_waiter(self):
        calls = 0
        entered = asyncio.Event()
        voices = [{"ShortName": "recovered"}]

        async def cancelled_then_success():
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await asyncio.Event().wait()
            return voices

        self.app.edge_tts.list_voices = cancelled_then_success

        first_task = asyncio.create_task(self.app._get_edge_voices())
        await entered.wait()
        second_task = asyncio.create_task(self.app._get_edge_voices())
        await asyncio.sleep(0)
        first_task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await first_task
        self.assertIs(
            await asyncio.wait_for(second_task, timeout=1.0),
            voices,
        )
        self.assertEqual(calls, 2)
        self.assertFalse(self.app._get_edge_voices_refresh_lock().locked())

    async def test_successful_edge_voice_cache_expires_after_ttl(self):
        calls = 0
        now = 100.0
        self.app.EDGE_VOICES_CACHE_TTL_SECONDS = 10.0
        self.app.time.monotonic = lambda: now

        async def fake_list_voices():
            nonlocal calls
            calls += 1
            return [{"ShortName": f"voice-{calls}"}]

        self.app.edge_tts.list_voices = fake_list_voices

        first = await self.app._get_edge_voices()
        second = await self.app._get_edge_voices()
        now = 111.0
        third = await self.app._get_edge_voices()

        self.assertEqual(first, [{"ShortName": "voice-1"}])
        self.assertEqual(second, [{"ShortName": "voice-1"}])
        self.assertEqual(third, [{"ShortName": "voice-2"}])
        self.assertEqual(calls, 2)

    async def test_expired_edge_voice_refresh_failure_serves_stale_cache(self):
        calls = 0
        now = 100.0
        self.app.EDGE_VOICES_CACHE_TTL_SECONDS = 10.0
        self.app.EDGE_RETRY_MAX_ATTEMPTS = 2
        self.app.EDGE_RETRY_BASE_DELAY_SECONDS = 0
        self.app.EDGE_VOICES_FAILURE_COOLDOWN_SECONDS = 10.0
        self.app.time.monotonic = lambda: now

        async def success_then_fail():
            nonlocal calls
            calls += 1
            if calls == 1:
                return [{"ShortName": "cached"}]
            raise RuntimeError("network down")

        self.app.edge_tts.list_voices = success_then_fail

        first = await self.app._get_edge_voices()
        now = 111.0
        second = await self.app._get_edge_voices()
        self.assertEqual(calls, 3)
        third = await self.app._get_edge_voices()

        self.assertEqual(first, [{"ShortName": "cached"}])
        self.assertEqual(second, [{"ShortName": "cached"}])
        self.assertEqual(third, [{"ShortName": "cached"}])
        self.assertEqual(calls, 3)

    async def test_invalid_edge_voice_refresh_retries_and_preserves_stale_cache(self):
        calls = 0
        now = 100.0
        cached = [{"ShortName": "cached"}]
        self.app.EDGE_VOICES_CACHE_TTL_SECONDS = 10.0
        self.app.EDGE_RETRY_MAX_ATTEMPTS = 2
        self.app.EDGE_RETRY_BASE_DELAY_SECONDS = 0
        self.app.EDGE_VOICES_FAILURE_COOLDOWN_SECONDS = 10.0
        self.app.time.monotonic = lambda: now

        async def success_then_invalid():
            nonlocal calls
            calls += 1
            if calls == 1:
                return cached
            return [] if calls == 2 else [{}]

        self.app.edge_tts.list_voices = success_then_invalid

        self.assertIs(await self.app._get_edge_voices(), cached)
        now = 111.0
        self.assertIs(await self.app._get_edge_voices(), cached)
        self.assertEqual(calls, 3)
        self.assertIs(await self.app._get_edge_voices(), cached)
        self.assertEqual(calls, 3)

    async def test_edge_voice_request_timeout_retries_then_enters_cooldown(self):
        calls = 0
        self.app.EDGE_VOICES_REQUEST_TIMEOUT_SECONDS = 0.01
        self.app.EDGE_RETRY_MAX_ATTEMPTS = 2
        self.app.EDGE_RETRY_BASE_DELAY_SECONDS = 0
        self.app.EDGE_VOICES_FAILURE_COOLDOWN_SECONDS = 10

        async def hangs():
            nonlocal calls
            calls += 1
            await asyncio.Event().wait()

        self.app.edge_tts.list_voices = hangs

        self.assertEqual(
            await asyncio.wait_for(self.app._get_edge_voices(), 0.25), []
        )
        self.assertEqual(calls, 2)
        self.assertEqual(await self.app._get_edge_voices(), [])
        self.assertEqual(calls, 2)

    async def test_edge_voice_timeout_preserves_stale_and_single_flight(self):
        cached = [{"ShortName": "cached"}]
        calls = 0
        entered = asyncio.Event()
        self.app._edge_voices_cache = cached
        self.app._edge_voices_cache_expires_at = 0
        self.app.EDGE_VOICES_REQUEST_TIMEOUT_SECONDS = 0.01
        self.app.EDGE_RETRY_MAX_ATTEMPTS = 1
        self.app.EDGE_VOICES_FAILURE_COOLDOWN_SECONDS = 0

        async def hangs():
            nonlocal calls
            calls += 1
            entered.set()
            await asyncio.Event().wait()

        self.app.edge_tts.list_voices = hangs
        first = asyncio.create_task(self.app._get_edge_voices())
        await entered.wait()
        second = asyncio.create_task(self.app._get_edge_voices())
        await asyncio.sleep(0)

        results = await asyncio.wait_for(
            asyncio.gather(first, second), timeout=0.25
        )

        self.assertIs(results[0], cached)
        self.assertIs(results[1], cached)
        self.assertEqual(calls, 1)

    async def test_edge_voice_timeout_log_includes_exception_type(self):
        self.app.EDGE_VOICES_REQUEST_TIMEOUT_SECONDS = 0.01
        self.app.EDGE_RETRY_MAX_ATTEMPTS = 1
        self.app.EDGE_VOICES_FAILURE_COOLDOWN_SECONDS = 0

        async def hangs():
            await asyncio.Event().wait()

        self.app.edge_tts.list_voices = hangs
        with self.assertLogs(self.app.logger, level="WARNING") as captured:
            self.assertEqual(await self.app._get_edge_voices(), [])

        self.assertIn("TimeoutError", "\n".join(captured.output))

    async def test_zero_edge_voice_timeout_disables_internal_deadline(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        voices = [{"ShortName": "unbounded"}]
        self.app.EDGE_VOICES_REQUEST_TIMEOUT_SECONDS = 0
        self.app.EDGE_RETRY_MAX_ATTEMPTS = 1

        async def blocked_fetch():
            entered.set()
            await release.wait()
            return voices

        self.app.edge_tts.list_voices = blocked_fetch
        task = asyncio.create_task(self.app._get_edge_voices())
        try:
            await asyncio.wait_for(entered.wait(), 0.2)
            await asyncio.sleep(0.02)
            self.assertFalse(task.done(), "0 必须关闭内部 attempt timeout")
            release.set()
            self.assertIs(await asyncio.wait_for(task, 0.2), voices)
        finally:
            release.set()
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def test_cancelling_voice_refresh_waiter_does_not_cancel_owner(self):
        calls = 0
        entered = asyncio.Event()
        release = asyncio.Event()
        voices = [{"ShortName": "owner-result"}]

        async def blocked_fetch():
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return voices

        self.app.edge_tts.list_voices = blocked_fetch
        owner = asyncio.create_task(self.app._get_edge_voices())
        await asyncio.wait_for(entered.wait(), 0.2)
        waiter = asyncio.create_task(self.app._get_edge_voices())
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter

        self.assertFalse(owner.done())
        release.set()
        self.assertIs(await asyncio.wait_for(owner, 0.2), voices)
        self.assertEqual(calls, 1)
        self.assertFalse(self.app._get_edge_voices_refresh_lock().locked())


class Mp3EncoderCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        disable_asyncio_debug()
        self.app = import_app_with_fakes()

    async def test_edge_encoder_accepts_compressed_audio_input(self):
        captured = {}

        async def fake_exec(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return FakeProc()

        self.app.asyncio.create_subprocess_exec = fake_exec
        proc = await self.app._create_mp3_encoder("edge")

        self.assertIsInstance(proc, FakeProc)
        self.assertEqual(captured["args"][:3], ("ffmpeg", "-i", "pipe:0"))
        self.assertIn("libmp3lame", captured["args"])
        self.assertNotIn("s16le", captured["args"])
        self.assertIs(captured["kwargs"]["stdin"], self.app.asyncio.subprocess.PIPE)

    async def test_kokoro_encoder_declares_raw_pcm_input_format(self):
        captured = {}

        async def fake_exec(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return FakeProc()

        self.app.asyncio.create_subprocess_exec = fake_exec
        proc = await self.app._create_mp3_encoder("kokoro")

        self.assertIsInstance(proc, FakeProc)
        self.assertIn("s16le", captured["args"])
        self.assertIn("24000", captured["args"])
        self.assertIn("1", captured["args"])
        self.assertIn("libmp3lame", captured["args"])


class FfmpegLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        disable_asyncio_debug()
        self.app = import_app_with_fakes()

    async def test_try_acquire_returns_false_when_limit_exhausted(self):
        limiter = self.app.FfmpegLimiter(1)

        self.assertTrue(await limiter.acquire())
        self.assertFalse(await limiter.acquire())

    async def test_release_allows_next_acquire(self):
        limiter = self.app.FfmpegLimiter(1)

        self.assertTrue(await limiter.acquire())
        limiter.release()

        self.assertTrue(await limiter.acquire())

    async def test_prefetch_cannot_consume_reserved_main_capacity(self):
        single = self.app.FfmpegLimiter(1)

        try:
            single_prefetch = await single.acquire(prefetch=True)
        except TypeError:
            self.fail("FfmpegLimiter.acquire must distinguish low-priority prefetch leases")
        self.assertFalse(single_prefetch)
        self.assertTrue(await single.acquire())
        single.release()

        dual = self.app.FfmpegLimiter(2)
        self.assertTrue(await dual.acquire(prefetch=True))
        self.assertFalse(await dual.acquire(prefetch=True))
        self.assertTrue(await dual.acquire())
        dual.release()
        self.assertFalse(await dual.acquire(prefetch=True))
        dual.release(prefetch=True)
        self.assertTrue(await dual.acquire(prefetch=True))
        dual.release(prefetch=True)


class ReapProcTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        disable_asyncio_debug()
        self.app = import_app_with_fakes()

    async def test_reap_proc_waits_without_killing_already_exited_proc(self):
        proc = FakeProc()
        proc.returncode = 0

        await self.app._reap_proc(proc)

        self.assertFalse(proc.killed)
        self.assertTrue(proc.waited)

    async def test_reap_proc_ignores_process_lookup_error_from_kill(self):
        class LookupProc(FakeProc):
            def kill(self):
                raise ProcessLookupError()

        proc = LookupProc()

        await self.app._reap_proc(proc)

        self.assertTrue(proc.waited)

    async def test_reap_proc_defers_cancellation_until_wait_and_release(self):
        wait_started = asyncio.Event()
        allow_wait = asyncio.Event()
        releases = []

        class BlockingWaitProc(FakeProc):
            def __init__(self):
                super().__init__()
                self.wait_cancelled = False

            async def wait(self):
                wait_started.set()
                try:
                    await allow_wait.wait()
                except asyncio.CancelledError:
                    self.wait_cancelled = True
                    raise
                self.waited = True

        class RecordingLimiter:
            def release(self, prefetch=False):
                releases.append(prefetch)

        proc = BlockingWaitProc()
        self.app._ffmpeg_limiter = RecordingLimiter()
        task = asyncio.create_task(self.app._reap_proc(proc))

        await asyncio.wait_for(wait_started.wait(), timeout=1.0)
        task.cancel()
        await asyncio.sleep(0)
        completed_before_reap = task.done()
        allow_wait.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)

        self.assertFalse(
            completed_before_reap,
            "cancellation must wait until process cleanup finishes",
        )
        self.assertFalse(proc.wait_cancelled)
        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)
        self.assertEqual(releases, [False])


if __name__ == "__main__":
    unittest.main()
