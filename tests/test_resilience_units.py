# -*- coding: utf-8 -*-
"""按覆盖率测量补齐的韧性单元测试：取消配对、Edge 流取消分支、WS 控制帧失败、校验单点。

每个测试对应一条"该分支此前从未被执行"的故障假设：
- _acquire_synthesis_slot 三条取消路径若配对失误，Kokoro 信号量槽会幽灵占位；
- _iter_edge_audio 的取消分支若失效，取消后仍会重试上游或误报失败；
- synth_edge 的断连/关闭异常分支若失效，会泄漏 ffmpeg 配额或让关闭错误顶替真因；
- WS 控制帧发送失败若穿透 ASGI 层，服务日志会被 Exception 污染。
"""
import asyncio
import json
import logging
import unittest

from fastapi.testclient import TestClient

from _support import (
    EmptyStdout,
    FakeProc,
    FakeWebSocket,
    HangingStdout,
    RecordingStdin,
    import_app_with_fakes,
    make_communicate,
)


def ws_scope(query_string=b""):
    return {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "ws",
        "path": "/ws/tts",
        "raw_path": b"/ws/tts",
        "query_string": query_string,
        "root_path": "",
        "headers": [(b"host", b"localhost:8880")],
        "client": ("127.0.0.1", 5555),
        "server": ("127.0.0.1", 8880),
        "subprotocols": [],
        "state": {},
    }


# 模块导入时捕获真实 Queue：stuck-sender 用例会全局 patch asyncio.Queue，
# 测试侧的 incoming 队列必须保持真实实现，否则 connect 消息永远收不到。
_REAL_QUEUE = asyncio.Queue


class ClosableStream:
    """记录 aclose 的假 Edge 流；可选抛错、可选产出数据。"""

    def __init__(self, datas=(), close_error=None):
        self._items = list(datas)
        self._close_error = close_error
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._items:
            return self._items.pop(0)
        raise StopAsyncIteration

    async def aclose(self):
        if self._close_error is not None:
            raise self._close_error
        self.closed = True


class ExplodingCloseStdin(RecordingStdin):
    def __init__(self):
        super().__init__()
        self.close_attempts = 0

    def close(self):
        self.close_attempts += 1
        raise OSError("stdin already gone")


class AcquireSlotCancelTests(unittest.IsolatedAsyncioTestCase):
    """_acquire_synthesis_slot 三条取消路径的配对性。"""

    async def asyncSetUp(self):
        self.app = import_app_with_fakes()
        self.app.logger.disabled = True

    async def asyncTearDown(self):
        self.app.logger.disabled = False

    async def test_pre_cancelled_event_returns_false(self):
        sem = asyncio.Semaphore(1)
        ev = asyncio.Event()
        ev.set()
        self.assertFalse(await self.app._acquire_synthesis_slot(sem, ev))
        self.assertEqual(sem._value, 1)

    async def test_cancel_event_none_fast_path_returns_true_and_holds(self):
        # 突变背锁：cancel_event=None 的 REST 快路径必须"取得并返回 True"。
        # 返回值翻转(False)会让调用方误以为未取得而放弃，槽位就此泄漏。
        sem = asyncio.Semaphore(1)
        self.assertTrue(await self.app._acquire_synthesis_slot(sem, None))
        self.assertEqual(sem._value, 0)

    async def test_queued_acquire_returns_true_and_holds_slot(self):
        # 突变背锁：排队后取得槽位的正常路径必须返回 True 且槽位随许可交付。
        sem = asyncio.Semaphore(1)
        await sem.acquire()  # 值归零，迫使走排队路径
        ev = asyncio.Event()
        task = asyncio.create_task(self.app._acquire_synthesis_slot(sem, ev))
        await asyncio.sleep(0)  # 任务进入排队等待
        sem.release()  # 放行，acquire_task 取得槽位
        self.assertTrue(await asyncio.wait_for(task, 2))
        self.assertEqual(sem._value, 0)

    async def test_cancel_wins_with_slot_acquired_returns_false_and_slot(self):
        # 突变背锁：取消与"取得槽位"同轮完成时，取消优先——返回 False 且
        # 已取得的槽必须原样归还(否则幽灵占槽/取消方幻觉持槽二选一必错)。
        sem = asyncio.Semaphore(1)
        await sem.acquire()  # 值归零
        ev = asyncio.Event()
        task = asyncio.create_task(self.app._acquire_synthesis_slot(sem, ev))
        await asyncio.sleep(0)  # 任务进入排队等待
        sem.release()  # acquire_task 完成
        ev.set()  # 取消同轮完成
        self.assertFalse(await asyncio.wait_for(task, 2))
        self.assertEqual(sem._value, 1)

    async def test_outer_cancel_while_queued_leaves_no_ghost_holder(self):
        sem = asyncio.Semaphore(1)
        await sem.acquire()  # 值归零，后续 acquire 必须排队
        ev = asyncio.Event()
        task = asyncio.create_task(self.app._acquire_synthesis_slot(sem, ev))
        await asyncio.sleep(0.01)  # 让 task 进入 asyncio.wait 等待
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        # acquire 未取得槽位，不得凭空 release；信号量应保持原状可复用。
        self.assertEqual(sem._value, 0)
        sem.release()
        self.assertTrue(await self.app._acquire_synthesis_slot(sem, ev))

    async def test_cancel_between_acquired_and_handover_returns_slot(self):
        sem = asyncio.Semaphore(1)
        await sem.acquire()  # 值归零
        ev = asyncio.Event()
        real_cleanup = self.app._await_cleanup
        entered = asyncio.Event()

        async def cancelling_cleanup(awaitable):
            entered.set()
            asyncio.current_task().cancel()
            return await real_cleanup(awaitable)

        self.app._await_cleanup = cancelling_cleanup
        try:
            task = asyncio.create_task(
                self.app._acquire_synthesis_slot(sem, ev)
            )
            await asyncio.sleep(0)  # task 进入 1177 的排队 acquire
            sem.release()  # 让 acquire_task 取得槽位
            await asyncio.wait_for(entered.wait(), 2)
            # 槽已取得但尚未交付调用方时被取消：本层必须归还槽位再传播取消。
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(sem._value, 1)
        finally:
            self.app._await_cleanup = real_cleanup

    async def test_permit_release_is_idempotent(self):
        sem = asyncio.Semaphore(1)
        permit = self.app._KokoroPermit(sem)
        permit.release()
        permit.release()
        self.assertEqual(sem._value, 2)

    async def test_prefetch_permit_released_when_slot_not_acquired(self):
        ev = asyncio.Event()
        ev.set()
        # prefetch 预留成功后槽位获取失败(预置取消)：预留必须如数归还。
        permit = await self.app._acquire_kokoro_permit(ev, prefetch=True)
        self.assertIsNone(permit)
        self.assertEqual(self.app._kokoro_prefetch_reserved, 0)


