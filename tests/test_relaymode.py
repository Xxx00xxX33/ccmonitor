import asyncio
import json
import queue
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from ccmonitor.daemon import Daemon
from ccmonitor.desktop import DesktopWriteBack
from ccmonitor.helper import HelperRelay
from ccmonitor.relaymode import PYTHON, SETUP_PROMPT, RelayMode, helper_brief, load_mode, save_mode
from ccmonitor.rotation import EVERY_SECONDS, RECONNECT_SECONDS, RETRY_SECONDS, HelperRotation


class ModeStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "relay-mode.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_default_is_a(self):
        self.assertEqual(load_mode(self.path), "a")
        self.path.write_text("{", encoding="utf-8")
        self.assertEqual(load_mode(self.path), "a")
        self.path.write_text('{"mode": "z"}', encoding="utf-8")
        self.assertEqual(load_mode(self.path), "a")

    def test_set_persists(self):
        RelayMode(path=self.path).set("b")
        self.assertEqual(load_mode(self.path), "b")
        self.assertEqual(RelayMode(path=self.path).mode, "b")

    def test_bad_mode_is_refused(self):
        with self.assertRaises(ValueError):
            save_mode("c", self.path)

    def test_helper_brief_is_steps_a_person_can_follow_anywhere(self):
        brief = helper_brief()
        for part in ("新建一个会话", "low", "Bypass permissions", SETUP_PROMPT):
            self.assertIn(part, brief)
        self.assertNotIn("start_session", brief)  # not in every Claude Desktop build (2026-10-14)

    def test_setup_prompt_runs_this_python_not_whatever_is_on_path(self):
        self.assertIn(f'"{PYTHON}" -I', SETUP_PROMPT)
        self.assertNotIn(" python -I", SETUP_PROMPT)
        self.assertNotIn("pythonw", PYTHON.lower().rsplit("/", 1)[-1])  # Monitor needs stdout

    def test_setup_prompt_stops_cleanly_without_the_tools_it_needs(self):
        for tool in ("Monitor", "mcp__ccd_session_mgmt__send_message"):
            self.assertIn(tool, SETUP_PROMPT.split("\n1.")[0])  # checked before anything else
        self.assertIn("切回转发模式 A", SETUP_PROMPT)


class FakeHelper:
    def __init__(self):
        self.session_id, self.since, self.paused_title, self._writer = "h1", 0.0, None, object()
        self.pauses = []

    @property
    def connected(self):
        return self._writer is not None and self.paused_title is None

    def pause(self, title):
        self.paused_title = title
        self.pauses.append(title)

    def resume(self):
        self.paused_title = None


