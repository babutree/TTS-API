# -*- coding: utf-8 -*-
import asyncio
import hmac
import json
import logging
import math
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
import uuid
from collections import deque
from contextlib import asynccontextmanager
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlsplit
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Request
from starlette.websockets import WebSocketState
from fastapi.responses import FileResponse, Response, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, field_validator
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from starlette.datastructures import MutableHeaders
from starlette.requests import ClientDisconnect
from starlette.exceptions import HTTPException as StarletteHTTPException

import edge_tts
from kokoro import KPipeline

LOG_MAX_LINES = 1000
LOG_MAX_CHARS = 256000
LOG_MAX_RECORD_CHARS = 4096
DEFAULT_MAX_TEXT_LENGTH = 100000
DEFAULT_EDGE_VOICES_CACHE_TTL_SECONDS = 86400.0
DEFAULT_EDGE_RETRY_MAX_ATTEMPTS = 2
DEFAULT_EDGE_RETRY_BASE_DELAY_SECONDS = 0.25
DEFAULT_EDGE_VOICES_FAILURE_COOLDOWN_SECONDS = 5.0
DEFAULT_EDGE_VOICES_REQUEST_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_FFMPEG_PROCESSES = 2
DEFAULT_MAX_SYNTHESIS_CONCURRENCY = 2
DEFAULT_MAX_SYNTHESIS_WAITERS = 16
DEFAULT_MAX_REQUEST_BODY_BYTES = 1024 * 1024
DEFAULT_RESPONSE_WRITE_TIMEOUT_SECONDS = 30.0
DEFAULT_KOKORO_MAX_UNIT_CHARS = 2000
EDGE_DECODER_EXIT_GRACE_SECONDS = 1.0
REQUEST_ID_MAX_LENGTH = 64
VOICE_MAX_LENGTH = 256
_VOICE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _sanitize_log_line(value: str) -> str:
    text = str(value).encode("utf-8", "backslashreplace").decode("utf-8")
    out = []
    for char in text:
        if (
            unicodedata.category(char).startswith("C")
            or char in "\u2028\u2029"
        ):
            codepoint = ord(char)
            escape = "\\u%04x" if codepoint <= 0xFFFF else "\\U%08x"
            out.append(escape % codepoint)
        else:
            out.append(char)
    return "".join(out)


def _truncate_log_line(line: str, limit: int) -> str:
    if len(line) <= limit:
        return line
    marker = "...[truncated]"
    if limit <= len(marker):
        return line[:limit]
    return line[: limit - len(marker)] + marker


class RingBufferHandler(logging.Handler):
    # 滚动保留最新 N 行日志：写满后丢最旧(deque maxlen)，避免日志无限增长。
    # 同时透传到 stdout，兼容 docker logs 实时查看。
    def __init__(
        self,
        max_lines: int,
        max_chars: int = LOG_MAX_CHARS,
        max_record_chars: int = LOG_MAX_RECORD_CHARS,
    ):
        super().__init__()
        if max_lines < 1 or max_chars < 1 or max_record_chars < 1:
            raise ValueError("log buffer limits must be positive")
        self.buffer = deque(maxlen=max_lines)
        self.max_chars = max_chars
        self.max_record_chars = min(max_record_chars, max_chars)
        self._stream = logging.StreamHandler()

    def setFormatter(self, fmt):
        super().setFormatter(fmt)
        self._stream.setFormatter(fmt)

    def emit(self, record):
        try:
            rendered = _sanitize_log_line(self.format(record))
            rendered = _truncate_log_line(rendered, self.max_record_chars)
            self.buffer.append(rendered)
            while self.buffer and sum(map(len, self.buffer)) > self.max_chars:
                self.buffer.popleft()

            self._stream.acquire()
            try:
                self._stream.stream.write(rendered + self._stream.terminator)
                self._stream.flush()
            finally:
                self._stream.release()
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
    parsed = int(value or DEFAULT_MAX_TEXT_LENGTH)
    if parsed < 1:
        raise ValueError("MAX_TEXT_LENGTH must be >= 1")
    return parsed


def parse_edge_voices_cache_ttl(value: str | None) -> float:
    ttl = float(value or DEFAULT_EDGE_VOICES_CACHE_TTL_SECONDS)
    if not math.isfinite(ttl) or ttl < 0:
        raise ValueError("EDGE_VOICES_CACHE_TTL_SECONDS must be finite and >= 0")
    return ttl


def parse_optional_positive_float(value: str | None, name: str) -> float:
    parsed = float(value or 0)
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{name} must be finite and >= 0")
    return parsed


def parse_positive_int(value: str | None, name: str, default: int) -> int:
    parsed = int(value or default)
    if parsed < 1:
        raise ValueError(f"{name} must be >= 1")
    return parsed