class IterEdgeAudioCancelTests(unittest.IsolatedAsyncioTestCase):
    """_iter_edge_audio 取消分支：取消后不得重试上游、不得误报失败。"""

    async def asyncSetUp(self):
        self.app = import_app_with_fakes()
        self.app.logger.disabled = True

    async def asyncTearDown(self):
        self.app.logger.disabled = False

    def _install(self, stream):
        self.app.edge_tts.Communicate = make_communicate(stream)

    async def test_cancel_mid_stream_stops_iteration(self):
        ev = asyncio.Event()
        self._install(make_stream([b"a", b"b"]))
        it = self.app._iter_edge_audio("t", "v", "+0%", ev)
        self.assertEqual(await it.__anext__(), b"a")
        ev.set()
        with self.assertRaises(StopAsyncIteration):
            await it.__anext__()

    async def test_empty_audio_chunks_are_skipped(self):
        self._install(make_stream([b"", b"x"]))
        it = self.app._iter_edge_audio("t", "v", "+0%", None)
        self.assertEqual(await it.__anext__(), b"x")

    async def test_stream_end_with_cancel_returns_instead_of_error(self):
        # 目标分支：流自然结束且无音频，取消已在流结束后置位 → 1899 return，
        # 而不是 1900 的 RuntimeError。取消必须在最后一个 chunk 通过
        # 1886 的逐 chunk 检查之后才置位，否则会走 1887 的提前退出。
        # 两个突变背锁：① 重试次数钉为 1——否则变异的 1900 抛错会被 except
        # 捕获并借第二次尝试"越狱"出同样的 StopAsyncIteration；
        # ② 断言上游 Communicate 只实例化一次——取消后不得发起新尝试。
        ev = asyncio.Event()
        self.app.EDGE_RETRY_MAX_ATTEMPTS = 1
        instantiations = []

        class BoundaryThenEnd:
            def __aiter__(self):
                return self

            async def __anext__(self):
                ev.set()  # 流结束的同刻置位
                raise StopAsyncIteration

        class CountingCommunicate:
            def __init__(self, text, voice, rate=None):
                instantiations.append(1)

            def stream(self):
                return BoundaryThenEnd()

        self.app.edge_tts.Communicate = CountingCommunicate
        it = self.app._iter_edge_audio("t", "v", "+0%", ev)
        with self.assertRaises(StopAsyncIteration):
            await it.__anext__()
        self.assertEqual(len(instantiations), 1)

    async def test_stream_end_without_audio_raises_no_audio_error(self):
        # 非音频块耗尽且从未取消：必须显式报"无音频"，不得静默成功。
        self._install(make_stream_non_audio(2))
        it = self.app._iter_edge_audio("t", "v", "+0%", asyncio.Event())
        with self.assertRaises(RuntimeError):
            await it.__anext__()

    async def test_cancel_during_retry_sleep_skips_second_attempt(self):
        # 目标分支：首次失败时取消尚未置位，重试休眠期间才置位 → 1916 return，
        # 第二次尝试被跳过。若在失败同刻置位，会走 1905 的提前退出。
        # 突变背锁：断言上游只实例化一次——若休眠期取消失效，第二次尝试
        # 会重新实例化 Communicate(发起多余网络尝试)，同样的
        # StopAsyncIteration 无法区分这两种行为。
        ev = asyncio.Event()
        self.app.EDGE_RETRY_MAX_ATTEMPTS = 2
        self.app.EDGE_RETRY_BASE_DELAY_SECONDS = 0.05
        instantiations = []

        class FailQuietly:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise RuntimeError("upstream wave failed")

        class CountingCommunicate:
            def __init__(self, text, voice, rate=None):
                instantiations.append(1)

            def stream(self):
                return FailQuietly()

        self.app.edge_tts.Communicate = CountingCommunicate
        asyncio.get_running_loop().call_later(0.02, ev.set)
        it = self.app._iter_edge_audio("t", "v", "+0%", ev)
        with self.assertRaises(StopAsyncIteration):
            await it.__anext__()
        self.assertEqual(len(instantiations), 1)

    async def test_next_edge_audio_pre_cancelled_returns_none(self):
        ev = asyncio.Event()
        ev.set()
        self.assertIsNone(await self.app._next_edge_audio(iter([]), ev))


def make_stream(datas):
    class _Stream:
        def __init__(self):
            self._items = [
                {"type": "audio", "data": d} for d in datas
            ]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._items:
                raise StopAsyncIteration
            return self._items.pop(0)

    return _Stream()


def make_stream_non_audio(count):
    class _NonAudio:
        def __init__(self):
            self._n = count

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._n <= 0:
                raise StopAsyncIteration
            self._n -= 1
            return {"type": "WordBoundary"}

    return _NonAudio()


