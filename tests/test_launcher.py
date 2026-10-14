"""The exe launcher's behaviour behind the controller (#71): 开机自启, taking over the old
pythonw daemon, and what the tray shows when Telegram is not configured."""
import tempfile
import unittest
from pathlib import Path

from ccmonitor import autostart
from ccmonitor.controller import Controller
from ccmonitor.legacy import Proc
from tests.test_controller import FakeService
from tests.test_resident import FakeRegistry

EXE = r"D:\repo\bin\ccmonitor.exe"
COMMAND = f'"{EXE}" --tray'


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.reg = FakeRegistry()
        self.services: list[FakeService] = []

    def controller(self, **kw) -> Controller:
        kw.setdefault("registry", self.reg)
        kw.setdefault("autostart_command", COMMAND)
        kw.setdefault("env", {})
        (Path(self.dir.name) / "config.json").write_text(
            '{"telegram_token": "1:t", "chat_id": 1}', encoding="utf-8")  # configured
        return Controller(port=12345, service_factory=self.factory, data_dir=Path(self.dir.name),
                          last_input=lambda: 0, **kw)

    # -- 开机自启: off / on / 指向旧位置, in the snapshot ------------------------------------
    def autostart(self, snap) -> tuple[str, str]:
        return snap.autostart, snap.autostart_label

    def test_autostart_is_off_without_a_run_value(self):
        self.assertEqual(self.autostart(self.controller().snapshot()), (autostart.OFF, "关"))

    def test_ticking_autostart_writes_the_exe_with_tray_and_reports_on(self):
        c = self.controller()
        self.assertEqual(self.autostart(c.set_autostart(True)), (autostart.ON, "开"))
        self.assertEqual(self.reg.values, {"ccmonitor": COMMAND})
        self.assertEqual(self.autostart(c.snapshot()), (autostart.ON, "开"))

    def test_a_run_value_for_the_old_launcher_points_to_the_old_location(self):
        self.reg.values["ccmonitor"] = r'"C:\py\pythonw.exe" "D:\repo\bin\ccmonitor-daemon.pyw"'
        self.assertEqual(self.autostart(self.controller().snapshot()), (autostart.STALE, "指向旧位置"))

    def test_an_exe_elsewhere_points_to_the_old_location(self):
        self.reg.values["ccmonitor"] = r'"D:\worktree\bin\ccmonitor.exe" --tray'
        self.assertEqual(self.controller().snapshot().autostart, autostart.STALE)

    def test_ticking_once_replaces_an_old_value(self):
        self.reg.values["ccmonitor"] = r'"C:\py\pythonw.exe" "D:\repo\bin\ccmonitor-daemon.pyw"'
        c = self.controller()
        self.assertEqual(c.set_autostart(True).autostart, autostart.ON)
        self.assertEqual(self.reg.values["ccmonitor"], COMMAND)

    def test_unticking_removes_only_our_value(self):
        self.reg.values["other"] = "x"
        c = self.controller()
        c.set_autostart(True)
        self.assertEqual(c.set_autostart(False).autostart, autostart.OFF)
        self.assertEqual(self.reg.values, {"other": "x"})

    def test_an_unreadable_run_key_reads_as_off(self):
        class Broken(FakeRegistry):
            def get(self, name):
                raise PermissionError(5, "access denied")
        self.assertEqual(self.controller(registry=Broken()).snapshot().autostart, autostart.OFF)


    def test_the_controller_and_the_command_line_tool_write_the_same_value(self):
        c = self.controller(autostart_command=None)  # the real default
        c.set_autostart(True)
        written = self.reg.values["ccmonitor"]
        self.assertEqual(written, autostart.default_command())
        self.assertTrue(written.endswith('ccmonitor.exe" --tray'), written)
        self.assertIn(str(Path(autostart.__file__).resolve().parent.parent / "bin"), written)

    # -- 接管: the old pythonw daemon -------------------------------------------------------
    OLD = Proc(4100, "pythonw.exe", r'"C:\py\pythonw.exe" "D:\repo\bin\ccmonitor-daemon.pyw"')
    OTHERS = [
        Proc(4200, "python.exe", r'python D:\repo\bin\ccmonitor-daemon.pyw'),  # not pythonw
        Proc(4300, "pythonw.exe", r'"C:\py\pythonw.exe" D:\tools\clock.pyw'),  # another program
        Proc(4400, "pythonw.exe", r'"C:\py\pythonw.exe" -m ccmonitor.gui --tray'),  # the new one
        Proc(4500, "pythonw.exe", None),  # command line unreadable
    ]

    def busy_port_controller(self, procs):
        self.killed: list[int] = []
        c = self.controller(processes=lambda: list(procs), kill=self.on_kill)
        self.busy = True
        return c

    def on_kill(self, pid):
        self.killed.append(pid)
        self.busy = False  # the old daemon's port is free again

    def factory(self, port, presence=None):
        service = FakeService(port, presence)
        if getattr(self, "busy", False):
            service.fail = OSError(10048, "address in use")
        self.services.append(service)
        return service

    def test_an_old_daemon_holding_the_port_is_shown_with_a_takeover(self):
        c = self.busy_port_controller([self.OLD, *self.OTHERS])
        snap = c.restore()
        self.assertEqual(snap.state, "出错")
        self.assertIn("旧的后台 ccmonitor", snap.detail)
        self.assertTrue(c.legacy_daemon())

    def test_takeover_ends_only_the_old_launchers_pythonw_and_runs(self):
        c = self.busy_port_controller([self.OLD, *self.OTHERS])
        c.restore()
        snap = c.take_over()
        self.assertEqual(self.killed, [4100])
        self.assertEqual(snap.state, "运行中")
        self.assertFalse(c.legacy_daemon())

    def test_a_busy_port_without_an_old_daemon_offers_no_takeover(self):
        c = self.busy_port_controller(self.OTHERS)
        snap = c.restore()
        self.assertIn("占用", snap.detail)
        self.assertFalse(c.legacy_daemon())
        c.take_over()
        self.assertEqual(self.killed, [])

    def test_no_process_list_is_read_while_the_port_is_free(self):
        def boom():
            raise AssertionError("listed processes")
        self.assertEqual(self.controller(processes=boom).restore().state, "运行中")


if __name__ == "__main__":
    unittest.main()
