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


if __name__ == "__main__":
    unittest.main()
