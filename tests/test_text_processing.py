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
    def test_fenced_code_block_removed_but_line_count_preserved(self):
        # 围栏内容整体删除，但保留其占用的换行数。两条理由：
        # 1) 与同类删除保持一致 —— 图片删除留下间隙("pre  post")而非焊接两侧文字，
        #    塌成 "beforeafter" 会造出一个不存在的词，TTS 会当成一个词读出来。
        # 2) WS 的 Kokoro 路径靠 \n 还原前端合成单元；前端不做删除、看到的是 3 行，
        #    后端若塌成 1 行，seg 计数即错位，变速续播会跳到错误句子。
        self.assertEqual(
            app.clean_text("before```py\nx=1\n```after"), "before\n\nafter"
        )

    def test_fenced_code_block_content_is_never_spoken(self):
        # 围栏内的代码必须消失，不能因为保行而漏读出来。
        cleaned = app.clean_text("A.\n```python\nprint(1)\n```\nB.")
        self.assertNotIn("print", cleaned)
        self.assertNotIn("python", cleaned)
        self.assertEqual(cleaned.count("\n"), 4)

    def test_block_removals_preserve_line_count(self):
        # 前后端单元对齐依赖"清洗不改变行数"这一性质，逐结构锁定。
        for src in (
            "A.\n---\nB.",
            "A.\n![img](x.png)\nB.",
            "A.\n# Title\nB.",
            "A.\n> quote\nB.",
            "A.\n- item\nB.",
            "A.\n1. item\nB.",
            "A.\n```\ncode();\n```\nB.",
        ):
            with self.subTest(src=src):
                self.assertEqual(
                    app.clean_text(src).count("\n"), src.count("\n")
                )

    def test_cross_line_and_blank_line_patterns_preserve_line_count(self):
        # 属性测试(随机差分)发现的三处换行吞噬回归的最小复现：
        # 1) 行内代码/链接/图片的字符类含 \n 时跨行配对吞换行；
        # 2) 行首 \s{0,3} 可跨空行把引用/标题吸到上一行；
        # 3) 列表规则的 \s+ 尾巴吞掉空列表项("*"+换行)的换行。
        for src in (
            "A\n`x\ny`z\nB",            # 行内代码跨行
            "A\n[t\ne](u\nv)w\nB",      # 链接文字与地址跨行
            "A\n![i\nm](a\nb)\nB",      # 图片跨行
            "A\n\n> quote\nB",          # 引用规则行首 \s{0,3} 吞空行
            "A\n\n# H\nB",              # 标题规则行首 \s{0,3} 吞空行
            "A\n*\nB",                  # 空列表项：* + 换行
            "A\n1.\nB",                 # 空有序列表项
            "A\n*\n\n> q\nB",           # 组合：列表+空行+引用
        ):
            with self.subTest(src=src):
                self.assertEqual(
                    app.clean_text(src).count("\n"), src.count("\n")
                )

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

    def test_multiplication_stars_are_preserved(self):
        self.assertEqual(app.clean_text("2*3*4"), "2*3*4")
        self.assertEqual(app.clean_text("2**8"), "2**8")

    def test_word_internal_stars_and_unicode_multiplication_are_preserved(self):
        # 词内星号是运算符/标识符的一部分，不应被 Markdown 清洗静默吞掉。
        for source in ("2**8**2", "a**b**c", "α*β*γ", "甲*乙*丙"):
            with self.subTest(source=source):
                self.assertEqual(app.clean_text(source), source)

    def test_double_stars_only_preserve_complete_ascii_power_chains(self):
        # ** 在纯文本中既可能是 Markdown，也可能是 Python 风格乘方。
        # 仅完整 ASCII 乘方链按运算符保留，CJK 相邻和混合链按 Markdown
        # 处理，不能只保住一侧分隔符而静默损坏剩余文本。
        cases = {
            "中文**加粗**中文": "中文加粗中文",
            "abc**重要**def": "abc重要def",
            "a**b**中文": "ab中文",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(app.clean_text(source), expected)

    def test_star_ambiguity_tradeoff_faces_are_locked(self):
        # S2 策略的取舍面必须显式锁定，防止未来"悄悄改策略"：
        # 1) ASCII 紧邻双星号串会被当作乘方链保留——即使它语义上很像粗体，
        #    因为无法用语法区分 text**bold**text 与 a**b**c。
        self.assertEqual(app.clean_text("text**bold**text"), "text**bold**text")
        # 2) Unicode 字母紧邻的单星号链保留，覆盖中文斜体这一旧版回退面。
        self.assertEqual(app.clean_text("中文*斜体*中文"), "中文*斜体*中文")

    def test_star_protection_never_rewrites_existing_private_use_text(self):
        # 实现若使用私用区占位符，用户的原始字符和完整的首选候选串均须
        # 原样保留；这迫使实现动态避让而非写死 U+E000。
        for source in (
            "\ue000 2**8**2",
            "\ue000clean-text-double-star-0\ue001 2**8**2",
        ):
            with self.subTest(source=source):
                self.assertEqual(app.clean_text(source), source)

    def test_bounded_markdown_emphasis_is_still_unwrapped(self):
        self.assertEqual(app.clean_text("**bold** and *italic*"), "bold and italic")
        # Markdown 标记可紧贴后续中英文正文；只限制开头不能落在词内。
        self.assertEqual(app.clean_text("**粗体**文字"), "粗体文字")
        self.assertEqual(app.clean_text("*italic*word"), "italicword")

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

    def test_kokoro_internal_split_bounds_and_preserves_original_text(self):
        original = "alpha beta,gamma delta"
        splitter = getattr(app, "split_kokoro_unit", None)
        self.assertIsNotNone(splitter, "Kokoro internal splitter is missing")
        fragments = splitter(original, 8)

        self.assertTrue(fragments)
        self.assertTrue(all(0 < len(fragment) <= 8 for fragment in fragments))
        self.assertEqual("".join(fragments), original)

    def test_kokoro_internal_split_hard_cuts_without_breaking_code_points(self):
        original = "😀" * 9
        splitter = getattr(app, "split_kokoro_unit", None)
        self.assertIsNotNone(splitter, "Kokoro internal splitter is missing")
        fragments = splitter(original, 4)

        self.assertEqual(fragments, ["😀" * 4, "😀" * 4, "😀"])
        self.assertEqual("".join(fragments), original)


class FilterForVoiceTests(unittest.TestCase):
    def test_han_classification_is_independent_of_runtime_unicode_database(self):
        original_name = app.unicodedata.name
        original_category = app.unicodedata.category
        app.unicodedata.name = lambda char, default="": default
        app.unicodedata.category = (
            lambda char: "Cn"
            if char == "\U00031350"
            else original_category(char)
        )
        try:
            self.assertTrue(app._is_han_char("\U00031350"))
            self.assertTrue(app._is_han_char("\U00030000"))
            self.assertFalse(app._is_han_char("\U0003134b"))
            self.assertTrue(app._contains_speakable_text("\U00031350"))
        finally:
            app.unicodedata.name = original_name
            app.unicodedata.category = original_category

    def test_speakable_gate_admits_unicode_letters_and_numbers(self):
        # 这里只锁定本地 admission；fake/分类通过不代表双语 Kokoro 能正确发音。
        for text in (
            "Привет",
            "かな",
            "한글",
            "Ελλάδα",
            "العربية",
            "é",
            "12345",
        ):
            with self.subTest(text=text):
                filtered = app.filter_for_voice(text, False)
                self.assertEqual(filtered, text)
                self.assertTrue(app._contains_speakable_text(filtered))

        for text in ("...", "😀", "\u200d"):
            with self.subTest(symbol_only=text):
                self.assertFalse(app._contains_speakable_text(text))

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


class PreviewTextTests(unittest.TestCase):
    """试听样句语言须与音色一致：中文音色中文样句，英文音色英文样句。"""

    def test_kokoro_chinese_voice_uses_chinese_sample(self):
        text = app._preview_text("kokoro", "zf_xiaoxiao")
        self.assertIn("你好", text)

    def test_kokoro_english_voice_uses_english_sample(self):
        text = app._preview_text("kokoro", "af_heart")
        self.assertIn("Hello", text)

    def test_edge_chinese_locale_uses_chinese_sample(self):
        for voice in (
            "zh-CN-XiaoxiaoNeural",
            "zh-CN-liaoning-XiaobeiNeural",
            "zh-HK-HiuGaaiNeural",
            "zh-TW-HsiaoChenNeural",
        ):
            with self.subTest(voice=voice):
                text = app._preview_text("edge", voice)
                self.assertIn("你好", text, f"Edge Chinese voice {voice} should preview Chinese")

    def test_edge_english_locale_uses_english_sample(self):
        text = app._preview_text("edge", "en-US-AvaNeural")
        self.assertIn("Hello", text)


if __name__ == "__main__":
    unittest.main()
