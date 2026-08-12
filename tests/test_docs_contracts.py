# -*- coding: utf-8 -*-
"""文档契约测试：公开 API 说明必须覆盖真实路由边界。"""
import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class ApiReferenceContractTests(unittest.TestCase):
    def test_protected_endpoint_summary_includes_voice_preview(self):
        api_doc = (ROOT / "API.md").read_text(encoding="utf-8")
        match = re.search(r"^Protected endpoints: (.+)$", api_doc, re.MULTILINE)

        self.assertIsNotNone(match)
        self.assertIn("GET /api/voices/preview", match.group(1))

    def test_voices_docs_cover_ui_edge_locale_whitelist_and_dialects(self):
        """API.md / api.html 须声明 UI 白名单 locale 与方言示例，避免与 index.html 漂移。"""
        api_doc = (ROOT / "API.md").read_text(encoding="utf-8")
        api_page = (ROOT / "api.html").read_text(encoding="utf-8")
        index_page = (ROOT / "index.html").read_text(encoding="utf-8")

        # 与 index.html EDGE_LOCALE_WHITELIST 对齐的单一事实源检查
        whitelist_match = re.search(
            r"const EDGE_LOCALE_WHITELIST = \[([^\]]+)\]",
            index_page,
        )
        self.assertIsNotNone(whitelist_match)
        locales = re.findall(r'"([^"]+)"', whitelist_match.group(1))
        self.assertEqual(
            locales,
            [
                "zh-CN",
                "zh-CN-liaoning",
                "zh-CN-shaanxi",
                "zh-HK",
                "zh-TW",
                "en-US",
                "en-GB",
            ],
        )

        for locale in locales:
            self.assertIn(locale, api_doc)
            self.assertIn(locale, api_page)

        for sample_id in (
            "zh-CN-liaoning-XiaobeiNeural",
            "zh-CN-shaanxi-XiaoniNeural",
            "zh-HK-HiuGaaiNeural",
            "zh-TW-HsiaoChenNeural",
        ):
            self.assertIn(sample_id, api_doc)
            self.assertIn(sample_id, api_page)

        # 明确“API 全量 vs UI 白名单”与“微软上游无本地回退”边界，避免读者以为服务端已过滤。
        # API.md 的关键短语用 Markdown 强调(**full**)，断言前先剥离 */_/` 标记并归一空白，
        # 只锁语义不耦合排版——这样把 **full** 改成 _full_、`full` 或去掉强调都不会误红。
        def _plain_md(text: str) -> str:
            stripped = re.sub(r"[*_`]+", "", text.lower())
            return re.sub(r"\s+", " ", stripped)

        api_doc_plain = _plain_md(api_doc)
        self.assertIn("full microsoft edge catalog", api_doc_plain)
        self.assertIn("no server-side locale filter", api_doc_plain)
        self.assertIn("without a local fallback catalog", api_doc_plain)

        # api.html 是 HTML(无 Markdown)：直接按原样锁 i18n 键与结构，无排版耦合问题。
        self.assertIn("voicesUiNote", api_page)
        self.assertIn('data-i18n-html="voicesUiNote"', api_page)
        self.assertIn("no local fallback catalog", api_page)

    def test_readme_first_impression_sells_ready_api_and_dual_engine(self):
        """README 面向未使用者：开箱/API/双引擎等卖点须写在首段与特性表，EN/CN 对齐。"""
        en = (ROOT / "README.md").read_text(encoding="utf-8")
        zh = (ROOT / "README_CN.md").read_text(encoding="utf-8")

        en_l = en.lower()
        # 英文首印象：开箱、API、双引擎、Markdown、Auto 路由
        for needle in (
            "ready-to-run",
            "docker compose",
            "rest",
            "websocket",
            "dual",
            "markdown",
            "auto",
            "tts_api_key",
        ):
            self.assertIn(needle, en_l, f"README.md missing first-impression signal: {needle}")
        self.assertIn("Why this project", en)
        self.assertIn("Programmable API", en)
        self.assertIn("Markdown-safe input", en)

        # 中文首印象
        for needle in (
            "开箱即用",
            "docker compose",
            "REST",
            "WebSocket",
            "双引擎",
            "Markdown",
            "为什么选它",
            "可编程 API",
        ):
            self.assertIn(needle, zh, f"README_CN.md missing first-impression signal: {needle}")
        self.assertIn("Markdown 安全输入", zh)
        self.assertIn("语言自动路由", zh)

    def test_markdown_star_disambiguation_is_documented(self):
        api_doc = (ROOT / "API.md").read_text(encoding="utf-8")
        en = (ROOT / "README.md").read_text(encoding="utf-8")
        zh = (ROOT / "README_CN.md").read_text(encoding="utf-8")

        self.assertIn("complete ASCII `**` power chains", api_doc)
        self.assertIn("CJK-adjacent `**` is treated as Markdown", api_doc)
        self.assertIn("`中文*加粗*中文`", api_doc)
        self.assertIn("ASCII `**` power chains", en)
        self.assertIn("ASCII `**` 乘方链", zh)

    def test_readmes_do_not_attribute_browser_playback_controls_to_apis(self):
        en = (ROOT / "README.md").read_text(encoding="utf-8")
        zh = (ROOT / "README_CN.md").read_text(encoding="utf-8")

        self.assertNotIn(
            "REST / WebSocket / `/v1` share the same voices and limits —\n"
            "seek, pause, and change speed while audio is playing.",
            en,
        )
        self.assertNotIn(
            "REST /\nWebSocket / `/v1` 上的程序共用同一套音色与限流，"
            "支持跳转、暂停和播放中变速。",
            zh,
        )
        self.assertIn("Playback controls belong to the browser UI", en)
        self.assertIn("播放控制属于浏览器 UI", zh)

    def test_readmes_align_quick_deploy_and_ai_install_contracts(self):
        """安装章节须双语对齐，并锁住真实命令和安全边界。"""
        en = (ROOT / "README.md").read_text(encoding="utf-8")
        zh = (ROOT / "README_CN.md").read_text(encoding="utf-8")

        # 价值主张必须先于安装说明：读者先知道这是什么，再被要求 clone。
        self.assertLess(en.index("## Why this project"), en.index("## Quick Start"))
        self.assertLess(zh.index("## 为什么选它"), zh.index("## 快速开始"))

        # 安装说明集中在单一章节内，不再散落成两处重复的部署流程。
        en_start = en.index("## Quick Start")
        zh_start = zh.index("## 快速开始")
        en_end = en.index("## API", en_start)
        zh_end = zh.index("## API 文档", zh_start)
        self.assertLess(en_start, en_end)
        self.assertLess(zh_start, zh_end)

        en_section = en[en_start:en_end]
        zh_section = zh[zh_start:zh_end]
        self.assertIn("### Docker (recommended)", en_section)
        self.assertIn("### Local", en_section)
        self.assertIn("### Let an AI install it for you", en_section)
        self.assertIn("### Docker（推荐）", zh_section)
        self.assertIn("### 本地运行", zh_section)
        self.assertIn("### 让 AI 帮你装", zh_section)

        shared_facts = (
            "git clone https://github.com/babutree/TTS-API.git",
            "docker compose config --quiet",
            "docker compose up --build -d",
            "docker compose ps",
            "docker compose logs --tail=100 tts-api",
            "http://localhost:8880/",
            "http://localhost:8880/index.html",
            "ready: true",
            "TTS_API_KEY",
            "TTS_CORS_ALLOW_ORIGINS",
            "8880:8880",
            "Python 3.10+",
            "ffmpeg",
            "espeak-ng",
            "docker compose down -v",
        )
        for fact in shared_facts:
            self.assertIn(fact, en_section, f"README.md missing deployment fact: {fact}")
            self.assertIn(fact, zh_section, f"README_CN.md missing deployment fact: {fact}")

        for item in range(1, 8):
            self.assertRegex(en_section, rf"(?m)^{item}\. ")
            self.assertRegex(zh_section, rf"(?m)^{item}\. ")

        self.assertIn("15 minutes", en_section)
        self.assertIn("15 分钟", zh_section)
        self.assertIn("all host interfaces", en_section)
        self.assertIn("所有主机接口", zh_section)
        self.assertIn("Do not run `docker compose down -v`", en_section)
        self.assertIn("不得运行 `docker compose down -v`", zh_section)
        self.assertNotIn("docker compose pull", en)
        self.assertNotIn("docker compose pull", zh)

    def test_reliability_timeout_docs_match_runtime_and_compose(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        index_source = (ROOT / "index.html").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        api_doc = (ROOT / "API.md").read_text(encoding="utf-8")
        en = (ROOT / "README.md").read_text(encoding="utf-8")
        zh = (ROOT / "README_CN.md").read_text(encoding="utf-8")

        name = "EDGE_VOICES_REQUEST_TIMEOUT_SECONDS"
        for label, text in (
            ("app.py", app_source),
            ("docker-compose.yml", compose),
            ("API.md", api_doc),
            ("README.md", en),
            ("README_CN.md", zh),
        ):
            self.assertIn(name, text, f"{label} missing {name}")

        self.assertRegex(
            app_source,
            r"DEFAULT_EDGE_VOICES_REQUEST_TIMEOUT_SECONDS\s*=\s*5\.0",
        )
        self.assertIn("EDGE_VOICES_REQUEST_TIMEOUT_SECONDS=5", compose)
        self.assertIn("per attempt", api_doc.lower())
        self.assertIn("per attempt", en.lower())
        self.assertIn("每次尝试", zh)

        self.assertRegex(index_source, r"WS_INACTIVITY_TIMEOUT_MS\s*=\s*60000")
        self.assertIn("60 seconds", en.lower())
        self.assertIn("60 秒", zh)

    def test_basic_auth_bypass_is_limited_to_strongly_authenticated_v1(self):
        for name in ("README.md", "README_CN.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            match = re.search(r"(?m)^\s*@v1 path ([^\r\n]+)$", text)
            self.assertIsNotNone(match, f"{name} missing Caddy v1 matcher")
            matcher = match.group(1)
            self.assertIn("/v1/*", matcher, f"{name} matcher missing /v1/*")
            self.assertNotIn("/api/*", matcher, f"{name} bypasses legacy API")
            self.assertNotIn("/ws/tts", matcher, f"{name} bypasses legacy WS")

    def test_resource_limit_settings_are_documented_everywhere(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        api_doc = (ROOT / "API.md").read_text(encoding="utf-8")
        en = (ROOT / "README.md").read_text(encoding="utf-8")
        zh = (ROOT / "README_CN.md").read_text(encoding="utf-8")

        settings = {
            "TTS_MAX_SYNTHESIS_WAITERS": "16",
            "TTS_MAX_REQUEST_BODY_BYTES": "1048576",
            "TTS_RESPONSE_WRITE_TIMEOUT_SECONDS": "30",
            "KOKORO_MAX_UNIT_CHARS": "2000",
        }
        for setting, default in settings.items():
            self.assertIn(setting, app_source)
            self.assertIn(f"{setting}={default}", compose)
            for name, text in (
                ("API.md", api_doc),
                ("README.md", en),
                ("README_CN.md", zh),
            ):
                self.assertIn(setting, text, f"{name} missing {setting}")

        self.assertIn("pre-stream total deadline", api_doc)
        self.assertIn("post-start idle timeout", api_doc)
        self.assertIn("pre-stream total deadline", en)
        self.assertIn("post-start idle timeout", en)
        self.assertIn("流前总期限", zh)
        self.assertIn("流开始后的空闲超时", zh)

    def test_websocket_cancel_docs_match_kokoro_cleanup_boundaries(self):
        api_doc = (ROOT / "API.md").read_text(encoding="utf-8")
        api_html = (ROOT / "api.html").read_text(encoding="utf-8")

        self.assertNotIn(
            "The server immediately stops synthesis",
            api_doc,
        )
        self.assertNotIn(
            "The server immediately stops synthesis",
            api_html,
        )
        self.assertNotIn("服务端立即停止合成", api_html)
        for text in (api_doc, api_html):
            self.assertIn("queued Kokoro", text)
            self.assertIn("next generated chunk", text)
            self.assertIn("Edge decoder process", text)
        self.assertIn("排队中的 Kokoro", api_html)
        self.assertIn("下一个生成块", api_html)
        self.assertIn("Edge 解码器进程", api_html)
        self.assertNotIn("guillemets", api_html)
        self.assertIn("Chinese book-title brackets", api_html)

    def test_openai_compat_docs_match_runtime(self):
        """API.md、交互页与双语 README 须锁定当前 /v1 兼容子集。"""
        api_doc = (ROOT / "API.md").read_text(encoding="utf-8")
        api_html = (ROOT / "api.html").read_text(encoding="utf-8")
        en = (ROOT / "README.md").read_text(encoding="utf-8")
        zh = (ROOT / "README_CN.md").read_text(encoding="utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        api_l = api_doc.lower()

        for path in (
            "/v1/audio/speech",
            "/v1/models",
            "/v1/audio/voices",
            "response_format",
            "Retry-After",
        ):
            self.assertIn(path, api_doc)

        self.assertIn('OPENAI_DEFAULT_ENGINE = "edge"', app_source)
        self.assertIn("same-origin exempt", api_l)
        self.assertIn("3.0", api_doc)
        self.assertIn("4.0", api_doc)
        self.assertIn("mp3", api_l)
        self.assertIn("Markdown", api_doc)
        self.assertIn("default engine is therefore `edge`", api_l)
        self.assertIn(
            "Voice aliases never override an explicit `model`.", api_doc
        )
        self.assertIn(
            "Only a fresh, non-empty cached Edge catalog may reject a voice.",
            api_doc,
        )
        self.assertIn(
            "Malformed requests are authenticated before body validation.",
            api_doc,
        )
        self.assertIn("Every pre-stream `/v1` error includes `X-Request-ID`.", api_doc)
        self.assertIn("404 → `not_found`", api_doc)
        self.assertIn("405 → `method_not_allowed`", api_doc)
        self.assertIn("compatibility subset", api_l)
        self.assertIn("seven engine-aware aliases", api_l)
        self.assertIn("| `coral` |", api_doc)
        self.assertIn("4096", api_doc)
        self.assertIn("MAX_TEXT_LENGTH", api_doc)
        self.assertIn("Kokoro language mismatch", api_doc)
        self.assertIn("returns `422` before synthesis", api_doc)
        self.assertIn("silent partial success", api_l)
        self.assertIn("pure Cyrillic, kana, Hangul, Greek, or Arabic", api_doc)
        self.assertIn("Pure Japanese text made only of Han characters", api_doc)
        self.assertIn("Chinese book-title brackets", api_doc)
        self.assertNotIn("CJK corner brackets, guillemets", api_doc)
        self.assertIn('"param"', api_doc)
        self.assertIn("custom voice", api_l)
        self.assertIn("POST** `/v1/audio/voices`", api_doc)

        for response_format in ("mp3", "opus", "aac", "flac", "wav", "pcm"):
            self.assertIn(f"`{response_format}`", api_doc)
        for media_type in (
            "audio/mpeg",
            "audio/ogg",
            "audio/aac",
            "audio/flac",
            "audio/wav",
            "application/octet-stream",
        ):
            self.assertIn(media_type, api_doc)
        for field in (
            "stream_format",
            "instructions",
            "lang_code",
            "lang",
            "language",
            "format",
        ):
            self.assertIn(f"`{field}`", api_doc)

        self.assertIn("/v1/audio/speech", api_html)
        self.assertIn("tocOpenAI", api_html)
        self.assertIn("real IDs for the selected model", api_html)
        self.assertIn("所选 model 对应引擎的真实 ID", api_html)
        self.assertIn(
            "`/api/auth` and `/api/logs` never use this exemption",
            api_doc,
        )
        self.assertIn(
            "<code>/api/auth</code> and <code>/api/logs</code> never use",
            api_html,
        )
        self.assertIn(
            "<code>/api/auth</code>、<code>/api/logs</code> 与",
            api_html,
        )

        for text, label in ((en, "README.md"), (zh, "README_CN.md")):
            self.assertIn("/v1/audio/speech", text, label)
            self.assertIn("3.0", text, label)
            self.assertIn("response_format", text, label)
            self.assertIn("Markdown", text, label)
            for response_format in ("opus", "aac", "flac", "wav", "pcm"):
                self.assertIn(response_format, text.lower(), label)
            self.assertIn("coral", text, label)
            self.assertIn("OPENAI_AGENT_TTS_COMPATIBILITY_AUDIT.md", text, label)
        self.assertIn("compatibility subset", en)
        self.assertIn("never override `model`", en)
        self.assertIn("Kokoro script mismatches", en)
        self.assertIn("兼容子集", zh)
        self.assertIn("不会覆盖 `model`", zh)
        self.assertIn("Kokoro 脚本不匹配", zh)
        self.assertIn("engine-aware aliases", changelog)
        self.assertIn("OpenAI binary output formats", changelog)
        self.assertIn("nullable `param`", changelog)
        self.assertIn("silent partial success", changelog.lower())

    def test_openai_agent_compatibility_audit_records_current_differences(self):
        audit = (ROOT / "OPENAI_AGENT_TTS_COMPATIBILITY_AUDIT.md").read_text(
            encoding="utf-8"
        )

        for heading in (
            "## 3. 当前本地 `/v1` 契约",
            "## 4. Hermes 兼容性",
            "## 5. OpenClaw 兼容性",
            "## 6. 与官方 OpenAI 的剩余差异",
            "## 7. 本轮确认并修复的缺陷",
            "## 8. R1-R42 当前状态与残余边界",
            "## 9. 拒绝的误报",
        ):
            self.assertIn(heading, audit)

        for evidence in (
            "db14b6e1712aaf5265cf5a6871adff7a9c61d31c",
            "ef05a7d18e8361205342aa6c5bb9d77404c2c3ce",
            "4f404262955cb711c56c07cce52076b6107303e5",
            "b4f8c491d3452926deb7628edbdb6fe2a85ff576",
            "319fd692d1c83bc05b3a38e4673f2e2fa5398db0",
            "f3cda0ceb18d8ba7465a6d223098ef0e56c8fee1",
            "ba756a23ad4335ebbb252be297ff7962a077f573",
            "POST** `/audio/voices`",
            "additionalProperties:false",
            "TTS_SYNTHESIS_TIMEOUT_SECONDS=0",
            "_PostStreamSynthesisError",
            'tts.auto: "always"',
            "extraBody.response_format",
            "Kokoro-only `200`",
            "纯 Cyrillic",
            "fenced code",
            "proc.wait()",
            "排队等待 semaphore",
            "没有独立清理期限",
            "覆盖原本的流超时/取消异常",
            "官方资料源间漂移",
            "`language` / `format`",
            "managed token",
            "跨 origin",
            "readResponseWithLimit()",
            "Edge decoder ended before upstream feed completed",
            "Edge decoder produced no PCM output",
            "延迟回填",
            "X-Client-Request-Id",
            "ASGI 2.4",
            "wait() 自身异常",
            "请求体大小",
            "kill() 正常返回",
            "EDGE_DECODER_EXIT_GRACE_SECONDS",
            "MAX_TEXT_LENGTH=0",
            "外部取消必须传播",
        ):
            self.assertIn(evidence, audit)

        self.assertNotIn("当前服务会在合成前稳定返回 `400`", audit)
        self.assertNotIn("应用请求体门禁尚不存在", audit)
        self.assertNotIn("可发音门禁只认可汉字与 ASCII", audit)
        self.assertNotIn("UI 正则仍只覆盖基本区", audit)
        self.assertNotIn("`coral` 当前没有别名映射", audit)
        self.assertRegex(
            audit,
            r'Kokoro ID 必须同时显式\s+`model:"kokoro"`',
        )


if __name__ == "__main__":
    unittest.main()
