<h1 align="center">TTS API</h1>

<p align="center">
  <a href="#"><img src="https://img.shields.io/badge/version-v0.1-blue" alt="Version" /></a>
  <a href="#"><img src="https://img.shields.io/badge/license-MIT-green" alt="License" /></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/python-3.10+-blue" alt="Python" /></a>
  <a href="https://fastapi.tiangolo.com"><img src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white" alt="FastAPI" /></a>
  <a href="https://linux.do" target="_blank" rel="noopener noreferrer"><img src="https://img.shields.io/badge/LinuxDo-论坛-F90?logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxNiIgaGVpZ2h0PSIxNiIgdmlld0JveD0iMCAwIDI0IDI0IiBmaWxsPSJub25lIiBzdHJva2U9IiNmZmYiIHN0cm9rZS13aWR0aD0iMiIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBzdHJva2UtbGluZWpvaW49InJvdW5kIj48cG9seWdvbiBwb2ludHM9IjEyIDIgMTUgOSAyMiA5IDE2LjUgMTQuNSAxOSAyMiAxMiAxNyA1IDIyIDcuNSAxNC41IDIgOSA5IDkiLz48L3N2Zz4=" alt="LinuxDo" /></a>
</p>

<p align="center">
  <a href="README.md">English</a> |
  <a href="README_CN.md">中文</a>
</p>

基于 FastAPI 构建的流式语音合成服务。本地可使用 CPU-only Kokoro 进行离线合成，也可使用微软 Edge TTS 进行云端合成，自带 Web 前端界面。

## 功能

| 能力 | 说明 |
|------|------|
| 双引擎 | Kokoro 本地 + Edge 微软云端 |
| 实时流式 | WebSocket 推送 PCM 帧，Web Audio API 连续播放 |
| 语速控制 | 0.5x–2.0x (UI) / 0.5x–3.0x (API)，播放中可逐句切换 |
| 跳转与暂停 | ±10s 跳转、暂停/继续、停止 |
| 语言自动路由 | Auto 模式逐句检测中/英文，分配对应音色 |
| REST API | POST /api/tts 返回流式 MP3 |
| 深色模式 | 持久化主题切换 |
| 中英文界面 | 语言实时切换 |

## 架构

```text
浏览器 (index.html)
  |  WebSocket /ws/tts  (JSON 请求 + PCM 二进制帧)
  |  REST  POST /api/tts (MP3 流)
  v
FastAPI (app.py)
  |-- 静态文件: /index.html, /static/style.css
  |-- 健康检查: GET /
  |-- 密钥探测: GET /api/auth
  |-- 音色目录: GET /api/voices (kokoro + edge)
  |-- TTS:     POST /api/tts (MP3 流)
  |-- 文档:    GET /api (交互式 API 测试页)
  |-- WS:      /ws/tts (交互式流式合成)
       |
        +-- Kokoro (本地, asyncio.to_thread + 线程锁)
        +-- Edge   (云端, ffmpeg 子进程转码)
```

## 项目文件

```text
tts-api
├── app.py              # FastAPI 后端
├── index.html          # Web 前端
├── api.html            # 交互式 API 文档页
├── style.css           # 样式
├── API.md              # 接口文档
├── Dockerfile          # 容器镜像
└── docker-compose.yml
```

## 快速开始

### 本地运行

需要 Python 3.10+，并预装 `ffmpeg` 和 `espeak-ng`。

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8880
```

打开 `http://localhost:8880/index.html`。

### Docker（推荐）

镜像安装 `espeak-ng`、`ffmpeg`、`libsndfile1`、CPU 版 torch 和 Python 依赖，然后在端口 `8880` 上运行 `uvicorn app:app`。默认内存上限 `4G`（PyTorch CPU + 两个 Kokoro pipeline 峰值约 2–2.5G）。

1. 首次运行前调整 `docker-compose.yml`：
   - `TTS_API_KEY` — 改成你自己的强随机值（例如 `openssl rand -hex 32`），留空则完全开放。
   - `MAX_TEXT_LENGTH` — 单次合成最大字符数（默认 `100000`）。
   - `TTS_CORS_ALLOW_ORIGINS` — 允许的浏览器来源，多个值用英文逗号分隔（默认 `*`）。
   - `EDGE_VOICES_CACHE_TTL_SECONDS` — Edge 音色列表缓存 TTL（默认 `86400` 秒）。刷新失败时，如已有成功缓存，会继续返回旧缓存。
   - `TTS_SYNTHESIS_TIMEOUT_SECONDS` — REST 与 WebSocket 合成超时；`0` 表示关闭（默认 `0`）。
   - `TTS_MAX_FFMPEG_PROCESSES` — `ffmpeg` 子进程并发上限，超限显式拒绝（默认 `2`）。
   - `TTS_MAX_SYNTHESIS_CONCURRENCY` — Kokoro 推理并发上限，REST 与 WebSocket 共用（默认 `2`）。采用排队等待而非拒绝；防止大量请求占着线程池 worker 阻塞在语言锁上，拖垮整个线程池。
   - `volumes` — `./models:/app/models` 缓存 Kokoro 模型权重，容器重建无需重新下载。
   - `ports` — 映射 `8880` 供本机直连。若要用外部反代网络（如 Caddy 的 `caddy_net`），先 `docker network create caddy_net`，再取消 `docker-compose.yml` 里可选的 `networks` 注释块。

2. 构建并启动：

   ```bash
   docker compose up --build -d
   ```

