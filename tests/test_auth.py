# -*- coding: utf-8 -*-
"""鉴权与中间件测试：密钥来源解析、同源判定、豁免路径、探针端点。

安全关键路径。断言覆盖：
- 时序安全比对的真假分支；
- Bearer 与 X-API-Key 的优先级与边界；
- 同源判定对 Origin/Referer/Host 的取舍；
- 中间件在"未配置密钥/豁免路径/同源/带密钥/无密钥"下的放行与拒绝；
- /api/auth 探针不吃同源豁免，必须真正校验密钥。
"""
import os
import unittest

from starlette.testclient import TestClient

from _support import import_app_with_fakes


def _load_with_key(key: str):
    old = os.environ.get("TTS_API_KEY")
    os.environ["TTS_API_KEY"] = key
    try:
        app = import_app_with_fakes()
    finally:
        if old is None:
            os.environ.pop("TTS_API_KEY", None)
        else:
            os.environ["TTS_API_KEY"] = old
    # pipeline 就绪，避免健康检查/合成前置 503 干扰鉴权断言。
    app.pipeline_zh = object()
    app.pipeline_en = object()
    return app


class HostOfTests(unittest.TestCase):
    def setUp(self):
        self.app = import_app_with_fakes()

    def test_origin_form_returns_host_with_port(self):
        self.assertEqual(self.app._host_of("https://a.com:8443"), "a.com:8443")

    def test_referer_full_url_returns_netloc_only(self):
        self.assertEqual(self.app._host_of("https://a.com:8443/path?q=1"), "a.com:8443")

    def test_empty_returns_empty(self):
        self.assertEqual(self.app._host_of(""), "")


class SameOriginTests(unittest.TestCase):
    def setUp(self):
        self.app = import_app_with_fakes()

    def test_origin_matching_host_is_same_origin(self):
        headers = {"host": "a.com:8443", "origin": "https://a.com:8443"}
        self.assertTrue(self.app._is_same_origin(headers))

    def test_origin_mismatch_is_not_same_origin(self):
        headers = {"host": "a.com", "origin": "https://evil.com"}
        self.assertFalse(self.app._is_same_origin(headers))

    def test_origin_takes_precedence_over_referer(self):
        # Origin 存在即以 Origin 判定；此处 Origin 不匹配则判否，即便 Referer 匹配。
        headers = {
            "host": "a.com",
            "origin": "https://evil.com",
            "referer": "https://a.com/x",
        }
        self.assertFalse(self.app._is_same_origin(headers))

    def test_referer_used_when_origin_absent(self):
        headers = {"host": "a.com", "referer": "https://a.com/page"}
        self.assertTrue(self.app._is_same_origin(headers))

    def test_missing_host_is_not_same_origin(self):
        self.assertFalse(self.app._is_same_origin({"origin": "https://a.com"}))

    def test_no_origin_no_referer_is_not_same_origin(self):
        self.assertFalse(self.app._is_same_origin({"host": "a.com"}))


class KeyMatchAndExtractTests(unittest.TestCase):
    def setUp(self):
        self.app = _load_with_key("secret-key")

    def test_correct_key_matches(self):
        self.assertTrue(self.app._key_matches("secret-key"))

    def test_wrong_key_does_not_match(self):
        self.assertFalse(self.app._key_matches("wrong"))

    def test_empty_provided_never_matches(self):
        self.assertFalse(self.app._key_matches(""))

    def test_extract_bearer(self):
        self.assertEqual(
            self.app._extract_rest_key({"authorization": "Bearer abc123"}), "abc123"
        )

    def test_extract_x_api_key(self):
        self.assertEqual(self.app._extract_rest_key({"x-api-key": "key99"}), "key99")

    def test_bearer_preferred_over_x_api_key(self):
        headers = {"authorization": "Bearer tok", "x-api-key": "other"}
        self.assertEqual(self.app._extract_rest_key(headers), "tok")

    def test_basic_auth_is_not_treated_as_bearer(self):
        # Caddy basic_auth 会带 Authorization: Basic ...，不得被当作 Bearer 密钥。
        self.assertEqual(self.app._extract_rest_key({"authorization": "Basic Zm9v"}), "")

    def test_no_headers_returns_empty(self):
        self.assertEqual(self.app._extract_rest_key({}), "")


class MiddlewareOpenModeTests(unittest.TestCase):
    def test_no_key_configured_allows_protected_route(self):
        app = _load_with_key("")
        client = TestClient(app.app)
        # 未配置密钥 = 完全开放：受保护路由无密钥也放行。
        self.assertEqual(client.get("/api/voices").status_code, 200)

    def test_no_key_configured_auth_probe_reports_disabled(self):
        app = _load_with_key("")
        client = TestClient(app.app)
        resp = client.get("/api/auth")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"auth": "disabled", "authorized": True})


class MiddlewareEnforcedModeTests(unittest.TestCase):
    def setUp(self):
        self.app = _load_with_key("secret")
        self.client = TestClient(self.app.app)

    def test_exempt_root_allowed_without_key(self):
        self.app.shutil.which = lambda name: "ffmpeg" if name == "ffmpeg" else None
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_protected_route_rejected_without_key(self):
        self.assertEqual(self.client.get("/api/voices").status_code, 401)

    def test_protected_route_allowed_with_x_api_key(self):
        resp = self.client.get("/api/voices", headers={"X-API-Key": "secret"})
        self.assertEqual(resp.status_code, 200)

    def test_protected_route_allowed_with_bearer(self):
        resp = self.client.get("/api/voices", headers={"Authorization": "Bearer secret"})
        self.assertEqual(resp.status_code, 200)

    def test_protected_route_rejected_with_wrong_key(self):
        resp = self.client.get("/api/voices", headers={"X-API-Key": "nope"})
        self.assertEqual(resp.status_code, 401)

    def test_same_origin_page_exempt_without_key(self):
        # 同源自有页面(Origin == Host)免密。
        resp = self.client.get("/api/voices", headers={"Origin": "http://testserver"})
        self.assertEqual(resp.status_code, 200)

    def test_cross_origin_without_key_rejected(self):
        resp = self.client.get("/api/voices", headers={"Origin": "http://evil.com"})
        self.assertEqual(resp.status_code, 401)


class AuthProbeEnforcedTests(unittest.TestCase):
    def setUp(self):
        self.app = _load_with_key("secret")
        self.client = TestClient(self.app.app)

    def test_probe_not_exempt_by_same_origin(self):
        # 关键：探针不吃同源豁免，同源但无密钥仍须 401。
        resp = self.client.get("/api/auth", headers={"Origin": "http://testserver"})
        self.assertEqual(resp.status_code, 401)
        body = resp.json()
        self.assertEqual(body["auth"], "enabled")
        self.assertFalse(body["authorized"])

    def test_probe_authorized_with_correct_key(self):
        resp = self.client.get("/api/auth", headers={"X-API-Key": "secret"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"auth": "enabled", "authorized": True})

    def test_probe_rejected_without_key(self):
        resp = self.client.get("/api/auth")
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
