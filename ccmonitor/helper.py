"""Helper-session relay (DESIGN.md 5, v2): a backup write path for prompts.

A resident Desktop session (docs/helper-session.md) runs hooks/relay_feed.py under
its Monitor tool. Each forward request is one JSON line on that feed; the helper
delivers it with Desktop's session-to-session messaging and reports back through
hooks/relay_ack.py. The helper cannot approve permission requests.

The feed listens on 127.0.0.1 only. Requests live in memory until acknowledged.

Used only in relay mode B (relaymode.py). The helper is rotated every 6 hours
(rotation.py): paused, cleared with /clear and re-primed with SETUP_PROMPT through UI
write-back. /clear gives it a new CLI session id, so while it is paused for rotation it
is recognised by its Desktop title instead.
"""
import asyncio
import itertools
import json
import logging
import threading
import time
from typing import Callable

from .relaymode import HELPER_TITLE
from .sessions import find_local_id, find_title, is_archived

log = logging.getLogger("ccmonitor")

ACK_SECONDS = 90
ARCHIVE_CHECK_SECONDS = 60
RESULTS = {
    "delivered": "已经辅助会话送达",
    "queued": "已经辅助会话排队：该会话正忙，这一轮结束后执行",
}


class HelperRelay:
    def __init__(self, loop: asyncio.AbstractEventLoop,
                 find_local_id: Callable[[str], str | None] = find_local_id, ack_seconds: float = ACK_SECONDS,
                 title_of: Callable[[str], str | None] = find_title, now: Callable[[], float] = time.monotonic,
                 archived: Callable[[str], bool] = is_archived, archive_check_seconds: float = ARCHIVE_CHECK_SECONDS):
        self._loop = loop
        self._find_local_id = find_local_id
        self._ack_seconds = ack_seconds
        self._title_of = title_of
        self._now = now
        self._archived = archived
        self._archive_check_seconds = archive_check_seconds
        self._watch: asyncio.Task | None = None
        self.since: float | None = None      # when the current helper context started serving
        self.paused_title: str | None = None  # set while rotating: the helper's Desktop title
        self._ids = itertools.count(1)
        self._pending: dict[int, list] = {}  # id -> [threading.Event, result]
        self._writer: asyncio.StreamWriter | None = None
        self._server: asyncio.Server | None = None
        self.session_id: str | None = None  # the helper's own CLI session id
        self.port = 0

    @property
    def connected(self) -> bool:
        """A feed is attached and the helper may take forwards."""
        return self._writer is not None and self.paused_title is None

    @property
    def attached(self) -> bool:
        return self._writer is not None

    def pause(self, title: str) -> None:
        """Stop handing out forwards while the helper is cleared and re-primed."""
        self.paused_title = title

    def resume(self) -> None:
        self.paused_title = None

    def is_helper(self, session_id: str) -> bool:
        """Events from the helper are plumbing, not something to notify about. Before its
        feed connects (its first turn) and after /clear its id is unknown, so it is also
        recognised by its Desktop title."""
        if not session_id:
            return False
        if session_id == self.session_id:
            return True
        title = self._title_of(session_id)
        return bool(title) and title in (self.paused_title, HELPER_TITLE)

    async def start(self, port: int) -> None:
        self._server = await asyncio.start_server(self._on_feed, "127.0.0.1", port)
        self.port = self._server.sockets[0].getsockname()[1]
        self._watch = asyncio.create_task(self._watch_archive())

    async def stop(self) -> None:
        if self._watch:
            self._watch.cancel()
        if self._writer:
            self._writer.close()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _on_feed(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            hello = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
        except Exception as e:
            log.warning("bad helper handshake: %s", type(e).__name__)
            writer.close()
            return
        if self._writer:
            self._writer.close()  # a re-armed Monitor replaces the old feed
        sid = hello.get("session_id")
        if self.paused_title or sid != self.session_id or self.since is None:
            self.since = self._now()  # a fresh context: new helper, or one just rotated
            if self.paused_title:
                log.info("helper session rotated")
        self.paused_title = None
        self._writer, self.session_id = writer, sid
        log.info("helper session connected")
        try:
            await reader.read()  # returns when the feed process exits (Monitor expiry, session closed)
        except ConnectionError:
            pass  # a killed feed process resets the connection instead of closing it
        finally:
            if self._writer is writer:
                self._writer = None
                log.info("helper session disconnected")
            writer.close()

    async def _watch_archive(self) -> None:
        while True:
            await asyncio.sleep(self._archive_check_seconds)
            try:
                await asyncio.to_thread(self.drop_if_archived)
            except Exception as e:
                log.warning("helper archive check failed: %s", type(e).__name__)

    def drop_if_archived(self) -> bool:
        """Archiving the helper in Desktop doesn't end its feed process (seen 2026-10-14:
        the feed stayed connected, so ccmonitor kept counting a session nobody runs as its
        helper). Once Desktop marks the helper archived, close the feed: forwards fall back
        to mode A, the window offers 「新建转发会话」 again, and the orphaned feed process
        sees the connection end and exits. Not while rotating, when the id is changing."""
        writer, sid = self._writer, self.session_id
        if writer is None or self.paused_title or not sid or not self._archived(sid):
            return False
        log.info("helper session archived; dropping its feed")
        if self._writer is writer:
            self._writer = None
        self._loop.call_soon_threadsafe(writer.close)
        return True

    def ack(self, request_id, result: str) -> None:
        entry = self._pending.get(request_id)
        if entry:
            entry[1] = result
            entry[0].set()

    async def _write(self, line: bytes) -> None:
        if not self._writer:
            raise ConnectionError("no feed")
        self._writer.write(line)
        await self._writer.drain()

    # Called from the side channel's worker thread.
    def send_prompt(self, session_id: str, text: str) -> str:
        if self.is_helper(session_id):
            return "未转发：目标就是转发会话自己"
        local_id = self._find_local_id(session_id)
        if not local_id:
            return "辅助会话未转发：找不到这个会话的 Desktop id"
        if not self.connected:
            return "辅助会话未连接，未转发"
        rid = next(self._ids)
        done = threading.Event()
        self._pending[rid] = [done, None]
        line = (json.dumps({"id": rid, "to": local_id, "text": text}, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            asyncio.run_coroutine_threadsafe(self._write(line), self._loop).result(5)
            if not done.wait(self._ack_seconds):
                return "已交给辅助会话，但没收到回执，请到电脑上确认"
            result = self._pending[rid][1] or ""
        except Exception as e:
            log.warning("helper forward failed: %s", type(e).__name__)
            return "辅助会话未连接，未转发"
        finally:
            self._pending.pop(rid, None)
        log.info("helper forward %s", result.split(":")[0])
        return RESULTS.get(result, f"辅助会话转发失败（{result[:80]}）")

    def approve(self, session_id: str, allow: bool, expect: str = "") -> str:
        return "辅助会话不能审批"
