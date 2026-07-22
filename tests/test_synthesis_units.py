# -*- coding: utf-8 -*-
"""合成单元函数测试：_feed_mp3 / _stream_mp3 / synth_kokoro / synth_edge。"""
import asyncio
import unittest

from _support import (
    AudioEdgeStream,
    FakeProc,
    FakeWebSocket,
    FailingEdgeStream,
    HangingStdout,
    ScriptedStdout,
    disable_asyncio_debug,
    drain_queue,
    import_app_with_fakes,
    make_communicate,
)


class FeedMp3Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        disable_asyncio_debug()
        self.app = import_app_with_fakes()

    async def test_kokoro_feed_writes_non_empty_pcm_and_sets_first_audio(self):
        proc = FakeProc()
        first_audio = asyncio.Event()

        async def fake_run_kokoro(text, voice, speed, cancel_event=None):
            return b"PCM" if text == "hello." else b""

        self.app.run_kokoro = fake_run_kokoro
        await self.app._feed_mp3(proc, "hello. skipped", "kokoro", "af_heart", 1.0, first_audio)

        self.assertTrue(first_audio.is_set())
        self.assertEqual(proc.stdin.written(), b"PCM")
        self.assertTrue(proc.stdin.closed)

    async def test_edge_feed_writes_audio_chunks_and_sets_first_audio(self):
        proc = FakeProc()
        first_audio = asyncio.Event()
        self.app.edge_tts.Communicate = make_communicate(AudioEdgeStream([b"A", b"B"]))

        await self.app._feed_mp3(proc, "hello", "edge", "voice", 1.25, first_audio)

        self.assertTrue(first_audio.is_set())
        self.assertEqual(proc.stdin.written(), b"AB")
        self.assertTrue(proc.stdin.closed)


class StreamMp3Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        disable_asyncio_debug()
        self.app = import_app_with_fakes()

    async def test_stream_yields_stdout_chunks_and_reaps_proc(self):
        proc = FakeProc(stdout=ScriptedStdout([b"one", b"two"]))
        feed_task = asyncio.create_task(asyncio.sleep(0))
        await feed_task

        chunks = []
        async for chunk in self.app._stream_mp3(proc, feed_task, "kokoro", "af_heart"):
            chunks.append(chunk)

        self.assertEqual(chunks, [b"one", b"two"])
        self.assertTrue(proc.waited)

    async def test_stream_cancellation_kills_proc(self):
        proc = FakeProc(stdout=ScriptedStdout([b"one", b"two"]))
        feed_task = asyncio.create_task(asyncio.Event().wait())
        agen = self.app._stream_mp3(proc, feed_task, "kokoro", "af_heart")

        self.assertEqual(await agen.__anext__(), b"one")
        await agen.aclose()

        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)
        self.assertTrue(feed_task.cancelled())

    async def test_stream_timeout_cancels_feed_and_reaps_proc(self):
        self.app.TTS_SYNTHESIS_TIMEOUT_SECONDS = 0.01
        proc = FakeProc(stdout=HangingStdout())
        feed_task = asyncio.create_task(asyncio.Event().wait())
        chunks = []

        async for chunk in self.app._stream_mp3(proc, feed_task, "kokoro", "af_heart"):
            chunks.append(chunk)

        self.assertEqual(chunks, [])
        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)
        self.assertTrue(feed_task.cancelled())


class SynthKokoroTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        disable_asyncio_debug()
        self.app = import_app_with_fakes()

    async def test_emits_seg_for_each_unit_and_chunks_pcm(self):
        queue = asyncio.Queue()
        pcm = b"a" * 3000

        async def fake_run_kokoro(text, voice, speed, cancel_event=None):
            return pcm if text == "hello" else b""

        self.app.run_kokoro = fake_run_kokoro
        await self.app.synth_kokoro(
            ["hello", "```\ncode\n```"], "af_heart", 1.0, queue, FakeWebSocket(), asyncio.Event()
        )

        items = drain_queue(queue)
        self.assertEqual(items[0], {"type": "seg", "text": "hello"})
        self.assertEqual(items[1], b"a" * 2048)
        self.assertEqual(items[2], b"a" * 952)
        self.assertEqual(items[3], {"type": "seg", "text": ""})

    async def test_disconnect_stops_before_emitting(self):
        queue = asyncio.Queue()

        await self.app.synth_kokoro(["hello"], "af_heart", 1.0, queue, FakeWebSocket(False), asyncio.Event())

        self.assertEqual(drain_queue(queue), [])


class SynthEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        disable_asyncio_debug()
        self.app = import_app_with_fakes()
        self.app.logger.disabled = True

    async def asyncTearDown(self):
        self.app.logger.disabled = False

    async def test_edge_read_aligns_16bit_pcm_chunks_and_reaps_proc(self):
        proc = FakeProc(stdout=ScriptedStdout([b"abc", b"de"]))
        queue = asyncio.Queue()

        async def fake_create_subprocess_exec(*args, **kwargs):
            return proc

        self.app.asyncio.create_subprocess_exec = fake_create_subprocess_exec
        self.app.edge_tts.Communicate = make_communicate(AudioEdgeStream([b"mp3"]))

        await self.app.synth_edge("hello", "voice", 1.0, queue, FakeWebSocket(), asyncio.Event())

        items = drain_queue(queue)
        self.assertEqual(items[0], {"type": "seg", "text": "hello"})
        self.assertEqual(items[1], b"ab")
        self.assertEqual(items[2], b"cd")
        self.assertTrue(proc.waited)

    async def test_synth_edge_raises_feed_errors_and_reaps_proc(self):
        proc = FakeProc(stdout=ScriptedStdout([]))
        source_error = RuntimeError("edge failed")

        async def fake_create_subprocess_exec(*args, **kwargs):
            return proc

        self.app.asyncio.create_subprocess_exec = fake_create_subprocess_exec
        self.app.edge_tts.Communicate = make_communicate(FailingEdgeStream(source_error))

        with self.assertRaises(RuntimeError):
            await self.app.synth_edge("hello", "bad", 1.0, asyncio.Queue(), FakeWebSocket(), asyncio.Event())

        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)

    async def test_synth_edge_respects_ffmpeg_process_limit(self):
        class ExhaustedLimiter:
            async def acquire(self):
                return False

            def release(self):
                raise AssertionError("release should not run when acquire fails")

        async def fake_create_subprocess_exec(*args, **kwargs):
            raise AssertionError("ffmpeg must not start when the process limit is exhausted")

        self.app._ffmpeg_limiter = ExhaustedLimiter()
        self.app.asyncio.create_subprocess_exec = fake_create_subprocess_exec
        self.app.edge_tts.Communicate = make_communicate(AudioEdgeStream([b"mp3"]))
        queue = asyncio.Queue()

        with self.assertRaisesRegex(RuntimeError, "ffmpeg process limit reached"):
            await self.app.synth_edge("hello", "voice", 1.0, queue, FakeWebSocket(), asyncio.Event())
        self.assertEqual(drain_queue(queue), [])


if __name__ == "__main__":
    unittest.main()