3. 首次启动会下载 Kokoro 模型（数百 MB）到 `./models`，并预热两个 pipeline。`GET /` 在预热完成前返回 `503`，完成后返回 `200`。compose `healthcheck` 的 `start_period: 60s` 覆盖此阶段。

4. 打开 `http://<host>:8880/index.html`（或通过你的反代访问）。

更新 / 重启 / 查看日志：

```bash
docker compose pull        # 使用预构建镜像时
docker compose up --build -d
docker compose logs -f tts-api
docker compose down
```

## API 文档

详见 [API.md](API.md)，或在浏览器打开 `/api` 查看带在线测试器的交互文档页。REST `/api/tts` 支持在线播放或 `?download=true` 附件下载，`/api/voices/preview` 可返回短音色试听样例。

## 测试

运行后端回归测试：

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -p "test_*.py" -v
```

测试用 fake Kokoro 和 Edge 模块隔离模型权重、`ffmpeg`、`espeak-ng` 与网络依赖，因此本地无需下载模型也能验证核心逻辑。覆盖范围包括文本清洗、句子切分、音色语言过滤、PCM 编码、请求校验、鉴权中间件、鉴权失败时的 CORS 响应头、REST 状态码、REST 预检清理、合成超时、request-id 响应头、readiness 检查、启动预热接线、Edge 音色缓存语义、ffmpeg 命令构造与并发限制、WebSocket 握手/鉴权/连接复用行为、合成单元管线，以及音色路由和 API 测试页的前端 HTML/JS 契约。真实音质与完整引擎集成仍需要带实际模型、系统二进制与 Edge TTS 网络访问的运行环境。

## 鉴权

服务内置 API Key 鉴权，方便外部客户端（浏览器扩展、脚本、其他后端）直接调用。

- 在 `docker-compose.yml` 的 `TTS_API_KEY` 中配置，请改成你自己的强随机值。
- 留空（未配置）= 完全开放，兼容本地直连。
- 自有页面（`/index.html`）与文档页（`/api`）与后端同源，浏览器直接使用无需密钥。
- 外部客户端携带密钥：REST 优先用 `X-API-Key: <密钥>`（尤其适合 Caddy Basic Auth 前置场景；`Authorization: Bearer <密钥>` 也可用），WebSocket 用 `/ws/tts?key=<密钥>`。

如需真正的网络隔离，仍建议部署在反向代理（如 Caddy）之后；密钥用于为外部集成提供受控接入能力。

## 反代配置（Caddy）

本服务不自带 TLS 或登录认证。典型部署是在 Caddy 前端终止 HTTPS 并（可选）添加登录网关。需转发 WebSocket 升级并设置较长超时，以免流式合成被中断。

```caddyfile
tts.example.com {
    encode gzip zstd

    # 可选：为前端页面加登录网关。
    # basic_auth 密码哈希生成：caddy hash-password
    basic_auth {
        alice $2a$14$REPLACE_WITH_YOUR_OWN_BCRYPT_HASH
    }

    reverse_proxy tts-api:8880 {
        transport http {
            read_timeout 1h
            write_timeout 1h
        }
    }
}
```

`reverse_proxy` 上游 `tts-api:8880` 是 compose 的 `container_name`。Caddy 与本服务需在同一 Docker 网络（取消 `docker-compose.yml` 中可选 `caddy_net` 注释，或接入任意共享网络）。Caddy 自动转发 `Upgrade`/`Connection` 头，`/ws/tts` 无需额外配置。

### Basic Auth + API Key 共存

如果对**整站**启用 Caddy `basic_auth`，所有请求（含程序化调用）都必须先过 Basic Auth。浏览器会发送 `Authorization: Basic …`。由于内置密钥也用 `Authorization: Bearer` 时会冲突，有两种方案：

- **仅用于前端** — 保持 `basic_auth` 全站，`TTS_API_KEY` 留空。自有页面通过 `X-API-Key` 传密钥，不会与 Basic Auth 冲突，但此模式下不需要密钥。
- **程序化客户端（CRX/脚本）** — 用 matcher 给 API/WS 路径跳过 Basic Auth，改由后端 `TTS_API_KEY` 鉴权：

  ```caddyfile
  tts.example.com {
      encode gzip zstd

      @api path /api/* /ws/tts
      handle @api {
          reverse_proxy tts-api:8880 {
              transport http {
                  read_timeout 1h
                  write_timeout 1h
              }
          }
      }

      handle {
          basic_auth {
              alice $2a$14$REPLACE_WITH_YOUR_OWN_BCRYPT_HASH
          }
          reverse_proxy tts-api:8880 {
              transport http {
                  read_timeout 1h
                  write_timeout 1h
              }
          }
      }
  }
  ```

  `@api` 下由后端校验 `TTS_API_KEY`（REST `X-API-Key`/`Bearer`，WS `?key=`），前端 UI 仍走登录网关。

## 局限

- Edge TTS 需要联网，首次请求延迟较高。
- 本项目当前使用 CPU 版 PyTorch wheel 运行 Kokoro；Docker 镜像与依赖锁定文件尚未接入 GPU 加速。
- 文本长度由 `MAX_TEXT_LENGTH` 控制，默认 `100000` 字。
- 公网或多用户部署建议降低 `MAX_TEXT_LENGTH`、设置 `TTS_SYNTHESIS_TIMEOUT_SECONDS`，并按宿主机 CPU/内存控制 `TTS_MAX_FFMPEG_PROCESSES`。
- 播放缓冲区随文本长度增长，长文本可能占用较多客户端内存。

## 许可证

MIT
