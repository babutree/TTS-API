# -*- coding: utf-8 -*-
import asyncio
import hmac
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Request
from starlette.websockets import WebSocketState
from fastapi.responses import FileResponse, Response, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator

import edge_tts
from kokoro import KPipeline

LOG_MAX_LINES = 1000
DEFAULT_MAX_TEXT_LENGTH = 100000
DEFAULT_EDGE_VOICES_CACHE_TTL_SECONDS = 86400.0
DEFAULT_MAX_FFMPEG_PROCESSES = 2
DEFAULT_MAX_SYNTHESIS_CONCURRENCY = 2
REQUEST_ID_MAX_LENGTH = 64


class RingBufferHandler(logging.Handler):
    # 滚动保留最新 N 行日志：写满后丢最旧(deque maxlen)，避免日志无限增长。
    # 同时透传到 stdout，兼容 docker logs 实时查看。
    def __init__(self, max_lines: int):
        super().__init__()
        self.buffer = deque(maxlen=max_lines)
        self._stream = logging.StreamHandler()

    def setFormatter(self, fmt):
        super().setFormatter(fmt)
        self._stream.setFormatter(fmt)

    def emit(self, record):
        try:
            self.buffer.append(self.format(record))
            self._stream.emit(record)
        except Exception:
            self.handleError(record)


_log_formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
_ring_handler = RingBufferHandler(LOG_MAX_LINES)
_ring_handler.setFormatter(_log_formatter)

_root_logger = logging.getLogger()
_root_logger.setLevel(logging.INFO)
_root_logger.handlers.clear()
_root_logger.addHandler(_ring_handler)

logger = logging.getLogger("tts-api")


def parse_max_text_length(value: str | None) -> int:
    return int(value or DEFAULT_MAX_TEXT_LENGTH)


def parse_edge_voices_cache_ttl(value: str | None) -> float:
    ttl = float(value or DEFAULT_EDGE_VOICES_CACHE_TTL_SECONDS)
    if ttl < 0:
        raise ValueError("EDGE_VOICES_CACHE_TTL_SECONDS must be >= 0")
    return ttl


def parse_optional_positive_float(value: str | None, name: str) -> float:
    parsed = float(value or 0)
    if parsed < 0:
        raise ValueError(f"{name} must be >= 0")
    return parsed


def parse_positive_int(value: str | None, name: str, default: int) -> int:
    parsed = int(value or default)
    if parsed < 1:
        raise ValueError(f"{name} must be >= 1")
    return parsed


def parse_cors_allow_origins(value: str | None) -> list[str]:
    if value is None or value.strip() == "":
        return ["*"]
    origins = [item.strip() for item in value.split(",") if item.strip()]
    if not origins:
        raise ValueError("TTS_CORS_ALLOW_ORIGINS must include at least one origin")
    return origins


MAX_TEXT_LENGTH = parse_max_text_length(os.environ.get("MAX_TEXT_LENGTH"))
EDGE_VOICES_CACHE_TTL_SECONDS = parse_edge_voices_cache_ttl(
    os.environ.get("EDGE_VOICES_CACHE_TTL_SECONDS")
)
CORS_ALLOW_ORIGINS = parse_cors_allow_origins(os.environ.get("TTS_CORS_ALLOW_ORIGINS"))
TTS_SYNTHESIS_TIMEOUT_SECONDS = parse_optional_positive_float(
    os.environ.get("TTS_SYNTHESIS_TIMEOUT_SECONDS"), "TTS_SYNTHESIS_TIMEOUT_SECONDS"
)
TTS_MAX_FFMPEG_PROCESSES = parse_positive_int(
    os.environ.get("TTS_MAX_FFMPEG_PROCESSES"),
    "TTS_MAX_FFMPEG_PROCESSES",
    DEFAULT_MAX_FFMPEG_PROCESSES,
)
TTS_MAX_SYNTHESIS_CONCURRENCY = parse_positive_int(
    os.environ.get("TTS_MAX_SYNTHESIS_CONCURRENCY"),
    "TTS_MAX_SYNTHESIS_CONCURRENCY",
    DEFAULT_MAX_SYNTHESIS_CONCURRENCY,
)


class FfmpegLimiter:
    def __init__(self, max_active: int):
        self.max_active = max_active
        self.active = 0
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        async with self._lock:
            if self.active >= self.max_active:
                return False
            self.active += 1
            return True

    def release(self):
        if self.active > 0:
            self.active -= 1


_ffmpeg_limiter = FfmpegLimiter(TTS_MAX_FFMPEG_PROCESSES)

# 合成并发闸门：Kokoro 推理跑在默认线程池(min(32, cpu+4))里，且按语言锁串行化同一 pipeline。
# 若并发请求过多，大量 to_thread 任务会占着线程池 worker 阻塞在语言锁上，拖垮整个线程池
# (连非 TTS 的 to_thread 一起饿死)。ffmpeg 子进程数由 FfmpegLimiter 兜底，但 WS 的 Kokoro
# 不产生 ffmpeg，故此前无任何闸门。这里用信号量给"合成"本身设并发上限，REST/WS 共用。
# 阻塞式(排队)而非 fail-fast：对 WS 交互点按语义更自然，且不伪装成功。惰性创建以绑定运行时事件循环。
_synthesis_semaphore = None


def _get_synthesis_semaphore() -> asyncio.Semaphore:
    global _synthesis_semaphore
    if _synthesis_semaphore is None:
        _synthesis_semaphore = asyncio.Semaphore(TTS_MAX_SYNTHESIS_CONCURRENCY)
    return _synthesis_semaphore

