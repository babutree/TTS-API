# -*- coding: utf-8 -*-
"""Regression guards for the three release-blocking defects fixed in v0.12.1.

P2: Edge feed() must close stdin even when aclose() raises, and must not let
    the close error mask the real upstream failure.
P3: reaping a process that survives kill() must be time-bounded so the ffmpeg
    quota slot is never held forever.
"""
import asyncio
import unittest

from _support import import_app_with_fakes


class FakeStdin:
    def __init__(self):
        self.closed = False

    def write(self, data):
        pass

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class FakeProc:
    def __init__(self, kill_exc=None, never_exits=False, returncode=None):
        self.stdin = FakeStdin()
        self.returncode = returncode
        self.pid = 4242
        self.kill_called = 0
        self._kill_exc = kill_exc
        self._never_exits = never_exits

    def kill(self):
        self.kill_called += 1
        if self._kill_exc is not None:
            raise self._kill_exc
        self.returncode = -9

    async def wait(self):
        if self._never_exits:
            await asyncio.sleep(3600)
        return self.returncode


class BoundedReapTests(unittest.IsolatedAsyncioTestCase):
    """P3: kill() failure must not turn reaping into an unbounded wait."""

    def setUp(self):
        self.app = import_app_with_fakes()
        self.app.logger.disabled = True
        self._original_timeout = self.app.UNKILLABLE_PROC_REAP_TIMEOUT_SECONDS
        self.app.UNKILLABLE_PROC_REAP_TIMEOUT_SECONDS = 0.05
        self.app._ffmpeg_limiter.active = 0
        self.app._ffmpeg_limiter.active_prefetch = 0

    def tearDown(self):
        self.app.UNKILLABLE_PROC_REAP_TIMEOUT_SECONDS = self._original_timeout
        self.app.logger.disabled = False

    async def test_unkillable_edge_decoder_releases_quota_within_bound(self):
        limiter = self.app._ffmpeg_limiter
        limiter.active = 1
        proc = FakeProc(kill_exc=PermissionError("EPERM"), never_exits=True)

        returncode = await asyncio.wait_for(
            self.app._reap_edge_pcm_decoder(proc, prefetch=False), 2.0
        )

        self.assertIsNone(returncode)
        self.assertEqual(limiter.active, 0)

    async def test_unkillable_rest_encoder_releases_quota_within_bound(self):
        limiter = self.app._ffmpeg_limiter
        limiter.active = 1
        proc = FakeProc(kill_exc=PermissionError("EPERM"), never_exits=True)

        await asyncio.wait_for(self.app._reap_proc(proc), 2.0)

        self.assertEqual(limiter.active, 0)

    async def test_normal_kill_path_still_reports_real_returncode(self):
        limiter = self.app._ffmpeg_limiter
        limiter.active = 1
        proc = FakeProc()

        returncode = await self.app._reap_edge_pcm_decoder(proc, prefetch=False)

        self.assertEqual(returncode, -9)
        self.assertEqual(proc.kill_called, 1)
        self.assertEqual(limiter.active, 0)

    async def test_already_exited_process_is_not_killed_again(self):
        limiter = self.app._ffmpeg_limiter
        limiter.active = 1
        proc = FakeProc(returncode=0)

        returncode = await self.app._reap_edge_pcm_decoder(proc, prefetch=False)

        self.assertEqual(returncode, 0)
        self.assertEqual(proc.kill_called, 0)
        self.assertEqual(limiter.active, 0)


class EdgeFeedCloseOrderTests(unittest.IsolatedAsyncioTestCase):
    """P2: a failing aclose() must not skip stdin.close() nor mask the cause."""

    def setUp(self):
        self.app = import_app_with_fakes()
        self.app.logger.disabled = True

    def tearDown(self):
        self.app.logger.disabled = False

    async def test_aclose_failure_keeps_upstream_error_and_closes_stdin(self):
        proc = FakeProc()

        class FailingStream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise RuntimeError("UPSTREAM_EDGE_FAILURE")

            async def aclose(self):
                raise RuntimeError("ACLOSE_FAILURE")

        stream = FailingStream()
        logger = self.app.logger

        async def close_edge_stream():
            await stream.aclose()

        async def feed():
            try:
                async for _ in stream:
                    pass
            finally:
                try:
                    await close_edge_stream()
                except BaseException as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    logger.warning("close failed: %s", exc)
                finally:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass

        with self.assertRaises(RuntimeError) as caught:
            await feed()

        self.assertEqual(str(caught.exception), "UPSTREAM_EDGE_FAILURE")
        self.assertTrue(proc.stdin.closed)

    async def test_cancellation_during_close_still_propagates(self):
        proc = FakeProc()
        logger = self.app.logger

        class CancelOnClose:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            async def aclose(self):
                raise asyncio.CancelledError()

        stream = CancelOnClose()

        async def feed():
            try:
                async for _ in stream:
                    pass
            finally:
                try:
                    await stream.aclose()
                except BaseException as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    logger.warning("close failed: %s", exc)
                finally:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass

        with self.assertRaises(asyncio.CancelledError):
            await feed()

        self.assertTrue(proc.stdin.closed)


class SourceContractTests(unittest.TestCase):
    """Lock the fixed source shape so the ordering cannot silently regress."""

    def setUp(self):
        import pathlib

        self.source = (
            pathlib.Path(__file__).resolve().parents[1] / "app.py"
        ).read_text(encoding="utf-8")

    def test_reap_helpers_use_the_bounded_waiter(self):
        self.assertIn("async def _reap_wait_bounded(", self.source)
        self.assertIn(
            "UNKILLABLE_PROC_REAP_TIMEOUT_SECONDS", self.source
        )
        # both reapers must route through the bounded helper
        self.assertEqual(
            self.source.count('_reap_wait_bounded(proc, kill_failed, "REST encoder")'),
            1,
        )
        self.assertIn('_reap_wait_bounded(\n            process, kill_failed, "Edge decoder"\n        )', self.source)

    def test_docs_do_not_reference_untracked_audit_file(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        for name in ("README.md", "README_CN.md", "API.md", "CHANGELOG.md"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertNotIn(
                "OPENAI_AGENT_TTS_COMPATIBILITY_AUDIT.md",
                text,
                f"{name} links a file that is gitignored and absent from a clean checkout",
            )


if __name__ == "__main__":
    unittest.main()
