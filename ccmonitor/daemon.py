"""The daemon: receives hook payloads from hooks/relay.py and classifies them. Run by the
GUI through Service (no entry point of its own since #71; see ccmonitor.gui).

Listens on 127.0.0.1 only. Payload contents stay in memory; the log records only
event type, time and error kinds (DESIGN.md 3.6).
"""
import asyncio
import json
import logging
import logging.handlers
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import paths
from .away import Presence
from .classify import Classifier
from .config import load_config
from .desktop import DesktopWriteBack
from .health import Health
from .helper import HelperRelay
from .notice import Notice
from .pending import PendingPrompts
from .relaymode import DESCRIBE, MODES, RelayMode
from .rotation import HelperRotation
from .options import AWAY_INSTRUCTION, OPTIONS_INSTRUCTION
from .sidechannel import SideChannel
from .sessions import find_title
from .stuck import StuckWatcher
from .telegram import BotApi, TelegramError, UpdatePoller
from .topics import TOPICS_FILE, TopicMap

DEFAULT_PORT = 47821
CHECK_SECONDS = 60
TOOL_EVENTS = {"PostToolUse", "PostToolUseFailure"}
PRESENCE_SECONDS = 15
SWEEP_SECONDS = 3600
MAX_BYTES = 4 * 1024 * 1024
LOG_FILE = "daemon.log"  # in the storage directory, which can move (#70)
LOG_KEEP_DAYS = 3  # user's call, 2026-10-14: older log files are deleted

log = logging.getLogger("ccmonitor")

# UserPromptSubmit hook output: adds the options line to the model's context (DESIGN.md 3.2).
def _context_reply(text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}


AWAY_REPLY = _context_reply(AWAY_INSTRUCTION)
OPTIONS_REPLY = _context_reply(OPTIONS_INSTRUCTION)