class RotationTest(unittest.TestCase):
    def setUp(self):
        self.helper = FakeHelper()
        self.clock = EVERY_SECONDS + 1.0
        self.mode, self.away = "b", True
        self.writes, self.codes, self.reports = [], [], []
        self.rot = HelperRotation(self.helper, self.write, lambda sid: "ccmonitor 转发" if sid == "h1" else None,
                                  lambda: self.away, lambda: self.mode, self.reports.append, now=lambda: self.clock,
                                  sleep=lambda s: None)

    def write(self, title, text):
        self.writes.append((title, text))
        code = self.codes.pop(0) if self.codes else "sent"
        return code, code

    def test_rotates_with_clear_then_setup_prompt(self):
        self.rot.check()
        self.assertEqual(self.writes, [("ccmonitor 转发", "/clear"), ("ccmonitor 转发", SETUP_PROMPT)])
        self.assertEqual(self.helper.paused_title, "ccmonitor 转发")  # no forwards until it reconnects
        self.assertEqual(self.reports, [])

    def test_not_before_six_hours(self):
        self.clock = EVERY_SECONDS - 1
        self.rot.check()
        self.assertEqual(self.writes, [])

    def test_only_in_mode_b(self):
        self.mode = "a"
        self.rot.check()
        self.assertEqual(self.writes, [])

    def test_waits_until_the_user_is_away(self):
        self.away = False
        self.rot.check()
        self.assertEqual(self.writes, [])
        self.away = True
        self.rot.check()
        self.assertEqual(len(self.writes), 2)

    def test_busy_helper_is_retried_next_check(self):
        self.codes = ["busy"]
        self.rot.check()
        self.assertIsNone(self.helper.paused_title)
        self.assertEqual(self.reports, [])
        self.rot.check()
        self.assertEqual(self.writes[-1], ("ccmonitor 转发", SETUP_PROMPT))

    def test_failed_clear_resumes_and_retries_in_30_minutes(self):
        self.codes = ["no_window"]
        self.rot.check()
        self.assertIsNone(self.helper.paused_title)
        self.assertEqual(len(self.reports), 1)
        self.rot.check()
        self.assertEqual(len(self.writes), 1)
        self.clock += RETRY_SECONDS
        self.rot.check()
        self.assertEqual(len(self.writes), 3)

    def test_setup_prompt_waits_out_the_busy_moment_after_clear(self):
        self.codes = ["sent", "busy", "busy", "sent"]
        self.rot.check()
        self.assertEqual([t for _, t in self.writes], ["/clear", SETUP_PROMPT, SETUP_PROMPT, SETUP_PROMPT])
        self.assertEqual(self.reports, [])

    def test_setup_prompt_still_busy_after_all_tries_is_reported(self):
        from ccmonitor.rotation import PRIME_TRIES
        self.codes = ["sent"] + ["busy"] * PRIME_TRIES
        self.rot.check()
        self.assertEqual(len(self.writes), 1 + PRIME_TRIES)
        self.assertEqual(len(self.reports), 1)

    def test_failed_setup_prompt_is_reported_once(self):
        self.codes = ["sent", "fill_failed"]
        self.rot.check()
        self.assertEqual(len(self.reports), 1)
        self.assertIn("「复制启动指令」", self.reports[0])  # the window's button, not a command
        self.assertNotIn("python -m", self.reports[0])
        self.helper._writer = None  # the feed died with /clear
        self.clock += RECONNECT_SECONDS
        self.rot.check()
        self.assertEqual(len(self.reports), 1)
        self.assertIsNone(self.helper.paused_title)

    def test_missing_reconnect_is_reported(self):
        self.rot.check()
        self.helper._writer = None
        self.clock += RECONNECT_SECONDS
        self.rot.check()
        self.assertIn("没有重新连上", self.reports[-1])

    def test_reconnect_ends_the_rotation(self):
        self.rot.check()
        self.helper.paused_title, self.helper.since = None, self.clock  # the handshake resumes it
        self.clock += RECONNECT_SECONDS
        self.rot.check()
        self.assertEqual(self.reports, [])
        self.assertEqual(len(self.writes), 2)

    def test_unknown_title_is_reported_and_retried_later(self):
        self.helper.session_id = "gone"
        self.rot.check()
        self.assertEqual(self.writes, [])
        self.assertIn("找不到", self.reports[-1])


