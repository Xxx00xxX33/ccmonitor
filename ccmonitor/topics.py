"""Session ↔ Telegram topic pairing for bots in threaded mode (topics in private chats).

Each Claude session gets one topic. The pairing is kept on disk so a restart does
not open duplicate topics; the file holds ids and the time of the topic's last
activity only (CLI session id → [topic id, epoch seconds]), no titles and nothing
from any session (DESIGN.md 3.6). Topics idle for STALE_SECONDS are deleted by the
side channel (user's call, 2026-10-12).
"""
import json
import logging
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Callable

log = logging.getLogger("ccmonitor")

TOPICS_FILE = "topics.json"  # in the storage directory
MAX_TOPICS = 500
STALE_SECONDS = 3 * 24 * 3600


class TopicMap:
    def __init__(self, path: Path | None = None, clock: Callable[[], float] = time.time):
        self._path = path
        self._clock = clock
        self._threads: OrderedDict[str, int] = OrderedDict()
        self._active: dict[str, float] = {}  # session -> last notice or user action in its topic
        if path and path.exists():
            upgraded = False
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                for k, v in data.items():
                    # Files from before 2026-10-12 hold the topic id alone: count from now,
                    # and save that at once, or each restart would start the count again.
                    upgraded |= isinstance(v, int)
                    thread, active = (v, clock()) if isinstance(v, int) else v
                    self._threads[str(k)] = int(thread)
                    self._active[str(k)] = float(active)
            except (OSError, ValueError, AttributeError, TypeError):
                log.warning("topics file unreadable; starting empty")
                self._threads.clear()
                self._active.clear()
                upgraded = False
            if upgraded:
                self._save()

    def thread_of(self, session_id: str) -> int | None:
        return self._threads.get(session_id)

    def session_of(self, thread_id: int) -> str | None:
        for session_id, thread in self._threads.items():
            if thread == thread_id:
                return session_id
        return None

    def set(self, session_id: str, thread_id: int) -> None:
        self._threads.pop(session_id, None)
        self._threads[session_id] = thread_id
        self._active[session_id] = self._clock()
        while len(self._threads) > MAX_TOPICS:
            self._active.pop(self._threads.popitem(last=False)[0], None)
        self._save()

    def touch(self, session_id: str) -> None:
        """Something happened in the session's topic: a notice, or the user acting there."""
        if session_id in self._threads:
            self._active[session_id] = self._clock()
            self._save()

    def stale(self, max_age: float = STALE_SECONDS) -> list[tuple[str, int]]:
        """(session, topic) pairs with no activity for max_age seconds."""
        cutoff = self._clock() - max_age
        return [(s, t) for s, t in self._threads.items() if self._active.get(s, 0) <= cutoff]

    def forget(self, session_id: str) -> None:
        self._active.pop(session_id, None)
        if self._threads.pop(session_id, None) is not None:
            self._save()

    def _save(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            data = {s: [t, round(self._active.get(s, 0))] for s, t in self._threads.items()}
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as e:
            log.warning("topics file not saved: %s", type(e).__name__)
