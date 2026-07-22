# -*- coding: utf-8 -*-
"""纯文本处理函数测试：clean_text / split_text / filter_for_voice / to_pcm。

这些函数是"读什么"的唯一事实源，直接决定听感与前后端句级对齐。
断言写规格上应当成立的期望值(而非照抄当前输出)：若实现与期望不符，
即为真实缺陷，应被暴露。中文字面量以真实 UTF-8 源码写入。
"""
import unittest

import numpy as np

from _support import import_app_with_fakes

app = import_app_with_fakes()


class CleanTextTests(unittest.TestCase):
    def test_fenced_code_block_removed_entirely(self):
        self.assertEqual(app.clean_text("before```py\nx=1\n```after"), "beforeafter")

    def test_inline_code_keeps_inner_text(self):
        self.assertEqual(app.clean_text("use `pip install` now"), "use pip install now")

    def test_image_removed_link_keeps_label(self):
        self.assertEqual(app.clean_text("pre ![alt](a.png) post"), "pre  post")
        self.assertEqual(app.clean_text("see [docs](http://x) here"), "see docs here")

    def test_heading_marker_stripped(self):
        self.assertEqual(app.clean_text("### Title\nbody"), "Title\nbody")

    def test_blockquote_and_list_markers_stripped(self):
        self.assertEqual(app.clean_text("> quoted line"), "quoted line")
        self.assertEqual(app.clean_text("- item one"), "item one")
        self.assertEqual(app.clean_text("1. first item"), "first item")

    def test_bold_and_italic_unwrapped(self):
        self.assertEqual(app.clean_text("this is **bold** text"), "this is bold text")
        self.assertEqual(app.clean_text("this is *italic* text"), "this is italic text")

    def test_snake_case_identifier_not_treated_as_italic(self):
        # 关键：下划线斜体不处理，否则会破坏 zf_xiaoxiao 等音色 ID / snake_case 标识符。
        self.assertEqual(app.clean_text("voice zf_xiaoxiao stays"), "voice zf_xiaoxiao stays")
        self.assertEqual(app.clean_text("keep _under_ intact"), "keep _under_ intact")

    def test_stray_unclosed_stars_removed(self):
        self.assertEqual(app.clean_text("broken **unclosed here"), "broken unclosed here")

    def test_strikethrough_unwrapped(self):
        self.assertEqual(app.clean_text("this ~~gone~~ ok"), "this gone ok")

    def test_horizontal_rule_removed(self):
        self.assertEqual(app.clean_text("a\n---\nb"), "a\n\nb")

    def test_double_quotes_removed_but_apostrophe_preserved(self):
        # 双引号(直/弯)、中文方括号引号、书名号一并去除；ASCII 单引号保留以护住英文缩写。
        self.assertEqual(
            app.clean_text('say "hello" and \u300cx\u300d and \u300ay\u300b'),
            "say hello and x and y",
        )
        self.assertEqual(app.clean_text("don't stop it's fine"), "don't stop it's fine")

    def test_chinese_curly_quotes_removed(self):
        # 他说“你好”。 -> 他说你好。(弯引号去除，中文正文与句号保留)
        self.assertEqual(
            app.clean_text("\u4ed6\u8bf4\u201c\u4f60\u597d\u201d\u3002"),
            "\u4ed6\u8bf4\u4f60\u597d\u3002",
        )


class SplitTextTests(unittest.TestCase):
    def test_english_sentence_split_requires_trailing_space(self):
        self.assertEqual(
            app.split_text("Hello world. Second one. Third"),
            ["Hello world.", "Second one.", "Third"],
        )

    def test_decimal_number_not_split(self):
        # 3.14 的点后无空格，不得被拆碎。
        self.assertEqual(
            app.split_text("Pi is 3.14 exactly. Next"),
            ["Pi is 3.14 exactly.", "Next"],
        )

    def test_english_period_without_space_not_split(self):
        self.assertEqual(app.split_text("A.B"), ["A.B"])

    def test_chinese_punctuation_zero_width_split(self):
        # 你好。世界！嘛？好 -> 中文句末标点后即切(其后通常无空格)。
        self.assertEqual(
            app.split_text("\u4f60\u597d\u3002\u4e16\u754c\uff01\u55ce\uff1f\u597d"),
            ["\u4f60\u597d\u3002", "\u4e16\u754c\uff01", "\u55ce\uff1f", "\u597d"],
        )

    def test_newlines_split_and_blank_lines_dropped(self):
        self.assertEqual(
            app.split_text("line1\nline2\n\nline3"),
            ["line1", "line2", "line3"],
        )

    def test_ellipsis_kept_with_leading_sentence(self):
        self.assertEqual(app.split_text("wait... really. ok"), ["wait...", "really.", "ok"])

    def test_whitespace_only_yields_empty_list(self):
        self.assertEqual(app.split_text("   \n  "), [])


class FilterForVoiceTests(unittest.TestCase):
    def test_chinese_voice_strips_latin_run(self):
        # 你好abc世界 -> 你好 世界(拉丁串整体替换为空格)。
        self.assertEqual(
            app.filter_for_voice("\u4f60\u597dabc\u4e16\u754c", True),
            "\u4f60\u597d \u4e16\u754c",
        )

    def test_chinese_voice_strips_snake_identifier_whole(self):
        # 读zf_xiaoxiao音 -> 读 音(next.js/snake_case 整体去除)。
        self.assertEqual(
            app.filter_for_voice("\u8bfbzf_xiaoxiao\u97f3", True),
            "\u8bfb \u97f3",
        )

    def test_english_voice_strips_chinese_run(self):
        # helloni好world -> hello world(CJK 串替换为空格)。
        self.assertEqual(
            app.filter_for_voice("hello\u4f60\u597dworld", False),
            "hello world",
        )

    def test_english_voice_strips_chinese_punctuation(self):
        # hi，there。end -> hi there end(中文标点也被剥离)。
        self.assertEqual(
            app.filter_for_voice("hi\uff0cthere\u3002end", False),
            "hi there end",
        )

    def test_english_voice_leaves_ascii_untouched(self):
        self.assertEqual(app.filter_for_voice("plain english text", False), "plain english text")

    def test_chinese_voice_leaves_pure_chinese_untouched(self):
        self.assertEqual(
            app.filter_for_voice("\u4f60\u597d\u4e16\u754c", True),
            "\u4f60\u597d\u4e16\u754c",
        )


class ToPcmTests(unittest.TestCase):
    def test_encodes_signed_16bit_little_endian(self):
        # 0.0 -> 0；1.0 -> 32767；负值饱和；小端字节序。
        audio = np.array([0.0, 1.0, -1.0], dtype=np.float32)
        pcm = app.to_pcm(audio)
        self.assertEqual(pcm, b"\x00\x00\xff\x7f\x01\x80")

    def test_clips_out_of_range_values(self):
        # 超出 [-1, 1] 的值先饱和裁剪，避免溢出回绕成杂音。
        audio = np.array([2.0, -2.0], dtype=np.float32)
        pcm = app.to_pcm(audio)
        self.assertEqual(pcm, b"\xff\x7f\x01\x80")

    def test_length_is_two_bytes_per_sample(self):
        audio = np.zeros(5, dtype=np.float32)
        self.assertEqual(len(app.to_pcm(audio)), 10)


if __name__ == "__main__":
    unittest.main()
