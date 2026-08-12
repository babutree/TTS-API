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

        async def fake_run_kokoro(
            text, voice, speed, cancel_event=None, permit=None
        ):
            return b"PCM" if text == "hello." else b""

        self.app.run_kokoro = fake_run_kokoro
        await self.app._feed_mp3(proc, "hello. skipped", "kokoro", "af_heart", 1.0, first_audio)

        self.assertTrue(first_audio.is_set())
        self.assertEqual(proc.stdin.written(), b"PCM")
        self.assertTrue(proc.stdin.closed)

    async def test_kokoro_feed_bounds_long_unit_and_releases_permit_once(self):
        cap = 2000
        original = "a" * (cap * 2 + 1)
        proc = FakeProc()
        calls = []

        class RecordingPermit:
            def __init__(self):
                self.release_count = 0

            def release(self):
                self.release_count += 1

        permit = RecordingPermit()

        async def fake_run_kokoro(
            text, voice, speed, cancel_event=None, permit=None
        ):
            calls.append(text)
            return text.encode("ascii")

        self.app.run_kokoro = fake_run_kokoro
        await self.app._feed_mp3(
            proc,
            original,
            "kokoro",
            "af_heart",
            1.0,
            kokoro_permit=permit,
        )

        self.assertGreater(len(calls), 1)
        self.assertTrue(all(0 < len(fragment) <= cap for fragment in calls))
        self.assertEqual("".join(calls), original)
        self.assertEqual(proc.stdin.written(), original.encode("ascii"))
        self.assertEqual(permit.release_count, 1)
        self.assertTrue(proc.stdin.closed)

    async def test_kokoro_fragment_failure_releases_permit_once(self):
        proc = FakeProc()

        class RecordingPermit:
            def __init__(self):
                self.release_count = 0

            def release(self):
                self.release_count += 1

        permit = RecordingPermit()
        calls = 0

        async def fake_run_kokoro(
            text, voice, speed, cancel_event=None, permit=None
        ):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("fragment failed")
            return b"PCM"

        self.app.run_kokoro = fake_run_kokoro
        with self.assertRaisesRegex(RuntimeError, "fragment failed"):
            await self.app._feed_mp3(
                proc,
                "a" * 2001,
                "kokoro",
                "af_heart",
                1.0,
                kokoro_permit=permit,
            )

        self.assertEqual(calls, 2)
        self.assertEqual(permit.release_count, 1)
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

        with self.assertRaisesRegex(
            self.app._EncoderInputError, "audio encoder input failed"
        ) as ctx:
            await self.app._feed_mp3(
                proc, "hello", "edge", "voice", 1.0, first_audio
            )

        self.assertIsInstance(ctx.exception.__cause__, BrokenPipeError)
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

    async def test_stream_owner_cancellation_while_read_pending_releases_once(self):
        limiter = self.app.FfmpegLimiter(1)
        self.app._ffmpeg_limiter = limiter
        self.assertTrue(await limiter.acquire())
        proc = FakeProc(stdout=HangingStdout())
        feed_task = asyncio.create_task(asyncio.Event().wait())
        agen = self.app._stream_mp3(
            proc, feed_task, "edge", "en-US-AvaNeural"
        )
        owner = asyncio.create_task(agen.__anext__())
        await asyncio.sleep(0)

        owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owner

        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)
        self.assertTrue(feed_task.cancelled())
        self.assertEqual(limiter.active, 0)

    async def test_stream_owner_cancellation_survives_kill_exit_race(self):
        class ExitRaceProc(FakeProc):
            def kill(self):
                self.killed = True
                self.returncode = 0
                raise ProcessLookupError("process exited before kill")

        proc = ExitRaceProc(stdout=HangingStdout())
        feed_task = asyncio.create_task(asyncio.Event().wait())
        agen = self.app._stream_mp3(
            proc, feed_task, "edge", "en-US-AvaNeural"
        )
        owner = asyncio.create_task(agen.__anext__())
        await asyncio.sleep(0)

        owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owner

        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)
        self.assertTrue(feed_task.cancelled())

    async def test_stream_owner_cancellation_survives_kill_oserror(self):
        class PermissionProc(FakeProc):
            def kill(self):
                self.killed = True
                raise PermissionError("terminate denied")

        proc = PermissionProc(stdout=HangingStdout())
        feed_task = asyncio.create_task(asyncio.Event().wait())
        agen = self.app._stream_mp3(
            proc, feed_task, "edge", "en-US-AvaNeural"
        )
        owner = asyncio.create_task(agen.__anext__())
        await asyncio.sleep(0)

        owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owner

        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)
        self.assertTrue(feed_task.cancelled())

    async def test_stream_timeout_cancels_feed_and_reaps_proc(self):
        self.app.TTS_SYNTHESIS_TIMEOUT_SECONDS = 0.01
        proc = FakeProc(stdout=HangingStdout())
        feed_task = asyncio.create_task(asyncio.Event().wait())
        chunks = []

        with self.assertRaises(self.app._PostStreamSynthesisError):
            async for chunk in self.app._stream_mp3(
                proc, feed_task, "kokoro", "af_heart"
            ):
                chunks.append(chunk)

        self.assertEqual(chunks, [])
        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)
        self.assertTrue(feed_task.cancelled())

    async def test_stream_timeout_survives_kill_exit_race(self):
        class ExitRaceProc(FakeProc):
            def kill(self):
                self.killed = True
                self.returncode = 0
                raise ProcessLookupError("process exited before kill")

        self.app.TTS_SYNTHESIS_TIMEOUT_SECONDS = 0.01
        proc = ExitRaceProc(stdout=HangingStdout())
        feed_task = asyncio.create_task(asyncio.Event().wait())

        with self.assertRaisesRegex(
            self.app._PostStreamSynthesisError, "timed out"
        ):
            async for _ in self.app._stream_mp3(
                proc, feed_task, "edge", "en-US-AvaNeural"
            ):
                pass

        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)
        self.assertTrue(feed_task.cancelled())

    async def test_stream_timeout_survives_kill_oserror(self):
        class PermissionProc(FakeProc):
            def kill(self):
                self.killed = True
                raise PermissionError("terminate denied")

        self.app.TTS_SYNTHESIS_TIMEOUT_SECONDS = 0.01
        proc = PermissionProc(stdout=HangingStdout())
        feed_task = asyncio.create_task(asyncio.Event().wait())

        with self.assertRaisesRegex(
            self.app._PostStreamSynthesisError, "timed out"
        ):
            async for _ in self.app._stream_mp3(
                proc, feed_task, "edge", "en-US-AvaNeural"
            ):
                pass

        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)
        self.assertTrue(feed_task.cancelled())

    async def test_stream_late_feed_failure_aborts_after_emitted_chunk(self):
        proc = FakeProc(stdout=ScriptedStdout([b"one"]))
        fail_now = asyncio.Event()

        async def fail_feed():
            await fail_now.wait()
            raise RuntimeError("edge failed after stream start")

        feed_task = asyncio.create_task(fail_feed())
        agen = self.app._stream_mp3(
            proc, feed_task, "edge", "en-US-AvaNeural"
        )

        self.assertEqual(await agen.__anext__(), b"one")
        fail_now.set()
        await asyncio.sleep(0)
        with self.assertRaises(self.app._PostStreamSynthesisError):
            await agen.__anext__()

        self.assertTrue(proc.waited)

    async def test_stream_late_feed_failure_interrupts_silent_encoder(self):
        limiter = self.app.FfmpegLimiter(1)
        self.app._ffmpeg_limiter = limiter
        self.assertTrue(await limiter.acquire())
        proc = FakeProc(
            stdout=self.app._PrefetchedStreamReader(HangingStdout(), b"one")
        )
        fail_now = asyncio.Event()

        async def fail_feed():
            await fail_now.wait()
            raise RuntimeError("edge failed while encoder stayed silent")

        feed_task = asyncio.create_task(fail_feed())
        agen = self.app._stream_mp3(
            proc, feed_task, "edge", "en-US-AvaNeural"
        )

        self.assertEqual(await agen.__anext__(), b"one")
        fail_now.set()
        await asyncio.sleep(0)
        with self.assertRaises(self.app._PostStreamSynthesisError):
            await asyncio.wait_for(agen.__anext__(), 0.05)

        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)
        self.assertEqual(limiter.active, 0)

    async def test_stream_same_tick_feed_failure_wins_over_new_chunk(self):
        gate = asyncio.Event()

        class SameTickStdout:
            async def read(self, _size):
                await gate.wait()
                return b"LATE-BYTES"

        async def fail_feed():
            await gate.wait()
            raise RuntimeError("same-tick feed failure")

        proc = FakeProc(stdout=SameTickStdout())
        feed_task = asyncio.create_task(fail_feed())
        agen = self.app._stream_mp3(
            proc, feed_task, "edge", "en-US-AvaNeural"
        )
        next_chunk = asyncio.create_task(agen.__anext__())
        await asyncio.sleep(0)

        gate.set()
        with self.assertRaises(self.app._PostStreamSynthesisError):
            await next_chunk

        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)

    async def test_stream_feed_cancellation_interrupts_silent_encoder(self):
        limiter = self.app.FfmpegLimiter(1)
        self.app._ffmpeg_limiter = limiter
        self.assertTrue(await limiter.acquire())
        proc = FakeProc(
            stdout=self.app._PrefetchedStreamReader(HangingStdout(), b"one")
        )
        feed_task = asyncio.create_task(asyncio.Event().wait())
        agen = self.app._stream_mp3(
            proc, feed_task, "edge", "en-US-AvaNeural"
        )

        self.assertEqual(await agen.__anext__(), b"one")
        feed_task.cancel()
        await asyncio.gather(feed_task, return_exceptions=True)
        with self.assertRaises(self.app._PostStreamSynthesisError):
            await asyncio.wait_for(agen.__anext__(), 0.05)

        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)
        self.assertEqual(limiter.active, 0)

    async def test_stream_normal_feed_completion_keeps_waiting_for_encoder(self):
        self.app.TTS_SYNTHESIS_TIMEOUT_SECONDS = 0.01
        proc = FakeProc(stdout=HangingStdout())
        feed_task = asyncio.create_task(asyncio.sleep(0))
        await feed_task

        with self.assertRaisesRegex(
            self.app._PostStreamSynthesisError, "timed out"
        ):
            async for _ in self.app._stream_mp3(
                proc, feed_task, "edge", "en-US-AvaNeural"
            ):
                pass

        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)

    async def test_stream_eof_while_feed_pending_aborts_and_cancels_feed(self):
        proc = FakeProc(stdout=ScriptedStdout([b"one"]))
        feed_task = asyncio.create_task(asyncio.Event().wait())
        chunks = []

        with self.assertRaises(self.app._PostStreamSynthesisError):
            async for chunk in self.app._stream_mp3(
                proc, feed_task, "edge", "en-US-AvaNeural"
            ):
                chunks.append(chunk)

        self.assertEqual(chunks, [b"one"])
        self.assertTrue(feed_task.cancelled())
        self.assertTrue(proc.waited)

    async def test_stream_known_nonzero_exit_wins_over_ready_chunk(self):
        proc = FakeProc(stdout=ScriptedStdout([b"one"]))
        proc.returncode = 1
        feed_task = asyncio.create_task(asyncio.sleep(0))
        await feed_task
        chunks = []

        with self.assertRaises(self.app._PostStreamSynthesisError):
            async for chunk in self.app._stream_mp3(
                proc, feed_task, "edge", "en-US-AvaNeural"
            ):
                chunks.append(chunk)

        self.assertEqual(chunks, [])
        self.assertTrue(proc.waited)

    async def test_stream_waits_for_delayed_nonzero_exit_status_at_eof(self):
        class DelayedNonzeroProc(FakeProc):
            def kill(self):
                self.killed = True
                raise ProcessLookupError()

            async def wait(self):
                self.waited = True
                self.returncode = 7
                return self.returncode

        proc = DelayedNonzeroProc(stdout=ScriptedStdout([b"one"]))
        feed_task = asyncio.create_task(asyncio.sleep(0))
        await feed_task
        chunks = []

        with self.assertRaises(self.app._PostStreamSynthesisError):
            async for chunk in self.app._stream_mp3(
                proc, feed_task, "edge", "en-US-AvaNeural"
            ):
                chunks.append(chunk)

        self.assertEqual(chunks, [b"one"])
        self.assertTrue(proc.waited)

    async def test_streaming_response_feed_failure_before_start_returns_502(self):
        proc = FakeProc(stdout=ScriptedStdout([b"one"]))

        async def fail_feed():
            raise RuntimeError("late upstream failure")

        feed_task = asyncio.create_task(fail_feed())
        await asyncio.sleep(0)
        response = self.app._Mp3StreamingResponse(
            proc,
            feed_task,
            "edge",
            "en-US-AvaNeural",
            media_type="audio/mpeg",
        )
        sent = []

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "path": "/v1/audio/speech",
            "headers": [],
        }

        await response(scope, receive, send)

        starts = [m for m in sent if m["type"] == "http.response.start"]
        bodies = [m for m in sent if m["type"] == "http.response.body"]
        self.assertEqual([m["status"] for m in starts], [502])
        self.assertNotIn(b"one", b"".join(m.get("body", b"") for m in bodies))
        self.assertIn(
            b"upstream_error",
            b"".join(m.get("body", b"") for m in bodies),
        )
        self.assertTrue(proc.waited)

    async def test_streaming_response_known_encoder_exit_before_start_returns_500(self):
        for returncode in (0, 1):
            with self.subTest(returncode=returncode):
                proc = FakeProc(stdout=ScriptedStdout([b"one"]))
                proc.returncode = returncode
                feed_task = asyncio.create_task(asyncio.Event().wait())
                response = self.app._Mp3StreamingResponse(
                    proc,
                    feed_task,
                    "edge",
                    "en-US-AvaNeural",
                    media_type="audio/mpeg",
                )
                sent = []

                async def receive():
                    return {"type": "http.disconnect"}

                async def send(message):
                    sent.append(message)

                scope = {
                    "type": "http",
                    "asgi": {"version": "3.0", "spec_version": "2.4"},
                    "http_version": "1.1",
                    "method": "POST",
                    "path": "/v1/audio/speech",
                    "headers": [],
                }

                await response(scope, receive, send)

                starts = [
                    m for m in sent if m["type"] == "http.response.start"
                ]
                bodies = [
                    m for m in sent if m["type"] == "http.response.body"
                ]
                self.assertEqual([m["status"] for m in starts], [500])
                payload = b"".join(
                    m.get("body", b"") for m in bodies
                )
                self.assertNotIn(b"one", payload)
                self.assertIn(b"synthesis_failed", payload)
                self.assertTrue(feed_task.cancelled())
                self.assertTrue(proc.waited)


class SynthKokoroTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        disable_asyncio_debug()
        self.app = import_app_with_fakes()

    async def test_emits_seg_for_each_unit_and_chunks_pcm(self):
        queue = asyncio.Queue()
        pcm = b"a" * 3000

        async def fake_run_kokoro(
            text, voice, speed, cancel_event=None, permit=None
        ):
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

    async def test_long_unit_uses_bounded_fragments_without_extra_seg(self):
        cap = 2000
        original = "a" * (cap * 2 + 1)
        queue = asyncio.Queue()
        calls = []

        async def fake_run_kokoro(
            text, voice, speed, cancel_event=None, permit=None
        ):
            calls.append(text)
            return text.encode("ascii")

        self.app.run_kokoro = fake_run_kokoro
        await self.app.synth_kokoro(
            [original],
            "af_heart",
            1.0,
            queue,
            FakeWebSocket(),
            asyncio.Event(),
        )

        items = drain_queue(queue)
        segs = [item for item in items if isinstance(item, dict)]
        pcm = b"".join(item for item in items if isinstance(item, bytes))
        self.assertEqual(segs, [{"type": "seg", "text": original}])
        self.assertGreater(len(calls), 1)
        self.assertTrue(all(0 < len(fragment) <= cap for fragment in calls))
        self.assertEqual("".join(calls), original)
        self.assertEqual(pcm, original.encode("ascii"))

    async def test_prefetch_long_unit_holds_one_permit_across_fragments(self):
        self.app.KOKORO_MAX_UNIT_CHARS = 2
        queue = asyncio.Queue()
        acquire_flags = []
        seen_permits = []

        class RecordingPermit:
            def __init__(self):
                self.release_count = 0

            def release(self):
                self.release_count += 1

        permit = RecordingPermit()

        async def fake_acquire(cancel_event=None, *, prefetch=False):
            acquire_flags.append(prefetch)
            return permit

        async def fake_run_kokoro(
            text,
            voice,
            speed,
            cancel_event=None,
            permit=None,
            *,
            prefetch=False,
        ):
            seen_permits.append(permit)
            return text.encode("ascii")

        self.app._acquire_kokoro_permit = fake_acquire
        self.app.run_kokoro = fake_run_kokoro
        await self.app.synth_kokoro(
            ["abcde"],
            "af_heart",
            1.0,
            queue,
            FakeWebSocket(),
            asyncio.Event(),
            prefetch=True,
        )

        self.assertEqual(acquire_flags, [True])
        self.assertEqual(seen_permits, [permit, permit, permit])
        self.assertEqual(permit.release_count, 1)

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

        async def audio_then_fail():
            # R39 后，首音频前失败不再创建 decoder；本例专门覆盖首块已提交后
            # 的上游失败仍会回收已存在 decoder。
            yield {"type": "audio", "data": b"mp3"}
            raise source_error

        self.app.asyncio.create_subprocess_exec = fake_create_subprocess_exec
        self.app.edge_tts.Communicate = make_communicate(audio_then_fail())

        with self.assertRaises(RuntimeError):
            await self.app.synth_edge("hello", "bad", 1.0, asyncio.Queue(), FakeWebSocket(), asyncio.Event())

        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)

    async def test_edge_retry_releases_decoder_lease_before_backoff(self):
        """R39: 首音频前退避不应持续占用空闲 decoder 槽。"""
        proc = FakeProc(stdout=HangingStdout())
        retry_sleep = asyncio.Event()
        limiter = self.app.FfmpegLimiter(1)
        real_sleep = self.app.asyncio.sleep
        self.app._ffmpeg_limiter = limiter
        self.app.EDGE_RETRY_MAX_ATTEMPTS = 2

        async def fake_create_subprocess_exec(*_args, **_kwargs):
            return proc

        async def blocked_sleep(_delay):
            retry_sleep.set()
            await asyncio.Event().wait()

        self.app.asyncio.create_subprocess_exec = fake_create_subprocess_exec
        self.app.asyncio.sleep = blocked_sleep
        self.app.edge_tts.Communicate = make_communicate(
            FailingEdgeStream(RuntimeError("retry me"))
        )
        task = asyncio.create_task(
            self.app.synth_edge(
                "hello", "voice", 1.0, asyncio.Queue(),
                FakeWebSocket(), asyncio.Event(),
            )
        )
        try:
            await asyncio.wait_for(retry_sleep.wait(), timeout=0.2)
            self.assertEqual(
                limiter.active,
                0,
                "首音频前退避期间应释放 decoder lease",
            )
        finally:
            self.app.asyncio.sleep = real_sleep
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_synth_edge_cancel_before_first_audio_does_not_claim_decoder(self):
        proc = FakeProc(stdout=HangingStdout())
        upstream_started = asyncio.Event()
        upstream_cancelled = asyncio.Event()
        cancel_event = asyncio.Event()
        decoder_created = False

        async def fake_create_subprocess_exec(*args, **kwargs):
            nonlocal decoder_created
            decoder_created = True
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
        self.assertFalse(decoder_created)

    async def test_synth_edge_decoder_eof_cancels_pending_feed_and_raises(self):
        proc = FakeProc(stdout=ScriptedStdout([]))
        upstream_started = asyncio.Event()
        upstream_cancelled = asyncio.Event()

        async def fake_create_subprocess_exec(*args, **kwargs):
            return proc

        async def blocked_upstream():
            yield {"type": "audio", "data": b"mp3"}
            upstream_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                upstream_cancelled.set()
            yield

        self.app.asyncio.create_subprocess_exec = fake_create_subprocess_exec
        self.app.edge_tts.Communicate = make_communicate(blocked_upstream())

        with self.assertRaisesRegex(
            RuntimeError,
            "Edge decoder ended before upstream feed completed",
        ):
            await asyncio.wait_for(
                self.app.synth_edge(
                    "hello",
                    "voice",
                    1.0,
                    asyncio.Queue(),
                    FakeWebSocket(),
                    asyncio.Event(),
                ),
                timeout=0.1,
            )

        self.assertTrue(upstream_started.is_set())
        self.assertTrue(upstream_cancelled.is_set())
        self.assertTrue(proc.stdin.closed)
        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)

    async def test_synth_edge_completed_feed_with_zero_pcm_raises(self):
        proc = FakeProc()

        class EofAfterFeedStdout:
            async def read(self, size):
                while not proc.stdin.closed:
                    await asyncio.sleep(0)
                await asyncio.sleep(0)
                return b""

        proc.stdout = EofAfterFeedStdout()

        async def fake_create_subprocess_exec(*args, **kwargs):
            return proc

        self.app.asyncio.create_subprocess_exec = fake_create_subprocess_exec
        self.app.edge_tts.Communicate = make_communicate(
            AudioEdgeStream([b"mp3"])
        )
        queue = asyncio.Queue()

        with self.assertRaisesRegex(
            RuntimeError,
            "Edge decoder produced no PCM output",
        ):
            await self.app.synth_edge(
                "hello",
                "voice",
                1.0,
                queue,
                FakeWebSocket(),
                asyncio.Event(),
            )

        self.assertEqual(
            drain_queue(queue),
            [{"type": "seg", "text": "hello"}],
        )
        self.assertTrue(proc.waited)

    async def test_synth_edge_known_nonzero_decoder_exit_raises(self):
        proc = FakeProc()

        class FailedAfterPcmStdout:
            def __init__(self):
                self.sent_pcm = False

            async def read(self, size):
                if not self.sent_pcm:
                    self.sent_pcm = True
                    return b"PCM!"
                while not proc.stdin.closed:
                    await asyncio.sleep(0)
                proc.returncode = 7
                return b""

        proc.stdout = FailedAfterPcmStdout()

        async def fake_create_subprocess_exec(*args, **kwargs):
            return proc

        self.app.asyncio.create_subprocess_exec = fake_create_subprocess_exec
        self.app.edge_tts.Communicate = make_communicate(
            AudioEdgeStream([b"mp3"])
        )
        queue = asyncio.Queue()

        with self.assertRaisesRegex(
            RuntimeError,
            "Edge decoder exited with status 7",
        ):
            await self.app.synth_edge(
                "hello",
                "voice",
                1.0,
                queue,
                FakeWebSocket(),
                asyncio.Event(),
            )

        self.assertEqual(
            drain_queue(queue),
            [{"type": "seg", "text": "hello"}, b"PCM!"],
        )
        self.assertTrue(proc.waited)

    async def test_synth_edge_delayed_nonzero_decoder_exit_raises(self):
        class DelayedExitProc(FakeProc):
            def kill(self):
                raise ProcessLookupError()

            async def wait(self):
                self.waited = True
                self.returncode = 7
                return self.returncode

        proc = DelayedExitProc()

        class PcmThenEofAfterFeedStdout:
            def __init__(self):
                self.sent_pcm = False

            async def read(self, size):
                if not self.sent_pcm:
                    self.sent_pcm = True
                    return b"PCM!"
                while not proc.stdin.closed:
                    await asyncio.sleep(0)
                return b""

        proc.stdout = PcmThenEofAfterFeedStdout()

        async def fake_create_subprocess_exec(*args, **kwargs):
            return proc

        self.app.asyncio.create_subprocess_exec = fake_create_subprocess_exec
        self.app.edge_tts.Communicate = make_communicate(
            AudioEdgeStream([b"mp3"])
        )
        queue = asyncio.Queue()

        with self.assertRaisesRegex(
            RuntimeError,
            "Edge decoder exited with status 7",
        ):
            await self.app.synth_edge(
                "hello",
                "voice",
                1.0,
                queue,
                FakeWebSocket(),
                asyncio.Event(),
            )

        self.assertEqual(
            drain_queue(queue),
            [{"type": "seg", "text": "hello"}, b"PCM!"],
        )
        self.assertTrue(proc.waited)

    async def test_synth_edge_kill_race_does_not_hide_natural_nonzero_exit(self):
        class NaturalExitDuringKillProc(FakeProc):
            def kill(self):
                # 进程在 returncode 检查后自然退出；平台层 kill 调用可能正常返回，
                # 但没有改变真实退出状态，不能据此把后续非零码归因于清理。
                self.killed = True

            async def wait(self):
                self.waited = True
                self.returncode = 7
                return self.returncode

        proc = NaturalExitDuringKillProc()

        class PcmThenEofAfterFeedStdout:
            def __init__(self):
                self.sent_pcm = False

            async def read(self, size):
                if not self.sent_pcm:
                    self.sent_pcm = True
                    return b"PCM!"
                while not proc.stdin.closed:
                    await asyncio.sleep(0)
                return b""

        proc.stdout = PcmThenEofAfterFeedStdout()

        async def fake_create_subprocess_exec(*args, **kwargs):
            return proc

        self.app.asyncio.create_subprocess_exec = fake_create_subprocess_exec
        self.app.edge_tts.Communicate = make_communicate(
            AudioEdgeStream([b"mp3"])
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "Edge decoder exited with status 7",
        ):
            await self.app.synth_edge(
                "hello",
                "voice",
                1.0,
                asyncio.Queue(),
                FakeWebSocket(),
                asyncio.Event(),
            )

        self.assertTrue(proc.waited)

    async def test_synth_edge_reports_decoder_that_stalls_after_pcm_eof(self):
        class StalledExitProc(FakeProc):
            async def wait(self):
                self.waited = True
                while self.returncode is None:
                    await asyncio.sleep(0)
                return self.returncode

        proc = StalledExitProc()

        class PcmThenEofAfterFeedStdout:
            def __init__(self):
                self.sent_pcm = False

            async def read(self, size):
                if not self.sent_pcm:
                    self.sent_pcm = True
                    return b"PCM!"
                while not proc.stdin.closed:
                    await asyncio.sleep(0)
                return b""

        proc.stdout = PcmThenEofAfterFeedStdout()

        async def fake_create_subprocess_exec(*args, **kwargs):
            return proc

        self.app.EDGE_DECODER_EXIT_GRACE_SECONDS = 0.01
        self.app.asyncio.create_subprocess_exec = fake_create_subprocess_exec
        self.app.edge_tts.Communicate = make_communicate(
            AudioEdgeStream([b"mp3"])
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "Edge decoder did not exit after PCM output ended",
        ):
            await asyncio.wait_for(
                self.app.synth_edge(
                    "hello",
                    "voice",
                    1.0,
                    asyncio.Queue(),
                    FakeWebSocket(),
                    asyncio.Event(),
                ),
                timeout=0.2,
            )

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

    async def test_synth_edge_cancel_during_segment_enqueue_reaps_decoder(self):
        """分段标记背压期间取消也必须回收 decoder 并释放配额。"""
        queue_put_started = asyncio.Event()
        releases = []

        class BlockingQueue:
            async def put(self, _item):
                queue_put_started.set()
                await asyncio.Event().wait()

        class RecordingLimiter:
            async def acquire(self, prefetch=False):
                return True

            def release(self, prefetch=False):
                releases.append(prefetch)

        class ClosableStream:
            def __init__(self):
                self.close_count = 0

            async def aclose(self):
                self.close_count += 1

        edge_stream = ClosableStream()

        proc = FakeProc()

        async def fake_prepare(*args, **kwargs):
            return edge_stream, b"first"

        async def fake_create(*args, **kwargs):
            await self.app._ffmpeg_limiter.acquire(
                prefetch=kwargs.get("prefetch", False)
            )
            return proc

        self.app._ffmpeg_limiter = RecordingLimiter()
        self.app._prepare_edge_audio = fake_prepare
        self.app._create_edge_pcm_decoder = fake_create
        task = asyncio.create_task(
            self.app.synth_edge(
                "hello",
                "voice",
                1.0,
                BlockingQueue(),
                FakeWebSocket(),
                asyncio.Event(),
                prefetch=True,
            )
        )

        await asyncio.wait_for(queue_put_started.wait(), timeout=1.0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)

        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)
        self.assertEqual(releases, [True])
        self.assertEqual(edge_stream.close_count, 1)

    async def test_synth_edge_preserves_queue_error_when_stream_close_also_fails(self):
        queue_error = RuntimeError("queue failed")
        close_error = RuntimeError("close failed")

        class FailingQueue:
            async def put(self, _item):
                raise queue_error

        class FailingCloseStream:
            async def aclose(self):
                raise close_error

        releases = []

        class RecordingLimiter:
            async def acquire(self, prefetch=False):
                return True

            def release(self, prefetch=False):
                releases.append(prefetch)

        proc = FakeProc()
        edge_stream = FailingCloseStream()

        async def fake_prepare(*args, **kwargs):
            return edge_stream, b"first"

        async def fake_create(*args, **kwargs):
            await self.app._ffmpeg_limiter.acquire(
                prefetch=kwargs.get("prefetch", False)
            )
            return proc

        self.app._ffmpeg_limiter = RecordingLimiter()
        self.app._prepare_edge_audio = fake_prepare
        self.app._create_edge_pcm_decoder = fake_create

        with self.assertRaisesRegex(RuntimeError, "queue failed") as caught:
            await self.app.synth_edge(
                "hello",
                "voice",
                1.0,
                FailingQueue(),
                FakeWebSocket(),
                asyncio.Event(),
                prefetch=True,
            )

        self.assertIs(caught.exception, queue_error)
        self.assertIs(caught.exception.__cause__, close_error)
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

    async def test_synth_edge_empty_stream_exhaustion_skips_decoder(self):
        proc = FakeProc(stdout=ScriptedStdout([]))
        self.app.EDGE_RETRY_MAX_ATTEMPTS = 2
        self.app.EDGE_RETRY_BASE_DELAY_SECONDS = 0

        async def fake_create_subprocess_exec(*args, **kwargs):
            raise AssertionError("decoder must not start before first Edge audio")

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
        self.assertFalse(proc.waited)

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

        proc = FakeProc(stdout=ScriptedStdout([b"PCM!"]))

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
