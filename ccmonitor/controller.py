"""The controller (#63): the one object the GUI calls, and the feature's only test seam.

No tkinter here. The window and the tray only draw what `snapshot()` returns and call
its methods; everything they show or change goes through this class. Whatever touches
the outside world is injected, so tests run against a temporary storage directory, a
fake registry, a fake Telegram API, a fake daemon, a fixed clock and a scripted input
source.
"""
import functools
import json
import logging
import os
import shutil
import subprocess
import threading
import time

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from .away import Presence
from . import autostart, legacy, paths, settings
from .relaymode import HELPER_STEPS, MODE_FILE, SETUP_PROMPT, load_mode, save_mode

log = logging.getLogger("ccmonitor")

RUNNING, DISABLED, ERROR = "运行中", "已停用", "出错"
UNCONFIGURED = "未配置"  # no bot token or chat id: cannot be enabled (#68)
AUTOSTART_STATES = {autostart.OFF: "关", autostart.ON: "开", autostart.STALE: "指向旧位置"}


@dataclass(frozen=True)
class Snapshot:
    state: str          # RUNNING / DISABLED / ERROR / UNCONFIGURED
    detail: str         # one line under the state; "" when there is nothing to add
    enabled: bool = False    # the daemon is running: the 启用/停用 button would 停用
    can_toggle: bool = True  # False while unconfigured: the button is greyed out
    # The status page (#67): one line each, already worded for the window.
    telegram: str = "未连接"
    away: str = "—"
    notifications: str = "—"
    relay_mode: str = "a"   # "a" or "b" (GLOSSARY: 转发模式)
    helper: str = "未连接"  # the helper session: 已连接 / 换班中 / 未连接
    autostart: str = autostart.OFF  # 开机自启: autostart.OFF / ON / STALE (#71)

    @property
    def toggle_label(self) -> str:
        """The 启用/停用 button and tray item: what pressing it does."""
        return "停用" if self.enabled else "启用"

    @property
    def needs_helper(self) -> bool:
        """Mode B with no helper session: show the steps and 「复制启动指令」."""
        return self.relay_mode == "b" and self.helper == "未连接"

    @property
    def autostart_label(self) -> str:
        return AUTOSTART_STATES[self.autostart]


@dataclass(frozen=True)
class StorageMove:
    """What changing the 存储目录 to `target` would do, shown before it is confirmed."""
    target: Path
    files: int             # how many files move
    config_conflict: bool  # the target already has a config.json: ask which one to keep


@dataclass(frozen=True)
class MoveResult:
    moved: int        # files now only in the new directory
    left: list[str]   # files that could not be deleted from the old one (relative paths)


def _locked(method):
    """Runs under the controller's lock: the window calls the blocking methods from a
    worker thread, and the 「获取 Chat ID」 and a later launch's 退出 may come meanwhile."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


def _unlink(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _same(a: Path, b: Path) -> bool:
    return _key(a) == _key(b)


def _inside(path: Path, folder: Path) -> bool:
    return _key(path).startswith(_key(folder).rstrip("\\/") + os.sep)


TOKEN_ENV, CHAT_ID_ENV = "CCMONITOR_TELEGRAM_TOKEN", "CCMONITOR_CHAT_ID"


@dataclass(frozen=True)
class TelegramSettings:
    """What the Telegram tab's fields show. A field set by an environment variable shows
    that value, read-only, with the variable's name as its source."""
    token: str = field(repr=False)
    chat_id: str
    token_env: str = ""     # the variable overriding the token; "" when the file decides
    chat_id_env: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)


@dataclass(frozen=True)
class TelegramResult:
    """The outcome of a Telegram tab button: one line to show under the buttons."""
    ok: bool
    message: str
    username: str = ""        # 测试连接: the bot's @username
    topics: bool = False      # 测试连接: getMe.has_topics_enabled
    chat_id: str = ""         # 获取 Chat ID: the id to fill in


NO_TOKEN = TelegramResult(False, "请先填写 bot token")
BAD_CHAT_ID = TelegramResult(False, "chat id 应是一个整数（群组为负数）")


def _chat_id(text: str) -> int | None:
    """The chat id field as a number; None when it isn't one (a group's is negative)."""
    try:
        return int(text.strip())
    except ValueError:
        return None



def _default_service(port: int, presence: Presence | None = None):
    from .daemon import Service  # imported late: the daemon pulls in everything
    return Service(port, presence=presence)


