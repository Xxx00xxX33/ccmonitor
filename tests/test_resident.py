import datetime
import unittest

from ccmonitor.autostart import Autostart
from ccmonitor.health import Health


class FakeDay:
    def __init__(self):
        self.day = datetime.date(2026, 10, 11)

    def __call__(self):
        return self.day

    def next(self):
        self.day += datetime.timedelta(days=1)


class HealthTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.today = FakeDay()
        self.h = Health(self.sent.append, today=self.today)

    def test_first_failure_of_a_kind_is_reported(self):
        self.h.report("write", "找不到 Desktop 窗口")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("找不到 Desktop 窗口", self.sent[0])

    def test_same_kind_is_silent_for_the_rest_of_the_day(self):
        for _ in range(5):
            self.h.report("write", "x")
        self.assertEqual(len(self.sent), 1)

    def test_kinds_are_independent(self):
        self.h.report("write", "x")
        self.h.report("telegram", "y")
        self.assertEqual(len(self.sent), 2)

    def test_reminds_again_next_day(self):
        self.h.report("write", "x")
        self.today.next()
        self.h.report("write", "x")
        self.assertEqual(len(self.sent), 2)

    def test_counts_silent_failures_in_status(self):
        for _ in range(3):
            self.h.report("write", "x")
        self.assertIn("write 3", self.h.summary())

    def test_sender_failure_is_swallowed(self):
        def broken(text):
            raise OSError("down")
        Health(broken, today=self.today).report("write", "x")  # must not raise


class FakeRegistry:
    def __init__(self):
        self.values = {}

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        self.values.pop(name, None)


class AutostartTest(unittest.TestCase):
    def setUp(self):
        self.reg = FakeRegistry()
        self.a = Autostart(self.reg, command='"D:/x/bin/ccmonitor.exe" --tray')

    def test_enable_writes_the_run_entry(self):
        self.a.enable()
        self.assertEqual(self.reg.values, {"ccmonitor": '"D:/x/bin/ccmonitor.exe" --tray'})
        self.assertTrue(self.a.enabled())

    def test_disable_removes_only_our_entry(self):
        self.reg.values["other"] = "x"
        self.a.enable()
        self.a.disable()
        self.assertEqual(self.reg.values, {"other": "x"})
        self.assertFalse(self.a.enabled())

    def test_stale_command_is_reported_as_not_current(self):
        self.reg.values["ccmonitor"] = '"C:/old/pythonw.exe" "D:/old/bin/ccmonitor-daemon.pyw"'
        self.assertFalse(self.a.enabled())
        self.assertIn("old", self.a.status())


if __name__ == "__main__":
    unittest.main()
