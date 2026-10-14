"""Parse the options section defined by the format contract (DESIGN.md 3.2)."""
import re
import unicodedata

HEADING = "选项："
MAX_ITEMS = 3
MAX_ITEM_CHARS = 40
# Sessions that answer in English translate the heading (seen live 2026-10-11 on
# "Options:"); a half-width colon is the same heading too.
HEADINGS = {HEADING.casefold(), "选项:", "options:"}

# Injected only while the user is away (DESIGN.md 3.2), with the away line; without it
# only when the away state can't be read. Based on the wording validated in VERIFY.md 4,
# minus its fixed first item "按刚才说的继续": Desktop's suggestion just copied that
# item (user's call, 2026-10-12).
OPTIONS_INSTRUCTION = (
    f"回答结束时，若有合理的下一步，最后一段以单独一行「{HEADING}」开头（用英文回答时写「Options:」），"
    f"下面最多 {MAX_ITEMS} 个编号项，每项是一条可以直接发出的具体指令、不超过 {MAX_ITEM_CHARS} 字，"
    "不要列出需要用户先提供材料或信息才能执行的项；"
    "没有合理下一步就省略这一段。"
)
AWAY_INSTRUCTION = "[用户不在电脑前] " + OPTIONS_INSTRUCTION
_ITEM = re.compile(r"\d+[.、]\s*(.+)")


def _width(text: str) -> float:
    """Item length in Chinese characters: other characters count as half, so an
    English item gets about as much room as a Chinese one."""
    return sum(1 if unicodedata.east_asian_width(c) in "WF" else 0.5 for c in text)


def options_label(text: str) -> str:
    """The notice's label for the options section, in the answer's language:
    "Options" when the section parse_options takes is headed "Options:", else "选项"."""
    lines = text.rstrip().split("\n")
    starts = [line.strip().casefold() for line in lines if line.strip().casefold() in HEADINGS]
    return "Options" if starts and starts[-1] == "options:" else "选项"


def parse_options(text: str) -> tuple[str, list[str]]:
    """Split an answer into (body, options).

    The section starts at the last line that is exactly a heading and runs to the
    end. If it breaks the contract the whole text is returned as body with no
    options, so a malformed answer still reaches the user as plain text.
    """
    lines = text.rstrip().split("\n")
    starts = [i for i, line in enumerate(lines) if line.strip().casefold() in HEADINGS]
    if not starts:
        return text, []
    start = starts[-1]
    options = []
    for line in lines[start + 1:]:
        if not line.strip():
            continue
        m = _ITEM.fullmatch(line.strip())
        if not m or _width(m.group(1)) > MAX_ITEM_CHARS:
            return text, []
        options.append(m.group(1).strip())
    if not 1 <= len(options) <= MAX_ITEMS:
        return text, []
    return "\n".join(lines[:start]).rstrip(), options
