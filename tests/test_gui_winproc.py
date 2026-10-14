"""The window's actions that arrive through a Win32 window procedure (the tray icon, a
later launch) must not touch Tk inside that procedure.

A Tk call made inside a window procedure that Tk's own message loop dispatched clears
_tkinter's saved thread state; the next Python callback Tcl runs before that event ends
(an after() timer that is already due) then aborts the process with "Fatal Python error:
PyEval_RestoreThread: ... the current Python thread state is NULL" (pythonw: 0xc0000409
in ucrtbase.dll). That is how 「停用」 from the tray menu killed ccmonitor.

The real window runs in a child process, since the failure is a process abort. Windows
only; it shows a tray icon for a moment.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CHILD = r'''
import sys, time
import tkinter as tk
from pathlib import Path
from ccmonitor import instance, tray
from ccmonitor.controller import Controller
from ccmonitor.gui import Window
from ccmonitor.win32 import user32
from tests.test_controller import FakeService
from tests.test_resident import FakeRegistry

data = Path(sys.argv[1])
c = Controller(registry=FakeRegistry(), port=1, service_factory=FakeService, data_dir=data,
               env={}, last_input=lambda: 0)
c.enable()
root = tk.Tk()
root.withdraw()  # hidden in the tray, as when the bug was seen
w = Window(root, c)
w.listen(1)


def menu_pick(menu, flags, x, y, reserved, hwnd, rect):
    """TrackPopupMenu: the menu stays open a moment, then the 启用/停用 item is chosen."""
    time.sleep(0.05)  # Tcl services nothing meanwhile: the timer below falls due
    labels = [item and item[0] for item in w.tray_menu()]
    return labels.index(c.snapshot().toggle_label) + 1


tray.user32.TrackPopupMenu = menu_pick
rounds = ["tray"] * 6 + ["show"]
seen = []


def step():
    if w._busy:
        root.after(20, step)
        return
    seen.append(c.snapshot().state)
    if not rounds:
        print("STATES", ",".join(seen), flush=True)
        w.tray.close()
        root.destroy()
        return
    root.after(0, lambda: None)  # a Python callback due when the window procedure returns
    if rounds.pop(0) == "tray":
        user32.PostMessageW(w.tray._hwnd, tray.WM_TRAY, 0, tray.WM_RBUTTONUP)
    else:
        user32.PostMessageW(w.listener._window.hwnd, w.listener._show_msg, 0, 0)
    root.after(150, step)


root.after(50, step)
root.mainloop()
print("EXIT OK", flush=True)
'''


class InboxTest(unittest.TestCase):
    def test_a_window_procedure_only_queues_and_the_timer_runs_in_order(self):
        from ccmonitor.gui import Inbox
        inbox, ran = Inbox(), []
        first, second = inbox.later(lambda: ran.append(1)), inbox.later(lambda: ran.append(2))
        first()
        second()
        self.assertEqual(ran, [])  # nothing runs inside the window procedure
        inbox.run_pending()
        self.assertEqual(ran, [1, 2])
        inbox.run_pending()
        self.assertEqual(ran, [1, 2])


@unittest.skipUnless(sys.platform == "win32", "Win32 tray")
class WindowProcedureTest(unittest.TestCase):
    def test_tray_toggle_and_show_from_a_later_launch_do_not_abort(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "ccm"
            data.mkdir()
            (data / "config.json").write_text(json.dumps({"telegram_token": "1:fake", "chat_id": 1}))
            env = dict(os.environ, CCMONITOR_HOME=str(data), APPDATA=tmp, LOCALAPPDATA=tmp,
                       PYTHONIOENCODING="utf-8")
            env.pop("CCMONITOR_TELEGRAM_TOKEN", None)
            env.pop("CCMONITOR_CHAT_ID", None)
            done = subprocess.run([sys.executable, "-c", CHILD, str(data)], cwd=ROOT, env=env,
                                  capture_output=True, encoding="utf-8", timeout=60)
            settings = json.loads((data / "settings.json").read_text())
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertIn("EXIT OK", done.stdout)
        # 停用, 启用, … from the tray, each remembered; the last tray action was 启用.
        self.assertIn("STATES 运行中,已停用,运行中,已停用,运行中,已停用,运行中,运行中", done.stdout)
        self.assertEqual(settings, {"enabled": True})


if __name__ == "__main__":
    unittest.main()