# =========================
# API Key 鉴权
# =========================
# TTS_API_KEY 空 = 完全开放(兼容本地直连)；非空 = 对外部程序化客户端(如浏览器扩展)启用鉴权。
# 自有同源页面(index.html / api 文档)免密：它们与后端同源，供人直接使用。
# 网络边界仍应由反向代理(Caddy)兜底；此处密钥为外部集成提供受控接入能力。
TTS_API_KEY = os.environ.get("TTS_API_KEY", "").strip()

# 永久豁免路径：健康检查(docker healthcheck 依赖)、自有页面与静态资源。否则页面/探活打不开。
_AUTH_EXEMPT_PATHS = frozenset({"/", "/index.html", "/api", "/static/style.css", "/favicon.ico"})


def _host_of(value: str) -> str:
    # 从 Origin/Referer 取 host(含端口)。Origin 形如 scheme://host:port；Referer 是完整 URL。
    if not value:
        return ""
    return urlsplit(value).netloc


def _is_same_origin(request_headers) -> bool:
    # 同源判定：Origin(优先)或 Referer 的 host 等于请求 Host。自有页面(index/api)据此免密。
    # 诚实边界：非浏览器客户端可伪造这些头，故真正网络隔离仍需反代；密钥用于受控外部接入。
    host = request_headers.get("host", "")
    if not host:
        return False
    origin = request_headers.get("origin", "")
    if origin:
        return _host_of(origin) == host
    referer = request_headers.get("referer", "")
    if referer:
        return _host_of(referer) == host
    return False


def _key_matches(provided: str) -> bool:
    # 时序安全比对，避免通过响应耗时侧信道逐字节猜测密钥。
    if not provided:
        return False
    return hmac.compare_digest(provided, TTS_API_KEY)


def _extract_rest_key(request_headers) -> str:
    # REST 密钥来源：Authorization: Bearer <key> 或 X-API-Key。
    auth = request_headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    return request_headers.get("x-api-key", "").strip()


def _redact_log_line(line: str) -> str:
    line = re.sub(r"(Authorization:\s*Bearer\s+)\S+", r"\1[REDACTED]", line, flags=re.I)
    line = re.sub(r"(X-API-Key:\s*)\S+", r"\1[REDACTED]", line, flags=re.I)
    line = re.sub(r"([?&]key=)[^&\s]+", r"\1[REDACTED]", line, flags=re.I)
    return re.sub(r"(TTS_API_KEY=)\S+", r"\1[REDACTED]", line, flags=re.I)


def _request_id_from_header(value: str | None) -> str:
    if value:
        cleaned = re.sub(r"[^A-Za-z0-9_.:-]", "", value.strip())
        if cleaned:
            return cleaned[:REQUEST_ID_MAX_LENGTH]
    return uuid.uuid4().hex


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await startup()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    # 未配置密钥 = 完全开放；豁免路径直接放行；同源自有页面免密；否则校验 REST 密钥。
    path = request.url.path
    # /api/auth 和 /api/logs 不走同源豁免，由端点自身校验密钥，
    # 使 api.html 测试器与 CRX 能真实验证密钥有效性(同源也需正确密钥才通过)。
    if path in ("/api/auth", "/api/logs"):
        return await call_next(request)
    if not TTS_API_KEY or path in _AUTH_EXEMPT_PATHS:
        return await call_next(request)
    if _is_same_origin(request.headers):
        return await call_next(request)
    if _key_matches(_extract_rest_key(request.headers)):
        return await call_next(request)
    return JSONResponse(status_code=401, content={"detail": "缺少或错误的 API Key"})


# CORS 必须在鉴权中间件之后添加(Starlette 中间件后加者位于外层)，
# 确保 401 响应也带跨域头，浏览器扩展等跨域客户端才能读到状态码而非被 CORS 拦截。
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# 静态资源：仅白名单文件，避免暴露源码 / Dockerfile
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

@app.get("/")
async def root():
    # 健康检查需真正反映引擎可用性：pipeline 未就绪则返回 503，避免容器被误判为健康
    if pipeline_zh is None or pipeline_en is None:
        return JSONResponse(status_code=503, content={"status": "starting", "ready": False})
    if shutil.which("ffmpeg") is None:
        return JSONResponse(status_code=503, content={"status": "ffmpeg missing", "ready": False})
    return {"status": "v0.1 engine running", "ready": True}

@app.get("/index.html")
async def index():
    return FileResponse(
        os.path.join(BASE_DIR, "index.html"),
        media_type="text/html; charset=utf-8"
    )

@app.get("/api")
async def api_docs():
    # API 文档页(自包含 HTML)：访问 /api 即见接口说明。与 /api/voices、/api/tts 同命名空间。
    return FileResponse(
        os.path.join(BASE_DIR, "api.html"),
        media_type="text/html; charset=utf-8"
    )

@app.get("/api/auth")
async def api_auth(request: Request):
    # 密钥探测端点：始终校验密钥本身(不走同源豁免)，供 api.html 测试器与 CRX 验证密钥有效性。
    # 未配置密钥 = 服务开放，任意请求视为已授权。
    if not TTS_API_KEY:
        return {"auth": "disabled", "authorized": True}
    if _key_matches(_extract_rest_key(request.headers)):
        return {"auth": "enabled", "authorized": True}
    return JSONResponse(status_code=401, content={"auth": "enabled", "authorized": False,
                                                   "detail": "缺少或错误的 API Key"})


@app.get("/api/logs")
async def api_logs(request: Request, limit: int = Query(default=100, ge=1, le=LOG_MAX_LINES)):
    # 诊断日志可能包含异常栈、请求路径或上游错误。配置 key 时必须真实校验，不能吃同源免密。
    if TTS_API_KEY and not _key_matches(_extract_rest_key(request.headers)):
        return JSONResponse(status_code=401, content={"detail": "缺少或错误的 API Key"})
    lines = list(_ring_handler.buffer)
    return {
        "limit": limit,
        "total_buffered": len(lines),
        "lines": [_redact_log_line(line) for line in lines[-limit:]],
    }

