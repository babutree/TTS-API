# -*- coding: utf-8 -*-
"""WebSocket 生命周期回归锁:三处 v0.12.2 修复。

Bug 1: 合成期间断连被 watcher 取走后，主循环不得再 receive(否则 Starlette 抛
       RuntimeError 穿透 ASGI 层，污染 docker logs 与 /api/logs)。
Bug 2: 前端句数与后端 seg 数必须 1:1，否则变速续播定位到错误句子。
Bug 3: 半开连接(客户端不读也不发 FIN)下 send 会阻塞，必须有上界，否则
       sender 卡住 → 队列满 → 生产者与主循环双双钉死 → handler 永久泄漏。
"""
import asyncio
import json
import unittest

from _support import import_app_with_fakes


def ws_scope():
    return {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "ws",
        "path": "/ws/tts",
        "raw_path": b"/ws/tts",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"localhost:8880")],
        "client": ("127.0.0.1", 5555),
        "server": ("127.0.0.1", 8880),
        "subprotocols": [],
        "state": {},
    }


class MidSynthesisDisconnectTests(unittest.IsolatedAsyncioTestCase):
    """Bug 1: watcher 吃掉 disconnect 后主循环必须干净退出。"""

    async def asyncSetUp(self):
        self.app = import_app_with_fakes()
        self.app.logger.disabled = True

    async def asyncTearDown(self):
        self.app.logger.disabled = False

    async def test_disconnect_during_synthesis_exits_without_runtime_error(self):
        incoming = asyncio.Queue()
        await incoming.put({"type": "websocket.connect"})
        await incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({
                "text": "hello world.", "engine": "kokoro",
                "voice": "af_heart", "speed": 1.0,
            }),
        })
        started = asyncio.Event()

        async def slow_kokoro(text, voice, speed, cancel_event=None, permit=None):
            started.set()
            for _ in range(200):
                if cancel_event is not None and cancel_event.is_set():
                    break
                await asyncio.sleep(0.005)
            return b"\x00\x01" * 16

        self.app.run_kokoro = slow_kokoro
        sent = []

        async def receive():
            return await incoming.get()

        async def send(message):
            sent.append(message["type"])

        async def inject():
            await started.wait()
            await asyncio.sleep(0.02)
            await incoming.put({"type": "websocket.disconnect", "code": 1000})

        injector = asyncio.create_task(inject())
        try:
            # 不得抛 RuntimeError('Cannot call "receive" once a disconnect ...')
            await asyncio.wait_for(
                self.app.app(ws_scope(), receive, send), 10
            )
        finally:
            injector.cancel()
            try:
                await injector
            except asyncio.CancelledError:
                pass

        self.assertIn("websocket.accept", sent)
        self.assertEqual(
            len(asyncio.all_tasks() - {asyncio.current_task()}), 0,
            "handler 退出后不得留下未清理的 task",
        )


