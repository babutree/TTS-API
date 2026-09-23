# -*- coding: utf-8 -*-
"""属性式不变量(试点)：纯函数在随机输入下的普遍性质。

与示例型测试互补：示例锁"给定输入的给定输出"，属性锁"任意输入的普遍关系"。
随机源用固定种子(random.Random)——可复现、离线、无新增依赖；性质若在未来
代码变更下破裂，失败样本会以相同种子重现。

已知非目标：clean_text 在退化星号串(如 "**!|")上不幂等——产品对每篇文本
仅清洗一次，幂等性不是契约；而换行数守恒是前后端 seg 对齐的硬契约。
"""
import random
import unittest

from _support import import_app_with_fakes

SEED = 20260920
# 混合 markdown 标记/中英文/emoji/换行/空白，覆盖清洗规则的每个分支族
MD_ALPHABET = (
    "aAzZ9 !?.,;:" "[]()<>#*_~`>-" "\\|/'" "\n\t" "😀清hello世界"
    "**__~~```>1."
)


class PropertyInvariantTests(unittest.TestCase):
    def setUp(self):
        self.app = import_app_with_fakes()
        # 注意：不设 logger.disabled——logging.getLogger 是进程级注册表，
        # disabled 状态会跨模块重导入泄漏(乱序实验 seed=22 实证)。
        # 纯函数路径本身不产生日志，无需静音。

    def test_clean_text_preserves_newline_count(self):
        # 硬契约：前端 splitSentences 与后端 seg 逐行 1:1 对齐依赖行数守恒。
        rng = random.Random(SEED)
        for _ in range(500):
            text = "".join(rng.choice(MD_ALPHABET) for _ in range(rng.randint(0, 120)))
            with self.subTest(text=text):
                self.assertEqual(
                    self.app.clean_text(text).count("\n"),
                    text.count("\n"),
                )

    def test_split_kokoro_unit_round_trip_is_lossless(self):
        # 任意文本与任意下限：碎片拼接必须还原原文(逐字符保留，绝不删除边界字符)。
        rng = random.Random(SEED + 1)
        for _ in range(200):
            text = "".join(
                rng.choice(MD_ALPHABET + "，；：、 ") for _ in range(rng.randint(1, 80))
            )
            limit = rng.choice([1, 2, 3, 7, 10, 50])
            with self.subTest(text=text, limit=limit):
                fragments = self.app.split_kokoro_unit(text, limit)
                self.assertEqual("".join(fragments), text)
                self.assertTrue(
                    all(len(f) <= limit for f in fragments[:-1]),
                    "仅允许末段短于上限",
                )

    def test_to_pcm_output_is_bounded_int16(self):
        import numpy as np

        rng = random.Random(SEED + 2)
        for _ in range(100):
            audio = [rng.uniform(-3.0, 3.0) for _ in range(rng.randint(1, 64))]
            pcm = self.app.to_pcm(np.array(audio))
            self.assertEqual(len(pcm) % 2, 0)
            self.assertGreater(len(pcm), 0)
            samples = np.frombuffer(pcm, dtype=np.int16)
            self.assertTrue((samples <= 32767).all() and (samples >= -32767).all())

    def test_edge_rate_for_speed_monotonic_and_well_formed(self):
        speeds = [0.5 + i * 0.05 for i in range(51)]
        rates = [self.app._edge_rate_for_speed(round(s, 2)) for s in speeds]
        for rate in rates:
            self.assertRegex(rate, r"^[+-]\d+%$")
        self.assertEqual(rates[speeds.index(1.0)], "+0%")
        as_numbers = [int(r[:-1]) for r in rates]
        self.assertEqual(as_numbers, sorted(as_numbers), "rate 必须随 speed 单调不减")

    def test_truncate_log_line_respects_limit(self):
        rng = random.Random(SEED + 3)
        marker = "...[truncated]"
        for _ in range(200):
            line = "".join(rng.choice(MD_ALPHABET) for _ in range(rng.randint(0, 60)))
            limit = rng.randint(1, 70)
            out = self.app._truncate_log_line(line, limit)
            self.assertLessEqual(len(out), limit)
            if len(line) > limit > len(marker):
                self.assertTrue(out.endswith(marker) or len(out) == limit)

    def test_sanitize_log_line_leaves_no_control_chars(self):
        import unicodedata

        rng = random.Random(SEED + 4)
        for _ in range(200):
            text = "".join(
                rng.choice(MD_ALPHABET + "\x00\x07\x1b\u2028\u2029\r")
                for _ in range(rng.randint(0, 60))
            )
            out = self.app._sanitize_log_line(text)
            for ch in out:
                self.assertFalse(
                    unicodedata.category(ch).startswith("C") or ch in "\u2028\u2029",
                    f"控制字符未被转义: {ch!r}",
                )

    def test_request_id_from_header_charset_and_length(self):
        rng = random.Random(SEED + 5)
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-")
        for _ in range(200):
            raw = "".join(rng.choice(MD_ALPHABET + "密🔑") for _ in range(rng.randint(0, 80)))
            out = self.app._request_id_from_header(raw)
            self.assertLessEqual(len(out), 64)
            if raw.strip():
                self.assertGreater(len(out), 0)
                self.assertTrue(set(out) <= allowed, f"非法字符残留: {out!r}")
        empty = self.app._request_id_from_header("")
        self.assertEqual(len(empty), 32)
        int(empty, 16)

    def test_ring_buffer_never_exceeds_either_bound(self):
        rng = random.Random(SEED + 6)
        handler = self.app.RingBufferHandler(max_lines=8, max_chars=200, max_record_chars=50)
        for i in range(300):
            line = "x" * rng.randint(0, 60) + str(i)
            record = __import__("logging").LogRecord(
                "t", logging_level := 20, __file__, 1, line, None, None
            )
            handler.emit(record)
            self.assertLessEqual(len(handler.buffer), 8)
            self.assertLessEqual(sum(map(len, handler.buffer)), 200)

    def test_filter_for_voice_removes_cross_language(self):
        rng = random.Random(SEED + 7)
        import re

        for _ in range(100):
            text = "".join(rng.choice(MD_ALPHABET) for _ in range(rng.randint(0, 60)))
            zh = self.app.filter_for_voice(text, True)
            self.assertFalse(re.search(r"[A-Za-z]", zh), f"中文音色残留英文: {zh!r}")
            en = self.app.filter_for_voice(text, False)
            self.assertFalse(
                any(self.app._is_han_char(c) for c in en),
                f"英文音色残留中文: {en!r}",
            )

    def test_clamp_speed_always_within_documented_bounds(self):
        rng = random.Random(SEED + 8)
        for _ in range(100):
            value = rng.uniform(-10.0, 10.0)
            clamped = self.app._clamp_speed(value)
            self.assertGreaterEqual(clamped, self.app.SPEED_MIN)
            self.assertLessEqual(clamped, self.app.SPEED_MAX)


if __name__ == "__main__":
    unittest.main()