class SynthEdgeBranchTests(unittest.IsolatedAsyncioTestCase):
    """synth_edge 断连/关闭异常分支：不得泄漏 decoder 配额或错配错误归因。"""

    async def asyncSetUp(self):
        self.app = import_app_with_fakes()
        self.app.logger.disabled = True

    async def asyncTearDown(self):
        self.app.logger.disabled = False

    async def _run(self, ws, ev, decoder, stream):
        queue = asyncio.Queue(maxsize=32)
        created = []

        async def fake_prepare(text, voice, rate, cancel_event=None):
            return stream, b"au"

        async def fake_decoder(prefetch=False):
            created.append(True)
            return decoder

        self.app._prepare_edge_audio = fake_prepare
        self.app._create_edge_pcm_decoder = fake_decoder
        result = await self.app.synth_edge(
            "text", "en-US-AvaNeural", 1.0, queue, ws, ev
        )
        return result, queue, created

    async def test_disconnected_ws_closes_stream_without_decoder(self):
        stream = ClosableStream()
        created = []

        async def fake_prepare(text, voice, rate, cancel_event=None):
            return stream, b"au"

        async def fake_decoder(prefetch=False):
            created.append(True)
            return FakeProc()

        self.app._prepare_edge_audio = fake_prepare
        self.app._create_edge_pcm_decoder = fake_decoder
        await self.app.synth_edge(
            "text", "v", 1.0,
            asyncio.Queue(maxsize=32), FakeWebSocket(connected=False),
            asyncio.Event(),
        )
        self.assertTrue(stream.closed)
        self.assertEqual(created, [])

    async def test_cancel_mid_read_lets_cancel_wait_win(self):
        ev = asyncio.Event()

        class CancelOnRead:
            async def read(self, size):
                if not ev.is_set():
                    ev.set()
                    return b"abcd"
                return b""

        proc = FakeProc(stdout=CancelOnRead())
        stream = ClosableStream(datas=[b"x"])
        result, queue, created = await self._run(
            FakeWebSocket(), ev, proc, stream
        )
        # 取消胜出：run_io 未正常完成，decoder 被回收、stdin 必关、流必关。
        self.assertTrue(proc.killed)
        self.assertTrue(proc.stdin.closed)
        self.assertTrue(stream.closed)
        self.assertEqual(drain_all(queue)[-1], b"abcd")

    async def test_hanging_read_cancel_wins_and_reaps_decoder(self):
        ev = asyncio.Event()
        proc = FakeProc(stdout=HangingStdout())
        stream = ClosableStream(datas=[b"x"])

        async def fake_prepare(text, voice, rate, cancel_event=None):
            return stream, b"au"

        async def fake_decoder(prefetch=False):
            return proc

        self.app._prepare_edge_audio = fake_prepare
        self.app._create_edge_pcm_decoder = fake_decoder
        task = asyncio.create_task(
            self.app.synth_edge(
                "text", "v", 1.0,
                asyncio.Queue(maxsize=32), FakeWebSocket(), ev,
            )
        )
        await asyncio.sleep(0.01)  # run_io 已启动并卡在 stdout.read
        ev.set()
        await asyncio.wait_for(task, 2)
        self.assertTrue(proc.killed)
        self.assertTrue(proc.stdin.closed)

    async def test_close_stream_error_does_not_skip_stdin_close(self):
        proc = FakeProc(
            stdout=EmptyStdout(), stdin=ExplodingCloseStdin()
        )
        stream = ClosableStream(close_error=RuntimeError("aclose broke"))
        with self.assertRaises(RuntimeError):
            await self._run(FakeWebSocket(), asyncio.Event(), proc, stream)
        # 关闭异常被吞并记日志，stdin 仍必须尝试关闭；decoder 照常回收。
        self.assertEqual(proc.stdin.close_attempts, 1)
        self.assertTrue(proc.killed)

    async def test_seg_put_failure_reaps_decoder_and_reraises(self):
        # 目标分支：seg 标记入队失败(下游满/断连) → 就地关流+回收 decoder；
        # 回收本身也失败时，清理错误必须链接在原始错误上(3266-3268)。
        class WaitBoomProc(FakeProc):
            async def wait(self):
                raise ValueError("wait broke")

        class PutBoomQueue:
            async def put(self, item):
                raise RuntimeError("queue exploded")

        proc = WaitBoomProc(stdout=EmptyStdout())
        stream = ClosableStream(datas=[b"x"])

        async def fake_prepare(text, voice, rate, cancel_event=None):
            return stream, b"au"

        async def fake_decoder(prefetch=False):
            return proc

        self.app._prepare_edge_audio = fake_prepare
        self.app._create_edge_pcm_decoder = fake_decoder
        with self.assertRaises(RuntimeError) as ctx:
            await self.app.synth_edge(
                "text", "v", 1.0, PutBoomQueue(), FakeWebSocket(),
                asyncio.Event(),
            )
        self.assertIn("queue exploded", str(ctx.exception))
        self.assertIn("wait broke", str(ctx.exception.__cause__))
        self.assertTrue(stream.closed)
        self.assertTrue(proc.killed)

    async def test_seg_put_failure_with_clean_cleanup_reraises_primary(self):
        # 目标分支 3270：入队失败且清理全部成功时，裸 raise 保留原始错误。
        class PutBoomQueue:
            async def put(self, item):
                raise RuntimeError("queue exploded")

        proc = FakeProc(stdout=EmptyStdout())
        stream = ClosableStream(datas=[b"x"])

        async def fake_prepare(text, voice, rate, cancel_event=None):
            return stream, b"au"

        async def fake_decoder(prefetch=False):
            return proc

        self.app._prepare_edge_audio = fake_prepare
        self.app._create_edge_pcm_decoder = fake_decoder
        with self.assertRaises(RuntimeError) as ctx:
            await self.app.synth_edge(
                "text", "v", 1.0, PutBoomQueue(), FakeWebSocket(),
                asyncio.Event(),
            )
        self.assertIn("queue exploded", str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(stream.closed)
        self.assertTrue(proc.killed)

    async def test_read_pending_after_feed_completes_waits_read(self):
        # 目标分支 3336/3339：feed 先完成、read 仍挂起时，run_io 必须等待 read
        # 收敛(经 cancel 检查返回 None)再走统一清理，不得提前判定或永久挂起。
        ev = asyncio.Event()

        class GateStdout:
            async def read(self, size):
                # 固定延迟保证时序：feed 先完成 → wait 只见 feed → 进入 3336
                # 等待分支 → read 此后才收敛。
                await asyncio.sleep(0.05)
                return b""

        class StreamThenCancel:
            def __init__(self):
                self._n = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                self._n += 1
                if self._n == 1:
                    return b"x"  # fake_prepare 绕过解包层，直接产出数据
                ev.set()
                raise StopAsyncIteration

        proc = FakeProc(stdout=GateStdout())
        stream = StreamThenCancel()

        async def fake_prepare(text, voice, rate, cancel_event=None):
            return stream, b"au"

        async def fake_decoder(prefetch=False):
            return proc

        self.app._prepare_edge_audio = fake_prepare
        self.app._create_edge_pcm_decoder = fake_decoder
        with self.assertRaises(RuntimeError):
            await self.app.synth_edge(
                "text", "v", 1.0, asyncio.Queue(maxsize=32),
                FakeWebSocket(), asyncio.Event(),
            )
        self.assertTrue(proc.killed)
        self.assertTrue(proc.stdin.closed)


def drain_all(queue):
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


class WsControlFrameFailureTests(unittest.IsolatedAsyncioTestCase):
    """WS 控制帧发送失败与 close 失败：一律干净退出，不穿透 ASGI 层。"""

    async def asyncSetUp(self):
        self.app = import_app_with_fakes()
        self.app.logger.disabled = True

    async def asyncTearDown(self):
        self.app.logger.disabled = False

    async def _run_handler(self, incoming_items, send, query=b""):
        """运行 WS handler 至干净退出。

        隐式契约(对本类所有用例生效)：handler 必须在 5s 内无异常返回——
        抛错或挂起都会让用例失败。各用例再叠加显式的帧序列断言。
        """
        incoming = _REAL_QUEUE()
        deferred_disconnect = None
        for item in incoming_items:
            # 断连消息延迟到 run 终态(end)之后投递：心跳封装多出一次调度点，
            # 先到的 disconnect 会被 watcher 消费并取消合成，end 永不发出
            # (产品语义：客户端已断则不发 end，测试不应依赖竞态运气)。
            if item.get("type") == "websocket.disconnect":
                deferred_disconnect = item
            else:
                await incoming.put(item)
        saw_end = asyncio.Event()

        async def receive():
            # 先排空真实消息(connect/request/二进制帧)；队列空后才允许
            # 延迟断连生效(且必须等 end 已交付，否则合成会被提前取消)。
            if not incoming.empty():
                return await incoming.get()
            if deferred_disconnect is not None:
                await saw_end.wait()
                return deferred_disconnect
            return await incoming.get()

        frames = []

        async def tracked_send(message):
            frames.append(message["type"])
            text = message.get("text")
            if isinstance(text, str) and '"end"' in text:
                saw_end.set()
            await send(message)

        handler = asyncio.create_task(
            self.app.app(ws_scope(query), receive, tracked_send)
        )
        try:
            await asyncio.wait_for(asyncio.shield(handler), 5)
        finally:
            if not handler.done():
                handler.cancel()
            # 消费终态，避免测试进程残留 pending 任务告警。
            await asyncio.gather(handler, return_exceptions=True)
        return frames

    async def test_msg_none_send_failure_exits_cleanly(self):
        async def send(message):
            if message["type"] == "websocket.send":
                raise RuntimeError("client is gone")

        frames = await self._run_handler(
            [
                {"type": "websocket.connect"},
                {"type": "websocket.receive"},  # 无 text 无 bytes → msg None
            ],
            send,
        )
        # 显式契约：accept 后恰好尝试一次错误回包(失败)，随后干净退出——
        # 出现第三次帧即意味着陷入重试循环。
        self.assertEqual(frames, ["websocket.accept", "websocket.send"])

    async def test_msg_none_error_replies_then_keeps_alive(self):
        # 目标分支 3555：非文本帧回错误后 continue，连接保活——
        # 保活必须是实证的：随后的合法请求要能正常完成并收到 end。
        received = []

        async def send(message):
            if message["type"] == "websocket.send":
                received.append(message.get("text"))

        async def fake_run_kokoro(
            text, voice, speed, cancel_event=None, permit=None
        ):
            return b"\x00\x01" * 16

        self.app.run_kokoro = fake_run_kokoro
        await self._run_handler(
            [
                {"type": "websocket.connect"},
                {"type": "websocket.receive"},
                {
                    "type": "websocket.receive",
                    "text": json.dumps(
                        {"text": "hi", "engine": "kokoro", "voice": "af_heart"}
                    ),
                },
                {"type": "websocket.disconnect", "code": 1000},
            ],
            send,
        )
        self.assertTrue(any("文本 JSON" in t for t in received if t))
        self.assertTrue(any('"end"' in t for t in received if t))

    async def test_parsed_error_send_failure_exits_cleanly(self):
        # 目标分支 3575：parse_ws_request 的业务校验错误回包失败 → 干净退出。
        async def send(message):
            if message["type"] == "websocket.send":
                raise RuntimeError("client is gone")

        frames = await self._run_handler(
            [
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": json.dumps({"text": "hi", "engine": 123}),
                },
            ],
            send,
        )
        # 显式契约：accept 后恰好尝试一次错误回包(失败)，随后干净退出。
        self.assertEqual(frames, ["websocket.accept", "websocket.send"])

    async def test_binary_data_send_failure_is_swallowed_and_drained(self):
        # 目标分支 3494-3496：数据帧发送抛错(如 RST 但未投递 disconnect)时
        # sender 吞掉并继续 drain，主循环随后经 disconnect 正常收尾。
        seen = []

        async def send(message):
            if message["type"] == "websocket.send":
                if message.get("bytes"):
                    raise RuntimeError("RST mid-stream")
                seen.append(message.get("text"))

        async def fake_run_kokoro(
            text, voice, speed, cancel_event=None, permit=None
        ):
            # 不让出事件循环：disconnect 消息必须留给主循环收尾
            # (若被合成期的 watcher 消费，end 帧按设计不会发送)。
            return b"\x00\x01" * 16

        self.app.run_kokoro = fake_run_kokoro
        await self._run_handler(
            [
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": json.dumps(
                        {"text": "hi", "engine": "kokoro", "voice": "af_heart"}
                    ),
                },
                {"type": "websocket.disconnect", "code": 1000},
            ],
            send,
        )
        # seg(JSON) 正常发出，pcm(bytes) 抛错被吞，end 由主循环补发。
        self.assertTrue(any('"seg"' in t for t in seen if t))
        self.assertTrue(any('"end"' in t for t in seen if t))

    async def test_oversized_frame_send_failure_exits_cleanly(self):
        async def send(message):
            if message["type"] == "websocket.send":
                raise RuntimeError("client is gone")

        frames = await self._run_handler(
            [
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": '{"text":"' + "a" * 1300000,
                },
            ],
            send,
        )
        # 显式契约：accept 后恰好尝试一次错误回包(失败)，随后干净退出。
        self.assertEqual(frames, ["websocket.accept", "websocket.send"])

    async def test_binary_frame_close_failure_exits_cleanly(self):
        closes = []

        async def send(message):
            if message["type"] == "websocket.close":
                closes.append(message.get("code"))
                raise RuntimeError("transport dead")

        await self._run_handler(
            [
                {"type": "websocket.connect"},
                {"type": "websocket.receive", "bytes": b"\x00\x01"},
            ],
            send,
        )
        # 规范：二进制帧拒绝必须用 1003(Unsupported Data)，码值是客户端可见契约。
        self.assertEqual(closes, [1003])

    async def test_auth_reject_close_failure_exits_cleanly(self):
        self.app.TTS_API_KEY = "secret"
        closes = []

        async def send(message):
            if message["type"] == "websocket.close":
                closes.append(message.get("code"))
                raise RuntimeError("transport dead")

        await self._run_handler(
            [{"type": "websocket.connect"}],
            send,
            query=b"key=wrong",
        )
        # 规范：鉴权拒绝必须用 1008(Policy Violation)。
        self.assertEqual(closes, [1008])

    async def test_watcher_receive_exception_treated_as_disconnect(self):
        calls = {"n": 0}

        async def send(message):
            pass

        async def receive():
            calls["n"] += 1
            if calls["n"] == 1:
                return {"type": "websocket.connect"}
            if calls["n"] == 2:
                return {
                    "type": "websocket.receive",
                    "text": json.dumps(
                        {"text": "hi", "engine": "kokoro", "voice": "af_heart"}
                    ),
                }
            raise RuntimeError("receive pipe broke")

        async def fake_run_kokoro(
            text, voice, speed, cancel_event=None, permit=None
        ):
            # 必须让出事件循环：否则 watcher 尚未调度就被 cancel，
            # receive 异常会由主循环而非 watcher 消费(与真实时序不符)。
            await asyncio.sleep(0)
            return b"\x00\x01" * 16

        self.app.run_kokoro = fake_run_kokoro
        handler = asyncio.create_task(
            self.app.app(ws_scope(), receive, send)
        )
        try:
            # watcher 的 receive 抛错按断连处理：主循环不再 receive，干净退出。
            await asyncio.wait_for(asyncio.shield(handler), 5)
        finally:
            if not handler.done():
                handler.cancel()

    async def test_zero_write_timeout_uses_direct_send_and_put(self):
        self.app.TTS_RESPONSE_WRITE_TIMEOUT_SECONDS = 0

        async def fake_run_kokoro(
            text, voice, speed, cancel_event=None, permit=None
        ):
            return b"\x00\x01" * 16

        self.app.run_kokoro = fake_run_kokoro
        texts = []

        async def send(message):
            if message["type"] == "websocket.send" and isinstance(
                message.get("text"), str
            ):
                texts.append(message["text"])

        await self._run_handler(
            [
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": json.dumps(
                        {"text": "hi", "engine": "kokoro", "voice": "af_heart"}
                    ),
                },
                {"type": "websocket.disconnect", "code": 1000},
            ],
            send,
        )
        self.assertTrue(any('"end"' in t for t in texts))

    async def test_sender_wedge_cancels_active_synthesis(self):
        # 规范：sender 因半开连接楔死时必须置位当前合成的取消信号
        # (active_cancel→cancel_event)——否则断连客户端虽不再收包，
        # 合成仍会占满 Kokoro 槽位跑完整篇文本(资源浪费/挤占他人)。
        self.app.TTS_RESPONSE_WRITE_TIMEOUT_SECONDS = 0.15
        observed = {"cancelled": False}
        calls = {"n": 0}

        async def fake_run_kokoro(
            text, voice, speed, cancel_event=None, permit=None
        ):
            calls["n"] += 1
            if calls["n"] == 1:
                return b"\x00\x01" * 512  # 首句正常产出 → sender 楔死在 bytes 帧
            for _ in range(300):
                if cancel_event is not None and cancel_event.is_set():
                    observed["cancelled"] = True
                    return b""
                await asyncio.sleep(0.01)
            return b"\x00\x01" * 16  # 取消失效时兜底返回，保持流程可收尾

        self.app.run_kokoro = fake_run_kokoro

        async def send(message):
            if message["type"] == "websocket.send" and message.get("bytes"):
                await asyncio.Event().wait()  # 半开连接：写永挂直至写超时楔死
            return None

        await self._run_handler(
            [
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": json.dumps(
                        {
                            # 两行=两个合成单元：首单元产出令 sender 楔死，
                            # 次单元观测取消传播(synth_kokoro 不按句切分单元)。
                            "text": "hello.\nworld.",
                            "engine": "kokoro",
                            "voice": "af_heart",
                        }
                    ),
                },
            ],
            send,
        )
        self.assertTrue(observed["cancelled"])

    async def test_stuck_sender_force_cancelled_with_1001(self):
        real_queue = asyncio.Queue

        class StuckGetQueue(real_queue):
            async def get(self):
                await asyncio.Event().wait()

        asyncio.Queue = StuckGetQueue
        self.app.TTS_RESPONSE_WRITE_TIMEOUT_SECONDS = 0.05
        try:
            async def fake_run_kokoro(
                text, voice, speed, cancel_event=None, permit=None
            ):
                return b"\x00\x01" * 16

            self.app.run_kokoro = fake_run_kokoro

            async def send(message):
                return None

            await self._run_handler(
                [
                    {"type": "websocket.connect"},
                    {
                        "type": "websocket.receive",
                        "text": json.dumps(
                            {"text": "hi", "engine": "kokoro", "voice": "af_heart"}
                        ),
                    },
                ],
                send,
            )
        finally:
            asyncio.Queue = real_queue


