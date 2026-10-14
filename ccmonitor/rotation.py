"""Rotate the mode-B helper session every 6 hours (DESIGN.md 5).

A session that clears itself loses its Monitor and nobody wakes it (seen live
2026-10-12), so the daemon does the rotation through UI write-back: pause the helper,
type /clear into it, then type SETUP_PROMPT; the helper restarts its feed and the
feed's handshake resumes it. Model, effort and permissions survive /clear (also seen
live), so the helper stays on haiku/low/bypass.

Runs on the side channel's worker, the same thread that hands prompts to the helper,
so it never starts while a forward is in flight: a forward in progress finishes first.
Only while the user is away, since UI writes switch the visible session.
"""
import logging
import os
import time
from typing import Callable

from .relaymode import SETUP_PROMPT

log = logging.getLogger("ccmonitor")

# CCMONITOR_ROTATE_SECONDS shortens the interval for live tests only.
EVERY_SECONDS = float(os.environ.get("CCMONITOR_ROTATE_SECONDS") or 6 * 3600)
RETRY_SECONDS = 30 * 60       # after a failed /clear
RECONNECT_SECONDS = 3 * 60    # the re-primed helper should be back by then
# Right after /clear, Desktop shows the session as running for a few seconds and the
# setup prompt is refused as busy (seen live 2026-10-12 13:18), so it is retried.
PRIME_TRIES = 15
PRIME_EVERY = 3.0


class HelperRotation:
    def __init__(self, helper, write: Callable[[str, str], tuple[str, str | None]],
                 title_of: Callable[[str], str | None], is_away: Callable[[], bool],
                 mode: Callable[[], str], report: Callable[[str], None],
                 now: Callable[[], float] = time.monotonic, every: float = EVERY_SECONDS,
                 sleep: Callable[[float], None] = time.sleep):
        self._helper = helper
        self._write = write          # UI write by Desktop title: (status, driver code)
        self._title_of = title_of
        self._is_away = is_away
        self._mode = mode
        self._report = report
        self._now = now
        self._every = every
        self._sleep = sleep
        self._primed_at: float | None = None  # when SETUP_PROMPT went in; waiting for the feed
        self._reported = False                # a failure was already reported for this rotation

    def due(self) -> bool:
        h = self._helper
        return (self._mode() == "b" and h.connected and h.since is not None
                and self._now() - h.since >= self._every)

    def check(self) -> None:
        """Called every minute on the worker."""
        h = self._helper
        if self._primed_at is not None:
            if h.connected:
                self._primed_at = None
            elif self._now() - self._primed_at >= RECONNECT_SECONDS:
                self._primed_at = None
                h.resume()  # stop recognising events by title; it may never come back
                if not self._reported:
                    self._report("转发会话换班后没有重新连上，消息先按模式 A 排队；请到电脑上看一下「ccmonitor 转发」会话")
            return
        if not self.due() or not self._is_away():
            return
        self._rotate()

    def _rotate(self) -> None:
        h = self._helper
        title = self._title_of(h.session_id or "")
        if not title:
            h.since = self._now() - self._every + RETRY_SECONDS
            self._report("转发会话换班失败：找不到它在 Desktop 里的标题，30 分钟后再试")
            return
        h.pause(title)
        status, code = self._write(title, "/clear")
        log.info("helper rotation clear %s", code)
        if code != "sent":
            h.resume()
            if code != "busy":  # mid-turn (e.g. re-arming its feed): try again next minute
                h.since = self._now() - self._every + RETRY_SECONDS
                self._report(f"转发会话换班失败（{status}），30 分钟后再试")
            return
        for _ in range(PRIME_TRIES):
            self._sleep(PRIME_EVERY)
            status, code = self._write(title, SETUP_PROMPT)
            if code != "busy":
                break
        log.info("helper rotation prime %s", code)
        self._reported = code not in ("sent", "sent_draft_lost")
        if self._reported:
            self._report(f"转发会话已清空，但启动指令没发进去（{status}）；消息先按转发模式 A 排队。"
                         f"请到电脑上归档「{title}」，再在 ccmonitor 窗口状态页点「新建转发会话」，"
                         "在 Claude Desktop 里按发送")
        self._primed_at = self._now()
