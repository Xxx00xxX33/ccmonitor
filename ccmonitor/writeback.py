"""Write-back interface (DESIGN.md 3.5). The UI Automation implementation is a later module."""
from typing import Protocol


class WriteBack(Protocol):
    def send_prompt(self, session_id: str, text: str) -> str:
        """Send text to the session; return a short status line for the user."""

    def approve(self, session_id: str, allow: bool, expect: str = "") -> str:
        """Click Allow once / Deny in the session; return a short status line.

        expect: the command the user saw; the dialog must show the same one."""

    def answer(self, session_id: str, question: dict, index: int, total: int, picked: list[int]) -> tuple[str, bool]:
        """Pick options `picked` of the question card's question `index` of `total`; return (status, answered)."""

    def read_suggestion(self, session_id: str) -> str | None:
        """The session's official suggestion text, or None (DESIGN.md 3.8)."""

    def send_suggestion(self, session_id: str, expect: str) -> tuple[str, str | None]:
        """Send the official suggestion if it still equals `expect`; return (status, new suggestion if it changed)."""


class NotImplementedWriteBack:
    def send_prompt(self, session_id: str, text: str) -> str:
        return "写入模块还没实现，未发送"

    def approve(self, session_id: str, allow: bool, expect: str = "") -> str:
        return "写入模块还没实现，未审批"

    def answer(self, session_id: str, question: dict, index: int, total: int, picked: list[int]) -> tuple[str, bool]:
        return "写入模块还没实现，未回答", False

    def read_suggestion(self, session_id: str) -> str | None:
        return None

    def send_suggestion(self, session_id: str, expect: str) -> tuple[str, str | None]:
        return "写入模块还没实现，未发送", None