@app.get("/static/style.css")
async def style_css():
    return FileResponse(
        os.path.join(BASE_DIR, "style.css"),
        media_type="text/css"
    )

@app.get("/favicon.ico")
async def favicon():
    return FileResponse(
        os.path.join(BASE_DIR, "favicon.ico"),
        media_type="image/x-icon",
    )

# =========================
# TTS CORE
# =========================
pipeline_zh = None
pipeline_en = None

# KPipeline 底层是 PyTorch 模型，同一实例在多线程并发推理时非线程安全(可能输出乱码或 segfault)。
# 每个 pipeline 配一把锁：中/英可并行，但同一 pipeline 的推理串行化。
lock_zh = threading.Lock()
lock_en = threading.Lock()


async def startup():
    global pipeline_zh, pipeline_en
    pipeline_zh = KPipeline(lang_code="z")
    pipeline_en = KPipeline(lang_code="a")
    # 预热：首次推理会惰性加载权重/编译算子，耗时数秒。启动时各跑一句短文本，
    # 把这份延迟前置到启动阶段，避免用户首句(尤其分段时切到另一 pipeline)卡顿。
    def _warmup():
        for _ in pipeline_zh("预热", voice="zf_xiaoxiao", speed=1.0):
            break
        for _ in pipeline_en("warm up", voice="af_heart", speed=1.0):
            break
    await asyncio.to_thread(_warmup)
    logger.info("v0.1 engine ready")


def to_pcm(audio: np.ndarray) -> bytes:
    audio = np.clip(audio, -1, 1)
    return (audio * 32767).astype(np.int16).tobytes()


def clean_text(text: str) -> str:
    # 去除 markdown 标记，避免被读出来
    text = re.sub(r'```[\s\S]*?```', '', text)          # 代码块
    text = re.sub(r'`([^`]*)`', r'\1', text)            # 行内代码
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text)    # 图片
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)  # 链接保留文字
    text = re.sub(r'^\s{0,3}#{1,6}\s*', '', text, flags=re.MULTILINE)  # 标题 #
    text = re.sub(r'^\s{0,3}>\s?', '', text, flags=re.MULTILINE)        # 引用 >
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)        # 无序列表
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)        # 有序列表
    text = re.sub(r'(\*\*|__)(.*?)\1', r'\2', text)     # 粗体
    # 斜体只处理 *...*；不处理 _..._，避免误伤 snake_case 标识符(如 zf_xiaoxiao)
    text = re.sub(r'\*(\S(?:.*?\S)?)\*', r'\1', text)   # 斜体 *text*
    text = re.sub(r'\*{2,}', '', text)                    # 残留 markdown 星号(如未闭合 **)
    text = re.sub(r'~~(.*?)~~', r'\1', text)            # 删除线
    text = re.sub(r'^\s*([-*_])(?:\s*\1){2,}\s*$', '', text, flags=re.MULTILINE)  # 分隔线(---/***/___)
    # 引号不发音，但会被 Kokoro 音素化成杂音(尤其结尾引号产生"嗯哼"声)，移除。
    # 直/弯双引号、中文方括号引号、书名号一并去除；保留 ASCII 单引号 ' 以免破坏英文缩写(don't/it's)。
    text = re.sub(r'["“”「」『』《》]', '', text)
    return text


def split_text(text: str):
    # 句末切分规则(与前端 splitSentences 保持一致，单一事实源)：
    #  1) 英文句末标点 .!? 后需跟空格才切——保护 3.14 / U.S.A / 省略号... 不被拆碎；
    #  2) 中文句末标点 。！？ 为零宽后切(其后通常无空格)；
    #  3) 换行 \n+ 直接切。
    parts = re.split(r'(?<=[.!?。！？]) +|(?<=[。！？])|\n+', text)
    return [t.strip() for t in parts if t and t.strip()]


# CJK 字符范围(用于英文音色剥离中文 + 可发音检测)。覆盖：
#   一-鿿  基本区(常用汉字)
#   㐀-䶿  扩展 A
#   豈-﫿  兼容表意文字
#   \U00020000-\U0002ffff  扩展 B~F(astral 平面，含大量生僻/古文字)
# 此前仅覆盖基本区，扩展区汉字既不会被英文音色剥离(漏给英文 pipeline 读错)，
# 也会让"纯扩展区文本"被误判为无可发音内容。此常量统一两处判定。
_CJK_CHARS = r"一-鿿㐀-䶿豈-﫿\U00020000-\U0002ffff"
# 中文标点/全角符号区(仅英文音色剥离时用，避免英文 pipeline 读出中文标点杂音)
_CJK_PUNCT = r"　-〿＀-￯"


def filter_for_voice(text: str, is_zh: bool) -> str:
    # 兜底防线：前端已按语言把文本路由到对应语言音色(单一事实源)，正常流程后端不会收到跨语言内容。
    # 本函数只为 REST 直传混排文本等旁路场景兜底——Kokoro 每个 pipeline 单语言，跨语言必读错。
    # 官方 issue #95/#238 证实中文 pipeline 连 DNS 等孤立英文缩写都读不出(按拼音转音标，英文无法音素化)，
    # 故中文音色一律剥离全部英文字母；英文音色一律剥离全部中文字符。剥离比硬读乱码更可接受。
    if is_zh:
        # 中文音色：移除拉丁字母及其相连的数字/常见标识符标点(next.js/snake_case 整体去除)。
        return re.sub(r"[A-Za-z][A-Za-z0-9._\-']*", ' ', text)
    # 英文音色：移除中文字符(含中文标点)，避免英文 pipeline 对中文产生不可听内容。
    return re.sub(rf'[{_CJK_CHARS}{_CJK_PUNCT}]+', ' ', text)


