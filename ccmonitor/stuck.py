"""Stuck detection (DESIGN.md 5, v2): a turn in progress with no new output for 10
minutes becomes one deferred notice per quiet stretch.

"Output" is the timestamp of the last user/assistant record in the session's
transcript or in any of its subagents' transcripts (a subagent writes to its own
file, so the main one stays silent while it works). Only timestamps and the
records' structure are looked at; nothing from the transcripts is kept.
"""
import datetime
import json
import os
import time
from pathlib import Path
from typing import Callable

from .classify import START_SESSION, Classifier
from .notice import Level, Notice

QUIET_SECONDS = 600
TAIL_BYTES = 256 * 1024
ENDED, WORKING = "ended", "working"
INTERRUPTED = "[Request interrupted by user"


def _turn_state(record: dict) -> str:
    message = record.get("message") or {}
    if record["type"] == "assistant":
        return ENDED if message.get("stop_reason") == "end_turn" else WORKING
    content = message.get("content")
    texts = [content] if isinstance(content, str) else [
        c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text"]
    return ENDED if any(t.startswith(INTERRUPTED) for t in texts) else WORKING


def read_tail(path: str) -> tuple[float, str] | None:
    """(time of the last user/assistant record, turn state), or None if unreadable."""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - TAIL_BYTES))
            lines = f.read().split(b"\n")
    except OSError:
        return None
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue  # empty, half-written, or cut by the seek
        if isinstance(record, dict) and record.get("type") in ("user", "assistant") and record.get("timestamp"):
            at = datetime.datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00")).timestamp()
            return at, _turn_state(record)
    return None


def subagent_activity(transcript_path: str) -> float | None:
    """Time of the newest user/assistant record in <session>/subagents/*.jsonl, if any."""
    if not transcript_path:
        return None
    newest = None
    for f in Path(transcript_path).with_suffix("").joinpath("subagents").glob("*.jsonl"):
        tail = read_tail(str(f))
        if tail and (newest is None or tail[0] > newest):
            newest = tail[0]
    return newest


class _Turn:
    def __init__(self, payload: dict, now: float):
        self.payload = payload
        self.started = now
        self.awaiting_since: float | None = None
        self.reported_for: float | None = None  # last-output time we already reported


class StuckWatcher:
    def __init__(self, classifier: Classifier, read_tail: Callable[[str], tuple[float, str] | None] = read_tail,
                 clock: Callable[[], float] = time.time, quiet_seconds: int = QUIET_SECONDS,
                 subagent_activity: Callable[[str], float | None] = subagent_activity):
        self._classifier = classifier
        self._read_tail = read_tail
        self._subagent_activity = subagent_activity
        self._clock = clock
        self._quiet = quiet_seconds
        self._turns: dict[str, _Turn] = {}

    def on_event(self, p: dict) -> None:
        sid, event = p.get("session_id"), p.get("hook_event_name")
        if not sid:
            return
        if event == "UserPromptSubmit":
            self._turns[sid] = _Turn({k: p.get(k) for k in ("session_id", "cwd", "transcript_path")}, self._clock())
        elif event in ("Stop", "StopFailure", "SessionEnd"):
            self._turns.pop(sid, None)
        elif sid in self._turns and (event == "PermissionRequest" or (
                event == "PreToolUse" and p.get("tool_name") == START_SESSION
                and (p.get("tool_input") or {}).get("initiation") == "offer")):
            self._turns[sid].awaiting_since = self._clock()

    def check(self) -> list[Notice]:
        now, notices = self._clock(), []
        for sid, turn in list(self._turns.items()):
            path = turn.payload.get("transcript_path") or ""
            tail = self._read_tail(path)
            if tail and tail[1] == ENDED and tail[0] >= turn.started:
                del self._turns[sid]  # ended without Stop: Deny or Esc
                continue
            last = max(turn.started, tail[0] if tail else 0, self._subagent_activity(path) or 0)
            if turn.awaiting_since is not None:
                if last <= turn.awaiting_since:
                    continue  # waiting for the user's approval, already notified as urgent
                turn.awaiting_since = None
            if now - last < self._quiet or turn.reported_for == last:
                continue
            turn.reported_for = last
            minutes = int((now - last) // 60)
            title, prompt = self._classifier.who(sid, turn.payload)
            notices.append(Notice(Level.DEFERRED, "stuck", sid, self._classifier.label(sid, turn.payload),
                                  f"已 {minutes} 分钟没有新输出：可能在跑长命令，也可能卡住了",
                                  title=title, prompt=prompt))
        return notices