class SegAlignmentTests(unittest.IsolatedAsyncioTestCase):
    """Bug 2: 含块级 markdown 时前后端单元数仍须 1:1。"""

    async def asyncSetUp(self):
        self.app = import_app_with_fakes()
        self.app.logger.disabled = True

    async def asyncTearDown(self):
        self.app.logger.disabled = False

    async def _collect_segs(self, raw_text):
        segs = []
        incoming = asyncio.Queue()
        await incoming.put({"type": "websocket.connect"})
        await incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({
                "text": raw_text, "engine": "kokoro",
                "voice": "af_heart", "speed": 1.0,
            }),
        })

        async def fake_kokoro(text, voice, speed, cancel_event=None, permit=None):
            return b"\x00\x01" * 8

        self.app.run_kokoro = fake_kokoro
        done = asyncio.Event()

        async def receive():
            if incoming.empty():
                await done.wait()
                return {"type": "websocket.disconnect", "code": 1000}
            return await incoming.get()

        async def send(message):
            if message["type"] == "websocket.send" and message.get("text"):
                try:
                    payload = json.loads(message["text"])
                except ValueError:
                    return
                if payload.get("type") == "seg":
                    segs.append(payload.get("text"))
                elif payload.get("type") in ("end", "error"):
                    done.set()

        await asyncio.wait_for(self.app.app(ws_scope(), receive, send), 10)
        return segs

    async def test_seg_count_matches_frontend_line_count(self):
        # 前端只切句、不做 markdown 清洗，故它看到的单元数就是原文行数。
        cases = [
            "Hello world.\nMiddle line.\nSecond line.",
            "Hello world.\n---\nSecond line.",
            "Hello world.\n![img](a.png)\nSecond line.",
            "Hello world.\n```\ncode();\n```\nSecond line.",
            "A.\n---\n---\nB.",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                segs = await self._collect_segs(raw)
                self.assertEqual(
                    len(segs), len(raw.split("\n")),
                    f"seg 数必须等于前端单元数；实际 {segs}",
                )

    async def test_fenced_code_is_not_spoken_but_still_emits_seg(self):
        segs = await self._collect_segs("A.\n```py\nprint(1)\n```\nB.")
        self.assertEqual(len(segs), 5)
        self.assertNotIn("print(1)", "".join(segs))


class HalfOpenConnectionTests(unittest.IsolatedAsyncioTestCase):
    """Bug 3: 半开连接下 handler 必须能退出，不得永久泄漏。"""

    async def asyncSetUp(self):
        self.app = import_app_with_fakes()
        self.app.logger.disabled = True
        self.app.TTS_RESPONSE_WRITE_TIMEOUT_SECONDS = 0.3

    async def asyncTearDown(self):
        self.app.logger.disabled = False

    async def _run_half_open(self, synthesis_timeout):
        self.app.TTS_SYNTHESIS_TIMEOUT_SECONDS = synthesis_timeout
        incoming = asyncio.Queue()
        await incoming.put({"type": "websocket.connect"})
        await incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({
                "text": "\n".join(f"sentence {i}." for i in range(80)),
                "engine": "kokoro", "voice": "af_heart", "speed": 1.0,
            }),
        })

        async def fake_kokoro(text, voice, speed, cancel_event=None, permit=None):
            return b"\x00\x01" * 512

        self.app.run_kokoro = fake_kokoro
        wedged = asyncio.Event()
        never = asyncio.Event()   # 永不置位 = 对端不读，写缓冲永久满
        count = {"n": 0}

        async def receive():
            return await incoming.get()

        async def send(message):
            if message["type"] == "websocket.send" and message.get("bytes"):
                count["n"] += 1
                wedged.set()
                await never.wait()

        handler = asyncio.create_task(self.app.app(ws_scope(), receive, send))
        try:
            await asyncio.wait_for(wedged.wait(), 5)
            await asyncio.sleep(0.1)
            await incoming.put({"type": "websocket.disconnect", "code": 1000})
            # 必须在写超时的量级内退出，而不是永久挂起
            await asyncio.wait_for(asyncio.shield(handler), 5)
        finally:
            never.set()
            if not handler.done():
                handler.cancel()
            try:
                await handler
            except BaseException:
                pass

    async def test_half_open_without_synthesis_timeout_still_exits(self):
        await self._run_half_open(0.0)

    async def test_half_open_with_synthesis_timeout_still_exits(self):
        # 关键反例：配了合成超时也救不了 queue.put(None)，故此路径必须自带上界。
        await self._run_half_open(1.0)


