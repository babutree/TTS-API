# -*- coding: utf-8 -*-
"""WebSocket 端到端行为测试：握手鉴权、请求校验、正常合成流、错误回传。

覆盖：
- 未配置密钥 = 握手放行；
- 配置密钥后：同源握手放行、外部无密钥拒绝、外部带 ?key= 放行；
- 无效 JSON、请求校验失败(缺 text/未知引擎/未知音色)回传 error 且连接保活;
- kokoro 正常流程 start -> seg -> 二进制帧 -> end;
- kokoro 合成异常回传 error(而非 end)，连接保活可继续；
- 长度超限回传 error。

用 TestClient.websocket_connect。run_kokoro 用可控 fake 顶掉真实推理。
"""
import os
import time
import unittest

from starlette.websockets import WebSocketDisconnect
from starlette.testclient import TestClient

from _support import import_app_with_fakes


def _load(key: str = ""):
    old = os.environ.get("TTS_API_KEY")
    os.environ["TTS_API_KEY"] = key
    try:
        app = import_app_with_fakes()
    finally:
        if old is None:
            os.environ.pop("TTS_API_KEY", None)
        else:
            os.environ["TTS_API_KEY"] = old
    app.pipeline_zh = object()
    app.pipeline_en = object()
    return app


def _install_kokoro_pcm(app, pcm: bytes):
    async def fake_run_kokoro(text, voice, speed, cancel_event=None):
        return pcm

    app.run_kokoro = fake_run_kokoro


def _install_kokoro_boom(app, error):
    async def fake_run_kokoro(text, voice, speed, cancel_event=None):
        raise error

    app.run_kokoro = fake_run_kokoro
    app.logger.disabled = True


def _collect_until_terminal(ws):
    """收集消息直到收到 end 或 error(终态)；返回 (类型序列, 终态 type)。"""
    import json

    seq = []
    terminal = None
    while True:
        msg = ws.receive()
        if "text" in msg:
            obj = json.loads(msg["text"])
            seq.append(("json", obj.get("type")))
            if obj.get("type") in ("end", "error"):
                terminal = obj.get("type")
                break
        elif "bytes" in msg:
            seq.append(("bin", len(msg["bytes"])))
        elif msg.get("type") == "websocket.close":
            break
    return seq, terminal


class WsHandshakeAuthTests(unittest.TestCase):
    def test_no_key_allows_connection(self):
        app = _load(key="")
        _install_kokoro_pcm(app, b"")
        client = TestClient(app.app)
        with client.websocket_connect("/ws/tts") as ws:
            ws.send_json({"text": "hi", "engine": "kokoro", "voice": "af_heart"})
            _, terminal = _collect_until_terminal(ws)
        self.assertEqual(terminal, "end")

    def test_same_origin_allowed_without_key(self):
        app = _load(key="secret")
        _install_kokoro_pcm(app, b"")
        client = TestClient(app.app)
        with client.websocket_connect(
            "/ws/tts", headers={"Origin": "http://testserver"}
        ) as ws:
            ws.send_json({"text": "hi", "engine": "kokoro", "voice": "af_heart"})
            _, terminal = _collect_until_terminal(ws)
        self.assertEqual(terminal, "end")

    def test_external_without_key_rejected(self):
        app = _load(key="secret")
        client = TestClient(app.app)
        with self.assertRaises(WebSocketDisconnect) as ctx:
            with client.websocket_connect(
                "/ws/tts", headers={"Origin": "http://evil.com"}
            ) as ws:
                ws.receive()

        self.assertEqual(ctx.exception.code, 1008)

    def test_external_with_query_key_allowed(self):
        app = _load(key="secret")
        _install_kokoro_pcm(app, b"")
        client = TestClient(app.app)
        with client.websocket_connect(
            "/ws/tts?key=secret", headers={"Origin": "http://evil.com"}
        ) as ws:
            ws.send_json({"text": "hi", "engine": "kokoro", "voice": "af_heart"})
            _, terminal = _collect_until_terminal(ws)
        self.assertEqual(terminal, "end")

    def test_external_with_wrong_query_key_rejected(self):
        app = _load(key="secret")
        client = TestClient(app.app)
        with self.assertRaises(WebSocketDisconnect) as ctx:
            with client.websocket_connect(
                "/ws/tts?key=wrong", headers={"Origin": "http://evil.com"}
            ) as ws:
                ws.receive()

        self.assertEqual(ctx.exception.code, 1008)


