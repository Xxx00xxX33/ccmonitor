import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ccmonitor.controller import Controller
from ccmonitor.daemon import Live
from ccmonitor.relaymode import SETUP_PROMPT, RelayMode
from tests.test_resident import FakeRegistry  # never the real HKCU Run key


class FakeService:
    """Stands in for daemon.Service: start() raises `fail` once if set."""

    def __init__(self, port: int, presence=None):
        self.port = port
        self.presence = presence  # what the daemon judges away with
        self.running = False
        self.fail: BaseException | None = None
        self.stops = 0
        # As _serve fills it with Telegram not configured; the mode is never saved here.
        self.live = Live(relay_mode=RelayMode("a"))

    def start(self) -> None:
        if self.fail:
            error, self.fail = self.fail, None
            raise error
        self.running = True

    def stop(self) -> None:
        self.stops += 1
        self.running = False


def _failing(port: int, presence=None) -> FakeService:
    service = FakeService(port, presence)
    service.fail = OSError(10048, "address in use")
    return service


class ControllerTest(unittest.TestCase):
    """The GUI's only object (#66): enable/disable and the status snapshot."""

    def setUp(self):
        self.services: list[FakeService] = []

        self.fail_next: BaseException | None = None

        def factory(port: int, presence=None) -> FakeService:
            service = FakeService(port, presence)
            service.fail, self.fail_next = self.fail_next, None
            self.services.append(service)
            return service

        self.factory = factory
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.data_dir = Path(self.dir.name)
        (self.data_dir / "config.json").write_text('{"telegram_token": "1:t", "chat_id": 1}', encoding="utf-8")
        self.now = 1000.0       # seconds
        self.last_input = 0     # tick (ms on the same clock) of the last keyboard or mouse input
        self.c = self.controller()

    def controller(self) -> Controller:
        """A fresh controller on the same storage directory: a restart."""
        return Controller(registry=FakeRegistry(), port=12345, service_factory=self.factory, data_dir=self.data_dir,
                          clock=lambda: self.now, last_input=lambda: self.last_input, env={})

    def idle_for(self, seconds: float) -> None:
        self.last_input = int(self.now * 1000)
        self.now += seconds

    def test_enable_starts_the_daemon_on_its_port_and_reports_running(self):
        snap = self.c.enable()
        self.assertEqual(snap.state, "运行中")
        self.assertEqual(snap.toggle_label, "停用")
        self.assertTrue(self.services[-1].running)
        self.assertEqual(self.services[-1].port, 12345)

    def test_before_enabling_it_is_disabled_and_nothing_runs(self):
        snap = self.c.snapshot()
        self.assertEqual((snap.state, snap.toggle_label), ("已停用", "启用"))
        self.assertEqual(self.services, [])

    def test_disable_stops_the_daemon_and_reports_disabled(self):
        self.c.enable()
        snap = self.c.disable()
        self.assertEqual((snap.state, snap.toggle_label), ("已停用", "启用"))
        self.assertFalse(self.services[-1].running)

    def test_toggle_enables_when_disabled_and_disables_when_running(self):
        self.assertEqual(self.c.toggle().state, "运行中")
        self.assertEqual(self.c.toggle().state, "已停用")
        self.assertEqual(self.c.toggle().state, "运行中")
        self.assertEqual([s.running for s in self.services], [False, True])

    def test_busy_port_is_an_error_and_pressing_again_retries(self):
        self.fail_next = OSError(10048, "address in use")
        snap = self.c.enable()
        self.assertEqual((snap.state, snap.toggle_label), ("出错", "启用"))
        self.assertIn("12345", snap.detail)
        self.assertIn("占用", snap.detail)
        self.assertEqual(self.c.toggle().state, "运行中")  # freed meanwhile: the retry starts it

    def test_a_daemon_that_ended_on_its_own_is_an_error(self):
        self.c.enable()
        self.services[-1].running = False  # its thread died
        snap = self.c.snapshot()
        self.assertEqual((snap.state, snap.toggle_label), ("出错", "启用"))
        self.assertIn("意外停止", snap.detail)
        self.assertEqual(self.c.toggle().state, "运行中")
        self.assertEqual(len(self.services), 2)

    def test_quit_stops_a_running_daemon(self):
        self.c.enable()
        self.c.quit()
        self.assertFalse(self.services[-1].running)
        self.assertEqual(self.services[-1].stops, 1)

    def test_quitting_while_a_start_is_under_way_leaves_nothing_running(self):
        # The window starts the daemon on a worker thread; 退出 (or a later launch's
        # --replace) can come from the Tk thread meanwhile.
        import threading
        starting, go = threading.Event(), threading.Event()

        class SlowService(FakeService):
            def start(self):
                starting.set()
                go.wait(5)
                super().start()
        services = []
        c = Controller(registry=FakeRegistry(), port=1, data_dir=self.data_dir, env={}, last_input=lambda: 0,
                       service_factory=lambda port, presence=None: services.append(SlowService(port)) or services[-1])
        worker = threading.Thread(target=c.enable)
        worker.start()
        self.assertTrue(starting.wait(5))
        threading.Timer(0.2, go.set).start()
        c.quit()
        worker.join(5)
        self.assertFalse(services[-1].running)

    def test_quit_while_disabled_does_nothing(self):
        self.c.quit()
        self.assertEqual(self.services, [])

    # -- 离开时间 (#69) ------------------------------------------------------------
    def test_a_new_away_threshold_applies_to_the_running_daemon_at_once(self):
        self.c.enable()
        presence = self.services[-1].presence
        self.idle_for(4 * 60)
        self.assertTrue(presence.is_away())  # default 3 minutes
        self.c.set_away_minutes("5")
        self.assertFalse(presence.is_away())
        self.c.set_away_minutes("4")
        self.assertTrue(presence.is_away())

    def test_away_threshold_accepts_whole_minutes_from_1_to_60(self):
        self.assertEqual(self.c.set_away_minutes(" 1 "), 1)
        self.assertEqual(self.c.set_away_minutes("60"), 60)
        self.assertEqual(self.c.away_minutes, 60)

    def test_out_of_range_or_non_integer_away_threshold_is_refused_with_a_reason(self):
        self.c.set_away_minutes("7")
        for bad in ("0", "61", "-3", "2.5", "abc", "", "三"):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as caught:
                self.c.set_away_minutes(bad)
            self.assertIn("1", str(caught.exception))
            self.assertIn("60", str(caught.exception))
        self.assertEqual(self.c.away_minutes, 7)
        self.idle_for(6 * 60)
        self.assertFalse(self.c.presence.is_away())  # still 7 minutes

    def test_away_threshold_survives_a_restart(self):
        self.c.set_away_minutes("10")
        self.c.disable()  # saving the other setting keeps this one
        restarted = self.controller()
        self.assertEqual(restarted.away_minutes, 10)
        self.idle_for(9 * 60)
        self.assertFalse(restarted.presence.is_away())
        self.idle_for(10 * 60)
        self.assertTrue(restarted.presence.is_away())

    def test_away_threshold_defaults_to_3_minutes_without_or_with_a_broken_settings_file(self):
        self.assertEqual(self.c.away_minutes, 3)
        for broken in ("{not json", '{"away_minutes": 0}', '{"away_minutes": "x"}', "[]"):
            with self.subTest(broken=broken):
                (self.data_dir / "settings.json").write_text(broken, encoding="utf-8")
                self.assertEqual(self.controller().away_minutes, 3)

    # -- 换班间隔 (mode B) --------------------------------------------------------------
    def test_rotate_interval_defaults_to_2_hours_and_survives_a_restart(self):
        self.assertEqual(self.c.rotate_hours, 2)
        self.assertEqual(self.c.set_rotate_hours(" 5 "), 5)
        self.c.set_away_minutes("4")  # saving the other setting keeps this one
        self.assertEqual(self.controller().rotate_hours, 5)

    def test_out_of_range_or_non_integer_rotate_interval_is_refused_with_a_reason(self):
        self.c.set_rotate_hours("3")
        for bad in ("0", "25", "-1", "1.5", "abc", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as caught:
                self.c.set_rotate_hours(bad)
            self.assertIn("24", str(caught.exception))
        self.assertEqual(self.c.rotate_hours, 3)
        (self.data_dir / "settings.json").write_text('{"rotate_hours": 99}', encoding="utf-8")
        self.assertEqual(self.controller().rotate_hours, 2)

    # -- 启用/停用 across restarts (#69) -----------------------------------------------
    def test_first_start_enables(self):
        self.assertEqual(self.c.restore().state, "运行中")

    def test_disabled_by_the_user_stays_disabled_after_a_restart(self):
        self.c.enable()
        self.c.disable()
        restarted = self.controller()
        self.assertEqual(restarted.restore().state, "已停用")
        self.assertFalse(any(s.running for s in self.services))

    def test_enabled_again_starts_after_a_restart(self):
        self.c.disable()
        self.c.toggle()
        self.assertEqual(self.controller().restore().state, "运行中")

    def test_quitting_is_not_remembered_as_disabled(self):
        self.c.enable()
        self.c.quit()
        self.assertEqual(self.controller().restore().state, "运行中")

    def test_an_enable_that_failed_is_still_the_users_choice(self):
        self.fail_next = OSError(10048, "address in use")
        self.assertEqual(self.c.enable().state, "出错")
        self.assertEqual(self.controller().restore().state, "运行中")


class StorageDirResolutionTest(unittest.TestCase):
    """Which storage directory a controller opens (#70): CCMONITOR_HOME, else DataDir in
    the ccmonitor.ini beside the exe, else the default under the user profile."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.ini = self.root / "bin" / "ccmonitor.ini"

    def write_ini(self, text: str) -> None:
        self.ini.parent.mkdir(parents=True, exist_ok=True)
        self.ini.write_text(text, encoding="utf-8")

    def storage_dir(self, env: dict) -> Path:
        return Controller(registry=FakeRegistry(), port=1, service_factory=FakeService, env=env, ini_path=self.ini,
                          last_input=lambda: 0).storage_dir

    def test_the_environment_variable_wins_over_the_ini(self):
        self.write_ini(f"[ccmonitor]\nDataDir = {self.root / 'from-ini'}\n")
        self.assertEqual(self.storage_dir({"CCMONITOR_HOME": str(self.root / "from-env")}),
                         self.root / "from-env")

    def test_the_ini_wins_over_the_default(self):
        self.write_ini(f"[ccmonitor]\nDataDir = {self.root / 'from-ini'}\n")
        self.assertEqual(self.storage_dir({}), self.root / "from-ini")

    def home(self) -> dict:
        """A fake user profile, so no test opens the real %USERPROFILE%\\.ccmonitor."""
        return {"USERPROFILE": str(self.root / "profile")}

    def test_without_an_ini_it_is_the_default_folder(self):
        # A worktree's bin\ has no ccmonitor.ini: it falls back to the default.
        self.assertEqual(self.storage_dir(self.home()), self.root / "profile" / ".ccmonitor")

    def test_an_ini_without_a_usable_data_dir_is_the_default_folder(self):
        for text in ("", "[ccmonitor]\n", "[ccmonitor]\nDataDir =\n", "not an ini [",
                     "[other]\nDataDir = X:\\y\n"):
            with self.subTest(text=text):
                self.write_ini(text)
                self.assertEqual(self.storage_dir(self.home()), self.root / "profile" / ".ccmonitor")


class FakeFiles:
    """Stands in for ProcessFiles: records release/attach and whether a daemon ran meanwhile."""

    def __init__(self, services: list):
        self.services = services
        self.calls: list = []

    def release(self) -> None:
        self.calls.append(("release", any(s.running for s in self.services)))

    def attach(self, data_dir: Path) -> None:
        self.calls.append(("attach", data_dir))


class StorageMoveTest(unittest.TestCase):
    """Changing the 存储目录 (#70): checked, confirmed with a file count, then every file
    moved with the daemon paused, and the choice kept in ccmonitor.ini."""

    TOKEN = "123:SECRET"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.old = self.root / "old"
        self.new = self.root / "new"
        self.ini = self.root / "repo" / "bin" / "ccmonitor.ini"
        self.env = {"LOCALAPPDATA": str(self.root / "Local")}
        self.services: list[FakeService] = []
        self.files = FakeFiles(self.services)
        self.put(self.old / "config.json", json.dumps({"telegram_token": self.TOKEN, "chat_id": 42}))
        self.put(self.old / "topics.json", "{}")
        self.put(self.old / "daemon.log", "started\n")
        self.put(self.old / "nativetab-0123.dll", "MZ")
        self.c = self.controller()
        self.c.set_away_minutes("9")  # settings.json: the fifth file

    def factory(self, port: int, presence=None) -> FakeService:
        service = FakeService(port, presence)
        self.services.append(service)
        return service

    def controller(self, data_dir: Path | None = None) -> Controller:
        return Controller(registry=FakeRegistry(), port=1, service_factory=self.factory, last_input=lambda: 0,
                          data_dir=data_dir or self.old, env=self.env, ini_path=self.ini,
                          files=self.files)

    def put(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_the_plan_counts_the_files_to_move(self):
        plan = self.c.plan_storage_move(self.new)
        self.assertEqual(plan.files, 5)
        self.assertFalse(plan.config_conflict)

    def test_an_unwritable_folder_is_refused_and_nothing_changes(self):
        self.c.enable()
        self.put(self.root / "a-file", "x")
        for target in (self.root / "a-file", self.root / "a-file" / "sub"):
            with self.subTest(target=target):
                with self.assertRaises(ValueError) as caught:
                    self.c.plan_storage_move(target)
                self.assertIn("不可写", str(caught.exception))
                with self.assertRaises(ValueError):
                    self.c.move_storage(target)
        self.assertTrue((self.old / "config.json").exists())
        self.assertFalse(self.ini.exists())
        self.assertEqual(self.c.storage_dir, self.old)
        self.assertEqual(self.c.snapshot().state, "运行中")
        self.assertEqual(len(self.services), 1)

    def test_the_current_folder_or_one_inside_it_is_refused(self):
        for target in (self.old, self.old / "inner"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.c.plan_storage_move(target)

    def test_a_folder_set_by_the_environment_variable_cannot_be_changed_here(self):
        self.env["CCMONITOR_HOME"] = str(self.old)
        with self.assertRaises(ValueError) as caught:
            self.c.plan_storage_move(self.new)
        self.assertIn("CCMONITOR_HOME", str(caught.exception))
        self.assertIn("CCMONITOR_HOME", self.c.storage_locked)

    def test_moving_takes_every_file_and_leaves_no_token_behind(self):
        result = self.c.move_storage(self.new)
        self.assertEqual(result.moved, 5)
        self.assertEqual(result.left, [])
        self.assertEqual(sorted(p.name for p in self.new.iterdir()),
                         ["config.json", "daemon.log", "nativetab-0123.dll", "settings.json", "topics.json"])
        self.assertIn(self.TOKEN, (self.new / "config.json").read_text(encoding="utf-8"))
        leftovers = [p for p in self.old.rglob("*") if p.is_file()] if self.old.exists() else []
        self.assertFalse(any(self.TOKEN in p.read_text(encoding="utf-8") for p in leftovers))
        self.assertEqual(self.c.storage_dir, self.new)
        self.assertEqual(self.files.calls, [("release", False), ("attach", self.new)])

    def test_the_choice_is_kept_in_the_ini_and_the_settings_come_along(self):
        self.c.move_storage(self.new)
        restarted = Controller(registry=FakeRegistry(), port=1, service_factory=self.factory, last_input=lambda: 0,
                               env=self.env, ini_path=self.ini, files=self.files)
        self.assertEqual(restarted.storage_dir, self.new)
        self.assertEqual(restarted.away_minutes, 9)

    def test_a_running_daemon_is_paused_for_the_move_and_runs_again_after(self):
        self.c.enable()
        self.c.move_storage(self.new)
        self.assertEqual(self.files.calls[0], ("release", False))  # stopped before the move
        self.assertEqual(self.c.snapshot().state, "运行中")
        self.assertEqual(len(self.services), 2)

    def test_a_disabled_ccmonitor_stays_disabled_after_the_move(self):
        self.c.disable()
        self.c.move_storage(self.new)
        self.assertEqual(self.c.snapshot().state, "已停用")
        self.assertEqual(self.controller(self.new).restore().state, "已停用")

    def test_a_config_already_in_the_new_folder_needs_a_choice(self):
        self.put(self.new / "config.json", json.dumps({"telegram_token": "999:OTHER", "chat_id": 7}))
        self.assertTrue(self.c.plan_storage_move(self.new).config_conflict)
        with self.assertRaises(ValueError):
            self.c.move_storage(self.new)
        self.assertTrue((self.old / "config.json").exists())
        self.assertFalse(self.ini.exists())

    def test_keeping_the_new_folders_config_drops_the_old_token(self):
        self.put(self.new / "config.json", json.dumps({"telegram_token": "999:OTHER", "chat_id": 7}))
        self.c.move_storage(self.new, keep_config="new")
        self.assertIn("999:OTHER", (self.new / "config.json").read_text(encoding="utf-8"))
        self.assertFalse((self.old / "config.json").exists())

    def test_keeping_the_current_config_replaces_the_new_folders(self):
        self.put(self.new / "config.json", json.dumps({"telegram_token": "999:OTHER", "chat_id": 7}))
        self.c.move_storage(self.new, keep_config="old")
        self.assertIn(self.TOKEN, (self.new / "config.json").read_text(encoding="utf-8"))
        self.assertFalse((self.old / "config.json").exists())

    def test_a_failed_move_gives_back_the_new_folders_own_config(self):
        self.put(self.new / "config.json", json.dumps({"telegram_token": "999:OTHER", "chat_id": 7}))
        self.put(self.root / "repo" / "bin", "a file where the ini's folder should be")
        with self.assertRaises(ValueError) as caught:
            self.c.move_storage(self.new, keep_config="old")
        self.assertIn("存储目录未变", str(caught.exception))
        self.assertEqual(json.loads((self.new / "config.json").read_text(encoding="utf-8")),
                         {"telegram_token": "999:OTHER", "chat_id": 7})
        self.assertEqual(sorted(p.name for p in self.new.iterdir()), ["config.json"])  # no token copy left
        self.assertIn(self.TOKEN, (self.old / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(self.c.storage_dir, self.old)

    def test_the_hooks_relay_down_mark_stays_put(self):
        # The storage directory chosen as %LOCALAPPDATA%\ccmonitor, where the hooks keep it.
        old = self.root / "Local" / "ccmonitor"
        self.put(old / "config.json", "{}")
        self.put(old / "relay-down", "")
        c = self.controller(old)
        self.assertEqual(c.plan_storage_move(self.new).files, 1)
        c.move_storage(self.new)
        self.assertTrue((old / "relay-down").exists())
        self.assertFalse((self.new / "relay-down").exists())
        self.assertTrue((self.new / "config.json").exists())


class LogFollowsTheMoveTest(unittest.TestCase):
    """ProcessFiles' log step (#70): daemon.log is closed so Windows can move it, then
    reopened in the new storage directory."""

    def test_the_log_is_released_then_written_in_the_new_folder(self):
        import logging
        from ccmonitor.daemon import _log_file, move_log
        with tempfile.TemporaryDirectory() as d:
            old, new = Path(d) / "old", Path(d) / "new"
            logger = logging.getLogger("test-move-log")
            logger.propagate = False
            logger.addHandler(_log_file(old))
            self.addCleanup(move_log, None, logger)
            logger.warning("before")
            move_log(None, logger)
            (old / "daemon.log").replace(Path(d) / "moved.log")  # fails on Windows while open
            move_log(new, logger)
            logger.warning("after")
            self.assertIn("after", (new / "daemon.log").read_text(encoding="utf-8"))
            move_log(None, logger)


class LogCleanupTest(unittest.TestCase):
    """daemon.log rotates at midnight and keeps 3 days (user's call, 2026-10-14)."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)

    def touch(self, name: str, days_old: float) -> Path:
        import os, time
        path = self.dir / name
        path.write_text("x", encoding="utf-8")
        then = time.time() - days_old * 86400
        os.utime(path, (then, then))
        return path

    def test_opening_the_log_deletes_files_older_than_3_days(self):
        from ccmonitor.daemon import _log_file
        old = [self.touch("daemon.log.2026-10-01", 13), self.touch("daemon.log.1", 4)]  # .1: size-rotated
        kept = self.touch("daemon.log.2026-10-12", 2)
        other = self.touch("topics.json", 30)
        _log_file(self.dir).close()
        self.assertFalse(any(p.exists() for p in old))
        self.assertTrue(kept.exists() and other.exists())

    def test_rollover_keeps_the_last_3_days(self):
        import logging, time
        from ccmonitor.daemon import _log_file
        for day in range(8, 13):
            self.touch(f"daemon.log.2026-10-{day:02d}", 1)  # recent by mtime: only the count prunes
        handler = _log_file(self.dir)
        self.addCleanup(handler.close)
        handler.rolloverAt = int(time.time()) - 1  # midnight has passed
        handler.emit(logging.makeLogRecord({"msg": "after midnight"}))
        rotated = sorted(p.name for p in self.dir.glob("daemon.log.*"))
        self.assertEqual(len(rotated), 3)
        self.assertIn("after midnight", (self.dir / "daemon.log").read_text(encoding="utf-8"))


TOKEN = "123456:SECRET-token"


class FakeBot:
    """Stands in for BotApi: answers each method from `answers` (a value, or an
    exception to raise) and records every call."""

    def __init__(self, token: str, answers: dict, calls: list):
        self.token, self.answers, self.calls = token, answers, calls

    def call(self, method, **params):
        self.calls.append((self.token, method, params))
        answer = self.answers.get(method)
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):  # answers this call itself (a script, or one that waits)
            return answer(**params)
        return answer


class TelegramTabTest(unittest.TestCase):
    """The Telegram tab (#68): bot config without hand-written JSON, and its helper buttons."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.data = root / "home" / ".ccmonitor"
        self.roaming = root / "Roaming"
        # The controller's env, not os.environ: where it looks for an older %APPDATA% config.
        self.env: dict[str, str] = {"APPDATA": str(self.roaming), "LOCALAPPDATA": str(root / "Local")}
        self.answers: dict = {}
        self.calls: list = []
        self.services: list[FakeService] = []

        def factory(port, presence=None):
            self.services.append(FakeService(port, presence))
            return self.services[-1]

        self.c = Controller(registry=FakeRegistry(), port=12345, data_dir=self.data, service_factory=factory, env=self.env,
                            telegram_api=lambda token: FakeBot(token, self.answers, self.calls))

    def put(self, path: Path, data: dict) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_saving_writes_config_json_in_the_storage_directory(self):
        self.c.save_telegram(TOKEN, "42")
        data = json.loads((self.data / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(data, {"telegram_token": TOKEN, "chat_id": 42})
        form = self.c.telegram_settings()
        self.assertEqual((form.token, form.chat_id), (TOKEN, "42"))

    def test_an_old_appdata_config_is_shown_left_alone_and_superseded_by_saving(self):
        old = self.put(self.roaming / "ccmonitor" / "config.json", {"telegram_token": "old:tok", "chat_id": 7})
        self.assertEqual(self.c.telegram_settings().token, "old:tok")
        self.c.save_telegram(TOKEN, "42")
        self.assertEqual(json.loads(old.read_text(encoding="utf-8")), {"telegram_token": "old:tok", "chat_id": 7})
        form = self.c.telegram_settings()
        self.assertEqual((form.token, form.chat_id), (TOKEN, "42"))

    def test_environment_overrides_are_shown_read_only_with_their_source(self):
        self.put(self.data / "config.json", {"telegram_token": "file:tok", "chat_id": 7})
        self.env["CCMONITOR_TELEGRAM_TOKEN"] = TOKEN
        form = self.c.telegram_settings()
        self.assertEqual((form.token, form.token_env), (TOKEN, "CCMONITOR_TELEGRAM_TOKEN"))
        self.assertEqual((form.chat_id, form.chat_id_env), ("7", ""))

    def test_saving_keeps_an_environment_override_out_of_the_file(self):
        self.env["CCMONITOR_TELEGRAM_TOKEN"] = TOKEN
        result = self.c.save_telegram(TOKEN, "42")
        self.assertTrue(result.ok)
        self.assertEqual(json.loads((self.data / "config.json").read_text(encoding="utf-8")), {"chat_id": 42})
        self.assertEqual(self.c.telegram_settings().chat_id, "42")

    def test_saving_refuses_a_chat_id_that_is_not_a_number_or_an_empty_token(self):
        self.assertFalse(self.c.save_telegram(TOKEN, "abc").ok)
        self.assertFalse(self.c.save_telegram(" ", "42").ok)
        self.assertFalse((self.data / "config.json").exists())

    def test_saving_restarts_a_running_daemon_so_the_new_config_takes_effect(self):
        self.put(self.data / "config.json", {"telegram_token": "old:tok", "chat_id": 7})
        self.c.enable()
        self.c.save_telegram(TOKEN, "42")
        self.assertEqual([s.running for s in self.services], [False, True])
        self.assertEqual(self.c.snapshot().state, "运行中")

    def test_saving_the_first_config_enables_ccmonitor(self):
        self.c.save_telegram(TOKEN, "42")
        self.assertEqual(self.c.snapshot().state, "运行中")

    def settings(self) -> dict:
        path = self.data / "settings.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def test_saving_while_disabled_keeps_it_disabled(self):
        self.put(self.data / "config.json", {"telegram_token": "old:tok", "chat_id": 7})
        self.c.disable()
        result = self.c.save_telegram(TOKEN, "42")
        self.assertTrue(result.ok)
        self.assertEqual(self.c.snapshot().state, "已停用")
        self.assertEqual(self.services, [])
        self.assertEqual(self.settings().get("enabled"), False)

    def test_saving_while_running_does_not_touch_the_remembered_choice(self):
        self.put(self.data / "config.json", {"telegram_token": "old:tok", "chat_id": 7})
        self.c.restore()  # runs without ever recording a choice
        self.c.save_telegram(TOKEN, "42")
        self.assertEqual(self.c.snapshot().state, "运行中")
        self.assertNotIn("enabled", self.settings())

    def test_saving_the_first_config_after_choosing_disabled_stays_disabled(self):
        self.c.disable()  # 停用 remembered, then the config was removed
        self.assertEqual(self.c.snapshot().state, "未配置")
        self.c.save_telegram(TOKEN, "42")
        self.assertEqual(self.c.snapshot().state, "已停用")
        self.assertEqual(self.services, [])

    def test_a_refused_enable_while_unconfigured_is_not_remembered(self):
        self.c.disable()
        self.assertEqual(self.c.enable().state, "未配置")
        self.assertEqual(self.settings().get("enabled"), False)
        self.c.save_telegram(TOKEN, "42")
        self.assertEqual(self.c.snapshot().state, "已停用")

    def test_unconfigured_it_says_so_and_cannot_be_enabled(self):
        snap = self.c.snapshot()
        self.assertEqual(snap.state, "未配置")
        self.assertFalse(snap.can_toggle)
        self.assertEqual(self.c.enable().state, "未配置")
        self.assertEqual(self.c.toggle().state, "未配置")
        self.assertEqual(self.services, [])

    def test_opening_ccmonitor_unconfigured_does_not_run_it(self):
        self.assertEqual(self.c.restore().state, "未配置")
        self.assertEqual(self.services, [])

    def test_a_config_from_the_environment_alone_counts_as_configured(self):
        self.env.update(CCMONITOR_TELEGRAM_TOKEN=TOKEN, CCMONITOR_CHAT_ID="42")
        self.assertTrue(self.c.snapshot().can_toggle)
        self.assertEqual(self.c.enable().state, "运行中")

    def test_testing_the_connection_shows_the_bot_username_and_topic_mode(self):
        self.answers["getMe"] = {"id": 1, "is_bot": True, "username": "cc_bot", "has_topics_enabled": True}
        result = self.c.test_connection(TOKEN)
        self.assertTrue(result.ok)
        self.assertEqual((result.username, result.topics), ("cc_bot", True))
        self.assertIn("@cc_bot", result.message)
        self.assertIn("话题模式：已开启", result.message)
        self.assertEqual(self.calls, [(TOKEN, "getMe", {})])

    def test_testing_the_connection_reports_topic_mode_off(self):
        self.answers["getMe"] = {"id": 1, "is_bot": True, "username": "cc_bot"}
        result = self.c.test_connection(TOKEN)
        self.assertFalse(result.topics)
        self.assertIn("话题模式：未开启", result.message)

    def test_a_failed_connection_test_says_why_without_the_token(self):
        from ccmonitor.telegram import TelegramError
        self.answers["getMe"] = TelegramError("getMe: 401 Unauthorized")
        result = self.c.test_connection(TOKEN)
        self.assertFalse(result.ok)
        self.assertIn("401", result.message)
        self.assertNotIn("SECRET", result.message)

    def test_testing_without_a_token_asks_for_one_and_calls_nothing(self):
        self.assertFalse(self.c.test_connection("  ").ok)
        self.assertEqual(self.calls, [])

    def test_fetching_the_chat_id_reads_the_latest_message_sent_to_the_bot(self):
        self.answers["getUpdates"] = [
            {"update_id": 1, "message": {"chat": {"id": 111}, "text": "older"}},
            {"update_id": 2, "message": {"chat": {"id": -100222}, "text": "hi"}},
        ]
        result = self.c.fetch_chat_id(TOKEN)
        self.assertTrue(result.ok)
        self.assertEqual(result.chat_id, "-100222")
        self.assertEqual([(t, m) for t, m, _ in self.calls], [(TOKEN, "getUpdates")])

    def test_fetching_with_no_message_yet_asks_the_user_to_send_one(self):
        self.answers["getUpdates"] = []
        result = self.c.fetch_chat_id(TOKEN)
        self.assertFalse(result.ok)
        self.assertEqual(result.chat_id, "")
        self.assertIn("发一条消息", result.message)

    def test_fetching_keeps_waiting_for_a_message_in_long_polls(self):
        script = iter([[], [], [{"update_id": 3, "message": {"chat": {"id": 77}}}]])
        self.answers["getUpdates"] = lambda **params: next(script)
        result = self.c.fetch_chat_id(TOKEN, wait=15, rounds=4)
        self.assertEqual((result.ok, result.chat_id), (True, "77"))
        self.assertEqual([p["timeout"] for _, _, p in self.calls], [0, 15, 15])

    def test_fetching_gives_up_after_the_last_round_or_at_the_first_error(self):
        self.answers["getUpdates"] = []
        result = self.c.fetch_chat_id(TOKEN, wait=15, rounds=2)
        self.assertFalse(result.ok)
        self.assertEqual(len(self.calls), 3)
        from ccmonitor.telegram import TelegramError
        self.calls.clear()
        self.answers["getUpdates"] = TelegramError("getUpdates: 401 Unauthorized")
        self.assertIn("401", self.c.fetch_chat_id(TOKEN, wait=15, rounds=4).message)
        self.assertEqual(len(self.calls), 1)

    def test_while_a_chat_id_fetch_waits_ccmonitor_cannot_be_enabled(self):
        import threading
        self.put(self.data / "config.json", {"telegram_token": TOKEN, "chat_id": 7})
        asked, answer = threading.Event(), threading.Event()

        def wait_for_a_message(**params):
            asked.set()
            answer.wait(5)
            return [{"update_id": 1, "message": {"chat": {"id": 5}}}]
        self.answers["getUpdates"] = wait_for_a_message
        results = []
        fetch = threading.Thread(target=lambda: results.append(self.c.fetch_chat_id(TOKEN)))
        fetch.start()
        self.addCleanup(fetch.join, 5)
        self.addCleanup(answer.set)
        self.assertTrue(asked.wait(5))
        snap = self.c.snapshot()
        self.assertEqual((snap.state, snap.can_toggle), ("已停用", False))
        for start in (self.c.enable, self.c.toggle, self.c.restore, self.c.take_over):
            with self.subTest(start=start.__name__):
                self.assertEqual(start().state, "已停用")
        self.assertEqual(self.services, [])
        self.assertFalse((self.data / "settings.json").exists())  # not remembered as 启用
        self.assertFalse(self.c.can_fetch_chat_id())  # one fetch at a time
        answer.set()
        fetch.join(5)
        self.assertEqual(results[0].chat_id, "5")
        self.assertEqual(self.c.enable().state, "运行中")

    def test_fetching_the_chat_id_is_available_while_unconfigured_or_disabled(self):
        self.assertTrue(self.c.can_fetch_chat_id())  # unconfigured
        self.put(self.data / "config.json", {"telegram_token": TOKEN, "chat_id": 7})
        self.assertTrue(self.c.can_fetch_chat_id())  # disabled

    def test_fetching_the_chat_id_is_unavailable_while_running(self):
        self.put(self.data / "config.json", {"telegram_token": TOKEN, "chat_id": 7})
        self.c.enable()
        self.assertFalse(self.c.can_fetch_chat_id())
        self.answers["getUpdates"] = [{"update_id": 2, "message": {"chat": {"id": 5}}}]
        result = self.c.fetch_chat_id(TOKEN)
        self.assertFalse(result.ok)
        self.assertIn("停用", result.message)
        self.assertEqual(self.calls, [])  # the running daemon's poll keeps its messages

    def test_sending_a_test_message_goes_to_the_chat_in_the_field(self):
        self.answers["sendMessage"] = {"message_id": 9}
        result = self.c.send_test_message(TOKEN, "-100222")
        self.assertTrue(result.ok)
        [(token, method, params)] = self.calls
        self.assertEqual((token, method, params["chat_id"]), (TOKEN, "sendMessage", -100222))
        self.assertIn("ccmonitor", params["text"])

    def test_a_failed_test_message_says_why(self):
        from ccmonitor.telegram import TelegramError
        self.answers["sendMessage"] = TelegramError("sendMessage: 400 Bad Request: chat not found")
        result = self.c.send_test_message(TOKEN, "42")
        self.assertFalse(result.ok)
        self.assertIn("chat not found", result.message)
        self.assertFalse(self.c.send_test_message(TOKEN, "abc").ok)


class StatusTest(unittest.TestCase):
    """The status page (#67): whether the phone will hear anything, and the relay mode switch."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.tick = 10_000_000         # ms, whole seconds: the controller's clock is in seconds
        self.input_at = self.tick      # tick of the last keyboard or mouse input
        self.side = SimpleNamespace(paused=False)
        self.bridge = SimpleNamespace(telegram_down=False)
        self.helper = SimpleNamespace(connected=False, paused_title=None)
        self.services: list[FakeService] = []
        (self.dir / "config.json").write_text('{"telegram_token": "1:t", "chat_id": 1}', encoding="utf-8")

        def factory(port: int, presence=None) -> FakeService:
            service = FakeService(port, presence)
            service.live = Live(relay_mode=RelayMode(path=self.dir / "relay-mode.json"), helper=self.helper,
                                side=self.side, bridge=self.bridge, bot_username="cc_bot")
            self.services.append(service)
            return service

        self.c = Controller(registry=FakeRegistry(), port=12345, data_dir=self.dir, service_factory=factory, env={},
                            clock=lambda: self.tick / 1000, last_input=lambda: self.input_at)
        self.presence = self.c.presence  # the daemon's away state too

    def test_telegram_shows_connected_with_the_bot_username(self):
        self.c.enable()
        self.assertEqual(self.c.snapshot().telegram, "已连接 @cc_bot")

    def test_telegram_requests_failing_show_as_retrying(self):
        self.c.enable()
        self.bridge.telegram_down = True
        self.assertEqual(self.c.snapshot().telegram, "连接失败，正在重试")
        self.assertEqual(self.c.snapshot().state, "运行中")  # recovers by itself: not an error

    def test_telegram_not_configured(self):
        (self.dir / "config.json").unlink()
        self.c.enable()
        self.assertEqual(self.c.snapshot().telegram, "未配置")

    def test_while_disabled_telegram_is_not_connected(self):
        self.assertEqual(self.c.snapshot().telegram, "未连接")

    def test_away_after_the_threshold_without_input_says_for_how_long(self):
        self.c.enable()
        self.assertEqual(self.c.snapshot().away, "在电脑前")
        self.tick += 5 * 60_000
        self.assertEqual(self.c.snapshot().away, "离开（5 分钟无键鼠输入）")

    def test_away_declared_from_the_phone(self):
        self.c.enable()
        self.tick += 30_000
        self.presence.declare_away()
        self.assertEqual(self.c.snapshot().away, "离开（手机声明）")
        self.input_at = self.tick + 1000  # someone touches the computer: the declaration ends
        self.tick += 2000
        self.assertEqual(self.c.snapshot().away, "在电脑前")

    def test_notifications_paused_from_the_phone(self):
        self.c.enable()
        self.assertEqual(self.c.snapshot().notifications, "开启")
        self.side.paused = True  # /pause
        self.assertEqual(self.c.snapshot().notifications, "已暂停（手机发 /resume 恢复）")

    def test_notifications_while_not_running(self):
        self.assertEqual(self.c.snapshot().notifications, "—")

    def test_switching_the_relay_mode_is_the_same_mode_telegram_mode_switches(self):
        self.c.enable()
        live_mode = self.services[-1].live.relay_mode  # what /mode reads and sets
        self.assertEqual(self.c.snapshot().relay_mode, "a")
        self.assertEqual(self.c.set_relay_mode("b").relay_mode, "b")
        self.assertEqual(live_mode.mode, "b")
        live_mode.set("a")  # /mode a from the phone
        self.assertEqual(self.c.snapshot().relay_mode, "a")

    def test_switching_while_disabled_holds_for_the_next_start(self):
        self.assertEqual(self.c.snapshot().relay_mode, "a")
        self.assertEqual(self.c.set_relay_mode("b").relay_mode, "b")
        self.assertEqual(self.c.snapshot().relay_mode, "b")
        self.c.enable()
        self.assertEqual(self.services[-1].live.relay_mode.mode, "b")

    def test_mode_b_without_a_helper_session_offers_the_setup_brief(self):
        self.c.enable()
        snap = self.c.snapshot()
        self.assertEqual((snap.helper, snap.needs_helper), ("未连接", False))  # mode A needs none
        snap = self.c.set_relay_mode("b")
        self.assertEqual((snap.helper, snap.needs_helper), ("未连接", True))
        self.helper.connected = True
        snap = self.c.snapshot()
        self.assertEqual((snap.helper, snap.needs_helper), ("已连接", False))

    def test_copy_button_copies_the_setup_prompt(self):
        self.assertEqual(self.c.setup_prompt(), SETUP_PROMPT)

    def test_new_helper_session_opens_a_prefilled_desktop_session_in_the_helper_folder(self):
        opened = []
        c = Controller(registry=FakeRegistry(), port=12345, data_dir=self.dir, env={}, open_url=opened.append)
        line = c.new_helper_session()
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].startswith("claude://code/new?q="))
        self.assertTrue((self.dir / "helper" / ".claude" / "settings.json").exists())
        self.assertIn("Bypass permissions", line)

    def test_new_helper_session_falls_back_to_the_copy_button_when_the_link_fails(self):
        def fail(url):
            raise OSError("no handler")
        c = Controller(registry=FakeRegistry(), port=12345, data_dir=self.dir, env={}, open_url=fail)
        self.assertIn("「复制启动指令」", c.new_helper_session())

    def test_open_log_opens_the_storage_directorys_log_in_notepad(self):
        opened = []
        c = Controller(registry=FakeRegistry(), port=12345, data_dir=self.dir, env={}, open_file=opened.append)
        c.open_log()
        self.assertEqual(opened, [["notepad.exe", str(self.dir / "daemon.log")]])
        self.assertTrue((self.dir / "daemon.log").exists())  # else notepad asks to create it

    def test_a_daemon_that_stopped_by_itself_raises_one_alert(self):
        self.c.enable()
        self.assertIsNone(self.c.alert())
        self.services[-1].running = False  # its thread died
        self.assertIn("意外停止", self.c.alert())
        self.assertIsNone(self.c.alert())  # one balloon, not one per check
        self.c.enable()
        self.services[-1].running = False
        self.assertIsNotNone(self.c.alert())  # a new stop after a recovery alerts again

    def test_a_busy_port_alerts(self):
        c = Controller(registry=FakeRegistry(), port=12345, data_dir=self.dir, env={}, service_factory=_failing,
                       processes=lambda: [])  # no real process list
        c.enable()
        self.assertIn("占用", c.alert())

    def test_disabling_and_telegram_trouble_raise_no_alert(self):
        self.c.enable()
        self.bridge.telegram_down = True
        self.assertIsNone(self.c.alert())
        self.c.disable()
        self.assertIsNone(self.c.alert())


if __name__ == "__main__":
    unittest.main()