class HelperStateTest(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.clock = 100.0
        self.helper = HelperRelay(self.loop, title_of=lambda sid: {"h": "ccmonitor 转发", "r": "改过名的转发"}.get(sid[0], "别的"),
                                  now=lambda: self.clock)

    def tearDown(self):
        self.loop.close()

    def hello(self, sid):
        class W:
            def close(self):
                pass

        async def run():
            reader = asyncio.StreamReader()
            reader.feed_data((json.dumps({"session_id": sid}) + "\n").encode())
            reader.feed_eof()
            task = asyncio.ensure_future(self.helper._on_feed(reader, W()))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return task
        task = self.loop.run_until_complete(run())
        self.loop.run_until_complete(task)

    def test_since_starts_at_first_connect_and_survives_rearming(self):
        self.hello("h1")
        self.assertEqual(self.helper.since, 100.0)
        self.clock = 200.0
        self.hello("h1")  # Monitor re-armed: same context
        self.assertEqual(self.helper.since, 100.0)

    def test_rotation_handshake_resets_since_and_resumes(self):
        self.hello("h1")
        self.helper.pause("ccmonitor 转发")
        self.assertFalse(self.helper.connected)
        self.clock = 300.0
        self.hello("h2")  # /clear gave it a new CLI session id
        self.assertEqual((self.helper.since, self.helper.paused_title, self.helper.session_id), (300.0, None, "h2"))

    def test_events_are_recognised_by_id_or_title(self):
        self.helper.session_id = "h1"
        self.assertTrue(self.helper.is_helper("h1"))
        self.assertTrue(self.helper.is_helper("h2"))  # its standard title, e.g. its first turn
        self.assertFalse(self.helper.is_helper("r1"))
        self.helper.pause("改过名的转发")
        self.assertTrue(self.helper.is_helper("r1"))  # the title it had when the rotation began
        self.assertFalse(self.helper.is_helper("x9"))
        self.assertFalse(self.helper.is_helper(""))


class DaemonModeControlTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.mode = RelayMode(path=Path(self.dir.name) / "m.json")
        self.daemon = Daemon(port=0, sink=queue.Queue().put, relay_mode=self.mode)
        self.loop = asyncio.new_event_loop()
        started = threading.Event()

        def serve():
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self.daemon.start())
            started.set()
            self.loop.run_forever()
        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()
        started.wait(5)

    def tearDown(self):
        asyncio.run_coroutine_threadsafe(self.daemon.stop(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()
        self.dir.cleanup()

    def ask(self, request):
        with socket.create_connection(("127.0.0.1", self.daemon.port), timeout=5) as s:
            s.sendall(json.dumps({"ccmonitor": "mode", **request}).encode())
            s.shutdown(socket.SHUT_WR)
            return json.loads(s.makefile("rb").read().decode())

    def test_status_and_switch(self):
        self.assertEqual(self.ask({}), {"mode": "a", "helper": "未连接"})
        self.assertEqual(self.ask({"mode": "b"})["mode"], "b")
        self.assertEqual(RelayMode(path=Path(self.dir.name) / "m.json").mode, "b")
        self.assertEqual(self.ask({"mode": "zzz"})["mode"], "b")


class CommandLineTest(unittest.TestCase):
    def test_without_daemon_the_choice_is_saved_for_the_next_start(self):
        import contextlib
        import io
        import os
        from unittest import mock
        from ccmonitor import relaymode

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "relay-mode.json"
            out = io.StringIO()
            with mock.patch.object(relaymode.paths, "DATA_DIR", Path(d)), \
                    mock.patch.object(relaymode, "ask_daemon", lambda req, port: None), \
                    mock.patch.dict(os.environ, {"CCMONITOR_PORT": "1"}), contextlib.redirect_stdout(out):
                relaymode.main(["b"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"mode": "b"})
            self.assertIn("下次启动生效", out.getvalue())

    def test_switching_to_b_without_a_helper_prints_how_to_make_one(self):
        import contextlib
        import io
        from unittest import mock
        from ccmonitor import relaymode

        out = io.StringIO()
        with mock.patch.object(relaymode, "ask_daemon", lambda req, port: {"mode": "b", "helper": "未连接"}), \
                contextlib.redirect_stdout(out):
            relaymode.main(["b"])
        self.assertIn("Bypass permissions", out.getvalue())


class TitleWriteTest(unittest.TestCase):
    def test_try_prompt_title_uses_the_title_as_given(self):
        requests = []
        wb = DesktopWriteBack(driver=lambda r: requests.append(r) or {"code": "sent"}, title_of=lambda s: None)
        self.assertEqual(wb.try_prompt_title("ccmonitor 转发", "/clear")[1], "sent")
        self.assertEqual(requests, [{"action": "prompt", "title": "ccmonitor 转发", "text": "/clear",
                                     "interject": False}])  # /clear never goes into a running turn


if __name__ == "__main__":
    unittest.main()