class ProcessFiles:
    """This process's hold on the storage directory: the open daemon.log and paths.DATA_DIR.
    Windows will not move an open file, so the log is closed before a move and reopened
    in the new place after it."""

    def release(self) -> None:
        from .daemon import move_log
        move_log(None)

    def attach(self, data_dir: Path) -> None:
        from .daemon import move_log
        paths.use(data_dir)
        move_log(data_dir)


class Controller:
    def __init__(self, *, port: int | None = None,
                 data_dir: Path | None = None,
                 registry=None,
                 telegram_api: Callable[[str], object] | None = None,
                 service_factory: Callable[[int], object] = _default_service,
                 clock: Callable[[], float] = time.time,
                 last_input: Callable[[], int] | None = None,
                 env: Mapping[str, str] = os.environ,
                 open_file: Callable[[list[str]], object] = subprocess.Popen,
                 ini_path: Path | None = None,
                 files=None,
                 autostart_command: str | None = None,
                 processes: Callable[[], list] | None = None,
                 kill: Callable[[int], None] | None = None):
        """port: the hook port (daemon.DEFAULT_PORT when None); data_dir: the storage
        directory (when None: CCMONITOR_HOME in `env`, else DataDir in `ini_path`
        (paths.INI_PATH when None), else the default); registry: the HKCU Run store for
        autostart; telegram_api: token -> BotApi; service_factory: (port, presence=) -> an
        object with start()/stop()/running and .live (daemon.Live), like daemon.Service;
        clock: seconds; last_input: the input source for the away state (away._last_input
        when None); env: where CCMONITOR_HOME and the CCMONITOR_TELEGRAM_TOKEN /
        CCMONITOR_CHAT_ID overrides are read; open_file: runs a command line without
        waiting, for 「打开日志」; files: what holds the storage directory open in this
        process, release()/attach(dir) around a move (ProcessFiles when None);
        autostart_command: the Run value 开机自启 writes (autostart.default_command() when
        None); processes, kill: list running processes as legacy.Proc and end one by pid
        (legacy.py when None)."""
        if port is None:
            from .daemon import DEFAULT_PORT
            port = DEFAULT_PORT
        self.port = port
        self.env = env
        self.ini_path = ini_path or paths.INI_PATH
        self.files = files or ProcessFiles()
        self.data_dir = data_dir or paths.resolve_data_dir(env, self.ini_path)
        self.registry = registry
        self.autostart_command = autostart_command or autostart.default_command()
        self.telegram_api = telegram_api
        self.clock = clock
        self.last_input = last_input
        self._make_service = service_factory
        self._open_file = open_file
        self._alerted = False  # the current 出错 state has had its balloon
        self._service = None
        self._error = ""  # why the last enable failed
        self._list_processes = processes or legacy.pythonw_processes
        self._kill = kill or legacy.kill
        self._legacy: list[int] = []  # pids of the old daemon found holding the port
        self._fetching = False  # a 「获取 Chat ID」 is waiting for Telegram
        self._lock = threading.RLock()
        # The away state, kept here and handed to every daemon start, so a new 离开时间
        # reaches the running daemon at once (#69).
        self.away_minutes = settings.away_minutes(settings.load(self.data_dir))
        if last_input is None:
            self.presence = Presence(idle_seconds=self.away_minutes * 60)
        else:
            self.presence = Presence(idle_seconds=self.away_minutes * 60, last_input=last_input,
                                     now=lambda: int(clock() * 1000) & 0xFFFFFFFF)

    def _chose_disabled(self) -> bool:
        """The user's last 启用/停用 was 停用 (remembered in settings.json)."""
        return settings.load(self.data_dir).get("enabled", True) is False

    def restore(self) -> Snapshot:
        """Opening ccmonitor (also at login): runs it unless the user last chose 停用."""
        if self._chose_disabled():
            return self.snapshot()
        return self._enable()

    def enable(self) -> Snapshot:
        """启用. Refused while 未配置 or while 「获取 Chat ID」 waits, and then not
        remembered as the user's choice."""
        if self._fetching or not self.telegram_settings().configured:
            return self.snapshot()
        self._save(enabled=True)
        return self._enable()

    def _running(self) -> bool:
        return bool(self._service and self._service.running)

    @_locked
    def _enable(self) -> Snapshot:
        if self._running():
            return self.snapshot()
        if self._fetching:  # it reads the bot's updates: the daemon's poll would race it
            return self.snapshot()
        self._stop()  # clears a daemon that ended on its own
        if not self.telegram_settings().configured:
            return self.snapshot()  # 未配置: neither 启用 nor opening ccmonitor runs it
        service = self._make_service(self.port, presence=self.presence)
        try:
            service.start()
        except OSError as e:
            log.error("daemon did not start: %s", type(e).__name__)
            if isinstance(e, TimeoutError):
                self._error = "启用超时：守护进程没有按时启动，详情见日志"
            else:
                self._legacy = [p.pid for p in self._list_processes() if legacy.is_old_daemon(p)]
                self._error = (f"旧的后台 ccmonitor（pythonw）正在运行，占用端口 {self.port}。"
                               "点「接管」结束它，由这里运行。" if self._legacy else
                               f"端口 {self.port} 被占用，可能已有另一个 ccmonitor 在运行")
        except Exception as e:
            log.exception("daemon did not start")
            self._error = f"启用失败：{type(e).__name__}"
        else:
            self._service, self._error, self._alerted = service, "", False
            self._legacy = []
        return self.snapshot()

    # -- 接管 the old pythonw daemon (#71) --------------------------------------------------
    def legacy_daemon(self) -> bool:
        """Whether the last start found the old `pythonw ccmonitor-daemon.pyw` on the port."""
        return bool(self._legacy)

    @_locked
    def take_over(self) -> Snapshot:
        """「接管」: ends only the old launcher's pythonw processes found then, and runs."""
        if self._fetching:
            return self.snapshot()
        for pid in self._legacy:
            self._kill(pid)
        self._legacy = []
        return self.enable()

    @_locked
    def disable(self) -> Snapshot:
        self._save(enabled=False)
        self._stop()
        self._error = ""
        return self.snapshot()

    @_locked
    def quit(self) -> None:
        """「退出 ccmonitor」: stops the daemon. Unlike disable(), it is not a choice to
        remember; the caller then ends the process."""
        self._stop()

    def _stop(self) -> None:
        # Dropped before it is stopped, so a snapshot taken meanwhile (the window's other
        # thread) sees 已停用, not a daemon that ended on its own.
        service, self._service = self._service, None
        if service:
            service.stop()

    def toggle(self) -> Snapshot:
        """The 启用/停用 button and tray item."""
        return self.disable() if self.snapshot().enabled else self.enable()

    # -- settings.json (#69) -------------------------------------------------------------
    @property
    def storage_dir(self) -> Path:
        """The 存储目录 shown on the settings tab."""
        return self.data_dir

    # -- 存储目录 (#70) -------------------------------------------------------------------
    @property
    def storage_locked(self) -> str:
        """Why the storage directory can't be changed here; "" when it can."""
        if self.env.get("CCMONITOR_HOME"):
            return "存储目录由环境变量 CCMONITOR_HOME 指定，要在这里更改请先去掉该变量"
        return ""

    def _files_to_move(self) -> list[Path]:
        """Every file in the storage directory except the hooks' relay-down mark, which
        stays where the hooks look for it (hooks/relay.py)."""
        mark = paths.relay_down_mark(self.env)
        try:
            found = [p for p in self.data_dir.rglob("*") if p.is_file()]
        except OSError:
            return []
        return [p for p in found if not _same(p, mark)]

    def plan_storage_move(self, target) -> StorageMove:
        """Checks a new storage directory before anything changes. ValueError, with the
        message to show, when it can't be used."""
        if self.storage_locked:
            raise ValueError(self.storage_locked)
        target = Path(target)
        if not target.is_absolute():
            raise ValueError("请选择一个完整路径的文件夹")
        if _same(target, self.data_dir):
            raise ValueError("这已经是当前的存储目录")
        if _inside(target, self.data_dir):
            raise ValueError("新目录不能放在当前存储目录里面")
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".ccmonitor-write-test"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as e:
            raise ValueError(f"新目录不可写：{target}（{type(e).__name__}）") from None
        return StorageMove(target, len(self._files_to_move()),
                           (target / "config.json").is_file())

    @_locked
    def move_storage(self, target, keep_config: str | None = None) -> MoveResult:
        """Moves every file to `target` and makes it the storage directory, in ccmonitor.ini.
        A running daemon is stopped first and started again after; 启用/停用 is unchanged.
        keep_config: when `target` already has a config.json, "old" (this one replaces it)
        or "new" (it stays and this one is deleted). ValueError when the plan is refused.

        Copies everything first and only then deletes the originals, so a failed copy
        leaves the old directory whole and in use."""
        plan = self.plan_storage_move(target)
        if plan.config_conflict and keep_config not in ("old", "new"):
            raise ValueError("新目录里已有 config.json，请选择保留哪一份")
        target, old = plan.target, self.data_dir
        files = self._files_to_move()
        was_running = self._running()
        self._stop()
        self.files.release()
        copied: list[Path] = []
        # keep_config "old" overwrites the target's own config.json: kept aside until the
        # move holds, and put back if it fails, so neither token is lost or left twice.
        target_config = target / "config.json"
        backup = target / "config.json.ccmonitor-move" if plan.config_conflict else None
        try:
            if backup and keep_config == "old":
                os.replace(target_config, backup)
            for src in files:
                rel = src.relative_to(old)
                if rel == Path("config.json") and plan.config_conflict and keep_config == "new":
                    continue
                dst = target / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied.append(dst)
            paths.write_ini_data_dir(target, self.ini_path)
        except OSError as e:
            log.error("storage move failed: %s", type(e).__name__)
            for dst in copied:
                _unlink(dst)
            if backup and backup.exists():
                try:
                    os.replace(backup, target_config)
                except OSError as e2:
                    log.error("target config.json not restored: %s", type(e2).__name__)
            self.files.attach(old)
            if was_running:
                self._enable()
            raise ValueError(f"移动失败，存储目录未变：{type(e).__name__}") from None
        if backup:
            _unlink(backup)
        left = [str(src.relative_to(old)) for src in files if not _unlink(src)]
        for folder in sorted({p.parent for p in files}, key=lambda p: len(p.parts), reverse=True):
            try:
                folder.rmdir()  # only once empty; the relay-down mark keeps its folder
            except OSError:
                pass
        self.data_dir = target
        self.files.attach(target)
        log.info("storage moved: %d files", len(files) - len(left))
        if left:
            log.warning("storage move left %d files behind", len(left))
        if was_running:
            self._enable()
        return MoveResult(len(files) - len(left), left)

    def _save(self, **changes) -> None:
        try:
            settings.save(self.data_dir, **changes)
        except OSError as e:  # the change still holds until the process ends
            log.warning("settings not saved: %s", type(e).__name__)

    def set_away_minutes(self, value) -> int:
        """The 离开时间 field: takes effect at once and is remembered. ValueError, with the
        message to show, for anything but whole minutes from 1 to 60."""
        minutes = settings.parse_away_minutes(value)
        self.away_minutes = minutes
        self.presence.set_idle_seconds(minutes * 60)
        self._save(away_minutes=minutes)
        return minutes

    # -- 开机自启 (#71) ------------------------------------------------------------------
    def _autostart(self) -> autostart.Autostart:
        if self.registry is None:
            self.registry = autostart.WinRegistry()
        return autostart.Autostart(self.registry, self.autostart_command)

    def _autostart_state(self) -> str:
        """autostart.OFF / ON / STALE (our Run value runs something else, e.g. the old
        .pyw); OFF when the Run key can't be read."""
        try:
            return self._autostart().state()
        except OSError as e:
            log.warning("autostart unreadable: %s", type(e).__name__)
            return autostart.OFF

    def set_autostart(self, on: bool) -> Snapshot:
        """The 开机自启 checkbox: writes `"<exe>" --tray` (replacing an old value) or
        removes it. Raises OSError when the Run key can't be written."""
        a = self._autostart()
        a.enable() if on else a.disable()
        return self.snapshot()

    def snapshot(self) -> Snapshot:
        live = self._live()
        both = {"autostart": self._autostart_state()}
        if live is not None:
            return Snapshot(RUNNING, "", enabled=True, **self._status(live), **both)
        # Not running: the status lines only say what is known without a daemon.
        stopped = {"relay_mode": load_mode(self._mode_path), **both}
        if self._error:
            return Snapshot(ERROR, self._error, **stopped)
        if self._service is not None:
            return Snapshot(ERROR, "守护进程意外停止，详情见日志", **stopped)
        if not self.telegram_settings().configured:
            return Snapshot(UNCONFIGURED, "先在「Telegram」页填好 bot token 和 chat id",
                            can_toggle=False, telegram="未配置", **stopped)
        if self._fetching:
            return Snapshot(DISABLED, "正在获取 Chat ID，结束后才能启用", can_toggle=False, **stopped)
        return Snapshot(DISABLED, "", **stopped)


    # -- the status page (#67) ----------------------------------------------------------
    def _live(self):
        """The running daemon's parts (daemon.Live), or None when it isn't running."""
        if self._error or self._service is None or not self._service.running:
            return None
        return self._service.live

    @property
    def _mode_path(self) -> Path:
        return self.data_dir / MODE_FILE

    def _status(self, live) -> dict:
        from .daemon import helper_state  # loaded already: a daemon is running
        return {"telegram": self._telegram(live), "away": self._away(self.presence),  # the daemon's too
                "notifications": self._notifications(live.side),
                "relay_mode": live.relay_mode.mode, "helper": helper_state(live.helper)}

    def alert(self) -> str | None:
        """The tray balloon: the reason, once, when ccmonitor has stopped and can't recover
        by itself (the 出错 state); None otherwise. Anything it recovers from by itself,
        like Telegram being unreachable, is only on the status page."""
        snap = self.snapshot()
        if snap.state != ERROR:
            self._alerted = False
            return None
        if self._alerted:
            return None
        self._alerted = True
        return snap.detail

    def open_log(self) -> None:
        """「打开日志」: the current storage directory's daemon.log, in Notepad."""
        path = self.data_dir / "daemon.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()  # else Notepad asks whether to create it
            self._open_file(["notepad.exe", str(path)])
        except OSError as e:
            log.warning("could not open the log: %s", type(e).__name__)

    helper_steps = HELPER_STEPS  # how to create the helper, on the 状态 page

    @staticmethod
    def setup_prompt() -> str:
        """「复制启动指令」: the first message of the helper session the user creates."""
        return SETUP_PROMPT

    def set_relay_mode(self, mode: str) -> Snapshot:
        """The A/B choice. Running, it switches the daemon's own RelayMode, the one
        Telegram /mode switches too; otherwise it is saved for the next start."""
        live = self._live()
        if live:
            live.relay_mode.set(mode)
        else:
            save_mode(mode, self._mode_path)
        log.info("relay mode %s", mode)
        return self.snapshot()

    @staticmethod
    def _notifications(side) -> str:
        if side is None:
            return "—"
        return "已暂停（手机发 /resume 恢复）" if side.paused else "开启"

    @staticmethod
    def _away(presence) -> str:
        source = presence.away_source()
        if source == "idle":
            return f"离开（{int(presence.quiet_seconds() // 60)} 分钟无键鼠输入）"
        if source == "declared":
            return "离开（手机声明）"
        return "在电脑前"

    @staticmethod
    def _telegram(live) -> str:
        if live.bridge is None:
            return "未配置"
        if live.bridge.telegram_down:
            return "连接失败，正在重试"
        return f"已连接 @{live.bot_username}" if live.bot_username else "已连接"

    # -- the Telegram tab (#68) ---------------------------------------------------------
    def _config_data(self, path: Path) -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def telegram_settings(self) -> TelegramSettings:
        data = self._config_data(paths.config_file(self.data_dir, self.env))
        token_env = TOKEN_ENV if self.env.get(TOKEN_ENV) else ""
        chat_env = CHAT_ID_ENV if self.env.get(CHAT_ID_ENV) else ""
        token = self.env[TOKEN_ENV] if token_env else str(data.get("telegram_token") or "")
        chat = self.env[CHAT_ID_ENV] if chat_env else str(data.get("chat_id") or "")
        return TelegramSettings(token, chat, token_env, chat_env)

    @_locked
    def save_telegram(self, token: str, chat_id: str) -> TelegramResult:
        """Writes the storage directory's config.json; a config older versions kept under
        %APPDATA% is left alone and no longer read once this file exists. A field set by
        an environment variable is not written, so the variable's value is not copied."""
        form = self.telegram_settings()
        token, chat = token.strip(), _chat_id(chat_id)
        if not form.token_env and not token:
            return NO_TOKEN
        if not form.chat_id_env and chat is None:
            return BAD_CHAT_ID
        path = self.data_dir / "config.json"
        data = self._config_data(path)
        if not form.token_env:
            data["telegram_token"] = token
        if not form.chat_id_env:
            data["chat_id"] = chat
        try:  # whole or not at all, like settings.json and ccmonitor.ini
            paths.write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))
        except OSError as e:
            log.error("config not saved: %s", type(e).__name__)
            return TelegramResult(False, f"保存失败：{type(e).__name__}")
        # The daemon reads its config when it starts. Saving never changes the remembered
        # 启用/停用: a running daemon is restarted; the first config runs ccmonitor only if
        # the user had not chosen 停用; otherwise it waits for 启用.
        if self._running():
            log.info("telegram config saved; restarting")
            self._stop()
        elif form.configured or self._chose_disabled():
            log.info("telegram config saved")
            return TelegramResult(True, "已保存，启用后生效")
        else:
            log.info("telegram config saved; enabling")
        snap = self._enable()
        if snap.state != RUNNING:
            return TelegramResult(True, f"已保存，但没能启用：{snap.detail or snap.state}")
        return TelegramResult(True, "已保存并启用")

    def _bot(self, token: str):
        if self.telegram_api is not None:
            return self.telegram_api(token)
        from .telegram import BotApi
        return BotApi(token)

    @staticmethod
    def _why(e: Exception) -> str:
        from .telegram import TelegramError
        return str(e) if isinstance(e, TelegramError) else type(e).__name__  # never the token

    def test_connection(self, token: str) -> TelegramResult:
        """「测试连接」 with the token in the field, saved or not: getMe."""
        token = token.strip()
        if not token:
            return NO_TOKEN
        try:
            me = self._bot(token).call("getMe") or {}
        except Exception as e:
            log.warning("connection test failed: %s", self._why(e))
            return TelegramResult(False, f"连接失败：{self._why(e)}")
        username, topics = str(me.get("username") or ""), bool(me.get("has_topics_enabled"))
        return TelegramResult(True, f"已连接 @{username}，话题模式：{'已开启' if topics else '未开启'}",
                              username=username, topics=topics)

    def can_fetch_chat_id(self) -> bool:
        """「获取 Chat ID」 reads the bot's updates itself, so only while the daemon is not
        polling them (disabled or unconfigured), and one fetch at a time; otherwise the
        two would race for messages."""
        return not (self._running() or self._fetching)

    @property
    def fetching(self) -> bool:
        """A 「获取 Chat ID」 is waiting for Telegram: 启用 is refused until it ends."""
        return self._fetching

    def fetch_chat_id(self, token: str, wait: int = 0, rounds: int = 0) -> TelegramResult:
        """The chat of the latest message sent to the bot: asked once, then, while no
        message has come, up to `rounds` long polls of `wait` seconds each (blocks; the
        window calls this off its own thread). 启用 is refused meanwhile."""
        token = token.strip()
        if not token:
            return NO_TOKEN
        with self._lock:
            if not self.can_fetch_chat_id():
                return TelegramResult(False, "运行中不能获取 Chat ID，请先停用")
            self._fetching = True
        try:
            bot = self._bot(token)
            for timeout in [0] + [wait] * rounds:
                try:
                    updates = bot.call("getUpdates", timeout=timeout, allowed_updates=["message"]) or []
                except Exception as e:
                    log.warning("chat id lookup failed: %s", self._why(e))
                    return TelegramResult(False, f"获取失败：{self._why(e)}")
                chats = [u["message"]["chat"]["id"] for u in updates
                         if isinstance(u.get("message"), dict) and "chat" in u["message"]]
                if chats:
                    return TelegramResult(True, f"已获取 Chat ID：{chats[-1]}，保存后生效",
                                          chat_id=str(chats[-1]))
            return TelegramResult(False, "还没收到消息：先在 Telegram 里给 bot 发一条消息")
        finally:
            self._fetching = False

    def send_test_message(self, token: str, chat_id: str) -> TelegramResult:
        """「发送测试消息」 with the fields' values, saved or not."""
        token, chat = token.strip(), _chat_id(chat_id)
        if not token:
            return NO_TOKEN
        if chat is None:
            return BAD_CHAT_ID
        try:
            self._bot(token).call("sendMessage", chat_id=chat,
                                  text="ccmonitor 测试消息：收到这条，说明 Telegram 配置正确。")
        except Exception as e:
            log.warning("test message failed: %s", self._why(e))
            return TelegramResult(False, f"发送失败：{self._why(e)}")
        return TelegramResult(True, "已发送，请在手机上查看")