async def run_kokoro(text, voice, speed=1.0, cancel_event=None):
    is_zh = voice.startswith(("zf_", "zm_"))
    pipeline = pipeline_zh if is_zh else pipeline_en
    lock = lock_zh if is_zh else lock_en

    # 按音色语言剥离另一语言(中文音色去英文、英文音色去中文)——Kokoro 单语言 pipeline 跨语言必读错。
    # 过滤后仅剩标点/空格(无目标语言内容)则跳过合成，返回空 PCM。
    text = filter_for_voice(text, is_zh).strip()
    if not text or not re.search(rf'[{_CJK_CHARS}A-Za-z]', text):
        return b""

    def infer():
        # 加锁串行化同一 pipeline 的推理，避免多线程并发污染或崩溃
        with lock:
            gen = pipeline(text, voice=voice, speed=speed)
            out = []
            for r in gen:
                if cancel_event and cancel_event.is_set():
                    return None
                out.append(r.output.audio.detach().cpu().numpy())
            return np.concatenate(out) if out else None

    # 合成并发闸门：进入线程池派发推理前先取信号量，把"同时在跑的 Kokoro 推理"钉在上限内，
    # 避免大量 to_thread 任务占着线程池 worker 阻塞在语言锁上拖垮整个进程。取消仍由 infer()
    # 内的生成器循环按 chunk 粒度处理(不在此处预检 cancel，以保持"总会派发一次推理"的既有契约)。
    semaphore = _get_synthesis_semaphore()
    async with semaphore:
        audio = await asyncio.to_thread(infer)
    return to_pcm(audio) if audio is not None else b""


# =========================
# REST API：音色目录 + 流式 TTS
# =========================

KOKORO_VOICES = [
    # 中文女性（Kokoro 官方仅 4 女，见 hexgrad/Kokoro-82M VOICES.md）
    {"id": "zf_xiaoxiao", "name": "晓晓", "gender": "female", "language": "zh"},
    {"id": "zf_xiaobei",  "name": "晓贝", "gender": "female", "language": "zh"},
    {"id": "zf_xiaoni",   "name": "晓妮", "gender": "female", "language": "zh"},
    {"id": "zf_xiaoyi",   "name": "晓伊", "gender": "female", "language": "zh"},
    # 中文男性（Kokoro 官方仅 4 男）
    {"id": "zm_yunjian", "name": "云健", "gender": "male", "language": "zh"},
    {"id": "zm_yunxi",   "name": "云希", "gender": "male", "language": "zh"},
    {"id": "zm_yunxia",  "name": "云夏", "gender": "male", "language": "zh"},
    {"id": "zm_yunyang", "name": "云扬", "gender": "male", "language": "zh"},
    # 英文女性
    {"id": "af_heart",   "name": "Heart",   "gender": "female", "language": "en"},
    {"id": "af_alloy",   "name": "Alloy",   "gender": "female", "language": "en"},
    {"id": "af_aoede",   "name": "Aoede",   "gender": "female", "language": "en"},
    {"id": "af_bella",   "name": "Bella",   "gender": "female", "language": "en"},
    {"id": "af_jessica", "name": "Jessica", "gender": "female", "language": "en"},
    {"id": "af_kore",    "name": "Kore",    "gender": "female", "language": "en"},
    {"id": "af_nicole",  "name": "Nicole",  "gender": "female", "language": "en"},
    {"id": "af_nova",    "name": "Nova",    "gender": "female", "language": "en"},
    {"id": "af_river",   "name": "River",   "gender": "female", "language": "en"},
    {"id": "af_sarah",   "name": "Sarah",   "gender": "female", "language": "en"},
    {"id": "af_sky",     "name": "Sky",     "gender": "female", "language": "en"},
    # 英文男性
    {"id": "am_adam",    "name": "Adam",    "gender": "male",   "language": "en"},
    {"id": "am_echo",    "name": "Echo",    "gender": "male",   "language": "en"},
    {"id": "am_eric",    "name": "Eric",    "gender": "male",   "language": "en"},
    {"id": "am_fenrir",  "name": "Fenrir",  "gender": "male",   "language": "en"},
    {"id": "am_liam",    "name": "Liam",    "gender": "male",   "language": "en"},
    {"id": "am_michael", "name": "Michael", "gender": "male",   "language": "en"},
    {"id": "am_onyx",    "name": "Onyx",    "gender": "male",   "language": "en"},
    {"id": "am_puck",    "name": "Puck",    "gender": "male",   "language": "en"},
    {"id": "am_santa",   "name": "Santa",   "gender": "male",   "language": "en"},
]

KOKORO_VOICE_IDS = frozenset(v["id"] for v in KOKORO_VOICES)

# 合成参数的合法边界(单一事实源)：REST(TTSRequest)与 WS(parse_ws_request)共用。
# 此前两条校验路径各自硬编码 0.5/3.0 与 ("kokoro","edge")，是双事实源、易漂移。
# 注意：统一的是"规则来源"而非"越界处理"——REST 是一次性请求，越界直接 422 拒绝；
# WS 是长驻交互会话，越界 clamp 到边界更符合其语义(不因一个小偏差打断整段会话)。
SPEED_MIN = 0.5
SPEED_MAX = 3.0
VALID_ENGINES = ("kokoro", "edge")


def _clamp_speed(value: float) -> float:
    return min(max(value, SPEED_MIN), SPEED_MAX)