class WsRequestValidationTests(unittest.TestCase):
    def setUp(self):
        self.app = _load(key="")
        _install_kokoro_pcm(self.app, b"")
        self.client = TestClient(self.app.app)

    def test_invalid_json_returns_error_and_keeps_alive(self):
        with self.client.websocket_connect("/ws/tts") as ws:
            ws.send_text("not-json{")
            msg = ws.receive_json()
            self.assertEqual(msg["type"], "error")
            # 连接保活：随后一个合法请求应可完成。
            ws.send_json({"text": "hi", "engine": "kokoro", "voice": "af_heart"})
            _, terminal = _collect_until_terminal(ws)
            self.assertEqual(terminal, "end")

    def test_missing_text_returns_error(self):
        with self.client.websocket_connect("/ws/tts") as ws:
            ws.send_json({"engine": "kokoro"})
            msg = ws.receive_json()
            self.assertEqual(msg["type"], "error")
            self.assertIn("text", msg["message"])

    def test_unknown_engine_returns_error(self):
        with self.client.websocket_connect("/ws/tts") as ws:
            ws.send_json({"text": "hi", "engine": "espeak"})
            msg = ws.receive_json()
            self.assertEqual(msg["type"], "error")

    def test_unknown_kokoro_voice_returns_error(self):
        with self.client.websocket_connect("/ws/tts") as ws:
            ws.send_json({"text": "hi", "engine": "kokoro", "voice": "nope"})
            msg = ws.receive_json()
            self.assertEqual(msg["type"], "error")

    def test_text_over_limit_returns_error(self):
        # 收窄上限，构造超限文本。
        self.app.MAX_TEXT_LENGTH = 5
        with self.client.websocket_connect("/ws/tts") as ws:
            ws.send_json({"text": "abcdefghij", "engine": "kokoro", "voice": "af_heart"})
            msg = ws.receive_json()
            self.assertEqual(msg["type"], "error")


class WsSynthesisFlowTests(unittest.TestCase):
    def test_happy_path_emits_start_seg_audio_end(self):
        app = _load(key="")
        _install_kokoro_pcm(app, b"\x01\x02\x03\x04")
        client = TestClient(app.app)
        with client.websocket_connect("/ws/tts") as ws:
            ws.send_json({"text": "hello.", "engine": "kokoro", "voice": "af_heart"})
            seq, terminal = _collect_until_terminal(ws)
        self.assertEqual(seq[0], ("json", "start"))
        self.assertIn(("json", "seg"), seq)
        self.assertTrue(any(kind == "bin" for kind, _ in seq))
        self.assertEqual(terminal, "end")

    def test_empty_pcm_still_emits_seg_and_end(self):
        # 清洗后有内容但合成零音频(如纯英文配英文音色被过滤空)：仍发 seg 与 end，不发二进制帧。
        app = _load(key="")
        _install_kokoro_pcm(app, b"")
        client = TestClient(app.app)
        with client.websocket_connect("/ws/tts") as ws:
            ws.send_json({"text": "hello.", "engine": "kokoro", "voice": "af_heart"})
            seq, terminal = _collect_until_terminal(ws)
        self.assertEqual(seq[0], ("json", "start"))
        self.assertIn(("json", "seg"), seq)
        self.assertFalse(any(kind == "bin" for kind, _ in seq))
        self.assertEqual(terminal, "end")

    def test_synth_exception_returns_error_not_end(self):
        app = _load(key="")
        _install_kokoro_boom(app, RuntimeError("kokoro exploded"))
        client = TestClient(app.app)
        try:
            with client.websocket_connect("/ws/tts") as ws:
                ws.send_json({"text": "hello.", "engine": "kokoro", "voice": "af_heart"})
                seq, terminal = _collect_until_terminal(ws)
        finally:
            app.logger.disabled = False
        # 合成失败必须回传 error 而非伪装成功的 end。
        self.assertEqual(terminal, "error")
        self.assertNotIn(("json", "end"), seq)

    def test_synthesis_timeout_returns_error_not_end(self):
        app = _load(key="")
        app.TTS_SYNTHESIS_TIMEOUT_SECONDS = 0.01

        async def slow_run_kokoro(text, voice, speed, cancel_event=None):
            while not cancel_event.is_set():
                await app.asyncio.sleep(0.01)
            return b""

        app.run_kokoro = slow_run_kokoro
        app.logger.disabled = True
        client = TestClient(app.app)
        try:
            with client.websocket_connect("/ws/tts") as ws:
                ws.send_json({"text": "hello.", "engine": "kokoro", "voice": "af_heart"})
                start = time.monotonic()
                seq, terminal = _collect_until_terminal(ws)
        finally:
            app.logger.disabled = False

        self.assertLess(time.monotonic() - start, 1.0)
        self.assertEqual(terminal, "error")
        self.assertNotIn(("json", "end"), seq)

    def test_connection_can_process_second_request_after_first_error(self):
        app = _load(key="")
        calls = 0

        async def flaky_run_kokoro(text, voice, speed, cancel_event=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("first request failed")
            return b"\x01\x02"

        app.run_kokoro = flaky_run_kokoro
        app.logger.disabled = True
        client = TestClient(app.app)
        try:
            with client.websocket_connect("/ws/tts") as ws:
                ws.send_json({"text": "first.", "engine": "kokoro", "voice": "af_heart"})
                first_seq, first_terminal = _collect_until_terminal(ws)
                ws.send_json({"text": "second.", "engine": "kokoro", "voice": "af_heart"})
                second_seq, second_terminal = _collect_until_terminal(ws)
        finally:
            app.logger.disabled = False

        self.assertEqual(first_terminal, "error")
        self.assertNotIn(("json", "end"), first_seq)
        self.assertEqual(second_terminal, "end")
        self.assertTrue(any(kind == "bin" for kind, _ in second_seq))


if __name__ == "__main__":
    unittest.main()