class ControlFrameSendFailureTests(unittest.IsolatedAsyncioTestCase):
    """回归锁：控制帧(error/start/end)发送失败时 handler 必须干净退出。

    审计发现：主循环的控制帧曾绕开 _send_bounded 直发，连接已坏但
    client_state 尚未反映时，send 异常会穿透 ASGI 层(污染服务日志)。
    修复后失败即视为断连：走统一出口，不抛异常、不挂起。
    """

    async def asyncSetUp(self):
        self.app = import_app_with_fakes()
        self.app.logger.disabled = True

    async def asyncTearDown(self):
        self.app.logger.disabled = False

    async def _run_handler(self, request_text, send):
        incoming = asyncio.Queue()
        await incoming.put({"type": "websocket.connect"})
        await incoming.put({"type": "websocket.receive", "text": request_text})

        async def receive():
            return await incoming.get()

        handler = asyncio.create_task(self.app.app(ws_scope(), receive, send))
        try:
            # 干净退出 = 不抛异常、不永久挂起；任一违背此处即失败
            await asyncio.wait_for(asyncio.shield(handler), 5)
        finally:
            if not handler.done():
                handler.cancel()

    async def test_parse_error_frame_send_failure_exits_cleanly(self):
        sent = []

        async def send(message):
            if message["type"] == "websocket.send":
                raise RuntimeError("client is gone")
            sent.append(message["type"])

        await self._run_handler("not-json{", send)
        self.assertEqual(sent, ["websocket.accept"])

    async def test_start_frame_send_failure_exits_cleanly(self):
        async def send(message):
            if message["type"] == "websocket.send":
                raise RuntimeError("client is gone")

        await self._run_handler(
            json.dumps({"text": "hi", "engine": "kokoro", "voice": "af_heart"}),
            send,
        )

    async def test_end_frame_send_failure_exits_cleanly(self):
        async def fake_run_kokoro(
            text, voice, speed, cancel_event=None, permit=None
        ):
            return b"\x00\x01" * 16

        self.app.run_kokoro = fake_run_kokoro

        async def send(message):
            # 用 JSON 解析而非子串匹配：send_json 是紧凑分隔符({"type":"end"})，
            # 子串易随序列化风格漂移而失配。
            if (
                message["type"] == "websocket.send"
                and isinstance(message.get("text"), str)
            ):
                try:
                    payload = json.loads(message["text"])
                except ValueError:
                    return
                if isinstance(payload, dict) and payload.get("type") == "end":
                    raise RuntimeError("client is gone")

        await self._run_handler(
            json.dumps({"text": "hi", "engine": "kokoro", "voice": "af_heart"}),
            send,
        )


class RunCountSegParityTests(unittest.IsolatedAsyncioTestCase):
    """跨端对偶：前端 run.count 必须等于后端为该 run 发出的 seg 数。

    变速续播按 timeline[k].sentenceIndex = run.startSentence + segWithinRun 定位，
    run 内 seg 数一旦不等于 count，其后所有 run 的 sentenceIndex 全部偏移。
    这里用 index.html 里真实的 splitSentences 产出 run.text(与 buildRunsFrom 的
    "\\n".join 语义一致)，再喂给真实 ASGI handler 数 seg，主路径与预取路径各验一遍。
    """

    RAW_INPUTS = (
        "Hello world. Second one.",
        "Hello world.\n---\nSecond line.",
        "A.\n```py\nprint(1)\n```\nB.",
        "A.\n---\n---\nB.",
        "  leading spaces.\n   more.  ",
        "A.\n\t\nB.",
        "A.\n   \nB.",
        "A.\n![i](x.png)\n![j](y.png)\nB.",
        "# Title\nBody text.\n## Sub\nMore.",
        "- item one\n- item two\n- item three",
    )

    NODE_SCRIPT = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
function grab(re){const m=html.match(re); if(!m) throw new Error('miss '+re); return m[0];}
const src=[grab(/const PYTHON_STRIP_EDGE_RE = [^\n]+/),
           grab(/function pythonStrip\(value\) \{[\s\S]*?\n\}/),
           grab(/function splitSentences\(text\) \{[\s\S]*?\n\}/)].join("\n");
