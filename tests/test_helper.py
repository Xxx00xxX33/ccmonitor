import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import unittest
from pathlib import Path

from ccmonitor.daemon import Daemon
from ccmonitor.desktop import DesktopWriteBack
from ccmonitor.helper import HelperRelay

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
HELPER_SID = "helper-cli-id"


def run_hook(script, port, payload, env=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    env = {**os.environ, "CCMONITOR_PORT": str(port), **(env or {})}
    return subprocess.run([sys.executable, "-I", str(HOOKS / script)], input=data, capture_output=True, env=env, timeout=10)


class HelperRelayTest(unittest.TestCase):
    def setUp(self):
        self.notices = queue.Queue()
        self.loop = asyncio.new_event_loop()
        self.helper = HelperRelay(self.loop, find_local_id=lambda cli: "local_" + cli, ack_seconds=5)
        self.daemon = Daemon(port=0, sink=self.notices.put, helper=self.helper)
        started = threading.Event()

        def serve():
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self.daemon.start())
            self.loop.run_until_complete(self.helper.start(0))
            started.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()
        started.wait(5)
        self.feed = None

    def tearDown(self):
        if self.feed:
            self.feed.kill()
            self.feed.wait(5)
        asyncio.run_coroutine_threadsafe(self.helper.stop(), self.loop).result(5)
        asyncio.run_coroutine_threadsafe(self.daemon.stop(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()

    def start_feed(self):
        env = {**os.environ, "CCMONITOR_PORT": str(self.daemon.port), "CCMONITOR_FEED_PORT": str(self.helper.port),
               "CLAUDE_CODE_SESSION_ID": HELPER_SID}
        self.feed = subprocess.Popen([sys.executable, "-I", str(HOOKS / "relay_feed.py")],
                                     stdout=subprocess.PIPE, env=env)
        for _ in range(50):
            if self.helper.connected:
                return
            threading.Event().wait(0.1)
        self.fail("feed did not connect")

    def send_in_background(self, cli, text):
        result = queue.Queue()
        threading.Thread(target=lambda: result.put(self.helper.send_prompt(cli, text)), daemon=True).start()
        return result

    def test_not_connected(self):
        self.assertIn("未连接", self.helper.send_prompt("cli-1", "继续"))

    def test_request_reaches_feed_and_ack_completes_it(self):
        self.start_feed()
        result = self.send_in_background("cli-1", "改用方案二\n第二行")
        line = json.loads(self.feed.stdout.readline().decode("utf-8"))
        self.assertEqual((line["to"], line["text"]), ("local_cli-1", "改用方案二\n第二行"))
        run_hook("relay_ack.py", self.daemon.port, {"ccmonitor": "ack", "id": line["id"], "result": "delivered"})
        self.assertIn("送达", result.get(timeout=5))

    def test_queued_ack(self):
        self.start_feed()
        result = self.send_in_background("cli-1", "x")
        line = json.loads(self.feed.stdout.readline().decode("utf-8"))
        run_hook("relay_ack.py", self.daemon.port, {"ccmonitor": "ack", "id": line["id"], "result": "queued"})
        self.assertIn("排队", result.get(timeout=5))

    def test_missing_ack_times_out(self):
        self.helper._ack_seconds = 0.5
        self.start_feed()
        self.assertIn("没收到回执", self.helper.send_prompt("cli-1", "x"))

    def test_feed_disconnect_is_noticed(self):
        self.start_feed()
        self.feed.kill()
        self.feed.wait(5)
        for _ in range(50):
            if not self.helper.connected:
                break
            threading.Event().wait(0.1)
        self.assertFalse(self.helper.connected)

    def test_archived_helper_loses_its_feed_and_the_feed_process_exits(self):
        archived = set()
        self.helper._archived = archived.__contains__
        self.start_feed()
        self.assertFalse(self.helper.drop_if_archived())  # still live in Desktop
        self.assertTrue(self.helper.connected)
        archived.add(HELPER_SID)
        self.assertTrue(self.helper.drop_if_archived())
        self.assertFalse(self.helper.connected)
        self.assertIn("未连接", self.helper.send_prompt("cli-1", "x"))  # falls back to mode A
        self.assertEqual(self.feed.wait(5), 0)  # the orphaned feed ends by itself

    def test_rotation_is_not_mistaken_for_archiving(self):
        self.helper._archived = lambda sid: True
        self.start_feed()
        self.helper.pause("ccmonitor 转发")
        self.assertFalse(self.helper.drop_if_archived())
        self.assertTrue(self.helper.attached)

    def test_helper_sessions_own_events_are_ignored(self):
        self.start_feed()
        run_hook("relay.py", self.daemon.port, {"hook_event_name": "Stop", "session_id": HELPER_SID,
                                                "last_assistant_message": "已转发"})
        run_hook("relay.py", self.daemon.port, {"hook_event_name": "Stop", "session_id": "other",
                                                "last_assistant_message": "别的会话"})
        self.assertEqual(self.notices.get(timeout=5).text, "别的会话")
        self.assertTrue(self.notices.empty())

    def test_unknown_session(self):
        self.helper._find_local_id = lambda cli: None
        self.start_feed()
        self.assertIn("找不到", self.helper.send_prompt("cli-1", "x"))

    def test_never_forwards_to_itself(self):
        self.start_feed()
        self.assertIn("转发会话自己", self.helper.send_prompt(HELPER_SID, "x"))

    def test_cannot_approve(self):
        self.assertIn("不能审批", self.helper.approve("cli-1", True, "x"))


class IsArchivedTest(unittest.TestCase):
    """sessions.is_archived reads Desktop's session records (local_*.json)."""

    def setUp(self):
        import tempfile
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)

    def record(self, name, **fields):
        path = self.root / "acct" / "org" / f"local_{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fields), encoding="utf-8")

    def test_archived_only_when_every_record_says_so(self):
        from ccmonitor.sessions import is_archived
        self.assertFalse(is_archived("h1", self.root))  # no record: unknown, not archived
        self.record("a", cliSessionId="h1", isArchived=False)
        self.assertFalse(is_archived("h1", self.root))
        self.record("a", cliSessionId="h1", isArchived=True)
        self.assertTrue(is_archived("h1", self.root))
        self.record("b", cliSessionId="h1")  # another live record of the same CLI session
        self.assertFalse(is_archived("h1", self.root))


class FallbackTest(unittest.TestCase):
    def wb(self, code):
        self.forwarded = []

        def fallback(sid, text, code):
            self.forwarded.append((sid, text))
            return "已经辅助会话送达"
        return DesktopWriteBack(driver=lambda req: {"code": code}, title_of=lambda s: "T", fallback=fallback)

    def test_busy_draft_and_breakage_fall_back_for_prompts(self):
        for code in ("busy", "draft_present", "no_window", "switch_failed", "error_COMException"):
            status = self.wb(code).send_prompt("cli-1", "继续")
            self.assertEqual(self.forwarded, [("cli-1", "继续")], code)
            self.assertIn("辅助会话", status)

    def test_success_and_uncertain_send_do_not_fall_back(self):
        # send_failed may already have delivered; forwarding again could send it twice.
        for code in ("sent", "send_failed"):
            self.wb(code).send_prompt("cli-1", "继续")
            self.assertEqual(self.forwarded, [], code)

    def test_approvals_never_fall_back(self):
        self.wb("no_window").approve("cli-1", True, "x")
        self.assertEqual(self.forwarded, [])


if __name__ == "__main__":
    unittest.main()
