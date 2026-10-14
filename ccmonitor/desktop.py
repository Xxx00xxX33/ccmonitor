"""Write-back into Claude Code Desktop through UI Automation (DESIGN.md 3.5).

The UI work is done by desktop.ps1 (System.Windows.Automation), which gets one JSON
request on stdin and answers with one JSON line. Text travels over stdin, never on
a command line, and nothing is written to disk.
"""
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from . import paths
from .sessions import find_title

SCRIPT = Path(__file__).resolve().parent / "desktop.ps1"
TIMEOUT = 90  # a safety net only: the driver keeps itself to 50 s (desktop.ps1 BudgetMs)

log = logging.getLogger("ccmonitor")

MESSAGES = {
    "sent": "已发送",
    "interjected": "已发送：会话正在运行，作为插话送达这一轮",
    "sent_draft_lost": "已发送，但输入框里原来的草稿没能填回去，请到电脑上补上",
    "fill_failed_draft_lost": "未写入：填字失败，输入框里原来的草稿也没能填回去，请到电脑上补上",
    "send_failed_draft_lost": "可能未发送：点了发送但没看到会话开始运行，原来的草稿也没能填回去，请到电脑上确认",
    "approved": "已允许一次",
    "denied": "已拒绝",
    "no_window": "未写入：找不到 Claude Desktop 窗口",
    "session_not_found": "未写入：侧边栏里找不到这个会话",
    "ambiguous_session": "未写入：侧边栏里有多个同名会话",
    "switch_failed": "未写入：切换到该会话失败",
    "draft_present": "未写入：该会话输入框里有未发送的草稿",
    "busy": "未写入：该会话正在运行，等这一轮结束再发",
    "fill_failed": "未写入：填字失败",
    "send_failed": "可能未发送：点了发送但没看到会话开始运行，请到电脑上确认",
    "no_approval": "未审批：该会话当前没有等待审批的请求",
    "approval_mismatch": "未审批：当前等待审批的命令和通知里的不一致",
    "approval_failed": "可能未审批：点了按钮但审批卡片没有消失，请到电脑上确认",
    "no_suggestion": "未发送：该会话当前没有官方建议",
    "out_of_time": "未读到官方建议：Desktop 响应太慢，等建议的时间用完了",
    "no_render_window": "未写入：找不到 Desktop 的渲染窗口",
    "clear_failed": "读完官方建议后清空输入框失败，请到电脑上确认",
    "no_title": "未写入：找不到这个会话在 Desktop 里的标题",
    "answered": "已回答",
    "no_question": "未回答：该会话当前没有等待回答的提问",
    "question_mismatch": "未回答：Desktop 里显示的问题和通知里的不一致",
    "option_mismatch": "未回答：Desktop 里找不到和通知一致的选项",
    "answer_failed": "可能未回答：点了选项但提问卡片没有往下走，请到电脑上确认",
}
# Suggestion results that are normal, not breakage (DESIGN.md 3.8).
NO_TITLE = "no_title"  # the session is not in Desktop's metadata (e.g. claude -p); the driver never ran
# A sidebar that can't single out the session (two live sessions share a title, seen live
# 2026-10-11) is a skip for suggestions, not breakage.
SUGGEST_EXPECTED = {"suggestion", "sent", "no_suggestion", "busy", "draft_present", NO_TITLE,
                    "ambiguous_session", "session_not_found", "suggestion_changed"}


# Codes that mean "worked" or "refused for a good reason"; anything else means
# something is broken and goes to Health.
EXPECTED = {"sent", "interjected", "sent_draft_lost", "approved", "denied", "busy", "draft_present", "no_approval", "approval_mismatch",
            "answered", "no_question", "question_mismatch", "option_mismatch"}
# Prompt results that never go to the fallback (the pending queue or the helper session):
# sent, and send_failed, where the click may have landed and a resend could send it twice.
NO_FALLBACK = {"sent", "interjected", "sent_draft_lost", "send_failed", "send_failed_draft_lost"}


def powershell_env() -> dict:
    """desktop.ps1 caches its compiled DLL in the storage directory, which it finds through
    CCMONITOR_HOME: always set to this process's, so the cache follows a moved directory (#70)."""
    return {**os.environ, "CCMONITOR_HOME": str(paths.DATA_DIR)}


def run_powershell(request: dict) -> dict | None:
    exe = shutil.which("pwsh") or shutil.which("powershell")
    if not exe:
        return None
    proc = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT)],
        input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
        capture_output=True, timeout=TIMEOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env=powershell_env(),
    )
    lines = proc.stdout.decode("utf-8", "replace").strip().splitlines()
    try:
        return json.loads(lines[-1]) if lines else None
    except ValueError:
        return None