class TTSRequest(BaseModel):
    text: str
    engine: str = "kokoro"
    voice: str = "zf_xiaoxiao"
    speed: float = 1.0
    ssml: bool = False

    @field_validator("text")
    @classmethod
    def check_text_length(cls, v):
        if len(v) > MAX_TEXT_LENGTH:
            raise ValueError(f"text length must be <= {MAX_TEXT_LENGTH}")
        return v

    @field_validator("engine")
    @classmethod
    def check_engine(cls, v):
        if v not in VALID_ENGINES:
            raise ValueError("engine must be 'kokoro' or 'edge'")
        return v

    @field_validator("speed")
    @classmethod
    def check_speed(cls, v):
        if v < SPEED_MIN or v > SPEED_MAX:
            raise ValueError(f"speed must be between {SPEED_MIN} and {SPEED_MAX}")
        return v

    @field_validator("voice")
    @classmethod
    def check_voice(cls, v, info):
        if info.data.get("engine") == "kokoro" and v not in KOKORO_VOICE_IDS:
            raise ValueError("unknown Kokoro voice")
        return v

    @field_validator("ssml")
    @classmethod
    def check_ssml(cls, v):
        if v:
            raise ValueError("raw SSML is not supported")
        return v


def parse_ws_request(req: dict):
    raw_text = req.get("text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        return {"type": "error", "message": "缺少有效的 text 字段"}
    if len(raw_text) > MAX_TEXT_LENGTH:
        return {"type": "error", "message": f"文本超过长度限制（最大 {MAX_TEXT_LENGTH} 字）"}

    text = clean_text(raw_text).strip()
    if not text:
        return {"type": "error", "message": "文本清洗后为空"}

    engine = req.get("engine", "kokoro")
    voice = req.get("voice", "zf_xiaoxiao")
    # WS 为长驻交互会话：speed 越界静默夹取、非数字回落 1.0(不打断会话)，
    # 与 REST 的越界即 422 语义不同但共用同一组边界常量(SPEED_MIN/MAX)，避免双事实源漂移。
    try:
        speed = float(req.get("speed", 1.0))
    except (ValueError, TypeError):
        speed = 1.0
    speed = _clamp_speed(speed)

    if engine not in VALID_ENGINES:
        return {"type": "error", "message": f"未知合成引擎：{engine}"}
    if engine == "kokoro" and voice not in KOKORO_VOICE_IDS:
        return {"type": "error", "message": f"未知 Kokoro 音色：{voice}"}
    return {"type": "ok", "text": text, "engine": engine, "voice": voice, "speed": speed}

_edge_voices_cache = None
_edge_voices_cache_expires_at = 0.0

async def _get_edge_voices():
    global _edge_voices_cache, _edge_voices_cache_expires_at
    now = time.monotonic()
    if _edge_voices_cache is not None and now < _edge_voices_cache_expires_at:
        return _edge_voices_cache
    try:
        # 仅在成功时写缓存；失败返回空列表但不缓存，下次请求可重试(避免瞬时网络抖动永久毒化)
        _edge_voices_cache = await edge_tts.list_voices()
        _edge_voices_cache_expires_at = now + EDGE_VOICES_CACHE_TTL_SECONDS
        return _edge_voices_cache
    except Exception as exc:
        if _edge_voices_cache is not None:
            logger.warning("Edge voices refresh failed; serving stale cache: %s", exc)
            return _edge_voices_cache
        return []

@app.get("/api/voices")
async def api_voices():
    edge_voices = await _get_edge_voices()
    return {
        "kokoro": KOKORO_VOICES,
        "edge": [
            {
                "id": v["ShortName"],
                "name": v.get("FriendlyName", v["ShortName"]),
                "gender": v.get("Gender", ""),
                "locale": v.get("Locale", ""),
            }
            for v in edge_voices
        ],
    }
async def _create_mp3_encoder(engine: str):
    acquired = await _ffmpeg_limiter.acquire()
    if not acquired:
        raise HTTPException(status_code=429, detail="ffmpeg process limit reached")
    if engine == "edge":
        try:
            return await asyncio.create_subprocess_exec(
                "ffmpeg", "-i", "pipe:0",
                "-codec:a", "libmp3lame", "-qscale:a", "2",
                "-f", "mp3", "pipe:1", "-loglevel", "quiet",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
            )
        except BaseException:
            _ffmpeg_limiter.release()
            raise
    try:
        return await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "pipe:0",
            "-codec:a", "libmp3lame", "-qscale:a", "2",
            "-f", "mp3", "pipe:1", "-loglevel", "quiet",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
    except BaseException:
        _ffmpeg_limiter.release()
        raise


async def _feed_mp3(proc, text: str, engine: str, voice: str, speed: float, first_audio=None):
    # first_audio(asyncio.Event)：产出首个音频源字节时置位，供 REST 预检判定"是否真正产出音频"。
    # 判定基于喂入 ffmpeg 的音频源(而非 ffmpeg 输出)，避免 mp3 muxer 空输入仍吐头字节造成误判成功。
    # 关键：_mark() 必须在 write/drain 之前置位。预检期 _stream_mp3 尚未启动、无人读 proc.stdout，
    # 若首块 PCM 撑满 ffmpeg 管道缓冲，drain() 会阻塞；把 _mark() 放 drain 之后将导致 first_audio
    # 永不置位、预检 hang 死。判定信号本就是"音频源已产出(pcm 非空)"，无需等 ffmpeg 消费。
    def _mark():
        if first_audio is not None:
            first_audio.set()
    try:
        if engine == "kokoro":
            sentences = split_text(text)
            for sent in sentences:
                pcm = await run_kokoro(sent, voice, speed)
                if pcm:
                    _mark()
                    proc.stdin.write(pcm)
                    await proc.stdin.drain()
        else:
            rate = f"{int((speed - 1) * 100):+d}%"
            comm = edge_tts.Communicate(text, voice, rate=rate)
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    _mark()
                    proc.stdin.write(chunk["data"])
                    await proc.stdin.drain()
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass


async def _reap_proc(proc):
    # 回收 ffmpeg 子进程，避免僵尸。幂等：已退出则仅 wait。
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    await proc.wait()
    _ffmpeg_limiter.release()


async def _create_edge_pcm_decoder():
    acquired = await _ffmpeg_limiter.acquire()
    if not acquired:
        raise RuntimeError("ffmpeg process limit reached")
    try:
        return await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", "pipe:0", "-f", "s16le", "-ar", "24000", "-ac", "1",
            "pipe:1", "-loglevel", "quiet",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        )
    except BaseException:
        _ffmpeg_limiter.release()
        raise