const split=new Function(src+"\nreturn splitSentences;")();
const raws=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));
fs.writeFileSync(process.argv[4], JSON.stringify(raws.map(raw=>{
  const s=split(raw);
  return s.length ? { text: s.join("\n"), count: s.length } : null;
})));
"""

    async def asyncSetUp(self):
        self.app = import_app_with_fakes()
        self.app.logger.disabled = True

    async def asyncTearDown(self):
        self.app.logger.disabled = False

    def _frontend_runs(self):
        import pathlib
        import subprocess
        import tempfile

        root = pathlib.Path(__file__).resolve().parents[1]
        tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="segparity-"))
        try:
            js = tmpdir / "r.js"
            src = tmpdir / "in.json"
            dst = tmpdir / "out.json"
            js.write_text(self.NODE_SCRIPT, encoding="utf-8")
            src.write_text(
                json.dumps(list(self.RAW_INPUTS), ensure_ascii=False),
                encoding="utf-8",
            )
            result = subprocess.run(
                ["node", str(js), str(root / "index.html"), str(src), str(dst)],
                capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            if result.returncode != 0:
                self.skipTest(f"node unavailable or failed: {result.stderr[:200]}")
            return json.loads(dst.read_text(encoding="utf-8"))
        finally:
            for leftover in tmpdir.glob("*"):
                leftover.unlink(missing_ok=True)
            tmpdir.rmdir()

    async def _collect_segs(self, run_text, prefetch):
        segs = []
        incoming = asyncio.Queue()
        await incoming.put({"type": "websocket.connect"})
        await incoming.put({
            "type": "websocket.receive",
            "text": json.dumps({
                "text": run_text, "engine": "kokoro",
                "voice": "af_heart", "speed": 1.0,
            }),
        })

        async def fake_kokoro(text, voice, speed, cancel_event=None, permit=None):
            return b"\x00\x01" * 8

        self.app.run_kokoro = fake_kokoro
        done = asyncio.Event()

        async def receive():
            if incoming.empty():
                await done.wait()
                return {"type": "websocket.disconnect", "code": 1000}
            return await incoming.get()

        async def send(message):
            if message["type"] == "websocket.send" and message.get("text"):
                try:
                    payload = json.loads(message["text"])
                except ValueError:
                    return
                if payload.get("type") == "seg":
                    segs.append(payload.get("text"))
                elif payload.get("type") in ("end", "error"):
                    done.set()

        scope = ws_scope()
        if prefetch:
            scope["query_string"] = b"prefetch=1"
        await asyncio.wait_for(self.app.app(scope, receive, send), 15)
        return segs

    async def test_run_count_equals_seg_count_on_both_paths(self):
        runs = self._frontend_runs()
        for raw, run in zip(self.RAW_INPUTS, runs):
            if run is None:
                continue
            with self.subTest(raw=raw):
                main_segs = await self._collect_segs(run["text"], prefetch=False)
                pre_segs = await self._collect_segs(run["text"], prefetch=True)
                self.assertEqual(
                    len(main_segs), run["count"],
                    f"主路径 seg 数须等于 run.count；run.text={run['text']!r}",
                )
                self.assertEqual(
                    len(pre_segs), run["count"],
                    f"预取路径 seg 数须等于 run.count；run.text={run['text']!r}",
                )


class SourceContractTests(unittest.TestCase):
    """锁定源码形状，防止上述三处修复被静默改回。"""

    def setUp(self):
        import pathlib

        self.source = (
            pathlib.Path(__file__).resolve().parents[1] / "app.py"
        ).read_text(encoding="utf-8")

    def test_watcher_reports_disconnect_to_main_loop(self):
        self.assertIn("watcher_saw_disconnect", self.source)
        # 不得退回"静默吞掉 disconnect"
        self.assertNotIn(
            "                try:\n                    await ws.receive()\n"
            "                except Exception:\n                    pass",
            self.source,
        )

    def test_kokoro_units_are_not_filtered(self):
        self.assertIn('units = text.split("\\n")', self.source)
        self.assertNotIn(
            'units = [u for u in text.split("\\n") if u.strip()]', self.source
        )

    def test_ws_sends_are_bounded(self):
        self.assertIn("_send_bounded", self.source)
        self.assertIn("send_wedged", self.source)

    def test_fence_removal_preserves_newlines(self):
        self.assertIn('lambda m: "\\n" * m.group(0).count("\\n")', self.source)


if __name__ == "__main__":
    unittest.main()