class DesktopWriteBack:
    def __init__(self, driver: Callable[[dict], dict | None] = run_powershell,
                 title_of: Callable[[str], str | None] = find_title,
                 on_failure: Callable[[str], None] | None = None,
                 fallback: Callable[[str, str, str], str] | None = None,
                 on_suggest_failure: Callable[[str], None] | None = None,
                 on_own_input: Callable[[int, int], None] | None = None,
                 on_own_run: tuple[Callable[[], None], Callable[[], None]] | None = None):
        self._driver = driver
        self._title_of = title_of
        self._on_failure = on_failure
        self._fallback = fallback
        self._on_suggest_failure = on_suggest_failure
        self._on_own_input = on_own_input  # (last real input, tick of the driver's own input)
        self._on_own_run = on_own_run      # (begin, end) around a run: its input is ours

    def send_prompt(self, session_id: str, text: str) -> str:
        """A prompt from the phone: sent even while the session runs, as a mid-turn
        message (user's call, 2026-10-12), rather than held until the turn ends."""
        status, code = self.try_prompt(session_id, text, interject=True)
        if self._fallback and code is not None and code not in NO_FALLBACK:
            return f"{status}\n{self._fallback(session_id, text, code)}"
        return status

    def try_prompt(self, session_id: str, text: str, interject: bool = False) -> tuple[str, str | None]:
        """One UI write of a prompt, no fallback: (status, driver code or None if it never ran).

        A draft in the box is set aside, the prompt sent, and the draft put back; if putting
        it back fails, its text comes back in the status so it isn't lost. A running session
        is refused as busy unless interject."""
        return self._prompt(self._title_of(session_id), text, interject)

    def try_prompt_title(self, title: str, text: str) -> tuple[str, str | None]:
        """try_prompt for a session known by its Desktop title (helper rotation, whose
        CLI session id changes with /clear). Never interjects: /clear must wait."""
        return self._prompt(title, text)

    def _prompt(self, title: str | None, text: str, interject: bool = False) -> tuple[str, str | None]:
        code, result = self._call_title(title, {"action": "prompt", "text": text, "interject": interject})
        status, code = self._status(code)
        if code and code.endswith("draft_lost") and result.get("text"):
            status += f"\n草稿原文：{result['text']}"
        return status, code

    def approve(self, session_id: str, allow: bool, expect: str = "") -> str:
        return self._run(session_id, {"action": "approve", "allow": allow, "expect": expect})[0]

    def answer(self, session_id: str, question: dict, index: int, total: int, picked: list[int]) -> tuple[str, bool]:
        """Pick options in the session's question card (DESIGN.md 3.9); return (status, answered).

        question: one entry of Notice.questions; index: its 1-based place among `total`;
        picked: indexes into its options (one for single-select)."""
        options = [question["options"][i] for i in picked]
        status, code = self._run(session_id, {"action": "answer", "question": question["question"], "index": index,
                                              "total": total, "multi": question["multi"], "options": options})
        if code != "answered":
            return status, False
        return f"{status}：{'、'.join(o['label'] for o in options)}", True

    def read_suggestion(self, session_id: str) -> str | None:
        """The session's official suggestion text, read and then cleared from its box (DESIGN.md 3.8)."""
        code, result = self._call(session_id, {"action": "suggest"})
        if code == "suggestion" and result.get("text"):
            return result["text"]
        if code not in SUGGEST_EXPECTED and self._on_suggest_failure:
            self._on_suggest_failure(MESSAGES.get(code, f"读取官方建议失败（{code or '无结果'}）"))
        return None

    def send_suggestion(self, session_id: str, expect: str) -> tuple[str, str | None]:
        """Send the official suggestion if it still equals `expect` (the text the user was shown).

        Returns (status echoing the exact text sent, the new suggestion if it changed instead).
        """
        code, result = self._call(session_id, {"action": "accept", "expect": expect})
        if code == "sent":
            return f"已发送官方建议：{result.get('text', '')}", None
        if code == "suggestion_changed" and result.get("text"):
            return "未发送：官方建议已经变了，通知已更新为新的建议，再点一次 💡 发送", result["text"]
        status = MESSAGES.get(code, f"发送官方建议失败（{code or '无结果'}），请到电脑上确认")
        if code not in SUGGEST_EXPECTED and self._on_suggest_failure:
            self._on_suggest_failure(status)
        return status, None

    def _call(self, session_id: str, request: dict) -> tuple[str | None, dict]:
        """(driver code, full result); code NO_TITLE if the session's title is unknown."""
        return self._call_title(self._title_of(session_id), request)

    def _call_title(self, title: str | None, request: dict) -> tuple[str | None, dict]:
        if not title:
            return NO_TITLE, {}
        action = request.pop("action")
        started = time.monotonic()
        # The window opens before the driver starts: its nudge is reported only on exit,
        # and an away check in between would otherwise read it as the user.
        if self._on_own_run:
            self._on_own_run[0]()
        try:
            result = self._driver({"action": action, "title": title, **request}) or {}
        except Exception as e:
            log.warning("desktop driver failed: %s after %.0f s", type(e).__name__, time.monotonic() - started)
            result = {}
        finally:
            if self._on_own_run:
                self._on_own_run[1]()
        own = result.get("own_input")
        if self._on_own_input and isinstance(own, list) and len(own) == 2 and all(isinstance(t, int) for t in own):
            log.info("write-back own input")
            self._on_own_input(*own)  # the driver woke the display; that input wasn't the user
        code = result.get("code")
        # Step times in ms (desktop.ps1 Mark), to see where a slow run went; never content.
        timing = result.get("timing") if isinstance(result.get("timing"), str) else ""
        log.info("write-back %s (%s %.1f s%s)", code, action, time.monotonic() - started,
                 f"; {timing}" if timing else "")
        return code, result

    def _run(self, session_id: str, request: dict) -> tuple[str, str | None]:
        """(status line for the user, driver code or None if the driver never ran)."""
        return self._status(self._call(session_id, request)[0])

    def _status(self, code: str | None) -> tuple[str, str | None]:
        if code == NO_TITLE:
            return "未写入：找不到这个会话在 Desktop 里的标题", None
        status = MESSAGES.get(code, f"写入失败（{code or '无结果'}），请到电脑上确认")
        if code not in EXPECTED and self._on_failure:
            self._on_failure(status)
        return status, code or "no_result"