async def _reap_edge_pcm_decoder(process):
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()
    _ffmpeg_limiter.release()


async def _start_synthesis(text: str, engine: str, voice: str, speed: float, request_id: str = ""):
    # 预检合成：启动 ffmpeg + feed，在提交 HTTP 200 前判定是否真正产出音频。
    # HTTP 流式一旦 yield 首字节即锁定 200 状态码，无法再改错误码；故失败判定必须前置于此。
    # 判定信号取自"喂入 ffmpeg 的音频源首字节"(first_audio)，而非 ffmpeg 输出——
    # mp3 muxer 对空输入仍会吐 ID3/Xing 头，以输出判定会把失败误判成功。
    # 成功返回 (proc, feed_task) 交由 _stream_mp3 流式读取；失败在此抛 HTTPException(200 未提交)。
    proc = await _create_mp3_encoder(engine)
    first_audio = asyncio.Event()
    feed_task = asyncio.create_task(_feed_mp3(proc, text, engine, voice, speed, first_audio))
    first_audio_wait = asyncio.create_task(first_audio.wait())

    # 等"首音频事件"或"feed 结束"二者先到者：有音频→放行流式；feed 先结束且无音频→判失败。
    # 若客户端在预检期断开/任务取消，必须回收 feed_task 与 ffmpeg；否则 StreamingResponse 尚未
    # 接管资源，_stream_mp3 的 finally 不会执行。
    try:
        timeout = TTS_SYNTHESIS_TIMEOUT_SECONDS or None
        done, _ = await asyncio.wait(
            {first_audio_wait, feed_task},
            return_when=asyncio.FIRST_COMPLETED,
            timeout=timeout,
        )
        if not done:
            first_audio_wait.cancel()
            if not feed_task.done():
                feed_task.cancel()
                try:
                    await feed_task
                except asyncio.CancelledError:
                    pass
            await _reap_proc(proc)
            raise HTTPException(status_code=504, detail="synthesis timed out")
    except BaseException:
        first_audio_wait.cancel()
        if not feed_task.done():
            feed_task.cancel()
            try:
                await feed_task
            except asyncio.CancelledError:
                pass
        elif not feed_task.cancelled():
            # 竞态：取消与 feed 结束同刻发生，feed_task 已 done。若带异常必须取出，
            # 否则 GC 时抛 "Task exception was never retrieved"。
            feed_task.exception()
        await _reap_proc(proc)
        raise

    if first_audio.is_set():
        first_audio_wait.cancel()  # 有音频：feed_task 继续喂后续内容，仅取消等待哨兵
        return proc, feed_task

    # 无音频产出：清理哨兵与子进程，区分"异常失败"与"无可发音内容"，诚实回传错误(不伪装成功)
    first_audio_wait.cancel()
    exc = feed_task.exception()
    await _reap_proc(proc)
    if exc is not None:
        # edge 归为上游(微软)故障 502，kokoro 归为本机引擎故障 500；不用 4xx 以免误判可重试的抖动
        logger.error("REST synthesis failed request_id=%s engine=%s voice=%s: %s",
                     request_id, engine, voice, exc,
                     exc_info=(type(exc), exc, exc.__traceback__))
        raise HTTPException(status_code=502 if engine == "edge" else 500,
                            detail="synthesis failed")
    # feed 正常结束却零音频：文本经语言过滤后无目标语言内容(如中文文本配英文音色)或纯标点
    raise HTTPException(status_code=400, detail="no speakable content for the given voice")