def parse_api_key(value: str | None) -> str:
    if value is None or value == "":
        return ""
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("TTS_API_KEY must be empty or a non-blank ASCII secret")
    try:
        cleaned.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("TTS_API_KEY must contain ASCII characters only") from exc
    return cleaned


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
EDGE_RETRY_MAX_ATTEMPTS = parse_positive_int(
    os.environ.get("EDGE_RETRY_MAX_ATTEMPTS"),
    "EDGE_RETRY_MAX_ATTEMPTS",
    DEFAULT_EDGE_RETRY_MAX_ATTEMPTS,
)
EDGE_RETRY_BASE_DELAY_SECONDS = parse_optional_positive_float(
    os.environ.get("EDGE_RETRY_BASE_DELAY_SECONDS")
    or str(DEFAULT_EDGE_RETRY_BASE_DELAY_SECONDS),
    "EDGE_RETRY_BASE_DELAY_SECONDS",
)
EDGE_VOICES_FAILURE_COOLDOWN_SECONDS = parse_optional_positive_float(
    os.environ.get("EDGE_VOICES_FAILURE_COOLDOWN_SECONDS")
    or str(DEFAULT_EDGE_VOICES_FAILURE_COOLDOWN_SECONDS),
    "EDGE_VOICES_FAILURE_COOLDOWN_SECONDS",
)
EDGE_VOICES_REQUEST_TIMEOUT_SECONDS = parse_optional_positive_float(
    os.environ.get("EDGE_VOICES_REQUEST_TIMEOUT_SECONDS")
    or str(DEFAULT_EDGE_VOICES_REQUEST_TIMEOUT_SECONDS),
    "EDGE_VOICES_REQUEST_TIMEOUT_SECONDS",
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
TTS_MAX_SYNTHESIS_WAITERS = parse_positive_int(
    os.environ.get("TTS_MAX_SYNTHESIS_WAITERS"),
    "TTS_MAX_SYNTHESIS_WAITERS",
    DEFAULT_MAX_SYNTHESIS_WAITERS,
)
TTS_MAX_REQUEST_BODY_BYTES = parse_positive_int(
    os.environ.get("TTS_MAX_REQUEST_BODY_BYTES"),
    "TTS_MAX_REQUEST_BODY_BYTES",
    DEFAULT_MAX_REQUEST_BODY_BYTES,
)
TTS_RESPONSE_WRITE_TIMEOUT_SECONDS = parse_optional_positive_float(
    os.environ.get("TTS_RESPONSE_WRITE_TIMEOUT_SECONDS")
    or str(DEFAULT_RESPONSE_WRITE_TIMEOUT_SECONDS),
    "TTS_RESPONSE_WRITE_TIMEOUT_SECONDS",
)
KOKORO_MAX_UNIT_CHARS = parse_positive_int(
    os.environ.get("KOKORO_MAX_UNIT_CHARS"),
    "KOKORO_MAX_UNIT_CHARS",
    DEFAULT_KOKORO_MAX_UNIT_CHARS,
)


class FfmpegLimiter:
    def __init__(self, max_active: int):
        self.max_active = max_active
        self.active = 0
        self.active_prefetch = 0
        self._lock = asyncio.Lock()

    async def acquire(self, prefetch: bool = False) -> bool:
        async with self._lock:
            if self.active >= self.max_active:
                return False
            # 投机预取不得占满全部 decoder：至少给普通 REST/WS 请求保留一个槽。
            # max_active=1 时预取直接失败并由浏览器按主请求回退。
            if prefetch and self.active_prefetch >= self.max_active - 1:
                return False
            self.active += 1
            if prefetch:
                self.active_prefetch += 1
            return True

    def release(self, prefetch: bool = False):
        if self.active > 0:
            self.active -= 1
        if prefetch and self.active_prefetch > 0:
            self.active_prefetch -= 1


_ffmpeg_limiter = FfmpegLimiter(TTS_MAX_FFMPEG_PROCESSES)

# 合成并发闸门：Kokoro 推理跑在默认线程池(min(32, cpu+4))里，且按语言锁串行化同一 pipeline。
# 若并发请求过多，大量 to_thread 任务会占着线程池 worker 阻塞在语言锁上，拖垮整个线程池
# (连非 TTS 的 to_thread 一起饿死)。ffmpeg 子进程数由 FfmpegLimiter 兜底，但 WS 的 Kokoro
# 不产生 ffmpeg，故此前无任何闸门。这里用信号量给"合成"本身设并发上限，REST/WS 共用。
# 阻塞式(排队)而非 fail-fast：对 WS 交互点按语义更自然，且不伪装成功。惰性创建以绑定运行时事件循环。
_synthesis_semaphore = None
_synthesis_waiter_count = 0
_synthesis_waiter_lock = threading.Lock()
_kokoro_prefetch_reserved = 0
_kokoro_prefetch_lock = threading.Lock()


def _get_synthesis_semaphore() -> asyncio.Semaphore:
    global _synthesis_semaphore
    if _synthesis_semaphore is None:
        _synthesis_semaphore = asyncio.Semaphore(TTS_MAX_SYNTHESIS_CONCURRENCY)
    return _synthesis_semaphore


class _SynthesisQueueFull(RuntimeError):
    pass


class _SynthesisPrefetchFull(RuntimeError):
    pass


def _reserve_synthesis_waiter() -> None:
    global _synthesis_waiter_count
    with _synthesis_waiter_lock:
        if _synthesis_waiter_count >= TTS_MAX_SYNTHESIS_WAITERS:
            raise _SynthesisQueueFull("Kokoro synthesis queue is full")
        _synthesis_waiter_count += 1


def _release_synthesis_waiter() -> None:
    global _synthesis_waiter_count
    with _synthesis_waiter_lock:
        if _synthesis_waiter_count > 0:
            _synthesis_waiter_count -= 1


def _reserve_kokoro_prefetch() -> None:
    global _kokoro_prefetch_reserved
    with _kokoro_prefetch_lock:
        if _kokoro_prefetch_reserved >= TTS_MAX_SYNTHESIS_CONCURRENCY - 1:
            raise _SynthesisPrefetchFull(
                "Kokoro prefetch capacity is reserved for normal synthesis"
            )
        _kokoro_prefetch_reserved += 1


def _release_kokoro_prefetch() -> None:
    global _kokoro_prefetch_reserved
    with _kokoro_prefetch_lock:
        if _kokoro_prefetch_reserved > 0:
            _kokoro_prefetch_reserved -= 1

# =========================
# API Key 鉴权
# =========================
# TTS_API_KEY 空 = 完全开放(兼容本地直连)；非空 = 对外部程序化客户端(如浏览器扩展)启用鉴权。
# 自有同源页面(index.html / api 文档)免密：它们与后端同源，供人直接使用。
# 网络边界仍应由反向代理(Caddy)兜底；此处密钥为外部集成提供受控接入能力。
TTS_API_KEY = parse_api_key(os.environ.get("TTS_API_KEY"))

# 永久豁免路径：健康检查(docker healthcheck 依赖)、自有页面与静态资源。否则页面/探活打不开。
_AUTH_EXEMPT_PATHS = frozenset({"/", "/index.html", "/api", "/static/style.css", "/favicon.ico"})


def _host_of(value: str) -> str:
    # 从 Origin/Referer 取 host(含端口)。Origin 形如 scheme://host:port；Referer 是完整 URL。
    if not value:
        return ""
    try:
        return urlsplit(value).netloc
    except ValueError:
        return ""


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
    try:
        return hmac.compare_digest(
            provided.encode("utf-8"), TTS_API_KEY.encode("utf-8")
        )
    except (AttributeError, TypeError, UnicodeError):
        return False


def _extract_rest_key(request_headers) -> str:
    # REST 密钥来源：Authorization: Bearer <key> 或 X-API-Key。
    auth = request_headers.get("authorization", "").strip()
    parts = auth.split(None, 1)
    if parts and parts[0].casefold() == "bearer":
        return parts[1].strip() if len(parts) == 2 else ""
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


def _is_v1_path(path: str) -> bool:
    return path == "/v1" or path.startswith("/v1/")


def _is_legacy_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def _request_id_for_request(request: Request) -> str:
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        return request_id
    request_id = _request_id_from_header(request.headers.get("x-request-id"))
    request.scope.setdefault("state", {})["request_id"] = request_id
    return request_id


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await startup()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


class _RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """在 JSON 解析前限制实际 ASGI body 字节数；固定长度与 chunked 同规则。"""

    def __init__(self, asgi_app):
        self.app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        limit = TTS_MAX_REQUEST_BODY_BYTES
        raw_length = request.headers.get("content-length")
        if raw_length:
            try:
                declared_length = int(raw_length)
            except ValueError:
                declared_length = None
            if declared_length is not None and declared_length > limit:
                await self._reject(scope, receive, send, request.headers)
                return

        received = 0
        response_started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _RequestBodyTooLarge()
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLarge:
            if response_started:
                raise
            await self._reject(scope, receive, send, request.headers)

    @staticmethod
    async def _reject(scope, receive, send, headers):
        state = scope.setdefault("state", {})
        request_id = state.get("request_id") or _request_id_from_header(
            headers.get("x-request-id")
        )
        state["request_id"] = request_id
        if _is_v1_path(scope.get("path", "")):
            content = _openai_error_body(
                "Request body is too large",
                "invalid_request_error",
                "request_too_large",
            )
        else:
            content = {"detail": "request body is too large"}
        response = JSONResponse(
            status_code=413,
            content=content,
            headers={"X-Request-ID": request_id},
        )
        await response(scope, receive, send)


class ApiKeyMiddleware:
    """纯 ASGI 鉴权；保持断连取消语义，不经 BaseHTTPMiddleware 改写为 500。"""

    def __init__(self, asgi_app):
        self.app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        path = scope.get("path", "")
        if _is_v1_path(path):
            request_id = _request_id_from_header(
                request.headers.get("x-request-id")
            )
            scope.setdefault("state", {})["request_id"] = request_id
            if TTS_API_KEY and not _key_matches(
                _extract_rest_key(request.headers)
            ):
                response = JSONResponse(
                    status_code=401,
                    content=_openai_error_body(
                        "Missing or invalid API key",
                        "invalid_request_error",
                        "invalid_api_key",
                    ),
                    headers={"X-Request-ID": request_id},
                )
                await response(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return

        # 日志包含诊断信息，配置 key 时必须在路由/query 校验前强鉴权，且不吃同源豁免。
        if path == "/api/logs":
            if TTS_API_KEY and not _key_matches(
                _extract_rest_key(request.headers)
            ):
                response = JSONResponse(
                    status_code=401,
                    content={"detail": "缺少或错误的 API Key"},
                )
                await response(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return

        # /api/auth 保留端点级探测语义，不把错误 key 改成通用 401 body。
        authorized = (
            path == "/api/auth"
            or not TTS_API_KEY
            or path in _AUTH_EXEMPT_PATHS
            or _key_matches(_extract_rest_key(request.headers))
            or _is_same_origin(request.headers)
        )
        if authorized:
            await self.app(scope, receive, send)
            return

        response = JSONResponse(
            status_code=401,
            content={"detail": "缺少或错误的 API Key"},
        )
        await response(scope, receive, send)


class _HttpRequestDisconnected(Exception):
    """请求已由 ASGI receive 明确报告断连，无 HTTP 响应可再发送。"""


class _HttpDisconnectMiddleware:
    """让已确认断连的 HTTP 请求静默结束，避免服务器误记应用异常。"""

    def __init__(self, asgi_app):
        self.app = asgi_app

    async def __call__(self, scope, receive, send):
        try:
            await self.app(scope, receive, send)
        except (_HttpRequestDisconnected, ClientDisconnect):
            if scope["type"] != "http":
                raise


class OpenAIUnhandledErrorMiddleware:
    """仅把 /v1 尚未开始发送的意外异常包装为稳定 OpenAI 错误。"""

    def __init__(self, asgi_app):
        self.app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not _is_v1_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_with_state(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                # 必须在 await send 前置位：客户端恰在响应头发送时断开时，
                # 不得误以为尚未发送并尝试第二个 500 响应。
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_with_state)
        except (_HttpRequestDisconnected, ClientDisconnect):
            raise
        except Exception as exc:
            if response_started:
                raise
            request_id = _request_id_for_request(Request(scope, receive))
            logger.error(
                "Unhandled /v1 pre-stream error request_id=%s type=%s",
                request_id,
                type(exc).__name__,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            response = JSONResponse(
                status_code=500,
                content=_openai_error_body(
                    "Internal server error",
                    "server_error",
                    "synthesis_failed",
                ),
                headers={"X-Request-ID": request_id},
            )
            await response(scope, receive, send)


class _HttpResponseWriteTimeout(TimeoutError):
    pass


class ResponseWriteTimeoutMiddleware:
    """限制单次 response body send；超时通过取消下游触发现有资源清理。"""

    def __init__(self, asgi_app, timeout_seconds=None):
        self.app = asgi_app
        self.timeout_seconds = timeout_seconds

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        timeout = (
            TTS_RESPONSE_WRITE_TIMEOUT_SECONDS
            if self.timeout_seconds is None
            else self.timeout_seconds
        )

        async def timed_send(message):
            if message.get("type") != "http.response.body" or not timeout:
                await send(message)
                return
            send_task = asyncio.create_task(send(message))
            try:
                done, _ = await asyncio.wait({send_task}, timeout=timeout)
                if send_task in done:
                    await send_task
                    return
                raise _HttpResponseWriteTimeout(
                    f"HTTP response body write exceeded {timeout:g} seconds"
                )
            finally:
                if not send_task.done():
                    send_task.cancel()
                await asyncio.gather(send_task, return_exceptions=True)

        await self.app(scope, receive, timed_send)


class LegacyApiRequestIdMiddleware:
    """为旧 /api 的成功与所有前置错误统一附加 request ID。"""

    def __init__(self, asgi_app):
        self.app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not _is_legacy_api_path(
            scope.get("path", "")
        ):
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        state = scope.setdefault("state", {})
        request_id = state.get("request_id") or _request_id_from_header(
            request.headers.get("x-request-id")
        )
        state["request_id"] = request_id

        async def send_with_request_id(message):
            if message.get("type") == "http.response.start":
                message = {**message, "headers": list(message.get("headers", []))}
                headers = MutableHeaders(scope=message)
                if "x-request-id" not in headers:
                    headers.append("X-Request-ID", request_id)
            await send(message)

        await self.app(scope, receive, send_with_request_id)


# RequestBodyLimit 位于 ApiKey 内层：未授权 /v1 在读取或累计 body 前先返回 401。
app.add_middleware(RequestBodyLimitMiddleware)
app.add_middleware(ApiKeyMiddleware)
app.add_middleware(_HttpDisconnectMiddleware)
app.add_middleware(OpenAIUnhandledErrorMiddleware)
app.add_middleware(ResponseWriteTimeoutMiddleware)


# CORS 必须在鉴权中间件之后添加(Starlette 中间件后加者位于外层)，
# 确保 401 响应也带跨域头，浏览器扩展等跨域客户端才能读到状态码而非被 CORS 拦截。
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)
# 位于 CORS 外层，确保旧 /api 的路由、鉴权、校验与 CORS 前置响应均可关联。
app.add_middleware(LegacyApiRequestIdMiddleware)


def _json_safe_error_text(value: str) -> str:
    # json.loads 可接受孤立 UTF-16 surrogate；Starlette 的 ensure_ascii=False
    # 无法把它编码为 UTF-8。仅转义这些无效码点，正常中英文与 emoji 保持原样。
    return str(value).encode("utf-8", "backslashreplace").decode("utf-8")


def _openai_error_body(message: str, err_type: str, code: str | None = None) -> dict:
    return {
        "error": {
            "message": _json_safe_error_text(message),
            "type": err_type,
            "param": None,
            "code": code,
        }
    }


def _openai_error_type_for_status(status_code: int) -> str:
    if status_code == 401:
        return "invalid_request_error"
    if status_code == 429:
        return "rate_limit_error"
    if status_code >= 500:
        return "server_error"
    return "invalid_request_error"


def _openai_error_code_for_status(status_code: int) -> str | None:
    return {
        400: "invalid_request",
        401: "invalid_api_key",
        404: "not_found",
        405: "method_not_allowed",
        422: "invalid_request",
        429: "rate_limit_exceeded",
        500: "synthesis_failed",
        502: "upstream_error",
        503: "engine_not_ready",
        504: "timeout",
    }.get(status_code)


def _openai_http_exception(
    status_code: int,
    message: str,
    *,
    err_type: str | None = None,
    code: str | None = None,
    headers: dict | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=_openai_error_body(
            message,
            err_type or _openai_error_type_for_status(status_code),
            code if code is not None else _openai_error_code_for_status(status_code),
        ),
        headers=headers,
    )


def _require_v1_api_key(request: Request) -> None:
    if TTS_API_KEY and not _key_matches(_extract_rest_key(request.headers)):
        raise _openai_http_exception(
            401,
            "Missing or invalid API key",
            headers={"X-Request-ID": _request_id_for_request(request)},
        )


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(
    request: Request, exc: StarletteHTTPException
):
    # 仅包装 /v1：既有 /api/* 仍返回 {detail:...}，避免破坏客户端契约。
    if not _is_v1_path(request.url.path):
        return await http_exception_handler(request, exc)
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        body = exc.detail
    else:
        message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        body = _openai_error_body(
            message,
            _openai_error_type_for_status(exc.status_code),
            _openai_error_code_for_status(exc.status_code),
        )
    headers = {
        **(exc.headers or {}),
        "X-Request-ID": _request_id_for_request(request),
    }
    return JSONResponse(
        status_code=exc.status_code, content=body, headers=headers
    )


@app.exception_handler(RequestValidationError)
async def _v1_aware_validation_exception_handler(
    request: Request, exc: RequestValidationError
):
    # 非 /v1 走 FastAPI 默认序列化(会处理 ctx 中的异常对象)；仅 /v1 改 OpenAI 形状。
    if not _is_v1_path(request.url.path):
        return await request_validation_exception_handler(request, exc)
    messages = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ()) if part != "body")
        msg = err.get("msg", "invalid request")
        messages.append(f"{loc}: {msg}" if loc else msg)
    message = "; ".join(messages) if messages else "Invalid request"
    return JSONResponse(
        status_code=422,
        content=_openai_error_body(message, "invalid_request_error", "invalid_request"),
        headers={"X-Request-ID": _request_id_for_request(request)},
    )

# =========================
# 静态资源：仅白名单文件，避免暴露源码 / Dockerfile
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

@app.get("/")
async def root():
    # 健康检查需真正反映引擎可用性：pipeline 未就绪则返回 503，避免容器被误判为健康。
    # max_text_length 始终下发，供 UI 与客户端在合成前对齐上限(与 REST/WS 同一事实源)。
    limits = {"max_text_length": MAX_TEXT_LENGTH}
    if pipeline_zh is None or pipeline_en is None:
        return JSONResponse(
            status_code=503,
            content={"status": "starting", "ready": False, **limits},
        )
    if shutil.which("ffmpeg") is None:
        return JSONResponse(
            status_code=503,
            content={"status": "ffmpeg missing", "ready": False, **limits},
        )
    return {"status": "v0.12 engine running", "ready": True, **limits}

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
        # RingBufferHandler 已做一次清洗；这里再做边界清洗，防止未来其他
        # 诊断路径直接写入 buffer 时把行分隔符原样下发。
        "lines": [
            _redact_log_line(_sanitize_log_line(line))
            for line in lines[-limit:]
        ],
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
    logger.info("v0.12 engine ready")


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
    # ``*`` 在纯文本里既可表示 Markdown，又可表示乘法/乘方，无法无歧义推断。
    # 采用保守策略：保护完整的 Unicode 单星号运算链，以及完整的 ASCII
    # ``**`` 乘方链；CJK 或混合脚本相邻的双星号仍按 Markdown 处理。先保护、
    # 再执行既有清洗、最后恢复，可避免只处理同一链的一半。
    def new_star_placeholder(label: str) -> str:
        index = 0
        while True:
            placeholder = f"\ue000clean-text-{label}-{index}\ue001"
            if placeholder not in text:
                return placeholder
            index += 1

    double_star = new_star_placeholder("double-star")
    single_star = new_star_placeholder("single-star")

    def protect_ascii_power_chain(match):
        return match.group(0).replace("**", double_star)

    # 仅匹配完整链，边界禁止 ASCII 操作数或星号；例如 a**b**中文 不会
    # 局部保护为 a<mark>b**中文，而是作为 Markdown 整体清洗。
    text = re.sub(
        r'(?<![A-Za-z0-9*])[A-Za-z0-9]+(?:\*\*[A-Za-z0-9]+)+(?![A-Za-z0-9*])',
        protect_ascii_power_chain,
        text,
    )
    # ``[^\W_]`` 等价于 Unicode 字母/数字，显式排除下划线。单星号也必须
    # 整链保护，避免 *italic*word 只保护闭合符而破坏既有 Markdown 清洗。
    def protect_unicode_multiplication_chain(match):
        return match.group(0).replace("*", single_star)

    text = re.sub(
        r'(?<![\w*])[^\W_]+(?:\*[^\W_]+)+(?![\w*])',
        protect_unicode_multiplication_chain,
        text,
    )

    text = re.sub(r'(\*\*|__)(.*?)\1', r'\2', text)  # 粗体
    # 斜体只处理 *...*；不处理 _..._，避免误伤 snake_case 标识符(如 zf_xiaoxiao)
    text = re.sub(r'\*(\S(?:.*?\S)?)\*', r'\1', text)  # 斜体 *text*
    text = re.sub(r'\*{2,}', '', text)                    # 残留 markdown 星号(如未闭合 **)
    text = re.sub(r'~~(.*?)~~', r'\1', text)            # 删除线
    text = re.sub(r'^\s*([-*_])(?:\s*\1){2,}\s*$', '', text, flags=re.MULTILINE)  # 分隔线(---/***/___)
    # 引号不发音，但会被 Kokoro 音素化成杂音(尤其结尾引号产生"嗯哼"声)，移除。
    # 直/弯双引号、中文方括号引号、书名号一并去除；保留 ASCII 单引号 ' 以免破坏英文缩写(don't/it's)。
    text = re.sub(r'["“”「」『』《》]', '', text)
    return text.replace(double_star, "**").replace(single_star, "*")


def split_text(text: str):
    # 句末切分规则(与前端 splitSentences 保持一致，单一事实源)：
    #  1) 英文句末标点 .!? 后需跟空格才切——保护 3.14 / U.S.A / 省略号... 不被拆碎；
    #  2) 中文句末标点 。！？ 为零宽后切(其后通常无空格)；
    #  3) 换行 \n+ 直接切。
    parts = re.split(r'(?<=[.!?。！？]) +|(?<=[。！？])|\n+', text)
    return [t.strip() for t in parts if t and t.strip()]


def split_kokoro_unit(text: str, max_chars: int | None = None) -> list[str]:
    """限制单次 Kokoro 聚合规模，同时逐字符保留原始合成单元。"""
    limit = KOKORO_MAX_UNIT_CHARS if max_chars is None else max_chars
    if limit < 1:
        raise ValueError("Kokoro unit limit must be >= 1")
    if not text:
        return []

    fragments = []
    start = 0
    while len(text) - start > limit:
        hard_end = start + limit
        # 优先在窗口后半段的空白或弱标点后切，避免制造很短的额外推理；
        # 找不到自然边界时按 Python code point 硬切，绝不删除边界字符。
        min_break = start + max(1, limit // 2)
        cut = hard_end
        for index in range(hard_end - 1, min_break - 1, -1):
            char = text[index]
            if char.isspace() or char in ",;:，；：、":
                cut = index + 1
                break
        fragments.append(text[start:cut])
        start = cut
    fragments.append(text[start:])
    return fragments


_HAN_CODEPOINT_RANGES = (
    (0x3007, 0x3007),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFA6D),
    (0xFA70, 0xFAD9),
    (0x20000, 0x2A6DF),
    (0x2A700, 0x2B739),
    (0x2B740, 0x2B81D),
    (0x2B820, 0x2CEA1),
    (0x2CEB0, 0x2EBE0),
    (0x2F800, 0x2FA1D),
    (0x30000, 0x3134A),
    (0x31350, 0x323AF),
)


def _is_han_char(char: str) -> bool:
    # 与 index.html 的 HAN_CHAR_RE 固定为同一 Unicode 15.0 分段，避免
    # Python 3.10/3.12 内置 UCD 版本不同导致前后端语言路由漂移。
    codepoint = ord(char)
    return any(start <= codepoint <= end for start, end in _HAN_CODEPOINT_RANGES)


def _contains_han(text: str) -> bool:
    return any(_is_han_char(char) for char in text)


def _is_cjk_punctuation(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3000 <= codepoint <= 0x303F
        or 0xFE10 <= codepoint <= 0xFE19
        or 0xFE30 <= codepoint <= 0xFE4F
        or 0xFF01 <= codepoint <= 0xFF0F
        or 0xFF1A <= codepoint <= 0xFF20
        or 0xFF3B <= codepoint <= 0xFF40
        or 0xFF5B <= codepoint <= 0xFF65
    )


def _contains_speakable_text(text: str) -> bool:
    return any(
        _is_han_char(char)
        or unicodedata.category(char).startswith(("L", "N"))
        for char in text
    )


def _is_chinese_kokoro_voice(voice: str) -> bool:
    return voice.startswith(("zf_", "zm_"))


def filter_for_voice(text: str, is_zh: bool) -> str:
    # 兜底防线：前端已按语言把文本路由到对应语言音色(单一事实源)，正常流程后端不会收到跨语言内容。
    # 本函数只为 REST 直传混排文本等旁路场景兜底——Kokoro 每个 pipeline 单语言，跨语言必读错。
    # 官方 issue #95/#238 证实中文 pipeline 连 DNS 等孤立英文缩写都读不出(按拼音转音标，英文无法音素化)，
    # 故中文音色一律剥离全部英文字母；英文音色一律剥离全部中文字符。剥离比硬读乱码更可接受。
    if is_zh:
        # 中文音色：移除拉丁字母及其相连的数字/常见标识符标点(next.js/snake_case 整体去除)。
        return re.sub(r"[A-Za-z][A-Za-z0-9._\-']*", ' ', text)
    # 英文音色：按实际 Unicode 名称移除已分配 Han，中文标点使用明确区段；
    # 不再把整个全角区误当标点，从而保留全角 Latin/数字。
    out = []
    removing = False
    for char in text:
        if _is_han_char(char) or _is_cjk_punctuation(char):
            if not removing:
                out.append(" ")
            removing = True
            continue
        out.append(char)
        removing = False
    return "".join(out)


async def _acquire_synthesis_slot(semaphore, cancel_event):
    """取得 Kokoro 槽位；WS 取消必须能唤醒仍在队列中的请求。"""
    if cancel_event is None:
        await semaphore.acquire()
        return True
    if cancel_event.is_set():
        return False

    # acquire() 在有空闲值时不会让出事件循环。先走同步快路径，必须与调用方
    # semaphore.locked() 的 waiter admission 快照保持在同一事件循环片段内；否则
    # 一批 WS 请求都能先看见“未锁满”，再各自 create_task(acquire())，从而绕过
    # TTS_MAX_SYNTHESIS_WAITERS 并一起进入 Semaphore 的内部等待队列。
    if not semaphore.locked():
        await semaphore.acquire()
        return True

    acquire_task = asyncio.create_task(semaphore.acquire())
    cancel_task = asyncio.create_task(cancel_event.wait())

    async def settle_waiters(*, release_acquired):
        if not acquire_task.done():
            acquire_task.cancel()
        if not cancel_task.done():
            cancel_task.cancel()
        await asyncio.gather(acquire_task, cancel_task, return_exceptions=True)
        acquired = (
            acquire_task.done()
            and not acquire_task.cancelled()
            and acquire_task.exception() is None
            and bool(acquire_task.result())
        )
        if release_acquired and acquired:
            semaphore.release()

    try:
        done, _ = await asyncio.wait(
            {acquire_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        # 外层 task 取消不能把仍在排队的 acquire_task 留成“幽灵占槽者”。
        await _await_cleanup(settle_waiters(release_acquired=True))
        raise

    if cancel_task in done or cancel_event.is_set():
        # acquire 与 cancel 同轮完成时取消优先；若槽已取得，必须原样归还。
        await _await_cleanup(settle_waiters(release_acquired=True))
        return False

    acquire_task.result()
    try:
        await _await_cleanup(settle_waiters(release_acquired=False))
    except asyncio.CancelledError:
        # 槽已取得但尚未交给调用方时，取消由本层负责归还。
        semaphore.release()
        raise
    return True


class _KokoroPermit:
    def __init__(self, semaphore, *, prefetch=False):
        self._semaphore = semaphore
        self._prefetch = prefetch
        self._released = False

    def release(self):
        if self._released:
            return
        self._released = True
        self._semaphore.release()
        if self._prefetch:
            _release_kokoro_prefetch()


async def _acquire_kokoro_permit(cancel_event=None, *, prefetch=False):
    if prefetch:
        _reserve_kokoro_prefetch()
    semaphore = _get_synthesis_semaphore()
    counted_waiter = False
    try:
        if (
            prefetch
            and semaphore.locked()
            and (cancel_event is None or not cancel_event.is_set())
        ):
            raise _SynthesisPrefetchFull(
                "Kokoro prefetch requires an immediately available synthesis slot"
            )
        if semaphore.locked():
            _reserve_synthesis_waiter()
            counted_waiter = True
        try:
            acquired = await _acquire_synthesis_slot(semaphore, cancel_event)
        finally:
            if counted_waiter:
                _release_synthesis_waiter()
        if not acquired:
            if prefetch:
                _release_kokoro_prefetch()
            return None
        return _KokoroPermit(semaphore, prefetch=prefetch)
    except BaseException:
        if prefetch:
            _release_kokoro_prefetch()
        raise


async def run_kokoro(
    text,
    voice,
    speed=1.0,
    cancel_event=None,
    permit=None,
    *,
    prefetch=False,
):
    if cancel_event is not None and cancel_event.is_set():
        return b""

    is_zh = _is_chinese_kokoro_voice(voice)
    pipeline = pipeline_zh if is_zh else pipeline_en
    lock = lock_zh if is_zh else lock_en

    # 按音色语言剥离另一语言(中文音色去英文、英文音色去中文)——Kokoro 单语言 pipeline 跨语言必读错。
    # 过滤后仅剩标点/空格(无目标语言内容)则跳过合成，返回空 PCM。
    text = filter_for_voice(text, is_zh).strip()
    if not text or not _contains_speakable_text(text):
        return b""

    worker_cancel = threading.Event()

    def cancelled():
        return worker_cancel.is_set() or (
            cancel_event is not None and cancel_event.is_set()
        )

    def infer():
        # 加锁串行化同一 pipeline 的推理，避免多线程并发污染或崩溃
        with lock:
            if cancelled():
                return None
            gen = pipeline(text, voice=voice, speed=speed)
            out = []
            for r in gen:
                if cancelled():
                    return None
                out.append(r.output.audio.detach().cpu().numpy())
            return np.concatenate(out) if out else None

    # 合成并发闸门：进入线程池派发推理前先取信号量，把"同时在跑的 Kokoro 推理"钉在上限内，
    # 避免大量 to_thread 任务占着线程池 worker 阻塞在语言锁上拖垮整个进程。排队期间已取消
    # 的请求不再派发；已进入线程的推理只能在生成器 chunk 边界协作停止，因此外层取消后仍需
    # 等线程真正退出再归还信号量，不能让逻辑并发数小于真实在途推理数。
    owns_permit = permit is None
    if permit is None:
        permit = await _acquire_kokoro_permit(
            cancel_event, prefetch=prefetch
        )
        if permit is None:
            return b""
    try:
        if cancel_event is not None and cancel_event.is_set():
            return b""
        worker_task = asyncio.create_task(asyncio.to_thread(infer))
        try:
            audio = await asyncio.shield(worker_task)
        except asyncio.CancelledError:
            worker_cancel.set()
            await _await_cleanup(worker_task)
            raise
    finally:
        if owns_permit:
            permit.release()
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


def _edge_rate_for_speed(speed: float) -> str:
    percent = int(
        ((Decimal(str(speed)) - Decimal("1")) * Decimal("100")).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    return f"{percent:+d}%"


def _validate_voice_value(value) -> str:
    if not isinstance(value, str):
        raise ValueError("voice must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("voice must not be empty")
    if len(cleaned) > VOICE_MAX_LENGTH:
        raise ValueError(f"voice length must be <= {VOICE_MAX_LENGTH}")
    if not _VOICE_ID_RE.fullmatch(cleaned):
        raise ValueError(
            "voice may contain only ASCII letters, digits, '.', '_' and '-'"
        )
    return cleaned


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
        if not math.isfinite(v) or v < SPEED_MIN or v > SPEED_MAX:
            raise ValueError(f"speed must be between {SPEED_MIN} and {SPEED_MAX}")
        return v

    @field_validator("voice")
    @classmethod
    def check_voice(cls, v, info):
        cleaned = _validate_voice_value(v)
        if (
            info.data.get("engine") == "kokoro"
            and cleaned not in KOKORO_VOICE_IDS
        ):
            raise ValueError("unknown Kokoro voice")
        return cleaned

    @field_validator("ssml")
    @classmethod
    def check_ssml(cls, v):
        if v:
            raise ValueError("raw SSML is not supported")
        return v


# OpenAI 兼容层默认：engine=edge。
# 不继承 TTSRequest 的 zf_xiaoxiao——该默认走中文 Kokoro pipeline，filter_for_voice
# 会把纯英文剥空(实测 "hello world" → "")；Edge 路径不经过 filter_for_voice。
OPENAI_DEFAULT_ENGINE = "edge"
OPENAI_DEFAULT_VOICES = {
    "edge": {
        "en": "en-US-AvaNeural",
        "zh": "zh-CN-XiaoxiaoNeural",
    },
    "kokoro": {
        "en": "af_heart",
        "zh": "zf_xiaoxiao",
    },
}
OPENAI_VOICE_ALIASES = {
    "edge": {
        "alloy": "en-US-AvaNeural",
        # OpenClaw 当前默认是 coral；映射成人 Ava，避免回退到儿童 Ana。
        "coral": "en-US-AvaNeural",
        "echo": "en-US-AndrewNeural",
        "fable": "en-US-GuyNeural",
        "onyx": "en-US-BrianNeural",
        "nova": "en-US-EmmaNeural",
        "shimmer": "en-US-JennyNeural",
    },
    "kokoro": {
        "alloy": "af_alloy",
        "coral": "af_heart",
        "echo": "am_echo",
        "fable": "af_bella",
        "onyx": "am_onyx",
        "nova": "af_nova",
        "shimmer": "af_sarah",
    },
}
OPENAI_AUDIO_FORMATS = {
    "mp3": {
        "media_type": "audio/mpeg",
        "extension": "mp3",
        "ffmpeg_args": (
            "-codec:a", "libmp3lame", "-qscale:a", "2", "-f", "mp3",
        ),
    },
    "opus": {
        "media_type": "audio/ogg",
        "extension": "opus",
        "ffmpeg_args": (
            "-codec:a", "libopus", "-b:a", "64k",
            "-ar", "48000", "-ac", "1", "-f", "opus",
        ),
    },
    "aac": {
        "media_type": "audio/aac",
        "extension": "aac",
        "ffmpeg_args": (
            "-codec:a", "aac", "-b:a", "128k", "-f", "adts",
        ),
    },
    "flac": {
        "media_type": "audio/flac",
        "extension": "flac",
        "ffmpeg_args": (
            "-codec:a", "flac", "-ar", "24000", "-ac", "1", "-f", "flac",
        ),
    },
    "wav": {
        "media_type": "audio/wav",
        "extension": "wav",
        "ffmpeg_args": (
            "-codec:a", "pcm_s16le", "-ar", "24000", "-ac", "1", "-f", "wav",
        ),
    },
    "pcm": {
        "media_type": "application/octet-stream",
        "extension": "pcm",
        "ffmpeg_args": (
            "-codec:a", "pcm_s16le", "-ar", "24000", "-ac", "1", "-f", "s16le",
        ),
    },
}
OPENAI_SUPPORTED_RESPONSE_FORMATS = frozenset(OPENAI_AUDIO_FORMATS)
# 429 退避提示：默认 ffmpeg 槽位为 2，短延迟足以让客户端重试而不盲目打满。
OPENAI_RETRY_AFTER_SECONDS = "2"


class OpenAISpeechRequest(BaseModel):
    """OpenAI /v1/audio/speech 形状；未知字段忽略，不 422。"""

    model_config = ConfigDict(extra="ignore")

    input: str = Field(..., min_length=1)
    model: str | None = None
    voice: str | None = None
    speed: float = 1.0
    response_format: str = "mp3"
    instructions: str | None = None
    stream_format: str = "audio"
    lang_code: str | None = None
    lang: str | None = None
    language: str | None = None
    format: str | None = None

    @field_validator("input")
    @classmethod
    def check_input_length(cls, v):
        if len(v) > MAX_TEXT_LENGTH:
            raise ValueError(f"input length must be <= {MAX_TEXT_LENGTH}")
        return v

    @field_validator("speed")
    @classmethod
    def check_speed(cls, v):
        if not math.isfinite(v) or v < SPEED_MIN or v > SPEED_MAX:
            raise ValueError(f"speed must be between {SPEED_MIN} and {SPEED_MAX}")
        return v

    @field_validator("voice")
    @classmethod
    def check_explicit_voice(cls, v):
        if v is None:
            return None
        return _validate_voice_value(v)


def _openai_resolve_engine(model: str | None) -> str:
    if model in VALID_ENGINES:
        return model
    # 官方 tts-1 / 未知 model 名回退 edge，避免落到会剥空英文的默认中文 Kokoro。
    return OPENAI_DEFAULT_ENGINE


def _openai_default_voice_for_text(text: str, engine: str) -> str:
    language = "zh" if _contains_han(text) else "en"
    return OPENAI_DEFAULT_VOICES[engine][language]


def _openai_resolve_voice(voice: str | None, text: str, engine: str) -> str:
    if voice is None:
        return _openai_default_voice_for_text(text, engine)
    # 模型校验已处理常规入口；这里保留防御性校验，避免直接单元调用绕过边界。
    cleaned = _validate_voice_value(voice)
    alias = OPENAI_VOICE_ALIASES[engine].get(cleaned.lower())
    if alias:
        return alias
    return cleaned


async def _openai_validate_voice(engine: str, voice: str) -> None:
    if engine == "kokoro":
        if voice not in KOKORO_VOICE_IDS:
            raise _openai_http_exception(422, f"Unknown Kokoro voice: {voice}")
        return
    if voice in KOKORO_VOICE_IDS:
        raise _openai_http_exception(
            422, f"Kokoro voice is incompatible with Edge model: {voice}"
        )
    # 合成请求只读 fresh cache，不触发目录上游；cold/empty/stale 均放行。
    edge_voices = _peek_fresh_edge_voice_catalog()
    if not edge_voices:
        return
    known = {v.get("ShortName") for v in edge_voices if v.get("ShortName")}
    if voice not in known:
        raise _openai_http_exception(422, f"Unknown Edge voice: {voice}")


def _is_latin_letter(char: str) -> bool:
    """判断 Unicode 字母是否属于 Latin 脚本；数字/标点由调用方视为中性。"""
    return (
        unicodedata.category(char).startswith("L")
        and "LATIN" in unicodedata.name(char, "")
    )


def _openai_validate_kokoro_text_compatibility(text: str, voice: str) -> None:
    """在启动合成前拒绝会被 Kokoro 静默删减的 /v1 请求。

    Kokoro 的两个 pipeline 是按脚本而非按语言工作的：中文音色允许 Han，
    英文音色允许 Latin；数字、标点和空白不决定脚本。纯日文汉字无法与
    中文汉字仅靠 Unicode 区分，因此仍按 Han 处理。
    """
    is_zh = _is_chinese_kokoro_voice(voice)
    allowed = _is_han_char if is_zh else _is_latin_letter
    incompatible = next(
        (
            char
            for char in text
            if unicodedata.category(char).startswith("L") and not allowed(char)
        ),
        None,
    )
    if incompatible is not None and is_zh:
        is_latin = _is_latin_letter(incompatible)
        script = "Latin letters" if is_latin else "non-Han letters"
        recommendation = (
            "use an English Kokoro voice or Edge"
            if is_latin
            else "use Edge"
        )
        raise _openai_http_exception(
            422,
            f"Chinese Kokoro voice '{voice}' cannot synthesize {script} "
            f"without dropping content; {recommendation}",
        )
    if incompatible is not None:
        is_han = _is_han_char(incompatible)
        script = "CJK characters" if is_han else "non-Latin letters"
        recommendation = (
            "use a Chinese Kokoro voice or Edge" if is_han else "use Edge"
        )
        raise _openai_http_exception(
            422,
            f"English Kokoro voice '{voice}' cannot synthesize {script} "
            f"without dropping content; {recommendation}",
        )


def parse_ws_request(req: dict):
    if not isinstance(req, dict):
        return {"type": "error", "message": "请求必须是 JSON 对象"}

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
    if not isinstance(engine, str) or not engine.strip():
        return {"type": "error", "message": "engine 必须是非空字符串"}
    try:
        voice = _validate_voice_value(voice)
    except ValueError as exc:
        return {"type": "error", "message": str(exc)}
    engine = engine.strip()
    # WS 为长驻交互会话：speed 越界静默夹取、非数字回落 1.0(不打断会话)，
    # 与 REST 的越界即 422 语义不同但共用同一组边界常量(SPEED_MIN/MAX)，避免双事实源漂移。
    try:
        speed = float(req.get("speed", 1.0))
    except (ValueError, TypeError):
        speed = 1.0
    if not math.isfinite(speed):
        speed = 1.0
    speed = _clamp_speed(speed)

    if engine not in VALID_ENGINES:
        return {"type": "error", "message": f"未知合成引擎：{engine}"}
    if engine == "kokoro" and voice not in KOKORO_VOICE_IDS:
        return {"type": "error", "message": f"未知 Kokoro 音色：{voice}"}
    return {"type": "ok", "text": text, "engine": engine, "voice": voice, "speed": speed}

_edge_voices_cache = None
_edge_voices_cache_expires_at = 0.0
_edge_voices_retry_after = 0.0
_edge_voices_refresh_lock = None
_edge_voices_refresh_generation = 0


def _peek_fresh_edge_voice_catalog():
    cache = _edge_voices_cache
    if cache is None or time.monotonic() >= _edge_voices_cache_expires_at:
        return None
    return cache


def _get_edge_voices_refresh_lock() -> asyncio.Lock:
    global _edge_voices_refresh_lock
    if _edge_voices_refresh_lock is None:
        _edge_voices_refresh_lock = asyncio.Lock()
    return _edge_voices_refresh_lock


def _validate_edge_voice_catalog(voices):
    if (
        not isinstance(voices, list)
        or not voices
        or any(
            not isinstance(voice, dict)
            or not isinstance(voice.get("ShortName"), str)
            or not voice["ShortName"].strip()
            for voice in voices
        )
    ):
        raise RuntimeError("Edge voices returned an invalid catalog")
    return voices


async def _get_edge_voices():
    global _edge_voices_cache, _edge_voices_cache_expires_at
    global _edge_voices_retry_after, _edge_voices_refresh_generation
    now = time.monotonic()
    if _edge_voices_cache is not None and now < _edge_voices_cache_expires_at:
        return _edge_voices_cache
    if now < _edge_voices_retry_after:
        return _edge_voices_cache if _edge_voices_cache is not None else []
    observed_generation = _edge_voices_refresh_generation

    # 双重检查 + 单飞刷新：过期瞬间的并发请求共用一次上游刷新，避免放大网络故障。
    async with _get_edge_voices_refresh_lock():
        now = time.monotonic()
        if _edge_voices_cache is not None and now < _edge_voices_cache_expires_at:
            return _edge_voices_cache
        if now < _edge_voices_retry_after:
            return _edge_voices_cache if _edge_voices_cache is not None else []
        # TTL/cooldown 为 0 时，时间判断无法识别“等待期间已有刷新完成”。
        # 代次只合并真正重叠的调用，不会把刷新完成后才到达的新请求继续缓存。
        if _edge_voices_refresh_generation != observed_generation:
            return _edge_voices_cache if _edge_voices_cache is not None else []

        attempts = max(1, EDGE_RETRY_MAX_ATTEMPTS)
        for attempt in range(attempts):
            try:
                voices = _validate_edge_voice_catalog(
                    await asyncio.wait_for(
                        edge_tts.list_voices(),
                        timeout=EDGE_VOICES_REQUEST_TIMEOUT_SECONDS or None,
                    )
                )
            except Exception as exc:
                if attempt + 1 < attempts:
                    logger.warning(
                        "Edge voices refresh failed (attempt %s/%s); retrying: %r",
                        attempt + 1, attempts, exc,
                    )
                    delay = EDGE_RETRY_BASE_DELAY_SECONDS * (2 ** attempt)
                    if delay:
                        await asyncio.sleep(delay)
                    continue

                _edge_voices_retry_after = (
                    time.monotonic() + EDGE_VOICES_FAILURE_COOLDOWN_SECONDS
                )
                _edge_voices_refresh_generation += 1
                if _edge_voices_cache is not None:
                    logger.warning(
                        "Edge voices refresh failed after %s attempt(s); "
                        "serving stale cache: %r",
                        attempts, exc,
                    )
                    return _edge_voices_cache
                logger.warning(
                    "Edge voices unavailable after %s attempt(s); "
                    "serving empty catalog: %r",
                    attempts, exc,
                )
                return []

            _edge_voices_cache = voices
            _edge_voices_cache_expires_at = (
                time.monotonic() + EDGE_VOICES_CACHE_TTL_SECONDS
            )
            _edge_voices_retry_after = 0.0
            _edge_voices_refresh_generation += 1
            return _edge_voices_cache

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
async def _create_openai_audio_encoder(engine: str, response_format: str):
    try:
        format_spec = OPENAI_AUDIO_FORMATS[response_format]
    except KeyError:
        raise ValueError(
            f"unsupported OpenAI audio response format: {response_format}"
        ) from None

    acquired = await _ffmpeg_limiter.acquire()
    if not acquired:
        raise HTTPException(status_code=429, detail="ffmpeg process limit reached")

    input_args = (
        ("-i", "pipe:0")
        if engine == "edge"
        else ("-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "pipe:0")
    )
    try:
        return await asyncio.create_subprocess_exec(
            "ffmpeg",
            *input_args,
            *format_spec["ffmpeg_args"],
            "pipe:1", "-loglevel", "quiet",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
    except BaseException:
        _ffmpeg_limiter.release()
        raise


async def _create_mp3_encoder(engine: str):
    return await _create_openai_audio_encoder(engine, "mp3")


async def _iter_edge_audio(text: str, voice: str, rate: str, cancel_event=None):
    """产出 Edge 音频块；仅在尚未观察到非空音频时重试上游失败。"""
    audio_started = False
    attempts = max(1, EDGE_RETRY_MAX_ATTEMPTS)

    for attempt in range(attempts):
        if cancel_event is not None and cancel_event.is_set():
            return
        try:
            communicate = edge_tts.Communicate(text, voice, rate=rate)
            async for chunk in communicate.stream():
                if cancel_event is not None and cancel_event.is_set():
                    return
                if chunk["type"] != "audio":
                    continue
                data = chunk["data"]
                if not data:
                    continue
                # 从这里开始禁止自动重试：即使消费者随后写管道失败，重连也可能重复朗读。
                audio_started = True
                yield data
            if audio_started:
                return
            if cancel_event is not None and cancel_event.is_set():
                return
            raise RuntimeError("Edge synthesis produced no audio")
        except Exception as exc:
            if audio_started or attempt + 1 >= attempts:
                raise
            if cancel_event is not None and cancel_event.is_set():
                return

            logger.warning(
                "Edge synthesis failed before first audio "
                "(voice=%s attempt=%s/%s); retrying: %s",
                voice, attempt + 1, attempts, exc,
            )
            delay = EDGE_RETRY_BASE_DELAY_SECONDS * (2 ** attempt)
            if delay:
                await asyncio.sleep(delay)
            if cancel_event is not None and cancel_event.is_set():
                return


async def _close_async_iterator(iterator):
    close = getattr(iterator, "aclose", None)
    if close is not None:
        await close()


async def _next_edge_audio(iterator, cancel_event=None):
    if cancel_event is None:
        return await iterator.__anext__()
    if cancel_event.is_set():
        return None

    next_task = asyncio.create_task(iterator.__anext__())
    cancel_task = asyncio.create_task(cancel_event.wait())
    try:
        done, _ = await asyncio.wait(
            {next_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if cancel_task in done or cancel_event.is_set():
            return None
        return next_task.result()
    finally:
        for task in (next_task, cancel_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(next_task, cancel_task, return_exceptions=True)


async def _prepare_edge_audio(text, voice, rate, cancel_event=None):
    source = (
        _iter_edge_audio(text, voice, rate, cancel_event=cancel_event)
        if cancel_event is not None
        else _iter_edge_audio(text, voice, rate)
    )
    iterator = source.__aiter__()
    try:
        first_audio = await _next_edge_audio(iterator, cancel_event)
    except StopAsyncIteration as exc:
        await _close_async_iterator(iterator)
        if cancel_event is not None and cancel_event.is_set():
            return None, None
        raise RuntimeError("Edge synthesis produced no audio") from exc
    except BaseException:
        await _close_async_iterator(iterator)
        raise
    if first_audio is None:
        await _close_async_iterator(iterator)
        return None, None
    return iterator, first_audio


class _EncoderInputError(RuntimeError):
    """本地编码器 stdin 写入/背压失败，不能误归类为 Edge 上游故障。"""


async def _write_encoder_input(proc, data: bytes, first_audio=None):
    try:
        proc.stdin.write(data)
        # write() 同步成功才算源音频已提交；drain() 失败仍由 feed 终态优先上报。
        if first_audio is not None:
            first_audio.set()
        await proc.stdin.drain()
    except Exception as exc:
        raise _EncoderInputError("audio encoder input failed") from exc


async def _feed_mp3(
    proc,
    text: str,
    engine: str,
    voice: str,
    speed: float,
    first_audio=None,
    *,
    kokoro_permit=None,
    edge_stream=None,
    first_edge_audio=None,
):
    # first_audio(asyncio.Event)：产出首个音频源字节时置位，供 REST 预检判定"是否真正产出音频"。
    # 判定基于喂入 ffmpeg 的音频源(而非 ffmpeg 输出)，避免 mp3 muxer 空输入仍吐头字节造成误判成功。
    # 关键：_mark() 必须在 write 成功后、drain 之前置位。预检期 _stream_mp3 尚未启动、
    # 无人读 proc.stdout，若首块 PCM 撑满 ffmpeg 管道缓冲，drain() 会阻塞；但 write()
    # 同步失败时不得误报已有音频，否则 REST 会把确定失败伪装成 200。
    try:
        if engine == "kokoro":
            sentences = split_text(text)
            for sent in sentences:
                for fragment in split_kokoro_unit(sent):
                    pcm = await run_kokoro(
                        fragment, voice, speed, permit=kokoro_permit
                    )
                    if pcm:
                        await _write_encoder_input(proc, pcm, first_audio)
        else:
            if edge_stream is None:
                rate = _edge_rate_for_speed(speed)
                edge_stream, first_edge_audio = await _prepare_edge_audio(
                    text, voice, rate
                )
            if edge_stream is not None:
                try:
                    await _write_encoder_input(
                        proc, first_edge_audio, first_audio
                    )
                    async for data in edge_stream:
                        await _write_encoder_input(proc, data, first_audio)
                finally:
                    await _close_async_iterator(edge_stream)
    finally:
        if kokoro_permit is not None:
            kokoro_permit.release()
        try:
            proc.stdin.close()
        except Exception:
            pass


async def _await_cleanup(awaitable):
    """等待清理动作结束；外层取消会延后传播，但不会被吞掉。"""
    cleanup_task = asyncio.ensure_future(awaitable)
    active_exception = sys.exc_info()[1]
    cancellation_error = (
        active_exception
        if isinstance(active_exception, asyncio.CancelledError)
        else None
    )
    while True:
        try:
            result = await asyncio.shield(cleanup_task)
            break
        except asyncio.CancelledError as exc:
            # cleanup_task 自身取消时没有可继续等待的清理；直接保留该语义。
            if cleanup_task.cancelled():
                if cancellation_error is not None:
                    raise cancellation_error
                raise
            if cancellation_error is None:
                cancellation_error = exc
        except Exception as exc:
            if cancellation_error is not None:
                raise cancellation_error from exc
            raise
    if cancellation_error is not None:
        raise cancellation_error
    return result


async def _reap_proc(proc):
    # 回收 ffmpeg 子进程并释放配额；调用方必须保持单一所有权，禁止重复进入。
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except OSError as exc:
            # kill 失败不等于进程仍不会退出；feed 已关闭 stdin，继续 wait 才能在
            # 确认终态后释放配额，也避免该异常跳过整个回收链。
            logger.warning("Could not kill REST encoder; waiting for exit: %s", exc)
    try:
        await _await_cleanup(proc.wait())
    finally:
        _ffmpeg_limiter.release()


async def _finalize_mp3_session(proc, feed_task, engine=None, voice=None):
    try:
        if not feed_task.done():
            feed_task.cancel()
        try:
            await feed_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if engine is None:
                logger.debug("Discarded REST feed failed during cleanup: %s", exc)
            else:
                logger.error(
                    "feed task failed after stream start (engine=%s voice=%s): %s",
                    engine,
                    voice,
                    exc,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
    finally:
        await _reap_proc(proc)


async def _dispose_mp3_session(proc, feed_task):
    """回收尚未交给 StreamingResponse 的预检资源。"""
    await _finalize_mp3_session(proc, feed_task)


async def _create_edge_pcm_decoder(prefetch: bool = False):
    acquired = await _ffmpeg_limiter.acquire(prefetch=prefetch)
    if not acquired:
        raise RuntimeError("ffmpeg process limit reached")
    try:
        return await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", "pipe:0", "-f", "s16le", "-ar", "24000", "-ac", "1",
            "pipe:1", "-loglevel", "quiet",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        )
    except BaseException:
        _ffmpeg_limiter.release(prefetch=prefetch)
        raise


async def _reap_edge_pcm_decoder(process, prefetch: bool = False):
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except OSError as exc:
            logger.warning(
                "Could not kill Edge decoder; waiting for exit: %s", exc
            )
    try:
        returncode = await _await_cleanup(process.wait())
        if returncode is None:
            returncode = process.returncode
        return returncode
    finally:
        _ffmpeg_limiter.release(prefetch=prefetch)


class _PrefetchedStreamReader:
    """把预检读取的首块无损放回流中，之后代理原始 reader。"""

    def __init__(self, reader, first_chunk: bytes):
        self._reader = reader
        self._pending = bytes(first_chunk)

    async def read(self, size=-1):
        if size == 0:
            return b""
        if self._pending:
            if size is None or size < 0 or len(self._pending) <= size:
                chunk = self._pending
                self._pending = b""
                return chunk
            chunk = self._pending[:size]
            self._pending = self._pending[size:]
            return chunk
        return await self._reader.read(size)


async def _cancel_and_consume_preflight_tasks(*tasks):
    tasks = tuple(task for task in tasks if task is not None)
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _await_with_deadline(awaitable, deadline):
    if deadline is None:
        return await awaitable
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        close = getattr(awaitable, "close", None)
        if close is not None:
            close()
        raise asyncio.TimeoutError()
    return await asyncio.wait_for(awaitable, remaining)


async def _dispose_synthesis_preflight(
    proc, feed_task, first_audio_wait, first_output_task
):
    """预检未移交时，消费 waiter 并保持 session 的唯一回收权。"""
    for task in (first_audio_wait, first_output_task):
        if not task.done():
            task.cancel()
    try:
        await _dispose_mp3_session(proc, feed_task)
    finally:
        await _cancel_and_consume_preflight_tasks(
            first_audio_wait, first_output_task
        )


async def _start_synthesis(
    text: str,
    engine: str,
    voice: str,
    speed: float,
    request_id: str = "",
    response_format: str = "mp3",
):
    # 预检合成：必须同时确认源音频已写入编码器、编码器也产出首块，再提交 HTTP 200。
    # 单看源音频会把 ffmpeg 立即 EOF 伪装成空 200；单看编码输出又会把空输入时的
    # MP3/WAV 容器头误判为成功。首输出必须并发读取，否则 stdin/stdout 背压可能互锁。
    timeout = TTS_SYNTHESIS_TIMEOUT_SECONDS or None
    deadline = (
        asyncio.get_running_loop().time() + timeout
        if timeout is not None
        else None
    )
    kokoro_permit = None
    edge_stream = None
    first_edge_audio = None
    try:
        if engine == "kokoro":
            try:
                kokoro_permit = await _await_with_deadline(
                    _acquire_kokoro_permit(), deadline
                )
            except _SynthesisQueueFull as exc:
                raise HTTPException(
                    status_code=429, detail="Kokoro synthesis queue is full"
                ) from exc
        else:
            edge_stream, first_edge_audio = await _await_with_deadline(
                _prepare_edge_audio(
                    text, voice, _edge_rate_for_speed(speed)
                ),
                deadline,
            )
            if edge_stream is None:
                raise RuntimeError("Edge synthesis cancelled before first audio")
        if response_format == "mp3":
            proc = await _await_with_deadline(
                _create_mp3_encoder(engine), deadline
            )
        else:
            proc = await _await_with_deadline(
                _create_openai_audio_encoder(engine, response_format), deadline
            )
    except asyncio.TimeoutError as exc:
        if kokoro_permit is not None:
            kokoro_permit.release()
        if edge_stream is not None:
            await _close_async_iterator(edge_stream)
        raise HTTPException(status_code=504, detail="synthesis timed out") from exc
    except HTTPException:
        if kokoro_permit is not None:
            kokoro_permit.release()
        if edge_stream is not None:
            await _close_async_iterator(edge_stream)
        raise
    except asyncio.CancelledError:
        if kokoro_permit is not None:
            kokoro_permit.release()
        if edge_stream is not None:
            await _close_async_iterator(edge_stream)
        raise
    except Exception as exc:
        if kokoro_permit is not None:
            kokoro_permit.release()
        if edge_stream is not None:
            await _close_async_iterator(edge_stream)
        if engine == "edge":
            raise HTTPException(status_code=502, detail="synthesis failed") from exc
        raise
    except BaseException:
        if kokoro_permit is not None:
            kokoro_permit.release()
        if edge_stream is not None:
            await _close_async_iterator(edge_stream)
        raise

    first_audio = asyncio.Event()
    try:
        feed_kwargs = (
            {"kokoro_permit": kokoro_permit}
            if kokoro_permit is not None
            else {}
        )
        if edge_stream is not None:
            feed_kwargs.update(
                {
                    "edge_stream": edge_stream,
                    "first_edge_audio": first_edge_audio,
                }
            )
        feed_task = asyncio.create_task(
            _feed_mp3(
                proc,
                text,
                engine,
                voice,
                speed,
                first_audio,
                **feed_kwargs,
            )
        )
        # feed_task 接管 Edge iterator；之后所有异常/取消均由其 finally 关闭。
        edge_stream = None
    except BaseException:
        if kokoro_permit is not None:
            kokoro_permit.release()
        if edge_stream is not None:
            await _close_async_iterator(edge_stream)
        await _await_cleanup(_reap_proc(proc))
        raise
    kokoro_permit = None
    first_audio_wait = asyncio.create_task(first_audio.wait())
    first_output_task = asyncio.create_task(proc.stdout.read(65536))
    output_observed = False
    first_output = None
    output_error = None

    try:
        while True:
            # feed 终态优先：同一调度轮内即使已置 first_audio，也不能吞掉确定的引擎错误。
            if feed_task.done():
                if feed_task.cancelled():
                    raise asyncio.CancelledError()
                exc = feed_task.exception()
                if exc is not None:
                    logger.error(
                        "REST synthesis failed request_id=%s engine=%s voice=%s: %s",
                        request_id,
                        engine,
                        voice,
                        exc,
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
                    raise HTTPException(
                        status_code=(
                            500
                            if isinstance(exc, _EncoderInputError)
                            else 502 if engine == "edge" else 500
                        ),
                        detail="synthesis failed",
                    )
                if not first_audio.is_set():
                    raise HTTPException(
                        status_code=400,
                        detail="no speakable content for the given voice",
                    )

            if first_output_task.done() and not output_observed:
                output_observed = True
                try:
                    first_output = first_output_task.result()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    output_error = exc

            # stdout EOF/读异常已经证明该编码器不可能再产生有效响应；即使全局
            # timeout=0，也不能继续占住 ffmpeg 槽等待尚未产出首音频的上游。
            # 已观察到的非零退出同理。先让出一轮，仍保持同刻 feed 确定错误优先。
            encoder_failed = output_observed and (
                output_error is not None
                or not first_output
                or (
                    proc.returncode is not None
                    and (
                        proc.returncode != 0 or not feed_task.done()
                    )
                )
            )
            if encoder_failed:
                if not feed_task.done():
                    await asyncio.sleep(0)
                    if feed_task.done():
                        continue
                logger.error(
                    "REST encoder preflight failed request_id=%s "
                    "engine=%s voice=%s format=%s returncode=%r error=%r",
                    request_id,
                    engine,
                    voice,
                    response_format,
                    proc.returncode,
                    output_error,
                )
                raise HTTPException(status_code=500, detail="synthesis failed")

            if first_audio.is_set() and output_observed:
                if not feed_task.done():
                    # first_audio 与 drain/上游异常可能同 tick；让确定的 feed 终态
                    # 优先于“编码器无输出”分类，避免把 Edge 故障误报成本地 500。
                    await asyncio.sleep(0)
                    if feed_task.done():
                        continue
                if proc.returncode not in (None, 0):
                    logger.error(
                        "REST encoder exited before handoff request_id=%s "
                        "engine=%s voice=%s format=%s returncode=%r",
                        request_id,
                        engine,
                        voice,
                        response_format,
                        proc.returncode,
                    )
                    raise HTTPException(status_code=500, detail="synthesis failed")

                await _cancel_and_consume_preflight_tasks(first_audio_wait)
                proc.stdout = _PrefetchedStreamReader(proc.stdout, first_output)
                return proc, feed_task

            pending = {
                task
                for task in (first_audio_wait, feed_task, first_output_task)
                if not task.done()
            }
            if not pending:
                # 正常状态均已在上方分类；这里只防止未来改动造成忙循环。
                raise HTTPException(status_code=500, detail="synthesis failed")

            remaining = None
            if deadline is not None:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise HTTPException(
                        status_code=504, detail="synthesis timed out"
                    )
            done, _ = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
                timeout=remaining,
            )
            if not done:
                raise HTTPException(status_code=504, detail="synthesis timed out")
    except BaseException as primary_error:
        try:
            await _await_cleanup(
                _dispose_synthesis_preflight(
                    proc, feed_task, first_audio_wait, first_output_task
                )
            )
        except BaseException as cleanup_error:
            if isinstance(cleanup_error, asyncio.CancelledError):
                # 清理期间新到达的 owner 取消高于此前普通请求错误；资源已由
                # _await_cleanup 等待收尾，不能再把取消改写回旧 HTTP 状态。
                raise
            logger.error(
                "REST preflight cleanup failed while preserving %s",
                type(primary_error).__name__,
                exc_info=(
                    type(cleanup_error),
                    cleanup_error,
                    cleanup_error.__traceback__,
                ),
            )
            raise primary_error from cleanup_error
        raise


async def _wait_for_http_disconnect(request: Request, disconnect_seen):
    # FastAPI 已在进入 endpoint 前完成请求体解析；这里只消费其后的 ASGI 生命周期消息。
    while True:
        message = await request.receive()
        if message.get("type") == "http.disconnect":
            disconnect_seen.set()
            return


async def _dispose_synthesis_task(task):
    """取消未交付的预检任务；若它已产出 session，则接管并回收。"""
    if not task.done():
        task.cancel()
    try:
        session = await task
    except asyncio.CancelledError:
        return
    except Exception as exc:
        # 请求已不可继续；取出并记录同刻完成的预检异常，避免 never-retrieved 告警。
        logger.debug("Discarded REST preflight failed during cleanup: %s", exc)
        return
    await _dispose_mp3_session(*session)


async def _stop_disconnect_watcher(watcher):
    if not watcher.done():
        watcher.cancel()
    # 把 watcher 自身的 CancelledError 吸收在独立 cleanup task 内；若 owner 此时
    # 被外部取消，_await_cleanup 会等 receive 真正退出后再恢复该取消语义。
    async def finish():
        try:
            await watcher
        except asyncio.CancelledError:
            pass
        if not watcher.cancelled():
            watcher.result()

    await _await_cleanup(finish())


async def _start_synthesis_for_request(
    request: Request,
    text: str,
    engine: str,
    voice: str,
    speed: float,
    request_id: str = "",
    response_format: str = "mp3",
):
    disconnect_seen = asyncio.Event()
    if response_format == "mp3":
        # 保持既有 MP3 内部调用形状；非 MP3 才显式携带格式。
        synthesis = _start_synthesis(text, engine, voice, speed, request_id)
    else:
        synthesis = _start_synthesis(
            text,
            engine,
            voice,
            speed,
            request_id,
            response_format=response_format,
        )
    synthesis_task = asyncio.create_task(
        synthesis
    )
    watcher = asyncio.create_task(
        _wait_for_http_disconnect(request, disconnect_seen)
    )
    session = None
    try:
        done, _ = await asyncio.wait(
            {synthesis_task, watcher},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if watcher in done:
            # watcher 正常完成只可能是明确的 http.disconnect；receive 异常原样传播。
            watcher.result()
            abandoned_task = synthesis_task
            synthesis_task = None
            await _await_cleanup(_dispose_synthesis_task(abandoned_task))
            raise _HttpRequestDisconnected()

        completed_task = synthesis_task
        synthesis_task = None
        session = completed_task.result()
        await _stop_disconnect_watcher(watcher)
        watcher = None
        # 停止 receive 的同一时刻仍可能交付 disconnect；此时 session 尚未交接，
        # 必须就地回收，不能让 StreamingResponse 再发送响应头。
        if disconnect_seen.is_set():
            abandoned_session = session
            session = None
            await _await_cleanup(_dispose_mp3_session(*abandoned_session))
            raise _HttpRequestDisconnected()
        result = session
        session = None
        return result
    except BaseException:
        if synthesis_task is not None:
            abandoned_task = synthesis_task
            synthesis_task = None
            await _await_cleanup(_dispose_synthesis_task(abandoned_task))
        if session is not None:
            abandoned_session = session
            session = None
            await _await_cleanup(_dispose_mp3_session(*abandoned_session))
        raise
    finally:
        if watcher is not None:
            await _stop_disconnect_watcher(watcher)


class _Mp3CleanupClaim:
    """在响应包装器与音频生成器之间同步转移唯一清理权。"""

    def __init__(self):
        self._claimed = False

    def claim(self):
        if self._claimed:
            return False
        self._claimed = True
        return True


class _PostStreamSynthesisError(RuntimeError):
    """响应已开始后检测到合成失败；让传输中断而不是正常结束残缺音频。"""


def _feed_task_failure(feed_task):
    """返回 feed 是否已失败及其异常；正常完成与仍在运行都不是失败。"""
    if not feed_task.done():
        return False, None
    if feed_task.cancelled():
        return True, None
    error = feed_task.exception()
    return error is not None, error


def _pre_stream_feed_failure_status(engine, error):
    # stdin/编码器故障属于本地 500；Edge 上游异常保持既有 502 语义。
    if error is None or isinstance(error, _EncoderInputError):
        return 500
    return 502 if engine == "edge" else 500


async def _raise_for_late_stream_failure(proc, feed_task, engine, voice):
    # stdout EOF 与 feed/process 终态可能在同一调度轮交付，先让状态收敛。
    await asyncio.sleep(0)
    feed_failed, feed_error = _feed_task_failure(feed_task)
    if feed_task.cancelled():
        reason = "audio feed was cancelled before stream completion"
    elif not feed_task.done():
        reason = "encoder output ended before audio feed completed"
    else:
        reason = "audio feed failed after stream start" if feed_failed else None

    cause = feed_error
    encoder_returncode = proc.returncode
    if reason is None and encoder_returncode is None:
        try:
            wait_for_exit = proc.wait()
            if TTS_SYNTHESIS_TIMEOUT_SECONDS:
                encoder_returncode = await asyncio.wait_for(
                    wait_for_exit, TTS_SYNTHESIS_TIMEOUT_SECONDS
                )
            else:
                encoder_returncode = await wait_for_exit
            if proc.returncode is not None:
                encoder_returncode = proc.returncode
        except asyncio.TimeoutError as exc:
            reason = "audio encoder did not exit after output ended"
            cause = exc
        except Exception as exc:
            reason = "audio encoder exit status could not be determined"
            cause = exc

    if encoder_returncode not in (None, 0):
        reason = f"audio encoder exited with status {encoder_returncode}"

    if reason is None:
        return
    logger.error(
        "REST stream failed after start (engine=%s voice=%s): %s",
        engine,
        voice,
        reason,
    )
    error = _PostStreamSynthesisError(reason)
    if cause is not None:
        raise error from cause
    raise error


async def _read_encoded_stream_chunk(proc, feed_task, engine, voice):
    """读取一个编码块，同时观察 feed 的确定失败，避免静默 stdout 掩盖错误。"""
    read_task = asyncio.create_task(proc.stdout.read(65536))
    timeout = TTS_SYNTHESIS_TIMEOUT_SECONDS or None
    deadline = (
        asyncio.get_running_loop().time() + timeout
        if timeout is not None
        else None
    )
    try:
        while not read_task.done():
            feed_failed, _ = _feed_task_failure(feed_task)
            if feed_failed:
                await _raise_for_late_stream_failure(
                    proc, feed_task, engine, voice
                )

            pending = {read_task}
            if not feed_task.done():
                pending.add(feed_task)
            remaining = None
            if deadline is not None:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    remaining = 0
            done, _ = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
                timeout=remaining,
            )
            if not done:
                logger.error(
                    "REST stream timed out after start "
                    "(engine=%s voice=%s)",
                    engine,
                    voice,
                )
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    except OSError as exc:
                        logger.warning(
                            "Could not kill REST encoder after stream timeout; "
                            "cleanup will wait for exit: %s",
                            exc,
                        )
                raise _PostStreamSynthesisError(
                    "audio stream timed out after response start"
                )
        feed_failed, _ = _feed_task_failure(feed_task)
        encoder_failed = (
            proc.returncode is not None
            and (
                proc.returncode != 0
                or not feed_task.done()
            )
        )
        if feed_failed or encoder_failed:
            await _raise_for_late_stream_failure(
                proc, feed_task, engine, voice
            )
        return read_task.result()
    finally:
        if not read_task.done():
            read_task.cancel()
            await _await_cleanup(
                asyncio.gather(read_task, return_exceptions=True)
            )


async def _stream_mp3(proc, feed_task, engine, voice, cleanup_claim=None):
    # 流式读取 ffmpeg 输出。proc/feed_task 由 _start_synthesis 预检后传入(首音频已确认)。
    try:
        while True:
            chunk = await _read_encoded_stream_chunk(
                proc, feed_task, engine, voice
            )
            if not chunk:
                await _raise_for_late_stream_failure(
                    proc, feed_task, engine, voice
                )
                break
            yield chunk
    except (asyncio.CancelledError, GeneratorExit):
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            except OSError as exc:
                logger.warning(
                    "Could not kill REST encoder after stream cancellation; "
                    "cleanup will wait for exit: %s",
                    exc,
                )
        raise
    finally:
        if cleanup_claim is None or cleanup_claim.claim():
            await _await_cleanup(
                _finalize_mp3_session(proc, feed_task, engine, voice)
            )


class _Mp3StreamingResponse(StreamingResponse):
    """持有预检 session，覆盖响应头发送前生成器尚未启动的取消窗口。"""

    def __init__(self, proc, feed_task, engine, voice, **kwargs):
        self._proc = proc
        self._feed_task = feed_task
        self._engine = engine
        self._voice = voice
        self._cleanup_claim = _Mp3CleanupClaim()
        super().__init__(
            _stream_mp3(
                proc,
                feed_task,
                engine,
                voice,
                cleanup_claim=self._cleanup_claim,
            ),
            **kwargs,
        )

    async def stream_response(self, send):
        # endpoint 返回 response 后到 response.start 前仍存在一个调度窗口；在真正
        # 提交 200 前再让终态收敛并复查，已知失败仍可诚实返回 JSON 500/502。
        await asyncio.sleep(0)
        feed_failed, feed_error = _feed_task_failure(self._feed_task)
        encoder_failed = (
            self._proc.returncode is not None
            and (
                self._proc.returncode != 0
                or not self._feed_task.done()
            )
        )
        if feed_failed or encoder_failed:
            status_code = (
                _pre_stream_feed_failure_status(self._engine, feed_error)
                if feed_failed
                else 500
            )
            request_id_value = next(
                (
                    value.decode("latin-1")
                    for name, value in self.raw_headers
                    if name.lower() == b"x-request-id"
                ),
                None,
            )
            request_id = _request_id_from_header(request_id_value)
            path = self._scope.get("path", "")
            if _is_v1_path(path):
                content = _openai_error_body(
                    "synthesis failed",
                    _openai_error_type_for_status(status_code),
                    _openai_error_code_for_status(status_code),
                )
            else:
                content = {"detail": "synthesis failed"}
            logger.error(
                "REST synthesis failed before response start "
                "(engine=%s voice=%s status=%s)",
                self._engine,
                self._voice,
                status_code,
                exc_info=(
                    (type(feed_error), feed_error, feed_error.__traceback__)
                    if feed_error is not None
                    else None
                ),
            )
            response = JSONResponse(
                status_code=status_code,
                content=content,
                headers={"X-Request-ID": request_id},
            )
            await response(self._scope, self._receive, send)
            return
        await super().stream_response(send)

    async def __call__(self, scope, receive, send):
        self._scope = scope
        self._receive = receive
        try:
            await super().__call__(scope, receive, send)
        finally:
            if self._cleanup_claim.claim():
                await _await_cleanup(
                    _dispose_mp3_session(self._proc, self._feed_task)
                )


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
        proc, feed_task = await _start_synthesis_for_request(
            request, cleaned, req.engine, req.voice, req.speed, request_id
        )
    except HTTPException as exc:
        logger.warning("REST request failed request_id=%s engine=%s voice=%s status=%s detail=%s",
                       request_id, req.engine, req.voice, exc.status_code, exc.detail)
        exc.headers = {**(exc.headers or {}), "X-Request-ID": request_id}
        raise
    # 预检成功后、返回 StreamingResponse 前若出岔子，生成器可能永不迭代 → finally 不触发。
    # 不把资源回收责任外包给框架驱动行为，此处自兜底：构造响应失败即就地回收 proc/feed_task。
    try:
        disposition = "attachment; filename=tts-output.mp3" if download else "inline"
        return _Mp3StreamingResponse(
            proc,
            feed_task,
            req.engine,
            req.voice,
            media_type="audio/mpeg",
            headers={"Content-Disposition": disposition, "X-Request-ID": request_id},
        )
    except BaseException:
        await _await_cleanup(_dispose_mp3_session(proc, feed_task))
        raise


@app.post("/v1/audio/speech")
async def openai_audio_speech(req: OpenAISpeechRequest, request: Request):
    _require_v1_api_key(request)
    request_id = _request_id_for_request(request)
    if pipeline_zh is None or pipeline_en is None:
        raise _openai_http_exception(
            503, "TTS engine not ready", headers={"X-Request-ID": request_id}
        )

    fmt = req.response_format.lower()
    if fmt not in OPENAI_SUPPORTED_RESPONSE_FORMATS:
        supported = ", ".join(OPENAI_AUDIO_FORMATS)
        raise _openai_http_exception(
            400,
            f"response_format '{req.response_format}' is not supported; "
            f"choose one of: {supported}",
            headers={"X-Request-ID": request_id},
        )
    stream_format = req.stream_format.strip().lower()
    if stream_format != "audio":
        raise _openai_http_exception(
            400,
            f"stream_format '{req.stream_format}' is not supported; "
            "only audio is available",
            headers={"X-Request-ID": request_id},
        )
    if req.instructions and req.instructions.strip():
        raise _openai_http_exception(
            400,
            "instructions is not supported by the local Edge/Kokoro engines",
            headers={"X-Request-ID": request_id},
        )
    if req.format and req.format.strip():
        raise _openai_http_exception(
            400,
            "format is not supported; use response_format",
            headers={"X-Request-ID": request_id},
        )
    if req.language and req.language.strip():
        raise _openai_http_exception(
            400,
            "language is not supported; select an engine and voice explicitly",
            headers={"X-Request-ID": request_id},
        )
    if req.lang_code and req.lang_code.strip():
        raise _openai_http_exception(
            400,
            "lang_code is not supported; select an engine and voice explicitly",
            headers={"X-Request-ID": request_id},
        )
    if req.lang and req.lang.strip():
        raise _openai_http_exception(
            400,
            "lang is not supported; select an engine and voice explicitly",
            headers={"X-Request-ID": request_id},
        )

    text = req.input.strip()
    if not text:
        raise _openai_http_exception(
            400, "input must not be empty", headers={"X-Request-ID": request_id}
        )
    cleaned = clean_text(text).strip()
    if not cleaned:
        raise _openai_http_exception(
            400,
            "input is empty after cleaning",
            headers={"X-Request-ID": request_id},
        )

    engine = _openai_resolve_engine(req.model)
    voice = _openai_resolve_voice(req.voice, cleaned, engine)
    await _openai_validate_voice(engine, voice)
    if engine == "kokoro":
        _openai_validate_kokoro_text_compatibility(cleaned, voice)

    try:
        proc, feed_task = await _start_synthesis_for_request(
            request,
            cleaned,
            engine,
            voice,
            req.speed,
            request_id,
            response_format=fmt,
        )
    except (_HttpRequestDisconnected, ClientDisconnect):
        raise
    except HTTPException as exc:
        logger.warning(
            "OpenAI speech failed request_id=%s engine=%s voice=%s status=%s detail=%s",
            request_id,
            engine,
            voice,
            exc.status_code,
            exc.detail,
        )
        headers = {**(exc.headers or {}), "X-Request-ID": request_id}
        if exc.status_code == 429 and "Retry-After" not in headers:
            headers["Retry-After"] = OPENAI_RETRY_AFTER_SECONDS
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            exc.headers = headers
            raise
        message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        raise _openai_http_exception(
            exc.status_code, message, headers=headers
        ) from None
    except Exception:
        logger.exception(
            "OpenAI speech preflight failed request_id=%s engine=%s voice=%s",
            request_id,
            engine,
            voice,
        )
        raise _openai_http_exception(
            500,
            "synthesis failed",
            headers={"X-Request-ID": request_id},
        ) from None
    try:
        format_spec = OPENAI_AUDIO_FORMATS[fmt]
        return _Mp3StreamingResponse(
            proc,
            feed_task,
            engine,
            voice,
            media_type=format_spec["media_type"],
            headers={
                "Content-Disposition": (
                    "inline; filename=tts-output."
                    f"{format_spec['extension']}"
                ),
                "X-Request-ID": request_id,
            },
        )
    except BaseException:
        await _await_cleanup(_dispose_mp3_session(proc, feed_task))
        raise


@app.get("/v1/models")
async def openai_list_models(request: Request):
    _require_v1_api_key(request)
    return {
        "object": "list",
        "data": [
            {"id": "kokoro", "object": "model", "owned_by": "tts-api"},
            {"id": "edge", "object": "model", "owned_by": "tts-api"},
        ],
    }


@app.get("/v1/audio/voices")
async def openai_list_voices(request: Request):
    _require_v1_api_key(request)
    data = [
        {
            "id": v["id"],
            "name": v["name"],
            "gender": v.get("gender", ""),
            "locale": "zh" if v.get("language") == "zh" else "en",
            "engine": "kokoro",
        }
        for v in KOKORO_VOICES
    ]
    try:
        edge_voices = await _get_edge_voices()
    except Exception:
        edge_voices = []
    for v in edge_voices:
        data.append(
            {
                "id": v["ShortName"],
                "name": v.get("FriendlyName", v["ShortName"]),
                "gender": v.get("Gender", ""),
                "locale": v.get("Locale", ""),
                "engine": "edge",
            }
        )
    return {"object": "list", "data": data}


def _preview_text(engine: str, voice: str) -> str:
    # 中文音色用中文样句：Kokoro 中文前缀 + Edge 中文 locale(zh-*)。
    # 此前 Edge zh-CN/zh-HK 等误走英文样句，Auto 试听中文边听起来像英文。
    if engine == "kokoro" and _is_chinese_kokoro_voice(voice):
        return "你好，这是音色试听。"
    if engine == "edge" and str(voice).lower().startswith("zh-"):
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
    try:
        voice = _validate_voice_value(voice)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
            headers={"X-Request-ID": request_id},
        ) from exc
    if engine == "kokoro" and voice not in KOKORO_VOICE_IDS:
        raise HTTPException(status_code=422, detail="unknown Kokoro voice", headers={"X-Request-ID": request_id})
    text = _preview_text(engine, voice)
    try:
        proc, feed_task = await _start_synthesis_for_request(
            request, text, engine, voice, speed, request_id
        )
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "X-Request-ID": request_id}
        raise
    try:
        return _Mp3StreamingResponse(
            proc,
            feed_task,
            engine,
            voice,
            media_type="audio/mpeg",
            headers={"Content-Disposition": "inline", "X-Request-ID": request_id},
        )
    except BaseException:
        await _await_cleanup(_dispose_mp3_session(proc, feed_task))
        raise


# =========================
# 合成分发：各引擎把 PCM 产出到 queue，随时响应 cancel_event 中止
# =========================
async def synth_kokoro(
    units, voice, speed, queue, ws, cancel_event, prefetch=False
):
    # units 是前端按 \n 传来的合成单元(句/语言子片段)，逐单元清洗+合成。
    # 每个单元恰好发一个 seg(即使清洗后为空)，保证前后端单元数 1:1 对齐，
    # 变速续播的句级时间线不漂移(后端不再二次按标点切分)。
    emitted_pcm = False
    prefetch_permit = None
    try:
        if prefetch:
            prefetch_permit = await _acquire_kokoro_permit(
                cancel_event, prefetch=True
            )
            if prefetch_permit is None:
                return False

        for u in units:
            if ws.client_state == WebSocketState.DISCONNECTED or cancel_event.is_set():
                break
            cleaned = clean_text(u).strip()
            # 句边界标记：前端据此建句级时间线，支持变速时按句定位与续播
            await queue.put({"type": "seg", "text": cleaned})
            if not cleaned:
                continue  # 清洗后为空(如独立图片/分隔线)：仍发 seg 保持计数，无音频
            for fragment in split_kokoro_unit(cleaned):
                if (
                    ws.client_state == WebSocketState.DISCONNECTED
                    or cancel_event.is_set()
                ):
                    break
                if prefetch_permit is None:
                    pcm = await run_kokoro(
                        fragment, voice, speed, cancel_event
                    )
                else:
                    pcm = await run_kokoro(
                        fragment,
                        voice,
                        speed,
                        cancel_event,
                        permit=prefetch_permit,
                    )
                if cancel_event.is_set():
                    break
                for i in range(0, len(pcm), 2048):
                    await queue.put(pcm[i:i + 2048])
                    emitted_pcm = True
        return emitted_pcm
    finally:
        if prefetch_permit is not None:
            prefetch_permit.release()


async def synth_edge(text, voice, speed, queue, ws, cancel_event, prefetch: bool = False):
    rate = _edge_rate_for_speed(speed)
    edge_stream, first_edge_audio = await _prepare_edge_audio(
        text, voice, rate, cancel_event=cancel_event
    )
    if edge_stream is None:
        return
    if ws.client_state == WebSocketState.DISCONNECTED or cancel_event.is_set():
        await _close_async_iterator(edge_stream)
        return

    try:
        process = await _create_edge_pcm_decoder(prefetch=prefetch)
    except BaseException:
        await _close_async_iterator(edge_stream)
        raise

    edge_stream_closed = False

    async def close_edge_stream():
        nonlocal edge_stream_closed
        if edge_stream_closed:
            return
        edge_stream_closed = True
        await _close_async_iterator(edge_stream)

    # Edge 整段连续流式，无法逐句切分音频边界，故整段发一个句边界标记(run 级粒度)。
    # 前端据此建时间线；变速时 Edge 段按整段重合成(云端成本高，run 级是自然单元)。
    # 进程已创建并占用 ffmpeg 配额；分段标记也可能因下游背压而阻塞。
    # 在进入 run_io 的统一清理保护前若发生取消，必须就地回收 decoder，
    # 否则该路径会遗留子进程并永久占住一个配额。
    try:
        await queue.put({"type": "seg", "text": text})
    except BaseException as primary_error:
        cleanup_error = None
        try:
            try:
                # 只吞掉普通 close 异常并挂到原始错误上；CancelledError 等控制
                # 信号不能被伪装成清理失败。
                await _await_cleanup(close_edge_stream())
            except Exception as exc:
                cleanup_error = exc
        finally:
            try:
                await _reap_edge_pcm_decoder(process, prefetch=prefetch)
            except Exception as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if cleanup_error is not None:
            raise primary_error from cleanup_error
        raise

    async def feed():
        # 无论是否异常，stdin 必须关闭，否则 ffmpeg 不 EOF，read() 永久阻塞
        try:
            process.stdin.write(first_edge_audio)
            await process.stdin.drain()
            async for data in edge_stream:
                if ws.client_state == WebSocketState.DISCONNECTED or cancel_event.is_set():
                    break
                process.stdin.write(data)
                await process.stdin.drain()
        finally:
            await close_edge_stream()
            try:
                process.stdin.close()
            except Exception:
                pass

    async def read():
        # ffmpeg 管道每次返回字节数任意；按 16-bit 样本(2 字节)对齐后再发，
        # 奇数尾字节留到下一帧，避免高低字节错位导致持续杂音。
        leftover = b""
        emitted_pcm = False
        while True:
            if ws.client_state == WebSocketState.DISCONNECTED or cancel_event.is_set():
                return None
            data = await process.stdout.read(2048)
            if not data:
                return emitted_pcm
            data = leftover + data
            even = len(data) & ~1
            if even:
                await queue.put(data[:even])
                emitted_pcm = True
            leftover = data[even:]

    async def run_io():
        feed_task = asyncio.create_task(feed())
        read_task = asyncio.create_task(read())
        try:
            await asyncio.wait(
                {feed_task, read_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            # 同一调度轮完成时优先传播上游错误；否则 decoder 的确定 EOF
            # 不能继续等待仍挂起的上游，否则 timeout=0 会永久占用 ffmpeg 槽。
            if feed_task.done():
                await feed_task
            if read_task.done():
                read_outcome = await read_task
                if not feed_task.done():
                    raise RuntimeError(
                        "Edge decoder ended before upstream feed completed"
                    )
            else:
                read_outcome = await read_task

            if not feed_task.done():
                await feed_task

            # None 表示 WS 断连或显式取消；只有正常 EOF 才验证完整终态。
            if read_outcome is None:
                return False
            # 子进程退出通知可能紧随 stdout EOF 到达，先让出一轮再检查已知状态；
            # 未知状态仍交给 finally 中的受控 reaper，不能在这里无界等待。
            await asyncio.sleep(0)
            if process.returncode not in (None, 0):
                raise RuntimeError(
                    f"Edge decoder exited with status {process.returncode}"
                )
            if not read_outcome:
                raise RuntimeError("Edge decoder produced no PCM output")

            # 正常 feed + PCM + EOF 路径必须先取得自然退出码，再允许 reaper 进入。
            # kill() 是否抛错不能证明退出码由清理产生；进程可能已自然失败，但
            # asyncio transport 尚未回填 returncode。短 grace 避免 timeout=0 时
            # decoder 关闭 stdout 后仍不退出而无界占用进程槽。
            try:
                decoder_returncode = await asyncio.wait_for(
                    process.wait(), EDGE_DECODER_EXIT_GRACE_SECONDS
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    "Edge decoder did not exit after PCM output ended"
                ) from exc
            if decoder_returncode is None:
                decoder_returncode = process.returncode
            if decoder_returncode not in (0,):
                raise RuntimeError(
                    f"Edge decoder exited with status {decoder_returncode}"
                )
            return True
        finally:
            for task in (feed_task, read_task):
                if not task.done():
                    task.cancel()
            await _await_cleanup(
                asyncio.gather(feed_task, read_task, return_exceptions=True)
            )

    # feed/read 中任一 await 都可能长期阻塞，循环边界轮询 cancel_event 无法及时中止。
    # 用独立哨兵竞速整个 I/O 组合；取消胜出时主动取消两侧任务，feed 的 finally
    # 会关闭 stdin，随后统一回收 decoder。
    work = None
    cancel_wait = None
    normal_io_completed = False
    decoder_returncode = None
    try:
        work = asyncio.create_task(run_io())
        cancel_wait = asyncio.create_task(cancel_event.wait())
        done, _ = await asyncio.wait(
            {work, cancel_wait},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_wait in done:
            return

        try:
            normal_io_completed = await work
        except Exception as exc:
            logger.error(
                "synth_edge task failed: %s",
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            raise
    finally:
        if cancel_wait is not None and not cancel_wait.done():
            cancel_wait.cancel()
        if work is not None and not work.done():
            work.cancel()
        try:
            tasks = [task for task in (work, cancel_wait) if task is not None]
            if tasks:
                await _await_cleanup(
                    asyncio.gather(*tasks, return_exceptions=True)
                )
        finally:
            try:
                await _await_cleanup(close_edge_stream())
            finally:
                # work 清理本身被取消时也必须进入 decoder 回收，避免泄漏进程配额。
                decoder_returncode = await _reap_edge_pcm_decoder(
                    process, prefetch=prefetch
                )

    # 正常 I/O 已在 run_io 内等待自然退出；此处只做防御性一致性检查。
    # 取消或异常路径的 kill 状态不会进入 normal_io_completed 分支。
    if (
        normal_io_completed
        and decoder_returncode not in (None, 0)
    ):
        raise RuntimeError(
            f"Edge decoder exited with status {decoder_returncode}"
        )


# =========================
# WS CORE
# =========================
@app.websocket("/ws/tts")
async def ws_tts(ws: WebSocket):
    sender_task = None
    is_prefetch = ws.query_params.get("prefetch") == "1"
    # WS 握手不经 HTTP 鉴权中间件，此处单独校验。同源自有页面(index.html)用 Origin 免密；
    # 外部客户端(如 CRX)浏览器无法为 WS 设自定义头，改用 ?key= 查询参数。未配置密钥 = 完全开放。
    # 校验失败以 1008(Policy Violation)在 accept 前拒绝握手。
    provided_key = ws.query_params.get("key", "").strip()
    if (
        TTS_API_KEY
        and not _key_matches(provided_key)
        and not _is_same_origin(ws.headers)
    ):
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

            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))
            if message.get("bytes") is not None:
                await ws.close(code=1003)
                return
            msg = message.get("text")
            if msg is None:
                await ws.send_json({"type": "error", "message": "请求必须使用文本 JSON 帧"})
                continue
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
                        produced_audio = await synth_kokoro(
                            units,
                            voice,
                            speed,
                            queue,
                            ws,
                            cancel_event,
                            prefetch=is_prefetch,
                        )
                        if not produced_audio and not cancel_event.is_set():
                            raise RuntimeError("Kokoro synthesis produced no PCM output")
                    else:
                        await synth_edge(
                            text, voice, speed, queue, ws, cancel_event,
                            prefetch=is_prefetch,
                        )

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
