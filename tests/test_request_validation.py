# -*- coding: utf-8 -*-
"""请求模型、WebSocket 请求解析、音色目录不变量测试。"""
import unittest

from _support import import_app_with_fakes


class TTSRequestValidationTests(unittest.TestCase):
    def setUp(self):
        self.app = import_app_with_fakes()

    def test_valid_kokoro_request_accepts_boundary_speeds(self):
        low = self.app.TTSRequest(text="hello", engine="kokoro", voice="af_heart", speed=0.5)
        high = self.app.TTSRequest(text="hello", engine="kokoro", voice="af_heart", speed=3.0)

        self.assertEqual(low.speed, 0.5)
        self.assertEqual(high.speed, 3.0)

    def test_rejects_text_over_limit(self):
        self.app.MAX_TEXT_LENGTH = 3

        with self.assertRaises(ValueError):
            self.app.TTSRequest(text="abcd", engine="kokoro", voice="af_heart")

    def test_rejects_unknown_engine(self):
        with self.assertRaises(ValueError):
            self.app.TTSRequest(text="hello", engine="espeak", voice="af_heart")

    def test_rejects_speed_outside_api_range(self):
        for speed in (0.49, 3.01):
            with self.subTest(speed=speed):
                with self.assertRaises(ValueError):
                    self.app.TTSRequest(text="hello", engine="kokoro", voice="af_heart", speed=speed)

    def test_rejects_unknown_kokoro_voice(self):
        with self.assertRaises(ValueError):
            self.app.TTSRequest(text="hello", engine="kokoro", voice="missing_voice")

    def test_edge_request_does_not_validate_voice_against_kokoro_catalog(self):
        req = self.app.TTSRequest(text="hello", engine="edge", voice="arbitrary-edge-voice")

        self.assertEqual(req.voice, "arbitrary-edge-voice")


class ParseWsRequestTests(unittest.TestCase):
    def setUp(self):
        self.app = import_app_with_fakes()

    def test_accepts_valid_request_and_strips_text(self):
        parsed = self.app.parse_ws_request(
            {"text": " hello world ", "engine": "kokoro", "voice": "af_heart", "speed": 1.5}
        )

        self.assertEqual(
            parsed,
            {"type": "ok", "text": "hello world", "engine": "kokoro", "voice": "af_heart", "speed": 1.5},
        )

    def test_missing_or_blank_text_is_error(self):
        for payload in ({"engine": "kokoro"}, {"text": "   "}, {"text": None}):
            with self.subTest(payload=payload):
                parsed = self.app.parse_ws_request(payload)
                self.assertEqual(parsed["type"], "error")
                self.assertIn("text", parsed["message"])

    def test_rejects_raw_text_over_limit_before_cleaning(self):
        self.app.MAX_TEXT_LENGTH = 5

        parsed = self.app.parse_ws_request({"text": "abcdef"})

        self.assertEqual(parsed["type"], "error")
        self.assertIn("长度", parsed["message"])

    def test_rejects_text_empty_after_cleaning(self):
        parsed = self.app.parse_ws_request({"text": "```python\nprint(1)\n```", "engine": "kokoro"})

        self.assertEqual(parsed["type"], "error")
        self.assertIn("清洗", parsed["message"])

    def test_speed_is_clamped_for_ws(self):
        high = self.app.parse_ws_request({"text": "hi", "speed": 9})
        low = self.app.parse_ws_request({"text": "hi", "speed": 0.1})

        self.assertEqual(high["speed"], 3.0)
        self.assertEqual(low["speed"], 0.5)

    def test_non_numeric_ws_speed_falls_back_to_one(self):
        parsed = self.app.parse_ws_request({"text": "hi", "speed": "fast"})

        self.assertEqual(parsed["type"], "ok")
        self.assertEqual(parsed["speed"], 1.0)

    def test_rejects_unknown_ws_engine(self):
        parsed = self.app.parse_ws_request({"text": "hi", "engine": "espeak"})

        self.assertEqual(parsed["type"], "error")
        self.assertIn("未知", parsed["message"])

    def test_rejects_unknown_kokoro_voice_but_allows_any_edge_voice(self):
        bad = self.app.parse_ws_request({"text": "hi", "engine": "kokoro", "voice": "nope"})
        edge = self.app.parse_ws_request({"text": "hi", "engine": "edge", "voice": "whatever"})

        self.assertEqual(bad["type"], "error")
        self.assertEqual(edge["type"], "ok")
        self.assertEqual(edge["voice"], "whatever")

    def test_defaults_to_kokoro_xiaoxiao_one_x(self):
        parsed = self.app.parse_ws_request({"text": "hi"})

        self.assertEqual(parsed["engine"], "kokoro")
        self.assertEqual(parsed["voice"], "zf_xiaoxiao")
        self.assertEqual(parsed["speed"], 1.0)


class VoiceCatalogTests(unittest.TestCase):
    def setUp(self):
        self.app = import_app_with_fakes()

    def test_kokoro_catalog_has_official_voice_count_and_unique_ids(self):
        ids = [voice["id"] for voice in self.app.KOKORO_VOICES]

        self.assertEqual(len(ids), 28)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(self.app.KOKORO_VOICE_IDS, frozenset(ids))

    def test_kokoro_language_and_gender_match_id_prefixes(self):
        for voice in self.app.KOKORO_VOICES:
            with self.subTest(voice=voice["id"]):
                prefix = voice["id"].split("_", 1)[0]
                expected_language = "zh" if prefix.startswith("z") else "en"
                expected_gender = "female" if prefix.endswith("f") else "male"
                self.assertEqual(voice["language"], expected_language)
                self.assertEqual(voice["gender"], expected_gender)


if __name__ == "__main__":
    unittest.main()
