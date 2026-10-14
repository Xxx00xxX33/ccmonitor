import unittest

from ccmonitor.tgformat import md_to_html, split_markdown, visible_len


class MarkdownToHtmlTest(unittest.TestCase):
    def test_escapes_html(self):
        self.assertEqual(md_to_html("a < b & c > d"), "a &lt; b &amp; c &gt; d")

    def test_bold_italic_and_inline_code(self):
        self.assertEqual(md_to_html("**粗** and *斜* and `x<y`"), "<b>粗</b> and <i>斜</i> and <code>x&lt;y</code>")

    def test_markup_inside_inline_code_is_left_alone(self):
        self.assertEqual(md_to_html("`**not bold**`"), "<code>**not bold**</code>")

    def test_snake_case_is_not_italic(self):
        self.assertEqual(md_to_html("call read_tail_now"), "call read_tail_now")

    def test_headings_become_bold(self):
        self.assertEqual(md_to_html("## 小结"), "<b>小结</b>")

    def test_links(self):
        self.assertEqual(md_to_html("[PR #16](https://github.com/x/y/pull/16)"),
                         '<a href="https://github.com/x/y/pull/16">PR #16</a>')

    def test_non_http_links_keep_their_text(self):
        self.assertEqual(md_to_html("[README](README.md)"), "README")

    def test_bullets(self):
        self.assertEqual(md_to_html("- 一\n* 二\n  - 三"), "• 一\n• 二\n  ◦ 三")

    def test_numbered_list_kept(self):
        self.assertEqual(md_to_html("1. 甲\n2. 乙"), "1. 甲\n2. 乙")

    def test_code_block(self):
        self.assertEqual(md_to_html("```python\nif a < b:\n    pass\n```"),
                         '<pre><code class="language-python">if a &lt; b:\n    pass</code></pre>')

    def test_table_becomes_rows(self):
        md = "| Read | Tap | Text |\n|---|---|---|\n| 15:18 | 15:19 | 列出 **清单** |"
        self.assertEqual(md_to_html(md), "<b>Read · Tap · Text</b>\n• 15:18 · 15:19 · 列出 <b>清单</b>")

    def test_quote_and_rule(self):
        self.assertEqual(md_to_html("> 注意\n\n---"), "<blockquote>注意</blockquote>\n\n──────────")


class SplitTest(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(split_markdown("短", 100), ["短"])

    def test_splits_at_paragraphs(self):
        md = "\n\n".join(["段" * 40] * 5)
        chunks = split_markdown(md, 100)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(visible_len(md_to_html(c)) <= 100 for c in chunks))
        self.assertEqual("\n\n".join(chunks), md)

    def test_code_block_is_not_cut_when_it_fits(self):
        code = "```\n" + "\n".join(["x = 1"] * 8) + "\n```"
        md = "前言\n\n" + code + "\n\n后记"
        for chunk in split_markdown(md, 70):
            self.assertEqual(chunk.count("```") % 2, 0, chunk)

    def test_oversized_code_block_is_split_into_closed_blocks(self):
        code = "```\n" + "\n".join([f"line {i}" for i in range(60)]) + "\n```"
        chunks = split_markdown(code, 120)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertTrue(c.startswith("```") and c.endswith("```"), c)
            self.assertLessEqual(visible_len(md_to_html(c)), 120)

    def test_oversized_paragraph_is_hard_split(self):
        chunks = split_markdown("字" * 250, 100)
        self.assertEqual("".join(chunks), "字" * 250)
        self.assertTrue(all(len(c) <= 100 for c in chunks))


if __name__ == "__main__":
    unittest.main()
