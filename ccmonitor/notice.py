"""What the side channel tells the user about one event."""
from dataclasses import dataclass, field
from enum import Enum


class Level(Enum):
    URGENT = "urgent"      # 立刻级：权限审批、Claude 提问
    DEFERRED = "deferred"  # 延后级：任务完成


@dataclass
class Notice:
    level: Level
    kind: str           # "approval" | "question" | "offer" | "completed" | "proposal" | "stuck" | "error"
    session_id: str
    session_label: str  # 会话标题 · 用户最后一句的前 30 字
    text: str
    options: list[str] = field(default_factory=list)  # 选项全文，点选时原样写入
    options_label: str = "选项"  # label above the options, in the answer's language ("Options" for English)
    command: str = ""   # approval only: full Bash command, used to check the dialog before approving
    title: str = ""     # session title, for the notice layout
    prompt: str = ""    # the user's last prompt, clipped, for the notice layout
    tool: str = ""      # approval only: tool name
    detail: str = ""    # approval only: command or tool input, clipped
    key: str = ""       # approval/question: the tool call (classify.tool_key), to match its PostToolUse
    # question only: [{"question", "multi", "options": [{"label", "description"}]}], in the card's order
    questions: list[dict] = field(default_factory=list)
