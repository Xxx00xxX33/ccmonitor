import asyncio
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ccmonitor.daemon import Daemon
from ccmonitor.notice import Level
from ccmonitor.options import AWAY_INSTRUCTION, OPTIONS_INSTRUCTION
from ccmonitor.stuck import StuckWatcher

RELAY = Path(__file__).resolve().parent.parent / "hooks" / "relay.py"


def run_relay(port: int, payload, state_dir: str | None = None) -> subprocess.CompletedProcess:
    data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    env = {**os.environ, "CCMONITOR_PORT": str(port)}
    if state_dir:
        env["LOCALAPPDATA"] = state_dir  # where the relay keeps its "daemon down since" mark
    return subprocess.run([sys.executable, "-I", str(RELAY)], input=data, capture_output=True, env=env, timeout=10)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class IntakeTest(unittest.TestCase):
    def setUp(self):
        self.notices = queue.Queue()
        self.away = False
        self.turn_ends = queue.Queue()
        self.daemon = Daemon(port=0, sink=self.notices.put, presence=self, on_turn_end=self.turn_ends.put)
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

    def is_away(self):
        return self.away

    def tearDown(self):
        asyncio.run_coroutine_threadsafe(self.daemon.stop(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()

    def test_session_event_goes_before_the_events_own_notice(self):
        order = queue.Queue()
        self.daemon._on_session_event = lambda event, sid, payload: order.put(("event", event, sid))
        self.daemon._sink = lambda n: order.put(("notice", n.kind, n.session_id))
        run_relay(self.daemon.port, {"hook_event_name": "Stop", "session_id": "s7", "last_assistant_message": "好了"})
        self.assertEqual(order.get(timeout=5), ("event", "Stop", "s7"))
        self.assertEqual(order.get(timeout=5), ("notice", "completed", "s7"))

    def test_tool_events_reach_the_session_event_without_the_tool_result(self):
        got = queue.Queue()
        self.daemon._on_session_event = lambda event, sid, payload: got.put((event, payload))
        run_relay(self.daemon.port, {"hook_event_name": "PostToolUse", "session_id": "s8", "tool_name": "Bash",
                                     "tool_input": {"command": "ls"}, "tool_response": {"stdout": "secret"}})
        event, payload = got.get(timeout=5)
        self.assertEqual((event, payload["tool_input"]), ("PostToolUse", {"command": "ls"}))
        self.assertNotIn("tool_response", payload)  # the relay drops it
        self.assertTrue(self.notices.empty())

    def test_permission_request_reaches_sink_as_urgent(self):
        r = run_relay(self.daemon.port, {"hook_event_name": "PermissionRequest", "session_id": "s1", "cwd": "D:\\p",
                                         "tool_name": "Bash", "tool_input": {"command": "echo 你好"}})
        self.assertEqual(r.returncode, 0)
        n = self.notices.get(timeout=5)
        self.assertEqual(n.level, Level.URGENT)
        self.assertIn("echo 你好", n.text)

    def test_relay_prints_nothing(self):
        # Hook stdout can reach the model; outside the options line the relay must stay silent.
        r = run_relay(self.daemon.port, {"hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": "hi"})
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))

    def test_turn_end_is_reported_for_held_prompts(self):
        run_relay(self.daemon.port, {"hook_event_name": "UserPromptSubmit", "session_id": "s3", "prompt": "x"})
        run_relay(self.daemon.port, {"hook_event_name": "Stop", "session_id": "s3", "last_assistant_message": "好"})
        self.assertEqual(self.turn_ends.get(timeout=5), "s3")
        self.assertTrue(self.turn_ends.empty())

    def test_events_from_one_session_keep_order(self):
        run_relay(self.daemon.port, {"hook_event_name": "UserPromptSubmit", "session_id": "s2", "cwd": "D:\\p", "prompt": "做 A"})
        run_relay(self.daemon.port, {"hook_event_name": "Stop", "session_id": "s2", "cwd": "D:\\p", "last_assistant_message": "好"})
        n = self.notices.get(timeout=5)
        self.assertEqual(n.session_label, "p · 做 A")

    def test_large_payload_arrives_whole(self):
        msg = "长" * 300_000 + "\n选项：\n1. 按刚才说的继续"
        run_relay(self.daemon.port, {"hook_event_name": "Stop", "session_id": "s4", "last_assistant_message": msg})
        n = self.notices.get(timeout=5)
        self.assertEqual(n.options, ["按刚才说的继续"])

    def test_garbage_input_is_dropped_and_daemon_survives(self):
        self.assertEqual(run_relay(self.daemon.port, b"not json").returncode, 0)
        run_relay(self.daemon.port, {"hook_event_name": "Stop", "session_id": "s3", "last_assistant_message": "ok"})
        self.assertEqual(self.notices.get(timeout=5).text, "ok")


    def test_prompt_while_away_gets_the_away_instruction(self):
        self.away = True
        r = run_relay(self.daemon.port, {"hook_event_name": "UserPromptSubmit", "session_id": "s5", "prompt": "hi"})
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout.decode("utf-8"))
        self.assertEqual(out, {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                      "additionalContext": AWAY_INSTRUCTION}})

    def test_prompt_at_desk_gets_no_instruction(self):
        r = run_relay(self.daemon.port, {"hook_event_name": "UserPromptSubmit", "session_id": "s5", "prompt": "hi"})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, b"")

    def test_the_instruction_no_longer_fixes_the_first_option(self):
        self.assertNotIn("按刚才说的继续", AWAY_INSTRUCTION)
        self.assertNotIn("不在电脑前", OPTIONS_INSTRUCTION)
        self.assertTrue(AWAY_INSTRUCTION.endswith(OPTIONS_INSTRUCTION))

    def test_english_answers_are_told_to_use_the_options_heading(self):
        # Seen 2026-10-12: English answers kept 「选项：」 unless told otherwise (VERIFY.md).
        self.assertIn("用英文回答时写「Options:」", OPTIONS_INSTRUCTION)

    def test_the_instruction_rules_out_options_that_need_material_first(self):
        self.assertIn("不要列出需要用户先提供材料或信息才能执行的项", OPTIONS_INSTRUCTION)

    def test_other_events_print_nothing_even_when_away(self):
        self.away = True
        for event in ("Stop", "PermissionRequest", "SessionStart"):
            r = run_relay(self.daemon.port, {"hook_event_name": event, "session_id": "s6"})
            self.assertEqual(r.stdout, b"", event)

    def test_quiet_turn_reaches_sink_as_stuck(self):
        self.daemon._watcher = StuckWatcher(self.daemon._classifier, read_tail=lambda p: None, quiet_seconds=0)
        run_relay(self.daemon.port, {"hook_event_name": "UserPromptSubmit", "session_id": "s9", "cwd": "D:\\p",
                                     "prompt": "跑测试", "transcript_path": "D:/none.jsonl"})
        task = asyncio.run_coroutine_threadsafe(self.daemon.watch_stuck(every=0.1), self.loop)
        try:
            n = self.notices.get(timeout=5)
        finally:
            task.cancel()
        self.assertEqual((n.kind, n.session_label), ("stuck", "p · 跑测试"))

    def test_away_verdict_changes_are_logged_once_each(self):
        prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "s5", "prompt": "hi"}
        with self.assertLogs("ccmonitor", level="INFO") as logs:
            for away in (False, False, True, True, False):
                self.away = away
                run_relay(self.daemon.port, prompt)
        verdicts = [r.getMessage() for r in logs.records if r.getMessage().startswith("presence")]
        self.assertEqual(verdicts, ["presence at desk", "presence away", "presence at desk"])

    def test_presence_failure_still_asks_for_options_but_not_away(self):
        def broken():
            raise OSError("boom")
        self.is_away = broken
        r = run_relay(self.daemon.port, {"hook_event_name": "UserPromptSubmit", "session_id": "s5", "prompt": "hi"})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout.decode("utf-8"))["hookSpecificOutput"]["additionalContext"],
                         OPTIONS_INSTRUCTION)


