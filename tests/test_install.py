import json
import tempfile
import unittest
from pathlib import Path

from ccmonitor.install import EVENTS, install, uninstall

CMD = 'python -I "D:/x/hooks/relay.py"'


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "settings.json"

    def tearDown(self):
        self.dir.cleanup()

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_creates_file_with_one_entry_per_event(self):
        install(self.path, CMD)
        hooks = self.read()["hooks"]
        self.assertEqual(sorted(hooks), sorted(EVENTS))
        for e in EVENTS:
            hook = {"type": "command", "command": CMD, "timeout": 5}
            if e in ("PostToolUse", "PostToolUseFailure"):
                hook["async"] = True  # every tool call fires these; Claude Code must not wait
            group = {"matcher": "mcp__ccd_session__start_session"} if e == "PreToolUse" else {}
            self.assertEqual(hooks[e], [{**group, "hooks": [hook]}])

    def test_keeps_existing_settings_and_hooks(self):
        other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.sh"}]}
        self.path.write_text(json.dumps({"model": "opus", "hooks": {"Stop": [other]}}), encoding="utf-8")
        install(self.path, CMD)
        s = self.read()
        self.assertEqual(s["model"], "opus")
        self.assertEqual(s["hooks"]["Stop"][0], other)
        self.assertEqual(len(s["hooks"]["Stop"]), 2)

    def test_is_idempotent_and_replaces_old_command(self):
        install(self.path, 'python -I "D:/old/hooks/relay.py"')
        install(self.path, CMD)
        install(self.path, CMD)
        self.assertEqual(self.read()["hooks"]["Stop"], [{"hooks": [{"type": "command", "command": CMD, "timeout": 5}]}])

    def test_uninstall_removes_only_ccmonitor_entries(self):
        other = {"hooks": [{"type": "command", "command": "guard.sh"}]}
        self.path.write_text(json.dumps({"hooks": {"Stop": [other]}}), encoding="utf-8")
        install(self.path, CMD)
        uninstall(self.path)
        self.assertEqual(self.read(), {"hooks": {"Stop": [other]}})

    def test_uninstall_drops_empty_hooks_key(self):
        self.path.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
        install(self.path, CMD)
        uninstall(self.path)
        self.assertEqual(self.read(), {"model": "opus"})

    def test_dry_run_does_not_write(self):
        result = install(self.path, CMD, dry_run=True)
        self.assertFalse(self.path.exists())
        self.assertIn("Stop", result["hooks"])


if __name__ == "__main__":
    unittest.main()
