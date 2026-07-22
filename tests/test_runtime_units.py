# -*- coding: utf-8 -*-
"""运行时内部单元测试：startup、run_kokoro、Edge 音色缓存、ffmpeg 命令、进程回收。"""
import asyncio
import unittest

import numpy as np

from _support import FakeProc, disable_asyncio_debug, import_app_with_fakes


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
        disable_asyncio_debug()
        self.app = import_app_with_fakes()

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

    async def test_failed_edge_voice_fetch_is_not_cached(self):
        calls = 0

        async def failing_then_ok():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("network down")
            return [{"ShortName": "en-US-AriaNeural"}]

        self.app.edge_tts.list_voices = failing_then_ok

        first = await self.app._get_edge_voices()
        second = await self.app._get_edge_voices()

        self.assertEqual(first, [])
        self.assertEqual(second, [{"ShortName": "en-US-AriaNeural"}])
        self.assertEqual(calls, 2)

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

        self.assertEqual(first, [{"ShortName": "cached"}])
        self.assertEqual(second, [{"ShortName": "cached"}])
        self.assertEqual(calls, 2)


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


if __name__ == "__main__":
    unittest.main()
