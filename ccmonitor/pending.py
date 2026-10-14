"""Prompts the UI write could not send yet, sent later by ccmonitor itself (DESIGN.md 5).

Replaces the helper session as the backup write path without a model in the loop: a
prompt for a running session waits for that session's turn to end (its Stop hook, with
a timer as backup for turns that end without one); a prompt the UI write failed on is
retried a few times. Only while the user is away: if they are back at the computer the
prompt is dropped and its text handed back on Telegram, so nothing is typed under
someone's hands (DESIGN.md 3.7). Texts live in memory only.
"""
import logging
import time
from collections import OrderedDict
from typing import Callable

log = logging.getLogger("ccmonitor")

RETRY_SECONDS = 60       # backup retry while waiting
TURN_END_SECONDS = 3     # Desktop's Stop button can outlive the Stop hook briefly
MAX_FAILURES = 5         # UI-write breakages before giving up
MAX_AGE_SECONDS = 3 * 3600
SENT = {"sent", "interjected", "sent_draft_lost"}
CLIP = 40


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= CLIP else text[:CLIP] + "…"


class PendingPrompts:
    def __init__(self, send: Callable[[str, str], tuple[str, str | None]], say: Callable[[str], None],
                 later: Callable[[float, Callable[[], None]], None], can_write: Callable[[], bool],
                 now: Callable[[], float] = time.monotonic):
        self._send = send            # UI write without fallback: (status, driver code)
        self._say = say
        self._later = later          # runs a callable on the side channel's worker after a delay
        self._can_write = can_write  # the user is away
        self._now = now
        self._queues: OrderedDict[str, list[list]] = OrderedDict()  # session -> [[text, since, failures]]
        self._timers: dict[str, int] = {}  # session -> the one live timer; older timers are stale
        self._ticket = 0

    def count(self) -> int:
        return sum(len(q) for q in self._queues.values())

    def hold(self, session_id: str, text: str, code: str) -> str:
        self._queues.setdefault(session_id, []).append([text, self._now(), 0])
        self._schedule(session_id, RETRY_SECONDS)
        log.info("prompt held (%s)", code)
        if code == "busy":
            return "已排队：这一轮结束后自动发送"
        return "已排队：稍后自动重试"

    def turn_ended(self, session_id: str) -> None:
        if session_id in self._queues:
            self._schedule(session_id, TURN_END_SECONDS, force=True)

    def _schedule(self, session_id: str, seconds: float, force: bool = False) -> None:
        if session_id in self._timers and not force:
            return
        self._ticket += 1
        self._timers[session_id] = ticket = self._ticket
        self._later(seconds, lambda: self._retry(session_id, ticket))

    def _retry(self, session_id: str, ticket: int) -> None:
        if self._timers.get(session_id) != ticket:
            return  # replaced by a later timer (e.g. a turn end)
        del self._timers[session_id]
        queue = self._queues.get(session_id)
        if not queue:
            return
        item = queue[0]
        text, since, failures = item
        if not self._can_write():
            del self._queues[session_id]
            self._say("你已回到电脑前，排队的消息未发送：\n" + "\n".join(t for t, _, _ in queue))
            return
        if self._now() - since > MAX_AGE_SECONDS:
            queue.pop(0)
            self._say(f"已放弃：排队超过 3 小时仍未发出：{text}")
        else:
            status, code = self._send(session_id, text)
            log.info("held prompt retry %s", code)
            if code in SENT:
                queue.pop(0)
                self._say(f"排队的消息{status}：{_clip(text)}")
            elif code in ("send_failed", "send_failed_draft_lost"):
                queue.pop(0)  # the click may have landed; sending again could send it twice
                self._say(f"排队的消息{status}：{_clip(text)}")
            elif code != "busy":
                item[2] = failures + 1
                if item[2] >= MAX_FAILURES:
                    queue.pop(0)
                    self._say(f"已放弃：重试 {MAX_FAILURES} 次仍未写入（{status}）：{text}")
        if queue:
            self._schedule(session_id, RETRY_SECONDS)
        else:
            self._queues.pop(session_id, None)
