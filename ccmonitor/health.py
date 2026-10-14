"""Failure reminders (DESIGN.md 3.3): at most one per kind per day, otherwise silent.

Counts are kept in memory for /status; nothing is persisted.
"""
import datetime
import logging
import threading
from collections import Counter
from typing import Callable

log = logging.getLogger("ccmonitor")


class Health:
    def __init__(self, send: Callable[[str], None], today: Callable[[], datetime.date] = datetime.date.today):
        self._send = send
        self._today = today
        self._reminded: dict[str, datetime.date] = {}
        self._counts: Counter[str] = Counter()
        self._lock = threading.Lock()  # reported from the event loop and worker threads

    def report(self, kind: str, detail: str) -> None:
        log.warning("failure %s", kind)
        today = self._today()
        with self._lock:
            self._counts[kind] += 1
            if self._reminded.get(kind) == today:
                return
            self._reminded[kind] = today
        try:
            self._send(f"⚠️ ccmonitor 出现故障（{kind}）：{detail}\n今天同类故障不再提醒，/status 可看累计次数")
        except Exception as e:
            log.warning("failure reminder not sent: %s", type(e).__name__)

    def summary(self) -> str:
        if not self._counts:
            return "无故障"
        return "故障累计：" + "，".join(f"{k} {n}" for k, n in sorted(self._counts.items()))
