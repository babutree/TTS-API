# -*- coding: utf-8 -*-
"""旧 /api 前置响应的 request-id 与鉴权顺序契约。"""
import unittest

from starlette.testclient import TestClient

from _support import import_app_with_fakes


class LegacyApiRequestIdTests(unittest.TestCase):
    def setUp(self):
        self.app = import_app_with_fakes()
        self.app.logger.disabled = True
        self.client = TestClient(self.app.app)

    def tearDown(self):
        self.app.logger.disabled = False

    def test_legacy_pre_endpoint_responses_preserve_request_id(self):
        """R30: middleware 401、body 422 与 route 405 均可关联同一请求。"""
        self.app.TTS_API_KEY = "secret"
        cases = [
            (
                "auth",
                self.client.post,
                "/api/tts",
                {"json": {"text": "hello"}, "headers": {"X-Request-ID": "rid-auth"}},
                401,
                "rid-auth",
            ),
            (
                "json",
                self.client.post,
                "/api/tts",
                {
                    "content": b"{",
                    "headers": {
                        "Content-Type": "application/json",
                        "X-API-Key": "secret",
                        "X-Request-ID": "rid-json",
                    },
                },
                422,
                "rid-json",
            ),
            (
                "method",
                self.client.get,
                "/api/tts",
                {
                    "headers": {
                        "X-API-Key": "secret",
                        "X-Request-ID": "rid-method",
                    },
                },
                405,
                "rid-method",
            ),
        ]
        for name, method, path, kwargs, status, request_id in cases:
            with self.subTest(name=name):
                response = method(path, **kwargs)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.headers.get("x-request-id"), request_id)

    def test_logs_auth_precedes_query_validation_and_keeps_request_id(self):
        """R30: /api/logs 不能因公开 query 校验先于未授权响应暴露不同结果。"""
        self.app.TTS_API_KEY = "secret"

        unauthorized = self.client.get(
            "/api/logs?limit=bad",
            headers={
                "Origin": "http://testserver",
                "Host": "testserver",
                "X-Request-ID": "rid-logs-auth",
            },
        )
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(
            unauthorized.headers.get("x-request-id"), "rid-logs-auth"
        )

        invalid_query = self.client.get(
            "/api/logs?limit=bad",
            headers={
                "X-API-Key": "secret",
                "X-Request-ID": "rid-logs-query",
            },
        )
        self.assertEqual(invalid_query.status_code, 422)
        self.assertEqual(
            invalid_query.headers.get("x-request-id"), "rid-logs-query"
        )


if __name__ == "__main__":
    unittest.main()