class Daemon:
    def __init__(self, port: int, sink: Callable[[Notice], None], classifier: Classifier | None = None,
                 presence: Presence | None = None, health: Health | None = None,
                 helper: HelperRelay | None = None, on_turn_end: Callable[[str], None] | None = None,
                 relay_mode: RelayMode | None = None,
                 on_session_event: Callable[[str, str, dict], None] | None = None):
        self.port = port
        self._sink = sink
        self._classifier = classifier or Classifier()
        self._presence = presence
        self._was_away: bool | None = None  # last away verdict, for logging transitions
        self._health = health
        self._helper = helper
        self._on_turn_end = on_turn_end  # a turn ended: held prompts for that session may go now
        self._relay_mode = relay_mode
        self._on_session_event = on_session_event  # (hook event, session, payload): stale buttons come off
        self._watcher = StuckWatcher(self._classifier)
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_connection, "127.0.0.1", self.port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await asyncio.wait_for(_read_all(reader), timeout=5)
            payload = json.loads(data.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload is not an object")
            if payload.get("ccmonitor") == "ack":
                if self._helper:
                    self._helper.ack(payload.get("id"), str(payload.get("result", "")))
                return
            if payload.get("ccmonitor") == "mode":
                writer.write(json.dumps(self._mode_request(payload.get("mode")), ensure_ascii=False).encode("utf-8"))
                await writer.drain()
                return
            if self._helper and self._helper.is_helper(str(payload.get("session_id") or "")):
                return  # the helper's own turns are plumbing, not something to notify about
            if payload.get("hook_event_name") == "UserPromptSubmit":
                # Options only while away: at the desk nothing goes to Telegram, and the
                # options would only crowd Desktop's own suggestion (DESIGN.md 3.2).
                away = self._away()
                reply = AWAY_REPLY if away else OPTIONS_REPLY if away is None else None
                if reply:
                    writer.write(json.dumps(reply, ensure_ascii=False).encode("utf-8"))
                    await writer.drain()
        except Exception as e:
            log.warning("dropped payload: %s", type(e).__name__)
            return
        finally:
            writer.close()
        if payload.get("hook_event_name") not in TOOL_EVENTS:  # one per tool call: too many to log
            log.info("event %s", payload.get("hook_event_name"))
        try:
            self._watcher.on_event(payload)
            if self._on_turn_end and payload.get("hook_event_name") in ("Stop", "StopFailure"):
                self._on_turn_end(str(payload.get("session_id", "")))
            if self._on_session_event:  # before the sink, so the event's own notice keeps its buttons
                self._on_session_event(str(payload.get("hook_event_name", "")), str(payload.get("session_id", "")),
                                       payload)
            notice = self._classifier.handle(payload)
            if notice:
                self._sink(notice)
        except Exception:
            log.exception("failed to handle %s", payload.get("hook_event_name"))

    def _mode_request(self, mode) -> dict:
        """python -m ccmonitor.relaymode: switch (mode a|b) or just report (no mode)."""
        if self._relay_mode and mode in MODES:
            self._relay_mode.set(mode)
            log.info("relay mode %s", mode)
        return {"mode": self._relay_mode.mode if self._relay_mode else "a", "helper": helper_state(self._helper)}

    async def watch_stuck(self, every: float = CHECK_SECONDS) -> None:
        while True:
            await asyncio.sleep(every)
            try:
                for notice in self._watcher.check():  # on the loop: on_event shares its state
                    log.info("stuck turn")
                    self._sink(notice)
            except Exception as e:
                log.warning("stuck check failed: %s", type(e).__name__)

    def _away(self) -> bool | None:
        """None when presence can't be read: notices still go out then (sidechannel), so
        the turn still gets options, without claiming the user is away."""
        try:
            away = bool(self._presence and self._presence.is_away())
            if away != self._was_away:  # transitions only, to trace a wrong verdict later
                log.info("presence %s", "away" if away else "at desk")
                self._was_away = away
            return away
        except Exception as e:
            log.warning("presence check failed: %s", type(e).__name__)
            if self._health:
                self._health.report("presence", "读不到键鼠空闲时间，离开状态只认 /away")
            return None


@dataclass
class Live:
    """A running daemon's parts the status page reads (#67), filled in by _serve before it
    listens. side, bridge and bot_username stay None while Telegram is not configured;
    bot_username also when getMe failed. The away state is the GUI's own Presence (#69),
    so it is not repeated here."""
    relay_mode: RelayMode | None = None
    helper: HelperRelay | None = None
    side: SideChannel | None = None       # .paused: /pause
    bridge: "TelegramBridge | None" = None  # .telegram_down
    bot_username: str | None = None       # getMe's username, without the @


def helper_state(helper: HelperRelay | None) -> str:
    if helper and helper.connected:
        return "已连接"
    if helper and helper.paused_title:
        return "换班中"
    return "未连接"


async def _read_all(reader: asyncio.StreamReader) -> bytes:
    """Read until the relay closes its end; the relay sends one payload per connection."""
    chunks, size = [], 0
    while chunk := await reader.read(65536):
        size += len(chunk)
        if size > MAX_BYTES:
            raise ValueError("payload too large")
        chunks.append(chunk)
    return b"".join(chunks)


def print_notice(n: Notice) -> None:
    """Sink when Telegram is not configured: the console, never a file."""
    if sys.stdout is None:  # pythonw: no console, nowhere to show it
        return
    print(f"[{n.level.value}/{n.kind}] {n.session_label}\n{n.text}")
    for i, o in enumerate(n.options, 1):
        print(f"  ({i}) {o}")
    print(flush=True)


class TelegramBridge:
    """Runs the side channel on one worker thread so its in-memory state needs no locks.

    Also notices Telegram outages. Telegram is the reminder channel itself, so an
    outage is reported to Health once requests succeed again.
    """

    def __init__(self, side: SideChannel | None, api: BotApi):
        self._side = side
        self._api = api
        self._poller = UpdatePoller(api, lambda u: self._run(self._side.handle_update, u))
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sidechannel")
        self.health: Health | None = None
        self._lock = threading.Lock()
        self._down_since: float | None = None
        self._down_count = 0
        self._closed = False

    def attach(self, side: SideChannel) -> None:
        self._side = side

    @property
    def telegram_down(self) -> bool:
        """The last Telegram request failed and none has succeeded since."""
        return self._down_since is not None

    def say(self, text: str) -> None:
        self._run(self._side.say, text)

    def session_event(self, event: str, session_id: str, payload: dict | None = None) -> None:
        # Queued straight on the worker (not via later), so it runs before the notice
        # the same event produces.
        self._run(self._side.session_event, event, session_id, payload)

    def greet(self, text: str) -> None:
        self._run(self._side.greet, text)

    def later(self, seconds: float, fn) -> None:
        """Run fn on the worker after `seconds`, without holding the worker meanwhile."""
        timer = threading.Timer(seconds, self._run, (fn,))
        timer.daemon = True
        timer.start()

    def notify(self, notice: Notice) -> None:
        self._run(self._side.notify, notice)

    def close(self) -> None:
        """The daemon stopped: drop whatever is still queued or timed, and send nothing more."""
        self._closed = True
        self._worker.shutdown(wait=False, cancel_futures=True)

    def _run(self, fn, *args) -> None:
        if self._closed:
            return
        def safe():
            try:
                fn(*args)
            except TelegramError as e:
                self._telegram_failed(fn.__name__, e)
                return
            except Exception as e:
                log.warning("%s failed: %s", fn.__name__, type(e).__name__)
                return
            self._telegram_ok()
        try:
            self._worker.submit(safe)
        except RuntimeError:
            pass  # closed meanwhile

    def _telegram_failed(self, what: str, error) -> None:
        log.warning("%s failed: %s", what, error)
        with self._lock:
            if self._down_since is None:
                self._down_since = time.monotonic()
            self._down_count += 1

    def _telegram_ok(self) -> None:
        with self._lock:
            if self._down_since is None:
                return
            minutes = max(1, round((time.monotonic() - self._down_since) / 60))
            count, self._down_since, self._down_count = self._down_count, None, 0
        if self.health:
            self.health.report("telegram", f"Telegram 曾不可用约 {minutes} 分钟（{count} 次请求失败），期间的通知可能丢失")

    async def poll_forever(self) -> None:
        delay = 5
        while True:
            try:
                await asyncio.to_thread(self._poller.poll_once)
                delay = 5
                self._telegram_ok()
            except Exception as e:
                self._telegram_failed("polling", e if isinstance(e, TelegramError) else type(e).__name__)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def start(self) -> None:
        try:
            await asyncio.to_thread(self._poller.skip_backlog)
        except Exception as e:
            self._telegram_failed("skip_backlog", e if isinstance(e, TelegramError) else type(e).__name__)
        self.greet("ccmonitor 已启动")

    async def get_me(self) -> dict | None:
        """The bot's getMe (username, has_topics_enabled, ...), or None when it failed;
        a failure counts as Telegram down until a later request succeeds, so the status
        page does not say 已连接 meanwhile."""
        try:
            me = await asyncio.to_thread(self._api.call, "getMe")
        except Exception as e:
            self._telegram_failed("getMe", e if isinstance(e, TelegramError) else type(e).__name__)
            return None
        self._telegram_ok()
        return me if isinstance(me, dict) else None


def _topic_map(me: dict | None) -> TopicMap | None:
    """One topic per session when the bot has threaded mode on (getMe.has_topics_enabled)."""
    if me and me.get("has_topics_enabled"):
        log.info("telegram threaded mode on: one topic per session")
        return TopicMap(paths.DATA_DIR / TOPICS_FILE)
    return None


async def _serve(port: int, stop: asyncio.Event | None = None,
                 ready: Callable[[OSError | None], None] = lambda error: None,
                 config: Callable[[], object] = load_config,
                 console: Callable[[Notice], None] = print_notice,
                 presence: Presence | None = None, live: Live | None = None) -> None:
    """Runs until `stop` is set, then closes the hook listener, Telegram polling and every
    periodic task. `ready` hears once: None when listening, or the OSError of a busy port.
    `presence`: the away state, owned by the caller so its threshold can change while
    running (#69); a default Presence when None. `live` is filled in before `ready`, for
    the status page (#67)."""
    stop = stop or asyncio.Event()
    live = live if live is not None else Live()
    cfg = config()
    relay_mode = RelayMode()
    log.info("relay mode %s", relay_mode.mode)
    presence = presence or Presence()
    started = time.time()
    background = []
    helper = HelperRelay(asyncio.get_running_loop())
    try:
        await helper.start(port + 1)
    except OSError:
        log.error("helper port %d is busy; helper relay off", port + 1)
        helper = None
    live.relay_mode, live.helper = relay_mode, helper
    if cfg:
        api = BotApi(cfg.telegram_token)
        bridge = TelegramBridge(None, api)
        health = Health(bridge.say)
        write_back = DesktopWriteBack(on_failure=lambda status: health.report("write", status),
                                      on_own_input=presence.ignore_input,
                                      on_own_run=(presence.begin_own_run, presence.end_own_run),
                                      fallback=lambda sid, text, code: fallback(sid, text, code),
                                      on_suggest_failure=lambda status: health.report("suggest", status))
        pending = PendingPrompts(write_back.try_prompt, bridge.say, bridge.later, presence.is_away)

        def fallback(sid: str, text: str, code: str) -> str:
            # Mode B forwards through the helper session (docs/helper-session.md); mode A, or
            # B with no helper connected, holds the prompt and sends it itself.
            if relay_mode.mode == "b" and helper and helper.connected:
                return f"改走转发会话：{helper.send_prompt(sid, text)}"
            return pending.hold(sid, text, code)

        def on_mode(arg: str | None) -> str:
            if arg in MODES:
                relay_mode.set(arg)
                log.info("relay mode %s", arg)
            state = helper_state(helper)
            text = f"转发模式：{DESCRIBE[relay_mode.mode]}；转发会话：{state}"
            if relay_mode.mode == "b" and state == "未连接":
                text += ("\n还没有转发会话，消息先按转发模式 A 排队。到电脑上打开 ccmonitor 窗口，在状态页点"
                         "「新建转发会话」，再在 Claude Desktop 里按发送")
            elif relay_mode.mode == "a" and state != "未连接":
                text += "\n转发模式 A 不用转发会话，可以到电脑上归档「ccmonitor 转发」"

            return text

        me = await bridge.get_me()

        side = SideChannel(api, cfg.chat_id, write_back, presence, topics=_topic_map(me),
                           later=bridge.later)
        live.side, live.bridge, live.bot_username = side, bridge, (me or {}).get("username")
        side.status_extra = lambda: (f"已运行 {(time.time() - started) / 3600:.1f} 小时；{health.summary()}；"
                                     f"排队待发：{pending.count()} 条；"
                                     f"转发模式：{relay_mode.mode.upper()}；转发会话：{helper_state(helper)}")
        side.on_mode = on_mode
        rotation = HelperRotation(helper, write_back.try_prompt_title, find_title, presence.is_away,
                                  lambda: relay_mode.mode, lambda text: health.report("rotation", text)) if helper else None
        bridge.attach(side)
        bridge.health = health
        sink = bridge.notify
        on_turn_end = lambda sid: bridge.later(0, lambda: pending.turn_ended(sid))
        on_session_event = bridge.session_event
        log.info("telegram side channel on")
    else:
        bridge = rotation = None
        health = Health(lambda text: None)
        sink = console
        on_turn_end = on_session_event = None
        log.info("telegram not configured; notices go to the console only")
    daemon = Daemon(port, sink, Classifier(title_of=find_title), presence, health, helper, on_turn_end, relay_mode,
                    on_session_event)
    try:
        await daemon.start()
    except OSError as e:
        log.error("port %d is busy; is another ccmonitor daemon running?", port)
        if helper:
            await helper.stop()
        if bridge:
            bridge.close()
        ready(e)
        return
    log.info("listening on 127.0.0.1:%d", daemon.port)
    ready(None)
    if bridge:
        # Telegram's startup calls come after listening, to keep the restart gap short:
        # hooks that fire while nobody listens retry only briefly (hooks/relay.py).
        await bridge.start()
        background.append(asyncio.create_task(bridge.poll_forever()))
    background.append(asyncio.create_task(daemon.watch_stuck()))
    if bridge and rotation:
        background.append(asyncio.create_task(_rotate_forever(bridge, rotation)))
    if bridge:
        background.append(asyncio.create_task(_watch_presence(bridge, side)))
        background.append(asyncio.create_task(_sweep_topics_forever(bridge, side)))
        background.append(asyncio.create_task(_sweep_answered_forever(bridge, side)))
    try:
        await stop.wait()
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await daemon.stop()
        if helper:
            await helper.stop()
        if bridge:
            bridge.close()
        log.info("stopped")


class Service:
    """The daemon on a background thread of its own, so the GUI can start (enable) and
    stop (disable) it within one process. Stopping frees the port; hooks that fire
    meanwhile give up quickly, as for a daemon that isn't running (hooks/relay.py)."""

    def __init__(self, port: int = DEFAULT_PORT, config: Callable[[], object] = load_config,
                 console: Callable[[Notice], None] = print_notice, presence: Presence | None = None):
        self.port = port
        self._config = config
        self._console = console  # the sink while Telegram is not configured
        self._presence = presence  # the GUI's, kept across restarts (#69); None: a fresh one each start
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self.live = Live()  # a fresh one each start, filled in by _serve

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, timeout: float = 10) -> None:
        """Returns once listening. Raises the OSError when the port is busy."""
        if self.running:
            return
        loop, stop = asyncio.new_event_loop(), asyncio.Event()
        started, outcome = threading.Event(), []
        live = self.live = Live()

        def ready(error: BaseException | None) -> None:
            outcome.append(error)
            started.set()

        def run() -> None:
            try:
                loop.run_until_complete(_serve(self.port, stop=stop, ready=ready, config=self._config,
                                               console=self._console, presence=self._presence,
                                               live=live))
            except Exception as e:
                log.exception("daemon failed")
                if not started.is_set():
                    ready(e)
            finally:
                # Doesn't wait for a Telegram long poll still in its thread; its update is dropped.
                loop.close()

        self._loop, self._stop = loop, stop
        self._thread = threading.Thread(target=run, name="daemon", daemon=True)
        self._thread.start()
        if not started.wait(timeout):
            self.stop()
            raise TimeoutError("daemon did not start")
        if outcome[0]:
            self._thread.join(timeout)
            self._thread = None
            raise outcome[0]

    def stop(self, timeout: float = 10) -> None:
        """Returns once the listener, polling and periodic tasks are closed."""
        if not self._thread:
            return
        try:
            self._loop.call_soon_threadsafe(self._stop.set)
        except RuntimeError:
            pass  # the loop already ended on its own
        self._thread.join(timeout)
        self._thread = None


