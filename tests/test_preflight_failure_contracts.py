# -*- coding: utf-8 -*-
"""REST 预检故障链回归锁：_start_synthesis 异常分支的资源配对与错误映射。

假设来源：分支覆盖率测量(基线 90%)显示 _start_synthesis 的 Timeout/HTTPException/
CancelledError/BaseException 清理分支、_await_with_deadline 过期分支、
_dispose_synthesis_task 的异常消费分支与 disconnect 后丢弃 session 分支均未执行。
这些路径任何一处配对失误都会泄漏 Kokoro 信号量槽或 ffmpeg 配额(默认仅 2 槽)。
每个测试即一条可证伪假设：当前行为应成立；若回归(清理缺失/错误码漂移)则失败。
"""
import asyncio
import unittest

from _support import FakeProc, import_app_with_fakes


class RecordingPermit:
    """记录 release 次数的假 Kokoro 许可。"""

    def __init__(self):
        self.releases = 0

    def release(self):
        self.releases += 1


class ClosableStream:
    """可观测 aclose 的假 Edge 流；不迭代即挂起，仅用于持有资源。"""

    def __init__(self):
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


class ClosableAwaitable:
    """带 close 的最简 awaitable，验证 deadline 已过时 close+Timeout。"""

    def __init__(self):
        self.closed = False

    def __await__(self):
        if False:
            yield
        return "ok"

    def close(self):
        self.closed = True


class HangingEncoder:
    async def __call__(self, *args, **kwargs):
        await asyncio.Event().wait()


class PreflightFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = import_app_with_fakes()
        self.app.logger.disabled = True
        self.app.TTS_SYNTHESIS_TIMEOUT_SECONDS = 0.05

    async def asyncTearDown(self):
        self.app.logger.disabled = False

    async def _run_and_expect_http(self, coro, status):
        with self.assertRaises(self.app.HTTPException) as ctx:
            await coro
        self.assertEqual(ctx.exception.status_code, status)
        return ctx.exception

    async def test_kokoro_timeout_returns_504_and_releases_permit(self):
        permit = RecordingPermit()

        async def fake_permit(cancel_event=None, *, prefetch=False):
            return permit

        self.app._acquire_kokoro_permit = fake_permit
        self.app._create_mp3_encoder = HangingEncoder()
        await self._run_and_expect_http(
            self.app._start_synthesis("hi", "kokoro", "af_heart", 1.0, "r1"), 504
        )
        self.assertEqual(permit.releases, 1)

    async def test_edge_timeout_returns_504_and_closes_stream(self):
        stream = ClosableStream()

        async def fake_prepare(text, voice, rate, cancel_event=None):
            return stream, b"au"

        self.app._prepare_edge_audio = fake_prepare
        self.app._create_mp3_encoder = HangingEncoder()
        await self._run_and_expect_http(
            self.app._start_synthesis(
                "hi", "edge", "en-US-AvaNeural", 1.0, "r1"
            ),
            504,
        )
        self.assertTrue(stream.closed)

    async def test_queue_full_maps_to_429(self):
        async def fake_permit(cancel_event=None, *, prefetch=False):
            raise self.app._SynthesisQueueFull("full")

        self.app._acquire_kokoro_permit = fake_permit
        await self._run_and_expect_http(
            self.app._start_synthesis("hi", "kokoro", "af_heart", 1.0, "r1"), 429
        )

    async def test_edge_upstream_failure_maps_to_502(self):
        async def fake_prepare(text, voice, rate, cancel_event=None):
            raise RuntimeError("upstream down")

        self.app._prepare_edge_audio = fake_prepare
        await self._run_and_expect_http(
            self.app._start_synthesis(
                "hi", "edge", "en-US-AvaNeural", 1.0, "r1"
            ),
            502,
        )

    async def test_http_exception_replays_and_releases_permit(self):
        permit = RecordingPermit()

        async def fake_permit(cancel_event=None, *, prefetch=False):
            return permit

        self.app._acquire_kokoro_permit = fake_permit

        async def boom(engine):
            raise self.app.HTTPException(status_code=429, detail="ffmpeg limit")

        self.app._create_mp3_encoder = boom
        await self._run_and_expect_http(
            self.app._start_synthesis("hi", "kokoro", "af_heart", 1.0, "r1"), 429
        )
        self.assertEqual(permit.releases, 1)

    async def test_cancelled_error_releases_permit(self):
        permit = RecordingPermit()

        async def fake_permit(cancel_event=None, *, prefetch=False):
            return permit

        self.app._acquire_kokoro_permit = fake_permit
        self.app._create_mp3_encoder = HangingEncoder()
        task = asyncio.create_task(
            self.app._start_synthesis("hi", "kokoro", "af_heart", 1.0, "r1")
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(permit.releases, 1)

    async def test_base_exception_releases_permit_and_reraises(self):
        permit = RecordingPermit()

        async def fake_permit(cancel_event=None, *, prefetch=False):
            return permit

        self.app._acquire_kokoro_permit = fake_permit

        class Fatal(BaseException):
            pass

        async def boom(engine):
            raise Fatal()

        self.app._create_mp3_encoder = boom
        with self.assertRaises(Fatal):
            await self.app._start_synthesis(
                "hi", "kokoro", "af_heart", 1.0, "r1"
            )
        self.assertEqual(permit.releases, 1)

    async def test_expired_deadline_closes_awaitable(self):
        awaitable = ClosableAwaitable()
        loop = asyncio.get_running_loop()
        with self.assertRaises(asyncio.TimeoutError):
            await self.app._await_with_deadline(awaitable, loop.time() - 1)
        self.assertTrue(awaitable.closed)

    async def test_dispose_synthesis_task_consumes_done_exception(self):
        async def failing():
            raise ValueError("late")

        task = asyncio.create_task(failing())
        await asyncio.sleep(0)
        self.assertTrue(task.done())
        # 已完成但带异常的预检任务被丢弃时不得向外抛出(请求已不可继续)。
        await self.app._dispose_synthesis_task(task)

    async def test_disconnect_after_session_discards_session(self):
        proc = FakeProc()

        async def fake_start(*args, **kwargs):
            feed = asyncio.create_task(asyncio.sleep(0))
            await feed
            return proc, feed

        self.app._start_synthesis = fake_start

        async def fake_watch(request, disconnect_seen):
            disconnect_seen.set()
            await asyncio.Event().wait()

        self.app._wait_for_http_disconnect = fake_watch
        with self.assertRaises(self.app._HttpRequestDisconnected):
            await self.app._start_synthesis_for_request(
                None, "hi", "kokoro", "af_heart", 1.0, "r1"
            )

    async def test_dispose_synthesis_task_disposes_delivered_session(self):
        # 目标分支 2518：任务已完成并交付 session 时，回收该 session。
        proc = FakeProc()

        async def delivered():
            feed = asyncio.create_task(asyncio.sleep(0))
            await feed
            return proc, feed

        task = asyncio.create_task(delivered())
        await asyncio.wait_for(task, 2)
        await self.app._dispose_synthesis_task(task)
        self.assertTrue(proc.killed)

    async def test_edge_cancelled_error_closes_stream(self):
        # 目标分支 2299：edge 引擎在编码器创建期间被取消 → 关闭已取得的流。
        stream = ClosableStream()

        async def fake_prepare(text, voice, rate, cancel_event=None):
            return stream, b"au"

        self.app._prepare_edge_audio = fake_prepare
        self.app._create_mp3_encoder = HangingEncoder()
        task = asyncio.create_task(
            self.app._start_synthesis(
                "hi", "edge", "en-US-AvaNeural", 1.0, "r1"
            )
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stream.closed)

    async def test_edge_encoder_exception_closes_stream_and_maps_502(self):
        # 目标分支 2305：edge 引擎编码器创建失败(普通异常) → 关流并映射 502。
        stream = ClosableStream()

        async def fake_prepare(text, voice, rate, cancel_event=None):
            return stream, b"au"

        async def boom(engine):
            raise RuntimeError("spawn failed")

        self.app._prepare_edge_audio = fake_prepare
        self.app._create_mp3_encoder = boom
        await self._run_and_expect_http(
            self.app._start_synthesis(
                "hi", "edge", "en-US-AvaNeural", 1.0, "r1"
            ),
            502,
        )
        self.assertTrue(stream.closed)

    async def test_edge_base_exception_closes_stream_and_reraises(self):
        # 目标分支 2313：edge 引擎 BaseException → 关流并原样传播。
        stream = ClosableStream()

        async def fake_prepare(text, voice, rate, cancel_event=None):
            return stream, b"au"

        class Fatal(BaseException):
            pass

        async def boom(engine):
            raise Fatal()

        self.app._prepare_edge_audio = fake_prepare
        self.app._create_mp3_encoder = boom
        with self.assertRaises(Fatal):
            await self.app._start_synthesis(
                "hi", "edge", "en-US-AvaNeural", 1.0, "r1"
            )
        self.assertTrue(stream.closed)


if __name__ == "__main__":
    unittest.main()