async def _stream_mp3(proc, feed_task, engine, voice):
    # 流式读取 ffmpeg 输出。proc/feed_task 由 _start_synthesis 预检后传入(首音频已确认)。
    try:
        while True:
            read = proc.stdout.read(65536)
            if TTS_SYNTHESIS_TIMEOUT_SECONDS:
                try:
                    chunk = await asyncio.wait_for(read, TTS_SYNTHESIS_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    logger.error("REST stream timed out after start (engine=%s voice=%s)", engine, voice)
                    if proc.returncode is None:
                        proc.kill()
                    break
            else:
                chunk = await read
            if not chunk:
                break
            yield chunk
    except (asyncio.CancelledError, GeneratorExit):
        if proc.returncode is None:
            proc.kill()
        raise
    finally:
        if not feed_task.done():
            feed_task.cancel()
            try:
                await feed_task
            except asyncio.CancelledError:
                pass
        elif not feed_task.cancelled():
            # 首块之后 feed 才失败：200 已提交无法改状态码，只能记日志(流会提前截断)
            exc = feed_task.exception()
            if exc:
                logger.error("feed task failed after stream start (engine=%s voice=%s): %s",
                             engine, voice, exc, exc_info=(type(exc), exc, exc.__traceback__))
        await _reap_proc(proc)


@app.post("/api/tts")
async def api_tts(req: TTSRequest, request: Request, download: bool = False):
    request_id = _request_id_from_header(request.headers.get("x-request-id"))
    if pipeline_zh is None or pipeline_en is None:
        raise HTTPException(status_code=503, detail="TTS engine not ready", headers={"X-Request-ID": request_id})
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text must not be empty", headers={"X-Request-ID": request_id})
    cleaned = clean_text(text).strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="text is empty after cleaning", headers={"X-Request-ID": request_id})
    # 预检：在提交 200 前确认真正产出音频；失败在此抛 HTTPException(正确错误码)。
    try:
        proc, feed_task = await _start_synthesis(cleaned, req.engine, req.voice, req.speed, request_id)
    except HTTPException as exc:
        logger.warning("REST request failed request_id=%s engine=%s voice=%s status=%s detail=%s",
                       request_id, req.engine, req.voice, exc.status_code, exc.detail)
        exc.headers = {**(exc.headers or {}), "X-Request-ID": request_id}
        raise
    # 预检成功后、返回 StreamingResponse 前若出岔子，生成器可能永不迭代 → finally 不触发。
    # 不把资源回收责任外包给框架驱动行为，此处自兜底：构造响应失败即就地回收 proc/feed_task。
    try:
        disposition = "attachment; filename=tts-output.mp3" if download else "inline"
        return StreamingResponse(
            _stream_mp3(proc, feed_task, req.engine, req.voice),
            media_type="audio/mpeg",
            headers={"Content-Disposition": disposition, "X-Request-ID": request_id},
        )
    except BaseException:
        if not feed_task.done():
            feed_task.cancel()
        elif not feed_task.cancelled():
            # 同族竞态：feed_task 已 done 带异常时取出，避免 GC "never retrieved" 告警
            feed_task.exception()
        await _reap_proc(proc)
        raise


def _preview_text(engine: str, voice: str) -> str:
    if engine == "kokoro" and voice.startswith(("zf_", "zm_")):
        return "你好，这是音色试听。"
    return "Hello, this is a short voice preview."


@app.get("/api/voices/preview")
async def api_voice_preview(
    request: Request,
    engine: str = Query(default="kokoro"),
    voice: str = Query(default="af_heart"),
    speed: float = Query(default=1.0, ge=SPEED_MIN, le=SPEED_MAX),
):
    request_id = _request_id_from_header(request.headers.get("x-request-id"))
    if pipeline_zh is None or pipeline_en is None:
        raise HTTPException(status_code=503, detail="TTS engine not ready", headers={"X-Request-ID": request_id})
    if engine not in VALID_ENGINES:
        raise HTTPException(status_code=422, detail="engine must be 'kokoro' or 'edge'", headers={"X-Request-ID": request_id})
    if engine == "kokoro" and voice not in KOKORO_VOICE_IDS:
        raise HTTPException(status_code=422, detail="unknown Kokoro voice", headers={"X-Request-ID": request_id})
    text = _preview_text(engine, voice)
    try:
        proc, feed_task = await _start_synthesis(text, engine, voice, speed, request_id)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "X-Request-ID": request_id}
        raise
    try:
        return StreamingResponse(
            _stream_mp3(proc, feed_task, engine, voice),
            media_type="audio/mpeg",
            headers={"Content-Disposition": "inline", "X-Request-ID": request_id},
        )
    except BaseException:
        if not feed_task.done():
            feed_task.cancel()
        elif not feed_task.cancelled():
            feed_task.exception()
        await _reap_proc(proc)
        raise


# =========================
# 合成分发：各引擎把 PCM 产出到 queue，随时响应 cancel_event 中止
# =========================
async def synth_kokoro(units, voice, speed, queue, ws, cancel_event):
    # units 是前端按 \n 传来的合成单元(句/语言子片段)，逐单元清洗+合成。
    # 每个单元恰好发一个 seg(即使清洗后为空)，保证前后端单元数 1:1 对齐，
    # 变速续播的句级时间线不漂移(后端不再二次按标点切分)。
    for u in units:
        if ws.client_state == WebSocketState.DISCONNECTED or cancel_event.is_set():
            break
        cleaned = clean_text(u).strip()
        # 句边界标记：前端据此建句级时间线，支持变速时按句定位与续播
        await queue.put({"type": "seg", "text": cleaned})
        if not cleaned:
            continue  # 清洗后为空(如独立图片/分隔线)：仍发 seg 保持计数，无音频
        pcm = await run_kokoro(cleaned, voice, speed, cancel_event)
        if cancel_event.is_set():
            break
        for i in range(0, len(pcm), 2048):
            await queue.put(pcm[i:i + 2048])


async def synth_edge(text, voice, speed, queue, ws, cancel_event):
    rate = f"{int((speed - 1) * 100):+d}%"
    communicate = edge_tts.Communicate(text, voice, rate=rate)

    process = await _create_edge_pcm_decoder()
    # Edge 整段连续流式，无法逐句切分音频边界，故整段发一个句边界标记(run 级粒度)。
    # 前端据此建时间线；变速时 Edge 段按整段重合成(云端成本高，run 级是自然单元)。
    await queue.put({"type": "seg", "text": text})

    async def feed():
        # 无论是否异常，stdin 必须关闭，否则 ffmpeg 不 EOF，read() 永久阻塞
        try:
            async for chunk in communicate.stream():
                if ws.client_state == WebSocketState.DISCONNECTED or cancel_event.is_set():
                    break
                if chunk["type"] == "audio":
                    process.stdin.write(chunk["data"])
                    await process.stdin.drain()
        finally:
            try:
                process.stdin.close()
            except Exception:
                pass

    async def read():
        # ffmpeg 管道每次返回字节数任意；按 16-bit 样本(2 字节)对齐后再发，
        # 奇数尾字节留到下一帧，避免高低字节错位导致持续杂音。
        leftover = b""
        while True:
            if ws.client_state == WebSocketState.DISCONNECTED or cancel_event.is_set():
                break
            data = await process.stdout.read(2048)
            if not data:
                break
            data = leftover + data
            even = len(data) & ~1
            if even:
                await queue.put(data[:even])
            leftover = data[even:]

    # return_exceptions=True：一方异常不会让另一方变成孤儿 task
    try:
        results = await asyncio.gather(feed(), read(), return_exceptions=True)
        for r in results:
            if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                logger.error("synth_edge task failed: %s", r, exc_info=(type(r), r, r.__traceback__))
                raise r
    finally:
        # 无论正常/异常，都确保 ffmpeg 子进程被回收，避免僵尸进程
        await _reap_edge_pcm_decoder(process)