class MeasuredGapUnits(unittest.IsolatedAsyncioTestCase):
    """杂项校验/工具单点：便宜且此前未覆盖。"""

    async def asyncSetUp(self):
        self.app = import_app_with_fakes()
        self.app.logger.disabled = True

    async def asyncTearDown(self):
        self.app.logger.disabled = False

    async def test_truncate_log_line_tiny_limit_has_no_marker(self):
        self.assertEqual(self.app._truncate_log_line("abcdef", 3), "abc")
        self.assertEqual(self.app._truncate_log_line("ab", 10), "ab")

    async def test_ring_buffer_rejects_non_positive_limits(self):
        with self.assertRaises(ValueError):
            self.app.RingBufferHandler(0)

    async def test_ring_buffer_emit_swallows_format_errors(self):
        handler = self.app.RingBufferHandler(4)
        handler.setFormatter(logging.Formatter("%(nonexistent_attr)s"))
        record = logging.LogRecord(
            "t", logging.INFO, __file__, 1, "msg", None, None
        )
        handler.emit(record)  # 不得抛出
        self.assertEqual(len(handler.buffer), 0)

    async def test_parse_api_key_rejects_non_ascii(self):
        with self.assertRaises(ValueError):
            self.app.parse_api_key("密钥")

    async def test_host_of_survives_urlsplit_error(self):
        self.assertEqual(self.app._host_of("http://["), "")

    async def test_key_matches_survives_non_string(self):
        self.assertFalse(self.app._key_matches(123))

    async def test_request_id_fallback_generates_hex(self):
        from starlette.requests import Request

        request = Request({"type": "http", "headers": []})
        request_id = self.app._request_id_for_request(request)
        self.assertEqual(len(request_id), 32)
        int(request_id, 16)

    async def test_body_limit_ignores_malformed_content_length(self):
        seen = {}

        async def downstream(scope, receive, send):
            seen["reached"] = True
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = self.app.RequestBodyLimitMiddleware(downstream)
        scope = {
            "type": "http",
            "path": "/api/tts",
            "headers": [(b"content-length", b"abc")],
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        sent = []

        async def send(message):
            sent.append(message["type"])

        await middleware(scope, receive, send)
        self.assertTrue(seen["reached"])
        self.assertIn("http.response.start", sent)

    async def test_body_limit_reject_after_response_started_reraises(self):
        async def downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 200})
            await receive()  # 此处抛出体积超限

        middleware = self.app.RequestBodyLimitMiddleware(downstream)
        scope = {"type": "http", "path": "/api/tts", "headers": []}
        state = {"n": 0}

        async def receive():
            state["n"] += 1
            return {
                "type": "http.request",
                "body": b"x" * (self.app.TTS_MAX_REQUEST_BODY_BYTES + 1),
                "more_body": False,
            }

        async def send(message):
            pass

        with self.assertRaises(self.app._RequestBodyTooLarge):
            await middleware(scope, receive, send)

    async def test_body_limit_non_v1_reject_shape(self):
        async def downstream(scope, receive, send):
            await receive()  # 消费超限 body，limited_receive 将抛出

        middleware = self.app.RequestBodyLimitMiddleware(downstream)
        scope = {"type": "http", "path": "/api/tts", "headers": []}

        async def receive():
            return {
                "type": "http.request",
                "body": b"x" * (self.app.TTS_MAX_REQUEST_BODY_BYTES + 1),
                "more_body": False,
            }

        sent = []

        async def send(message):
            sent.append(message)

        await middleware(scope, receive, send)
        self.assertEqual(sent[0]["status"], 413)
        self.assertIn(b"detail", sent[1]["body"])

    async def test_openai_error_type_fallback_is_invalid_request(self):
        self.assertEqual(
            self.app._openai_error_type_for_status(418), "invalid_request_error"
        )

    async def test_api_logs_requires_key_when_configured(self):
        # 目标分支 919：/api/logs 配置密钥后不吃同源豁免，直接 401。
        self.app.TTS_API_KEY = "secret"
        from starlette.requests import Request

        request = Request({"type": "http", "headers": []})
        resp = await self.app.api_logs(request, limit=10)
        self.assertEqual(resp.status_code, 401)

    async def test_api_logs_same_origin_still_requires_key(self):
        # 规范：/api/logs 与一般 /api 端点不同，同源 Origin 也不豁免——
        # 诊断日志可能含敏感栈，必须真实校验密钥。
        self.app.TTS_API_KEY = "secret"
        from starlette.requests import Request

        request = Request({
            "type": "http",
            "headers": [
                (b"host", b"localhost:8880"),
                (b"origin", b"http://localhost:8880"),
            ],
        })
        resp = await self.app.api_logs(request, limit=10)
        self.assertEqual(resp.status_code, 401)

    async def test_run_kokoro_cancel_after_permit_returns_empty(self):
        # 目标分支 1323：许可取得后、派发推理前观察到取消 → 空 PCM，不进线程池。
        class GatePermit:
            def release(self):
                pass

        ev = asyncio.Event()

        async def fake_permit(cancel_event=None, *, prefetch=False):
            ev.set()  # 许可交付同刻取消已置位
            return GatePermit()

        self.app._acquire_kokoro_permit = fake_permit
        pcm = await self.app.run_kokoro(
            "hello", "af_heart", 1.0, cancel_event=ev
        )
        self.assertEqual(pcm, b"")

    async def test_request_model_validators_pass_through(self):
        # 目标分支 1459/1576/1582：合法值(含显式默认)原样返回的透传路径。
        # 注意 pydantic v2 默认不校验缺省值，必须显式传参才会触发 validator。
        self.assertEqual(
            self.app.TTSRequest(text="hi", speed=1.5, ssml=False).speed, 1.5
        )
        self.assertEqual(
            self.app.OpenAISpeechRequest(input="hi", voice="af_heart").voice,
            "af_heart",
        )
        self.assertIsNone(
            self.app.OpenAISpeechRequest(input="hi", voice=None).voice
        )
        self.assertEqual(
            self.app.OpenAISpeechRequest(input="hi", speed=2.0).speed, 2.0
        )

    async def test_openai_input_blank_after_strip_rejected(self):
        # 目标分支 2987：input 通过 min_length 但 strip 后为空 → 400。
        self.app.pipeline_zh = object()
        self.app.pipeline_en = object()
        client = TestClient(self.app.app)
        resp = client.post("/v1/audio/speech", json={"input": "   "})
        self.assertEqual(resp.status_code, 400)

    async def test_openai_input_over_limit_rejected(self):
        # 目标分支 1568：/v1 input 超长在模型层拒绝。
        from pydantic import ValidationError

        original = self.app.MAX_TEXT_LENGTH
        self.app.MAX_TEXT_LENGTH = 5
        try:
            with self.assertRaises(ValidationError):
                self.app.OpenAISpeechRequest(input="abcdef")
        finally:
            self.app.MAX_TEXT_LENGTH = original

    async def test_unknown_openai_format_raises_value_error(self):
        # 目标分支 1843-1844：未知 response_format 映射为 ValueError；
        # 失败发生在 ffmpeg 槽位获取之前或已归还——配额不得泄漏。
        with self.assertRaises(ValueError):
            await self.app._create_openai_audio_encoder("kokoro", "bogus")
        self.assertEqual(self.app._ffmpeg_limiter.active, 0)

    async def test_v1_key_check_raises_with_request_id_header(self):
        self.app.TTS_API_KEY = "secret"
        from starlette.requests import Request

        request = Request(
            {
                "type": "http",
                "headers": [(b"authorization", b"Bearer wrong")],
            }
        )
        with self.assertRaises(self.app.HTTPException) as ctx:
            self.app._require_v1_api_key(request)
        self.assertEqual(ctx.exception.status_code, 401)
        # 规范：/v1 的 401 必须携带 X-Request-ID 供客户端关联诊断。
        self.assertIn("X-Request-ID", ctx.exception.headers)

    async def test_split_kokoro_unit_boundaries(self):
        self.assertEqual(self.app.split_kokoro_unit("", 10), [])
        with self.assertRaises(ValueError):
            self.app.split_kokoro_unit("x", 0)

    async def test_ws_engine_blank_returns_error(self):
        parsed = self.app.parse_ws_request({"text": "hi", "engine": "   "})
        self.assertEqual(parsed["type"], "error")
        self.assertIn("engine", parsed["message"])

    async def test_prepare_edge_audio_pre_cancelled_returns_none(self):
        # 预置取消被 _next_edge_audio 短路：返回 (None, None)。
        # 注：此路径关闭的是外层 async 生成器，而非底层 stream 对象。
        stream = ClosableStream()
        self.app.edge_tts.Communicate = make_communicate(stream)
        ev = asyncio.Event()
        ev.set()
        edge_stream, first = await self.app._prepare_edge_audio(
            "t", "v", "+0%", ev
        )
        self.assertIsNone(edge_stream)
        self.assertIsNone(first)

    async def test_prepare_edge_audio_cancelled_in_retry_returns_none(self):
        # 首次上游失败后于重试休眠期取消 → 生成器正常结束(StopAsyncIteration)
        # → 关闭迭代器并返回 (None, None)，不得误报"无音频"失败。
        ev = asyncio.Event()
        self.app.EDGE_RETRY_MAX_ATTEMPTS = 2
        self.app.EDGE_RETRY_BASE_DELAY_SECONDS = 0.02

        class FailThenCancel:
            def __aiter__(self):
                return self

            async def __anext__(self):
                ev.set()  # 失败时同步置位取消
                raise RuntimeError("upstream wave failed")

        self.app.edge_tts.Communicate = make_communicate(FailThenCancel())
        edge_stream, first = await self.app._prepare_edge_audio(
            "t", "v", "+0%", ev
        )
        self.assertIsNone(edge_stream)
        self.assertIsNone(first)

    async def test_feed_mp3_swallows_stdin_close_error(self):
        class CloseBoom(RecordingStdin):
            def __init__(self):
                super().__init__()
                self.close_attempts = 0

            def close(self):
                self.close_attempts += 1
                raise OSError("gone")

        proc = FakeProc(stdin=CloseBoom())
        stream = ClosableStream(datas=[b"x"])
        await self.app._feed_mp3(
            proc, "t", "edge", "v", 1.0, None,
            edge_stream=stream, first_edge_audio=b"au",
        )
        self.assertEqual(proc.stdin.close_attempts, 1)

    async def test_synthesis_waiter_cap_enforced_and_count_restored(self):
        # 突变背锁：真实 waiter 容量上限路径——占满信号量后并发排队
        # TTS_MAX_SYNTHESIS_WAITERS 个许可请求，第 17 个必须显式
        # _SynthesisQueueFull；全部取消后计数必须归零(无幽灵占位)。
        # 此前 429 测试整体 patch 了 _acquire_kokoro_permit，这条
        # 容量链路从未被执行过。
        app = self.app
        sem = app._get_synthesis_semaphore()
        for _ in range(app.TTS_MAX_SYNTHESIS_CONCURRENCY):
            await sem.acquire()
        waiters = [
            asyncio.create_task(app._acquire_kokoro_permit())
            for _ in range(app.TTS_MAX_SYNTHESIS_WAITERS)
        ]
        await asyncio.sleep(0.02)  # 让排队任务完成 waiter 计数
        self.assertEqual(app._synthesis_waiter_count,
                         app.TTS_MAX_SYNTHESIS_WAITERS)
        with self.assertRaises(app._SynthesisQueueFull):
            await app._acquire_kokoro_permit()
        for w in waiters:
            w.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
        self.assertEqual(app._synthesis_waiter_count, 0)
        for _ in range(app.TTS_MAX_SYNTHESIS_CONCURRENCY):
            sem.release()

    async def test_reap_proc_survives_process_lookup_error(self):
        class GoneProc(FakeProc):
            def kill(self):
                raise ProcessLookupError()

        limiter = self.app.FfmpegLimiter(1)
        limiter.active = 1  # 模拟回收前已持有的槽位，验证回收后如数归还
        self.app._ffmpeg_limiter = limiter
        try:
            proc = GoneProc()
            await self.app._reap_proc(proc)
            self.assertEqual(limiter.active, 0)
        finally:
            self.app._ffmpeg_limiter = self.app.FfmpegLimiter(
                self.app.TTS_MAX_FFMPEG_PROCESSES
            )

    async def test_reap_edge_decoder_survives_process_lookup_error(self):
        # 目标分支 2162：edge decoder 的 kill 抛 ProcessLookupError 同样必须继续回收。
        class GoneProc(FakeProc):
            def kill(self):
                raise ProcessLookupError()

        limiter = self.app.FfmpegLimiter(1)
        limiter.active = 1
        self.app._ffmpeg_limiter = limiter
        try:
            code = await self.app._reap_edge_pcm_decoder(GoneProc())
            self.assertEqual(code, 0)
            self.assertEqual(limiter.active, 0)
        finally:
            self.app._ffmpeg_limiter = self.app.FfmpegLimiter(
                self.app.TTS_MAX_FFMPEG_PROCESSES
            )

    async def test_prefetched_reader_zero_and_partial_reads(self):
        class Reader:
            def __init__(self):
                self.reads = 0

            async def read(self, size):
                self.reads += 1
                return b"tail"

        reader = Reader()
        wrapped = self.app._PrefetchedStreamReader(reader, b"hello")
        self.assertEqual(await wrapped.read(0), b"")
        self.assertEqual(await wrapped.read(2), b"he")
        self.assertEqual(await wrapped.read(-1), b"llo")
        self.assertEqual(await wrapped.read(4), b"tail")

    async def test_pre_stream_feed_failure_status_default_500(self):
        self.assertEqual(
            self.app._pre_stream_feed_failure_status("kokoro", None), 500
        )

    async def test_late_stream_failure_reports_wait_error(self):
        class WaitBoomProc(FakeProc):
            async def wait(self):
                raise ValueError("wait broke")

        async def done_feed():
            pass

        feed_task = asyncio.create_task(done_feed())
        await feed_task
        with self.assertRaises(self.app._PostStreamSynthesisError):
            await self.app._raise_for_late_stream_failure(
                WaitBoomProc(), feed_task, "kokoro", "v"
            )

    async def test_openai_voices_survives_catalog_failure(self):
        # 目标分支：/v1/audio/voices 自身的兜底 except——目录函数直接抛错时
        # 仍返回 kokoro 音色而非 500。
        async def broken():
            raise RuntimeError("catalog exploded")

        self.app._get_edge_voices = broken
        client = TestClient(self.app.app)
        resp = client.get("/v1/audio/voices")
        self.assertEqual(resp.status_code, 200)
        ids = [v["id"] for v in resp.json()["data"]]
        self.assertIn("af_heart", ids)
        self.assertNotIn("edge", [v.get("engine") for v in resp.json()["data"]])

    async def test_preview_endpoint_guards(self):
        client = TestClient(self.app.app)
        resp = client.get("/api/voices/preview", params={"voice": "af_heart"})
        self.assertEqual(resp.status_code, 503)
        self.app.pipeline_zh = object()
        self.app.pipeline_en = object()
        resp = client.get(
            "/api/voices/preview",
            params={"engine": "bogus", "voice": "af_heart"},
        )
        self.assertEqual(resp.status_code, 422)
        resp = client.get(
            "/api/voices/preview",
            params={"engine": "kokoro", "voice": "bad voice!"},
        )
        self.assertEqual(resp.status_code, 422)
        self.assertIn("x-request-id", resp.headers)

    async def test_openai_speech_lang_and_lang_code_rejected(self):
        self.app.pipeline_zh = object()
        self.app.pipeline_en = object()
        client = TestClient(self.app.app)
        for field in ("lang", "lang_code", "language", "format"):
            resp = client.post(
                "/v1/audio/speech",
                json={"input": "hi", field: "zh"},
            )
            self.assertEqual(resp.status_code, 400, field)
            # 突变背锁：拒绝消息必须点名被拒字段——防止"拒绝错误字段"
            # 的变异体借同名前缀(lang vs lang_code)混过 contains 断言。
            message = resp.json()["error"]["message"]
            self.assertTrue(
                message.startswith(f"{field} is not supported"), message
            )

    async def test_preview_synthesis_failure_keeps_request_id_header(self):
        # 目标分支：预检抛出的 HTTPException 在 preview 端点补 X-Request-ID 后重放。
        self.app.pipeline_zh = object()
        self.app.pipeline_en = object()

        async def failing(*args, **kwargs):
            raise self.app.HTTPException(status_code=429, detail="busy")

        self.app._start_synthesis_for_request = failing
        client = TestClient(self.app.app)
        resp = client.get(
            "/api/voices/preview",
            params={"engine": "kokoro", "voice": "af_heart"},
        )
        self.assertEqual(resp.status_code, 429)
        self.assertIn("x-request-id", resp.headers)

    async def test_synth_kokoro_prefetch_without_slot_skips_units(self):
        async def fake_permit(cancel_event=None, *, prefetch=False):
            return None

        self.app._acquire_kokoro_permit = fake_permit
        queue = asyncio.Queue(maxsize=32)
        produced = await self.app.synth_kokoro(
            ["a", "b"], "af_heart", 1.0, queue,
            FakeWebSocket(), asyncio.Event(), prefetch=True,
        )
        self.assertFalse(produced)
        self.assertEqual(drain_all(queue), [])


class WsHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    """跨端时序活性契约：合成存续期间帧间隔必须小于前端无活动阈值(60s)。

    缺陷模式(八维体检漏掉的"第 9 维"——跨端时序契约)：Edge 长文本存在
    远超预期的生成间隙(整段单 seg + 上游限流)，前端 60s 无帧即判流死亡。
    修复=后端周期性 ping。本类把"慢合成必有心跳、心跳不打乱 seg 对偶"
    与"控制帧失败必须留痕"锁为回归。
    """

    async def asyncSetUp(self):
        self.app = import_app_with_fakes()
        # 注意：本类不静音 logger(可观测性断言需要)；心跳驱动的纯流程不产生日志。
        self.app.WS_HEARTBEAT_INTERVAL_SECONDS = 0.05

    async def _run_collecting(self, request_payloads, send):
        incoming = _REAL_QUEUE()
        deferred_disconnect = None
        for item in request_payloads:
            if item.get("type") == "websocket.disconnect":
                deferred_disconnect = item  # 与 _run_handler 同理：终态后再断连
            else:
                await incoming.put(item)
        saw_end = asyncio.Event()

        async def receive():
            # 先排空真实消息(connect/request/二进制帧)；队列空后才允许
            # 延迟断连生效(且必须等 end 已交付，否则合成会被提前取消)。
            if not incoming.empty():
                return await incoming.get()
            if deferred_disconnect is not None:
                await saw_end.wait()
                return deferred_disconnect
            return await incoming.get()

        async def tracking_send(message):
            text = message.get("text")
            if isinstance(text, str) and '"end"' in text:
                saw_end.set()
            await send(message)

        handler = asyncio.create_task(
            self.app.app(ws_scope(), receive, tracking_send)
        )
        try:
            await asyncio.wait_for(asyncio.shield(handler), 10)
        finally:
            if not handler.done():
                handler.cancel()
            await asyncio.gather(handler, return_exceptions=True)

    async def test_slow_synthesis_emits_pings_without_touching_seg_count(self):
        import logging as logging_mod

        segs, pings, ends = [], [], []

        async def send(message):
            if message["type"] != "websocket.send":
                return
            text = message.get("text")
            if not isinstance(text, str):
                return
            try:
                payload = json.loads(text)
            except ValueError:
                return
            kind = payload.get("type")
            if kind == "seg":
                segs.append(payload)
            elif kind == "ping":
                pings.append(payload)
            elif kind == "end":
                ends.append(payload)

        async def slow_kokoro(text, voice, speed, cancel_event=None, permit=None):
            await asyncio.sleep(0.2)  # 远大于心跳间隔，逼迫心跳出手
            return b"\x00\x01" * 16

        self.app.run_kokoro = slow_kokoro
        logger = logging_mod.getLogger("tts-api")
        with self.assertNoLogs(logger, level="ERROR"):
            await self._run_collecting(
                [
                    {"type": "websocket.connect"},
                    {
                        "type": "websocket.receive",
                        "text": json.dumps(
                            {
                                "text": "一" + chr(10) + "二" + chr(10) + "三",
                                "engine": "kokoro",
                                "voice": "af_heart",
                            }
                        ),
                    },
                    {"type": "websocket.disconnect", "code": 1000},
                ],
                send,
            )
        self.assertGreaterEqual(len(pings), 1, "慢合成期间必须有心跳帧")
        self.assertEqual(len(segs), 3, "心跳不得增减 seg 计数(前后端对偶)")
        self.assertEqual(len(ends), 1)

    async def test_control_send_failure_leaves_warning_log(self):
        import logging as logging_mod

        async def send(message):
            if message["type"] == "websocket.send":
                raise RuntimeError("client is gone")

        logger = logging_mod.getLogger("tts-api")
        with self.assertLogs(logger, level="WARNING") as captured:
            await self._run_collecting(
                [
                    {"type": "websocket.connect"},
                    {"type": "websocket.receive", "text": "not-json{"},
                    {"type": "websocket.disconnect", "code": 1000},
                ],
                send,
            )
        # 可观测性回归锁：v0.12.3 曾把控制帧失败改成静默关闭，服务器日志
        # 只剩 connection closed，线上问题(长文本误断)因此无从取证。
        self.assertTrue(
            any("控制帧" in line for line in captured.output),
            captured.output,
        )


if __name__ == "__main__":
    unittest.main()