class FakeServer:
    """Accepts one connection, reads the payload, then replies with `reply` or stays silent."""

    def __init__(self, reply: bytes | None, hold: float = 0):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, args=(reply, hold), daemon=True).start()

    def _serve(self, reply, hold):
        conn, _ = self.sock.accept()
        with conn:
            while conn.recv(65536):
                pass
            if reply is not None:
                conn.sendall(reply)
            time.sleep(hold)
        self.sock.close()


PROMPT = {"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": "hi"}


class RelayReplyTest(unittest.TestCase):
    def test_malformed_reply_is_not_printed(self):
        for reply in (b"not json", b'{"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": "x"}}',
                      b'{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": 5}}',
                      b'{"decision": "block", "reason": "x"}'):
            r = run_relay(FakeServer(reply).port, PROMPT)
            self.assertEqual((r.returncode, r.stdout), (0, b""), reply)

    def test_silent_daemon_does_not_hang_the_prompt(self):
        t = time.monotonic()
        r = run_relay(FakeServer(None, hold=5).port, PROMPT)
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        self.assertLess(time.monotonic() - t, 3)


class RelayWithoutDaemonTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.mark = Path(self.dir.name) / "ccmonitor" / "relay-down"

    def tearDown(self):
        self.dir.cleanup()

    def test_waits_for_a_daemon_that_is_restarting(self):
        # A turn that ends while the daemon restarts must not lose its notice (seen live
        # 2026-10-12 11:54: the Stop hook landed in a 3-second gap).
        port = free_port()
        got = queue.Queue()

        def late_server():
            time.sleep(1.5)
            with socket.socket() as srv:
                srv.bind(("127.0.0.1", port))
                srv.listen()
                srv.settimeout(5)
                conn, _ = srv.accept()
                with conn:
                    got.put(conn.recv(65536))
        threading.Thread(target=late_server, daemon=True).start()
        r = run_relay(port, {"hook_event_name": "Stop", "session_id": "s"}, self.dir.name)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))
        self.assertIn(b'"Stop"', got.get(timeout=5))
        self.assertFalse(self.mark.exists())  # reaching the daemon clears the mark

    def test_gives_up_within_the_hook_timeout(self):
        t = time.monotonic()
        r = run_relay(free_port(), {"hook_event_name": "Stop", "session_id": "s"}, self.dir.name)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))
        self.assertLess(time.monotonic() - t, 5)  # hooks are registered with a 5 s timeout
        self.assertTrue(self.mark.exists())

    def test_a_daemon_down_for_long_costs_no_wait(self):
        self.mark.parent.mkdir(parents=True)
        self.mark.touch()
        old = time.time() - 60
        os.utime(self.mark, (old, old))
        t = time.monotonic()
        r = run_relay(free_port(), {"hook_event_name": "Stop", "session_id": "s"}, self.dir.name)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))
        self.assertLess(time.monotonic() - t, 2)

    def test_events_without_notices_never_wait(self):
        # A prompt waits on its hook; holding it for a daemon that may not come back isn't worth it.
        for event in ("UserPromptSubmit", "SessionStart", "SessionEnd"):
            t = time.monotonic()
            r = run_relay(free_port(), {"hook_event_name": event, "session_id": "s"}, self.dir.name)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))
            self.assertLess(time.monotonic() - t, 2, event)


if __name__ == "__main__":
    unittest.main()
