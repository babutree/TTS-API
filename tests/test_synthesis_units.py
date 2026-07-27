# -*- coding: utf-8 -*-
"""合成单元函数测试：_feed_mp3 / _stream_mp3 / synth_kokoro / synth_edge。"""
import asyncio
import unittest
from unittest import mock

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


def make_sequenced_communicate(stream_factories):
    factories = iter(stream_factories)
    attempts = []

    class SequencedCommunicate:
        def __init__(self, text, voice, rate=None):
            attempts.append((text, voice, rate))
            self._stream_factory = next(factories)

        def stream(self):
            return self._stream_factory()

    return SequencedCommunicate, attempts


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

    async def test_edge_feed_does_not_signal_first_audio_when_write_fails(self):
        class FailingWriteStdin:
            def __init__(self):
                self.closed = False

            def write(self, data):
                raise BrokenPipeError("ffmpeg stdin closed")

            async def drain(self):
                raise AssertionError("drain must not run after write fails")

            def close(self):
                self.closed = True

        stdin = FailingWriteStdin()
        proc = FakeProc(stdin=stdin)
        first_audio = asyncio.Event()
        self.app.edge_tts.Communicate = make_communicate(
            AudioEdgeStream([b"A"])
        )

        with self.assertRaisesRegex(BrokenPipeError, "stdin closed"):
            await self.app._feed_mp3(
                proc, "hello", "edge", "voice", 1.0, first_audio
            )

        self.assertFalse(first_audio.is_set())
        self.assertTrue(stdin.closed)

    async def test_edge_feed_retries_failure_before_first_audio(self):
        proc = FakeProc()
        first_audio = asyncio.Event()
        self.app.EDGE_RETRY_MAX_ATTEMPTS = 2
        self.app.EDGE_RETRY_BASE_DELAY_SECONDS = 0

        async def fail_before_audio():
            raise RuntimeError("transient edge failure")
            yield

        async def succeed():
            yield {"type": "audio", "data": b"A"}

        communicate, attempts = make_sequenced_communicate(
            [fail_before_audio, succeed]
        )
        self.app.edge_tts.Communicate = communicate

        raised = None
        try:
            await self.app._feed_mp3(
                proc, "hello", "edge", "voice", 1.0, first_audio
            )
        except RuntimeError as exc:
            raised = exc

        self.assertIsNone(raised)
        self.assertEqual(len(attempts), 2)
        self.assertTrue(first_audio.is_set())
        self.assertEqual(proc.stdin.written(), b"A")

    async def test_edge_feed_retries_stream_that_ends_without_audio(self):
        proc = FakeProc()
        first_audio = asyncio.Event()
        self.app.EDGE_RETRY_MAX_ATTEMPTS = 2
        self.app.EDGE_RETRY_BASE_DELAY_SECONDS = 0

        async def metadata_only():
            yield {"type": "metadata"}

        async def succeed():
            yield {"type": "audio", "data": b"A"}

        communicate, attempts = make_sequenced_communicate(
            [metadata_only, succeed]
        )
        self.app.edge_tts.Communicate = communicate

        await self.app._feed_mp3(
            proc, "hello", "edge", "voice", 1.0, first_audio
        )

        self.assertEqual(len(attempts), 2)
        self.assertTrue(first_audio.is_set())
        self.assertEqual(proc.stdin.written(), b"A")

    async def test_edge_feed_retries_use_exponential_backoff(self):
        proc = FakeProc()
        delays = []
        self.app.EDGE_RETRY_MAX_ATTEMPTS = 3
        self.app.EDGE_RETRY_BASE_DELAY_SECONDS = 0.25

        async def fail_before_audio():
            raise RuntimeError("transient edge failure")
            yield

        async def succeed():
            yield {"type": "audio", "data": b"A"}

        async def record_sleep(delay):
            delays.append(delay)

        communicate, attempts = make_sequenced_communicate(
            [fail_before_audio, fail_before_audio, succeed]
        )
        self.app.edge_tts.Communicate = communicate

        with mock.patch.object(asyncio, "sleep", record_sleep):
            await self.app._feed_mp3(
                proc, "hello", "edge", "voice", 1.0
            )

        self.assertEqual(len(attempts), 3)
        self.assertEqual(delays, [0.25, 0.5])
        self.assertEqual(proc.stdin.written(), b"A")

    async def test_edge_iterator_stops_before_upstream_attempt_when_cancelled(self):
        cancel_event = asyncio.Event()
        cancel_event.set()

        class UnexpectedCommunicate:
            def __init__(self, *args, **kwargs):
                raise AssertionError("cancelled synthesis must not contact Edge")

        self.app.edge_tts.Communicate = UnexpectedCommunicate

        chunks = [
            data
            async for data in self.app._iter_edge_audio(
                "hello", "voice", "+0%", cancel_event
            )
        ]

        self.assertEqual(chunks, [])

    async def test_edge_iterator_stops_retry_when_failure_sets_cancel_event(self):
        cancel_event = asyncio.Event()
        self.app.EDGE_RETRY_MAX_ATTEMPTS = 2
        self.app.EDGE_RETRY_BASE_DELAY_SECONDS = 0

        async def fail_and_cancel():
            cancel_event.set()
            raise RuntimeError("upstream failed while request was cancelled")
            yield

        async def unexpected_retry():
            raise AssertionError("cancelled synthesis must not retry Edge")
            yield

        communicate, attempts = make_sequenced_communicate(
            [fail_and_cancel, unexpected_retry]
        )
        self.app.edge_tts.Communicate = communicate

        chunks = [
            data
            async for data in self.app._iter_edge_audio(
                "hello", "voice", "+0%", cancel_event
            )
        ]

        self.assertEqual(chunks, [])
        self.assertEqual(len(attempts), 1)

    async def test_edge_feed_does_not_retry_after_first_audio(self):
        proc = FakeProc()
        first_audio = asyncio.Event()
        self.app.EDGE_RETRY_MAX_ATTEMPTS = 2
        self.app.EDGE_RETRY_BASE_DELAY_SECONDS = 0

        async def audio_then_fail():
            yield {"type": "audio", "data": b"A"}
            raise RuntimeError("edge failed after audio")

        communicate, attempts = make_sequenced_communicate([audio_then_fail])
        self.app.edge_tts.Communicate = communicate

        with self.assertRaisesRegex(RuntimeError, "after audio"):
            await self.app._feed_mp3(
                proc, "hello", "edge", "voice", 1.0, first_audio
            )

        self.assertEqual(len(attempts), 1)
        self.assertTrue(first_audio.is_set())
        self.assertEqual(proc.stdin.written(), b"A")


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

    async def test_synth_edge_cancel_interrupts_blocked_upstream_and_reaps_proc(self):
        proc = FakeProc(stdout=HangingStdout())
        upstream_started = asyncio.Event()
        upstream_cancelled = asyncio.Event()
        cancel_event = asyncio.Event()

        async def fake_create_subprocess_exec(*args, **kwargs):
            return proc

        async def blocked_upstream():
            upstream_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                upstream_cancelled.set()
            yield

        self.app.asyncio.create_subprocess_exec = fake_create_subprocess_exec
        self.app.edge_tts.Communicate = make_communicate(blocked_upstream())
        task = asyncio.create_task(
            self.app.synth_edge(
                "hello", "voice", 1.0, asyncio.Queue(),
                FakeWebSocket(), cancel_event,
            )
        )

        await asyncio.wait_for(upstream_started.wait(), timeout=1.0)
        cancel_event.set()
        timed_out = False
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
        except asyncio.TimeoutError:
            timed_out = True
        finally:
            if not task.done():
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        self.assertFalse(timed_out, "cancel_event must interrupt pending Edge I/O")
        self.assertTrue(upstream_cancelled.is_set())
        self.assertTrue(proc.stdin.closed)
        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)

    async def test_synth_edge_outer_cancel_waits_for_reap_and_releases_lease(self):
        wait_started = asyncio.Event()
        allow_wait = asyncio.Event()
        releases = []

        class BlockingWaitProc(FakeProc):
            def __init__(self):
                super().__init__(stdout=ScriptedStdout([]))
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
            async def acquire(self, prefetch=False):
                return True

            def release(self, prefetch=False):
                releases.append(prefetch)

        proc = BlockingWaitProc()

        async def fake_create_subprocess_exec(*args, **kwargs):
            return proc

        self.app._ffmpeg_limiter = RecordingLimiter()
        self.app.asyncio.create_subprocess_exec = fake_create_subprocess_exec
        self.app.edge_tts.Communicate = make_communicate(AudioEdgeStream([b"mp3"]))
        task = asyncio.create_task(
            self.app.synth_edge(
                "hello", "voice", 1.0, asyncio.Queue(),
                FakeWebSocket(), asyncio.Event(), prefetch=True,
            )
        )

        await asyncio.wait_for(wait_started.wait(), timeout=1.0)
        task.cancel()
        await asyncio.sleep(0)
        completed_before_reap = task.done()
        allow_wait.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)

        self.assertFalse(
            completed_before_reap,
            "outer cancellation must wait until decoder cleanup finishes",
        )
        self.assertFalse(proc.wait_cancelled)
        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)
        self.assertEqual(releases, [True])

    async def test_synth_edge_retries_failure_before_first_audio(self):
        proc = FakeProc(stdout=ScriptedStdout([b"PCM!"]))
        self.app.EDGE_RETRY_MAX_ATTEMPTS = 2
        self.app.EDGE_RETRY_BASE_DELAY_SECONDS = 0

        async def fake_create_subprocess_exec(*args, **kwargs):
            return proc

        async def fail_before_audio():
            raise RuntimeError("transient edge failure")
            yield

        async def succeed():
            yield {"type": "audio", "data": b"mp3"}

        communicate, attempts = make_sequenced_communicate(
            [fail_before_audio, succeed]
        )
        self.app.asyncio.create_subprocess_exec = fake_create_subprocess_exec
        self.app.edge_tts.Communicate = communicate
        queue = asyncio.Queue()

        raised = None
        try:
            await self.app.synth_edge(
                "hello", "voice", 1.0, queue, FakeWebSocket(), asyncio.Event()
            )
        except RuntimeError as exc:
            raised = exc

        self.assertIsNone(raised)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(proc.stdin.written(), b"mp3")
        self.assertEqual(
            drain_queue(queue),
            [{"type": "seg", "text": "hello"}, b"PCM!"],
        )
        self.assertTrue(proc.waited)

    async def test_synth_edge_does_not_retry_after_first_audio(self):
        proc = FakeProc(stdout=ScriptedStdout([]))
        self.app.EDGE_RETRY_MAX_ATTEMPTS = 2
        self.app.EDGE_RETRY_BASE_DELAY_SECONDS = 0

        async def fake_create_subprocess_exec(*args, **kwargs):
            return proc

        async def audio_then_fail():
            yield {"type": "audio", "data": b"mp3"}
            raise RuntimeError("edge failed after audio")

        communicate, attempts = make_sequenced_communicate([audio_then_fail])
        self.app.asyncio.create_subprocess_exec = fake_create_subprocess_exec
        self.app.edge_tts.Communicate = communicate

        with self.assertRaisesRegex(RuntimeError, "after audio"):
            await self.app.synth_edge(
                "hello", "voice", 1.0, asyncio.Queue(),
                FakeWebSocket(), asyncio.Event(),
            )

        self.assertEqual(len(attempts), 1)
        self.assertEqual(proc.stdin.written(), b"mp3")
        self.assertTrue(proc.waited)

    async def test_synth_edge_empty_stream_exhaustion_raises_and_reaps_proc(self):
        proc = FakeProc(stdout=ScriptedStdout([]))
        self.app.EDGE_RETRY_MAX_ATTEMPTS = 2
        self.app.EDGE_RETRY_BASE_DELAY_SECONDS = 0

        async def fake_create_subprocess_exec(*args, **kwargs):
            return proc

        async def metadata_only():
            yield {"type": "metadata"}

        communicate, attempts = make_sequenced_communicate(
            [metadata_only, metadata_only]
        )
        self.app.asyncio.create_subprocess_exec = fake_create_subprocess_exec
        self.app.edge_tts.Communicate = communicate

        with self.assertRaisesRegex(RuntimeError, "no audio"):
            await self.app.synth_edge(
                "hello", "voice", 1.0, asyncio.Queue(),
                FakeWebSocket(), asyncio.Event(),
            )

        self.assertEqual(len(attempts), 2)
        self.assertEqual(proc.stdin.written(), b"")
        self.assertTrue(proc.waited)

    async def test_synth_edge_respects_ffmpeg_process_limit(self):
        class ExhaustedLimiter:
            async def acquire(self, prefetch=False):
                return False

            def release(self, prefetch=False):
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

    async def test_prefetch_lease_type_flows_through_create_and_reap(self):
        events = []

        class RecordingLimiter:
            async def acquire(self, prefetch=False):
                events.append(("acquire", prefetch))
                return True

            def release(self, prefetch=False):
                events.append(("release", prefetch))

        proc = FakeProc(stdout=ScriptedStdout([]))

        async def fake_create_subprocess_exec(*args, **kwargs):
            return proc

        self.app._ffmpeg_limiter = RecordingLimiter()
        self.app.asyncio.create_subprocess_exec = fake_create_subprocess_exec
        self.app.edge_tts.Communicate = make_communicate(AudioEdgeStream([b"mp3"]))

        await self.app.synth_edge(
            "hello", "voice", 1.0, asyncio.Queue(), FakeWebSocket(), asyncio.Event(),
            prefetch=True,
        )

        self.assertEqual(events, [("acquire", True), ("release", True)])
        self.assertTrue(proc.waited)

    async def test_prefetch_spawn_cancellation_releases_same_lease_type(self):
        events = []

        class RecordingLimiter:
            async def acquire(self, prefetch=False):
                events.append(("acquire", prefetch))
                return True

            def release(self, prefetch=False):
                events.append(("release", prefetch))

        async def cancelled_create_subprocess_exec(*args, **kwargs):
            raise asyncio.CancelledError

        self.app._ffmpeg_limiter = RecordingLimiter()
        self.app.asyncio.create_subprocess_exec = cancelled_create_subprocess_exec

        with self.assertRaises(asyncio.CancelledError):
            await self.app._create_edge_pcm_decoder(prefetch=True)

        self.assertEqual(events, [("acquire", True), ("release", True)])


if __name__ == "__main__":
    unittest.main()
