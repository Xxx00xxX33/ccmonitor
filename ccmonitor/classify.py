"""Turn official hook payloads into notices (DESIGN.md 3.4).

Holds per-session labels in memory only; nothing read here is written to disk.
"""
import json
import ntpath
import re
from typing import Callable

from .notice import Level, Notice
from .options import options_label, parse_options

COMMAND_CHARS = 1000
PROMPT_LABEL_CHARS = 30
# Prompts the app injects rather than the user types: CI events, task notifications,
# messages from other sessions, system reminders (seen live in a notice label).
_INJECTED = re.compile(r"^\s*(<[a-z][\w-]*[\s>/]|Another Claude session sent a message)")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _question_text(questions: list[dict]) -> str:
    parts = []
    for i, q in enumerate(questions, 1):
        number = f"{i}/{len(questions)} " if len(questions) > 1 else ""
        parts.append(number + q["question"] + ("（可多选）" if q["multi"] else ""))
        for o in q["options"]:
            extra = o["description"] if o["description"] != o["label"] else ""
            parts.append(f"- {o['label']}" + (f"：{extra}" if extra else ""))
    return "\n".join(parts)


def tool_key(p: dict) -> str:
    """One tool call's identity across its PermissionRequest and its PostToolUse, which
    has no shared id (PermissionRequest carries no tool_use_id). Kept in memory only."""
    return json.dumps([p.get("tool_name") or "", p.get("tool_input") or {}], sort_keys=True, ensure_ascii=False)


TRANSCRIPT_TAIL = 1 << 20  # the call being asked about is near the end; a pending one is the last


def answered(transcript_path: str, key: str) -> bool:
    """Whether the session's transcript already holds a result for the latest tool call with
    this tool_key. A Deny pressed in Desktop writes a rejected result there and fires no hook:
    no Stop on an interrupt, no PostToolUseFailure or PermissionDenied for a manual denial
    (hooks docs, checked 2026-10-13). False when unsure: no path, unreadable, call not found.
    Reads the file's tail only and keeps nothing.
    """
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, 2)
            start = max(0, f.tell() - TRANSCRIPT_TAIL)
            f.seek(start)
            lines = f.read().split(b"\n")
    except (OSError, TypeError, ValueError):
        return False
    if start:
        lines = lines[1:]  # cut mid-line
    call, results = None, set()
    for line in lines:
        try:
            content = (json.loads(line).get("message") or {}).get("content")
        except (ValueError, AttributeError):
            continue
        for block in content if isinstance(content, list) else ():
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and tool_key(
                    {"tool_name": block.get("name"), "tool_input": block.get("input")}) == key:
                call = block.get("id")
            elif block.get("type") == "tool_result":
                results.add(block.get("tool_use_id"))
    return call is not None and call in results


class Classifier:
    def __init__(self, title_of: Callable[[str], str | None] | None = None):
        self._title_of = title_of
        self._titles: dict[str, str] = {}
        self._prompts: dict[str, str] = {}

    def handle(self, p: dict) -> Notice | None:
        sid = p.get("session_id")
        if not sid:
            return None
        event = p.get("hook_event_name")
        if event == "SessionStart":
            if p.get("session_title"):
                self._titles[sid] = p["session_title"]
        elif event == "UserPromptSubmit":
            prompt = str(p.get("prompt", ""))
            if not _INJECTED.match(prompt):
                self._prompts[sid] = " ".join(prompt.split())
        elif event == "SessionEnd":
            self._titles.pop(sid, None)
            self._prompts.pop(sid, None)
        elif event == "PermissionRequest":
            return self._permission(sid, p)
        elif event == "StopFailure":
            detail = p.get("error_details") or p.get("last_assistant_message") or ""
            text = f"回合因 API 错误中断：{p.get('error') or 'unknown'}" + (f"（{_clip(detail, 200)}）" if detail else "")
            return self._notice(Level.DEFERRED, "error", sid, p, text)
        elif event == "Stop":
            answer = p.get("last_assistant_message") or ""
            body, options = parse_options(answer)
            return self._notice(Level.DEFERRED, "completed", sid, p, body, options=options,
                                options_label=options_label(answer))
        return None

    def _permission(self, sid: str, p: dict) -> Notice:
        tool = p.get("tool_name") or "?"
        tool_input = p.get("tool_input") or {}
        key = tool_key(p)
        if tool == "AskUserQuestion":
            questions = [{"question": str(q.get("question", "")), "multi": bool(q.get("multiSelect")),
                          "options": [{"label": str(o.get("label", "")), "description": str(o.get("description") or "")}
                                      for o in q.get("options", [])]}
                         for q in tool_input.get("questions", [])]
            return self._notice(Level.URGENT, "question", sid, p, _question_text(questions), questions=questions,
                                key=key)
        command = tool_input.get("command", "") if tool == "Bash" else ""
        detail = _clip(command or json.dumps(tool_input, ensure_ascii=False), COMMAND_CHARS)
        return self._notice(Level.URGENT, "approval", sid, p, f"{tool}: {detail}",
                            command=command, tool=tool, detail=detail, key=key)

    def _notice(self, level: Level, kind: str, sid: str, p: dict, text: str, **kw) -> Notice:
        title, prompt = self.who(sid, p)
        return Notice(level, kind, sid, self.label(sid, p), text, title=title, prompt=prompt, **kw)

    def who(self, sid: str, p: dict) -> tuple[str, str]:
        """(session title, the user's last prompt clipped to 30 characters, or "")."""
        # The Desktop sidebar title is what the user recognises; SessionStart's
        # session_title is Claude Code's internal slug and only a fallback.
        title = ((self._title_of(sid) if self._title_of else None) or self._titles.get(sid)
                 or ntpath.basename(str(p.get("cwd", "")).rstrip("\\/")) or sid[:8])
        prompt = self._prompts.get(sid)
        return title, _clip(prompt, PROMPT_LABEL_CHARS) if prompt else ""

    def label(self, sid: str, p: dict) -> str:
        title, prompt = self.who(sid, p)
        return f"{title} · {prompt}" if prompt else title
