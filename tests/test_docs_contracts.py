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


if __name__ == "__main__":
    unittest.main()
