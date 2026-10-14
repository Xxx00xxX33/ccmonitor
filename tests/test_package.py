import unittest

from ccmonitor import package


class InsideTest(unittest.TestCase):
    def test_package_identity_counts(self):
        self.assertTrue(package.inside(env={}, identity=lambda: True))

    def test_a_claude_session_counts_without_identity(self):
        # Claude Desktop's sessions have no package identity, yet their HKCU writes are redirected.
        self.assertTrue(package.inside(env={"CLAUDECODE": "1"}, identity=lambda: False))

    def test_a_login_start_is_outside(self):
        self.assertFalse(package.inside(env={}, identity=lambda: False))


class RelaunchTest(unittest.TestCase):
    def setUp(self):
        self.created = []

    def relaunch(self, inside=True, port=47821, env=None, created=True):
        self.create_result = created
        return package.relaunch_outside(
            ["--tray"], port=port, default_port=47821, env=env or {},
            is_inside=lambda env: inside, python="C:\\Py\\pythonw.exe", cwd="D:\\repo",
            create=lambda cmd, cwd: (self.created.append((cmd, cwd)), self.create_result)[1])

    def test_outside_the_package_nothing_happens(self):
        self.assertFalse(self.relaunch(inside=False))
        self.assertEqual(self.created, [])

    def test_inside_the_package_the_real_instance_restarts_outside(self):
        self.assertTrue(self.relaunch())
        self.assertEqual(self.created, [('C:\\Py\\pythonw.exe -m ccmonitor.gui --tray --port 47821', "D:\\repo")])

    def test_a_throwaway_instance_stays_where_it_is(self):
        # A process started outside the package gets the user's own environment, not ours:
        # a smoke test's port or CCMONITOR_HOME would be lost.
        self.assertFalse(self.relaunch(port=47971))
        self.assertFalse(self.relaunch(env={"CCMONITOR_HOME": "C:\\tmp\\x"}))
        self.assertEqual(self.created, [])

    def test_a_failed_relaunch_carries_on_inside(self):
        self.assertFalse(self.relaunch(created=False))
        self.assertEqual(len(self.created), 1)


if __name__ == "__main__":
    unittest.main()