# =========================
# WS CORE
# =========================
@app.websocket("/ws/tts")
async def ws_tts(ws: WebSocket):
    sender_task = None
    # WS 握手不经 HTTP 鉴权中间件，此处单独校验。同源自有页面(index.html)用 Origin 免密；
    # 外部客户端(如 CRX)浏览器无法为 WS 设自定义头，改用 ?key= 查询参数。未配置密钥 = 完全开放。
    # 校验失败以 1008(Policy Violation)在 accept 前拒绝握手。
    if TTS_API_KEY and not _is_same_origin(ws.headers):
        if not _key_matches(ws.query_params.get("key", "").strip()):
            await ws.close(code=1008)
            return
    await ws.accept()

    queue = asyncio.Queue(maxsize=32)

    async def sender():
        while True:
            item = await queue.get()
            try:
                if item is None:
                    break
                if ws.client_state == WebSocketState.DISCONNECTED:
                    continue  # 丢弃剩余数据但继续 drain，避免生产者在 put 时永久阻塞
                try:
                    # bytes → PCM 二进制帧；dict → JSON 标记(如句边界 seg)。同队列保证顺序
                    if isinstance(item, (bytes, bytearray)):
                        await ws.send_bytes(item)
                    else:
                        await ws.send_json(item)
                except Exception:
                    # 发送失败(连接已断)：继续 drain 队列，不退出，防止后续 put 堆积死锁
                    pass
            finally:
                queue.task_done()

    sender_task = asyncio.create_task(sender())

    try:
        while True:
            # 每个请求独立的取消信号：断连或收到任意消息时置位。
            # 注意：合成期间收到的消息只作为取消信号，消息内容会被丢弃；客户端若要继续合成需再发新请求。
            cancel_event = asyncio.Event()

            msg = await ws.receive_text()
            try:
                req = json.loads(msg)
            except (ValueError, TypeError):
                await ws.send_json({"type": "error", "message": "无效的 JSON 请求"})
                continue

            parsed = parse_ws_request(req)
            if parsed["type"] == "error":
                await ws.send_json(parsed)
                continue

            text = parsed["text"]
            engine = parsed["engine"]
            voice = parsed["voice"]
            speed = parsed["speed"]

            await ws.send_json({"type": "start"})

            # reader 任务独占 ws.receive：合成期间监听客户端。收到任何消息(停止/新请求)
            # 或断连都立即置位 cancel_event，让合成循环中止，避免"停止后仍满载合成"。
            async def watch_cancel():
                try:
                    await ws.receive()
                except Exception:
                    pass
                cancel_event.set()

            watcher = asyncio.create_task(watch_cancel())

            synth_error = None
            try:
                async def run_synthesis():
                    if engine == "kokoro":
                        # 前端用 \n 连接合成单元(句/语言子片段)，按 \n 还原即 1:1 对齐，
                        # 不再二次按标点切分，杜绝前后端句数漂移(变速续播时间线依赖此对齐)
                        units = [u for u in text.split("\n") if u.strip()]
                        await synth_kokoro(units, voice, speed, queue, ws, cancel_event)
                    else:
                        await synth_edge(text, voice, speed, queue, ws, cancel_event)

                if TTS_SYNTHESIS_TIMEOUT_SECONDS:
                    try:
                        await asyncio.wait_for(run_synthesis(), TTS_SYNTHESIS_TIMEOUT_SECONDS)
                    except asyncio.TimeoutError as e:
                        cancel_event.set()
                        raise TimeoutError("synthesis timed out") from e
                else:
                    await run_synthesis()
            except Exception as e:
                # 合成期异常(非法 Edge 音色/引擎故障等)不得击穿主循环断连：记录日志 + 回传错误，
                # 保活连接以处理后续请求。这是长驻循环的正确控制流，非静默 fallback(错误已显式暴露)。
                synth_error = e
                logger.error("合成失败(engine=%s voice=%s): %s", engine, voice,
                             e, exc_info=(type(e), e, e.__traceback__))
            finally:
                # 必须 await 确保 watcher 彻底退出 ws.receive，否则下轮主循环
                # ws.receive_text 会与其并发读同一 socket，Starlette 直接报错
                watcher.cancel()
                try:
                    await watcher
                except asyncio.CancelledError:
                    pass

            await queue.put(None)
            await sender_task
            if ws.client_state != WebSocketState.DISCONNECTED:
                if synth_error is not None:
                    # 合成失败：回传 error 而非 end，让前端脱离"合成中"并提示，不伪装成功
                    await ws.send_json({"type": "error", "message": "合成失败，请重试或更换音色"})
                else:
                    await ws.send_json({"type": "end"})

            sender_task = asyncio.create_task(sender())

    except WebSocketDisconnect:
        pass
    finally:
        if sender_task and not sender_task.done():
            sender_task.cancel()
