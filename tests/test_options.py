import unittest

from ccmonitor.options import options_label, parse_options


class OptionsLabelTest(unittest.TestCase):
    def test_label_follows_the_heading_the_answer_used(self):
        self.assertEqual(options_label("x\n选项：\n1. a"), "选项")
        self.assertEqual(options_label("x\n选项:\n1. a"), "选项")
        self.assertEqual(options_label("x\nOptions:\n1. a"), "Options")
        self.assertEqual(options_label("x\nOPTIONS:\n1. a"), "Options")

    def test_label_uses_the_last_heading_like_parse_options(self):
        self.assertEqual(options_label("选项：\n1. a\n\nmore\nOptions:\n1. b"), "Options")
        self.assertEqual(options_label("Options:\n1. a\n\nmore\n选项：\n1. b"), "选项")

    def test_no_heading_defaults_to_chinese(self):
        self.assertEqual(options_label("no section here"), "选项")


class ParseOptionsTest(unittest.TestCase):
    def test_extracts_trailing_options_section(self):
        text = "做完了。\n\n选项：\n1. 按刚才说的继续\n2. 先写测试\n3. 先改文档"
        body, options = parse_options(text)
        self.assertEqual(body, "做完了。")
        self.assertEqual(options, ["按刚才说的继续", "先写测试", "先改文档"])

    def test_no_section_returns_whole_text(self):
        body, options = parse_options("输出是 `2`。")
        self.assertEqual(body, "输出是 `2`。")
        self.assertEqual(options, [])

    def test_uses_last_heading_when_word_appears_earlier(self):
        text = "选项：\n这一行是正文里的用法。\n\n选项：\n1. 按刚才说的继续"
        body, options = parse_options(text)
        self.assertEqual(body, "选项：\n这一行是正文里的用法。")
        self.assertEqual(options, ["按刚才说的继续"])

    def test_accepts_chinese_enumeration_comma(self):
        _, options = parse_options("x\n选项：\n1、按刚才说的继续\n2、换个做法")
        self.assertEqual(options, ["按刚才说的继续", "换个做法"])

    def test_more_than_three_items_is_rejected(self):
        text = "x\n选项：\n1. 按刚才说的继续\n2. a\n3. b\n4. c"
        self.assertEqual(parse_options(text), (text, []))

    def test_item_over_40_chars_is_rejected(self):
        text = "x\n选项：\n1. 按刚才说的继续\n2. " + "长" * 41
        self.assertEqual(parse_options(text), (text, []))

    def test_unnumbered_line_in_section_is_rejected(self):
        text = "x\n选项：\n1. 按刚才说的继续\n随便一句话"
        self.assertEqual(parse_options(text), (text, []))

    def test_heading_without_items_is_rejected(self):
        text = "x\n选项："
        self.assertEqual(parse_options(text), (text, []))

    def test_english_heading_from_an_english_session(self):
        # The last answer of a live session, 2026-10-11 19:18.
        text = ("Done.\n\nOptions:\n1. Continue as planned\n2. Merge #335 once CI passes and deploy it\n"
                "3. Merge it after CI passes, but don't deploy yet")
        body, options = parse_options(text)
        self.assertEqual(body, "Done.")
        self.assertEqual(options, ["Continue as planned", "Merge #335 once CI passes and deploy it",
                                   "Merge it after CI passes, but don't deploy yet"])

    def test_heading_variants(self):
        for heading in ("选项:", "options:", "OPTIONS:"):
            self.assertEqual(parse_options(f"x\n{heading}\n1. 按刚才说的继续")[1], ["按刚才说的继续"], heading)
        self.assertEqual(parse_options("x\nOptions\n1. a")[1], [])  # the colon is part of the heading

    def test_latin_characters_count_as_half(self):
        self.assertEqual(parse_options("x\nOptions:\n1. " + "a" * 80)[1], ["a" * 80])
        self.assertEqual(parse_options("x\nOptions:\n1. " + "a" * 81)[1], [])

    def test_blank_lines_inside_section_are_ignored(self):
        _, options = parse_options("x\n选项：\n\n1. 按刚才说的继续\n\n2. b\n")
        self.assertEqual(options, ["按刚才说的继续", "b"])


if __name__ == "__main__":
    unittest.main()
