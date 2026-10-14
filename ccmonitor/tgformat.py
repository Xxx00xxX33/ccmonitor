"""Markdown from Claude's answers → Telegram HTML, and splitting long answers into
messages Telegram accepts (DESIGN.md 3.3).

Covers what answers actually use: bold, italics, inline code, fenced code, headings,
bullet and numbered lists, links, block quotes, rules and tables. Tables become one
line per row, which reads better on a phone than a padded grid.
"""
import html
import re

LIMIT = 3800  # Telegram allows 4096 visible characters; leave room for header and footer

_FENCE = re.compile(r"^\s*```(.*)$")
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_RULE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_TABLE_SEP = re.compile(r"^:?-{2,}:?$")


def _inline(text: str) -> str:
    codes: list[str] = []

    def keep(m: re.Match) -> str:
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    s = re.sub(r"`([^`\n]+)`", keep, text)
    s = html.escape(s, quote=False)
    s = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s\"]+)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r"\[([^\]\n]+)\]\(([^)\s]+)\)", r"\1", s)  # relative links: keep the text
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![*\w])\*(?![\s*])(.+?)(?<![\s*])\*(?![*\w])", r"<i>\1</i>", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{html.escape(codes[int(m.group(1))], quote=False)}</code>", s)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _table(lines: list[str]) -> list[str]:
    rows = [_cells(l) for l in lines]
    rows = [r for r in rows if not all(_TABLE_SEP.match(c) for c in r if c)]
    out = []
    for i, r in enumerate(rows):
        joined = " · ".join(_inline(c) for c in r)
        out.append(f"<b>{joined}</b>" if i == 0 and len(rows) > 1 else f"• {joined}")
    return out


def md_to_html(md: str) -> str:
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE.match(line)
        if fence:
            lang = fence.group(1).strip()
            body = []
            i += 1
            while i < len(lines) and not _FENCE.match(lines[i]):
                body.append(lines[i])
                i += 1
            i += 1  # closing fence (or end of text)
            cls = f' class="language-{html.escape(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>{html.escape(chr(10).join(body), quote=False)}</code></pre>")
            continue
        if line.lstrip().startswith("|"):
            block = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                block.append(lines[i])
                i += 1
            out.extend(_table(block))
            continue
        if line.lstrip().startswith(">"):
            block = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                block.append(_inline(lines[i].lstrip()[1:].lstrip()))
                i += 1
            out.append("<blockquote>" + "\n".join(block) + "</blockquote>")
            continue
        if _RULE.match(line):
            out.append("──────────")
        elif m := _HEADING.match(line):
            out.append(f"<b>{_inline(m.group(1))}</b>")
        elif m := _BULLET.match(line):
            indent = m.group(1)
            out.append(f"{indent}{'◦' if len(indent) >= 2 else '•'} {_inline(m.group(2))}")
        else:
            out.append(_inline(line))
        i += 1
    return "\n".join(out)


def visible_len(html_text: str) -> int:
    """What Telegram counts against its limit: text without tags, entities decoded."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", html_text)))


def _blocks(md: str) -> list[str]:
    """Paragraphs separated by blank lines; a fenced code block stays one block."""
    blocks, cur, in_fence = [], [], False
    for line in md.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        if not line.strip() and not in_fence:
            if cur:
                blocks.append("\n".join(cur))
                cur = []
            continue
        cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def _fits(md: str, limit: int) -> bool:
    return visible_len(md_to_html(md)) <= limit


def _split_block(block: str, limit: int) -> list[str]:
    lines = block.split("\n")
    fence = _FENCE.match(lines[0])
    if fence and len(lines) > 2:
        head = lines[0].strip()
        body = lines[1:-1] if _FENCE.match(lines[-1]) else lines[1:]
        pieces, cur = [], []
        for line in body:
            if cur and not _fits("\n".join([head, *cur, line, "```"]), limit):
                pieces.append("\n".join([head, *cur, "```"]))
                cur = []
            cur.append(line[:limit - 10])
        if cur:
            pieces.append("\n".join([head, *cur, "```"]))
        return pieces
    return [block[i:i + limit] for i in range(0, len(block), limit)]


def split_markdown(md: str, limit: int = LIMIT) -> list[str]:
    """Cut an answer into chunks whose rendered text fits `limit`, at paragraph
    boundaries and never inside a code block unless the block alone is too long."""
    if _fits(md, limit):
        return [md]
    chunks, cur = [], []
    for block in _blocks(md):
        if not _fits(block, limit):
            if cur:
                chunks.append("\n\n".join(cur))
                cur = []
            chunks.extend(_split_block(block, limit))
            continue
        if cur and not _fits("\n\n".join([*cur, block]), limit):
            chunks.append("\n\n".join(cur))
            cur = []
        cur.append(block)
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks
