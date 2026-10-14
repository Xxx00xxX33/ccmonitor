import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ccmonitor import paths


class PathsTest(unittest.TestCase):
    """A fake user profile: real AppData, plus the copy Windows keeps for the Claude package."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.roaming = root / "Roaming"
        self.local = root / "Local"
        self.package = self.local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache"
        self.data = root / "home" / ".ccmonitor"
        env = mock.patch.dict(os.environ, {"APPDATA": str(self.roaming), "LOCALAPPDATA": str(self.local)})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def put(self, path: Path, data, age: float = 0) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        if age:
            t = time.time() - age
            os.utime(path, (t, t))
        return path

    def test_sessions_are_found_in_the_package_when_appdata_has_none(self):
        # A daemon started at login sees the real AppData, where Desktop wrote nothing.
        self.put(self.package / "Roaming" / "Claude" / "claude-code-sessions" / "a" / "b" / "local_1.json", {})
        self.assertEqual(paths.desktop_sessions_root(),
                         self.package / "Roaming" / "Claude" / "claude-code-sessions")

    def test_sessions_seen_through_appdata_are_used_when_present(self):
        # Inside the package, or with an unpackaged Desktop, %APPDATA% shows them.
        self.put(self.roaming / "Claude" / "claude-code-sessions" / "a" / "b" / "local_1.json", {})
        self.assertEqual(paths.desktop_sessions_root(), self.roaming / "Claude" / "claude-code-sessions")

    def test_without_any_sessions_the_appdata_place_is_named(self):
        self.assertEqual(paths.desktop_sessions_root(), self.roaming / "Claude" / "claude-code-sessions")

    def test_topics_from_both_old_places_are_merged_keeping_the_latest_pairing(self):
        self.put(self.package / "Local" / "ccmonitor" / "topics.json",
                 {"s1": [11, 100], "s2": [12, 200], "s3": 13}, age=600)
        self.put(self.local / "ccmonitor" / "topics.json", {"s2": [22, 300], "s4": [24, 400]})
        self.assertEqual(paths.migrate(self.data), ["topics.json"])
        merged = json.loads((self.data / "topics.json").read_text(encoding="utf-8"))
        self.assertEqual(merged, {"s1": [11, 100], "s2": [22, 300], "s3": 13, "s4": [24, 400]})

    def test_the_newest_relay_mode_is_brought_over(self):
        self.put(self.local / "ccmonitor" / "relay-mode.json", '"a"', age=600)
        self.put(self.package / "Local" / "ccmonitor" / "relay-mode.json", '"b"')
        self.assertEqual(paths.migrate(self.data), ["relay-mode.json"])
        self.assertEqual((self.data / "relay-mode.json").read_text(encoding="utf-8"), '"b"')

    def test_migration_leaves_files_already_in_the_new_place(self):
        self.put(self.data / "topics.json", {"s9": [99, 900]})
        self.put(self.data / "relay-mode.json", '"a"')
        self.put(self.local / "ccmonitor" / "topics.json", {"s1": [11, 100]})
        self.put(self.local / "ccmonitor" / "relay-mode.json", '"b"')
        self.assertEqual(paths.migrate(self.data), [])
        self.assertEqual(json.loads((self.data / "topics.json").read_text(encoding="utf-8")), {"s9": [99, 900]})

    def test_nothing_to_migrate_creates_nothing(self):
        self.assertEqual(paths.migrate(self.data), [])
        self.assertFalse(self.data.exists())

    def test_config_is_read_where_an_older_version_kept_it(self):
        env = {k: v for k, v in os.environ.items() if k != "CCMONITOR_HOME"}
        with mock.patch.object(paths, "DATA_DIR", self.data):
            self.assertEqual(paths.config_file(env=env), self.data / "config.json")
            old = self.put(self.package / "Roaming" / "ccmonitor" / "config.json", {})
            self.assertEqual(paths.config_file(env=env), old)
            new = self.put(self.data / "config.json", {})
            self.assertEqual(paths.config_file(env=env), new)

    def test_a_ccmonitor_home_never_borrows_an_older_versions_config(self):
        # 2026-10-14: a throwaway window on a temp CCMONITOR_HOME read the real token from
        # %APPDATA% and polled the real bot, so the running daemon got 409 Conflict.
        self.put(self.roaming / "ccmonitor" / "config.json", {})
        self.put(self.package / "Roaming" / "ccmonitor" / "config.json", {})
        env = dict(os.environ, CCMONITOR_HOME=str(self.data))
        self.assertEqual(paths.config_file(self.data, env), self.data / "config.json")


if __name__ == "__main__":
    unittest.main()
