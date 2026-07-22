# -*- coding: utf-8 -*-
"""共享测试支撑：fake 引擎注入、app 干净重导入、常用假对象。

设计原则：
- 每次 import_app_with_fakes 都强制重载 app，隔离模块级状态
  (MAX_TEXT_LENGTH / _edge_voices_cache / TTS_API_KEY 等)，避免用例互相污染。
- kokoro / edge_tts 用最小 fake 顶掉重依赖(模型权重、网络)，让纯逻辑可被离线、
  确定性地测试。fake 只保留被测路径真正会触碰的接口。
"""
import asyncio
import importlib
import sys
import types
import warnings

from starlette.websockets import WebSocketState


def _install_fake_engine_modules():
    edge_tts = types.ModuleType("edge_tts")

    async def _list_voices():
        # 默认无 Edge 音色；需要时用例自行覆盖 app.edge_tts.list_voices。
        return []

    edge_tts.list_voices = _list_voices
    edge_tts.Communicate = object  # 需要时用例自行覆盖为可控 fake
    sys.modules["edge_tts"] = edge_tts

    kokoro = types.ModuleType("kokoro")
    kokoro.KPipeline = object
    sys.modules["kokoro"] = kokoro


def import_app_with_fakes():
    """强制重新导入 app，附带 fake 依赖。返回全新的 app 模块对象。"""
    sys.modules.pop("app", None)
    _install_fake_engine_modules()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return importlib.import_module("app")


class RecordingStdin:
    """记录写入内容的假 stdin，用于断言喂入 ffmpeg 的字节与关闭行为。"""

    def __init__(self):
        self.chunks = []
        self.closed = False

    def write(self, data):
        self.chunks.append(bytes(data))

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    def written(self) -> bytes:
        return b"".join(self.chunks)


class ScriptedStdout:
    """按脚本逐次返回块的假 stdout；耗尽后返回 b'' 表示 EOF。"""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, size):
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class EmptyStdout:
    async def read(self, size):
        return b""


class HangingStdout:
    async def read(self, size):
        await asyncio.Event().wait()


class FakeProc:
    """假子进程：记录 kill/wait，模拟 returncode 生命周期。"""

    def __init__(self, stdin=None, stdout=None):
        self.returncode = None
        self.killed = False
        self.waited = False
        self.stdin = stdin if stdin is not None else RecordingStdin()
        self.stdout = stdout if stdout is not None else EmptyStdout()

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        self.waited = True
        if self.returncode is None:
            self.returncode = 0


class FakeWebSocket:
    """最小假 WebSocket：仅暴露合成分发函数会读的 client_state。"""

    def __init__(self, connected=True):
        self.client_state = (
            WebSocketState.CONNECTED if connected else WebSocketState.DISCONNECTED
        )


class FailingEdgeStream:
    """async 迭代即抛错，用于模拟 Edge 上游流失败。"""

    def __init__(self, error):
        self.error = error

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise self.error


class AudioEdgeStream:
    """按脚本产出 Edge 音频块的 async 迭代器。"""

    def __init__(self, datas):
        self._items = [{"type": "audio", "data": d} for d in datas]
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._i]
        self._i += 1
        return item


def make_communicate(stream_obj):
    """构造一个 Edge Communicate fake 类，其 stream() 返回给定 async 迭代器。"""

    class _Communicate:
        def __init__(self, text, voice, rate=None):
            self.text = text
            self.voice = voice
            self.rate = rate

        def stream(self):
            return stream_obj

    return _Communicate


def drain_queue(queue: asyncio.Queue):
    """非阻塞取出 asyncio.Queue 中所有项，返回列表(保序)。"""
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


def disable_asyncio_debug():
    """关闭 unittest IsolatedAsyncioTestCase 默认 debug，避免慢任务噪声污染测试信号。"""
    asyncio.get_running_loop().set_debug(False)