async def _rotate_forever(bridge: TelegramBridge, rotation: HelperRotation) -> None:
    """Mode B's helper rotation, checked every minute on the side channel's worker."""
    while True:
        await asyncio.sleep(CHECK_SECONDS)
        bridge.later(0, rotation.check)


async def _watch_presence(bridge: TelegramBridge, side: SideChannel) -> None:
    """Keeps the phone's /away button in step with the away state."""
    while True:
        await asyncio.sleep(PRESENCE_SECONDS)
        bridge.later(0, side.check_presence)


async def _sweep_topics_forever(bridge: TelegramBridge, side: SideChannel) -> None:
    """Deletes topics idle for 3 days; first a minute after start, then hourly."""
    await asyncio.sleep(CHECK_SECONDS)
    while True:
        bridge.later(0, side.sweep_topics)
        await asyncio.sleep(SWEEP_SECONDS)


async def _sweep_answered_forever(bridge: TelegramBridge, side: SideChannel) -> None:
    """Takes buttons off approvals denied in Desktop, which no hook reports; every minute."""
    while True:
        await asyncio.sleep(CHECK_SECONDS)
        bridge.later(0, side.sweep_answered)


LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"


def _prune_logs(path: Path, now: Callable[[], float] = time.time) -> None:
    """Deletes rotated logs untouched for LOG_KEEP_DAYS. Rollover only prunes when it
    happens, so this also catches days the daemon was off and the old size-rotated
    daemon.log.1/.2."""
    cutoff = now() - LOG_KEEP_DAYS * 86400
    for old in path.parent.glob(path.name + ".*"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass


def _log_file(data_dir: Path) -> logging.Handler:
    """daemon.log, rotated at midnight to daemon.log.YYYY-MM-DD; LOG_KEEP_DAYS kept."""
    path = Path(data_dir) / LOG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    _prune_logs(path)
    handler = logging.handlers.TimedRotatingFileHandler(path, when="midnight", backupCount=LOG_KEEP_DAYS,
                                                        encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    return handler


def setup_logging() -> None:
    """Event types, times and error kinds only (DESIGN.md 3.6); never payload content."""
    handlers: list[logging.Handler] = [_log_file(paths.DATA_DIR)]
    if sys.stderr is not None:  # absent under pythonw
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        handlers.append(handler)
    logging.basicConfig(level=logging.INFO, handlers=handlers)


def move_log(data_dir: Path | None, logger: logging.Logger | None = None) -> None:
    """Closes the log file (Windows won't move an open file) and, given a directory,
    opens daemon.log there instead: around a storage directory move (#70)."""
    logger = logger or logging.getLogger()
    for handler in [h for h in logger.handlers if isinstance(h, logging.FileHandler)]:
        logger.removeHandler(handler)
        handler.close()
    if data_dir is not None:
        logger.addHandler(_log_file(data_dir))
