import queue
import socket
import tempfile
import time
import unittest

from ccmonitor.daemon import Service
from tests.test_intake import free_port, run_relay


def port_is_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


class ServiceTest(unittest.TestCase):
    """The GUI enables and disables the daemon in its own process (#65)."""

    def setUp(self):
        self.notices = queue.Queue()
        self.port = free_port()
        self.state = tempfile.TemporaryDirectory()  # the relay's "daemon down since" mark
        self.addCleanup(self.state.cleanup)
        # No Telegram config: notices go to the console sink, here a queue.
        self.service = Service(self.port, config=lambda: None, console=self.notices.put)
        self.addCleanup(self.service.stop)

    def stop_event(self, sid: str) -> dict:
        return {"hook_event_name": "Stop", "session_id": sid, "last_assistant_message": "好了"}

    def test_start_receive_stop_release_and_start_again(self):
        self.service.start()
        self.assertTrue(self.service.running)
        run_relay(self.port, self.stop_event("s1"), self.state.name)
        self.assertEqual(self.notices.get(timeout=5).session_id, "s1")

        self.service.stop()
        self.assertFalse(self.service.running)
        self.assertTrue(port_is_free(self.port))

        self.service.start()
        run_relay(self.port, self.stop_event("s2"), self.state.name)
        self.assertEqual(self.notices.get(timeout=5).session_id, "s2")

    def test_while_stopped_hooks_give_up_quickly_and_nothing_is_handled(self):
        self.service.start()
        self.service.stop()
        began = time.monotonic()
        r = run_relay(self.port, {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "x"},
                      self.state.name)
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        self.assertLess(time.monotonic() - began, 3)  # interpreter start-up included; no retrying
        self.assertTrue(self.notices.empty())

    def test_busy_port_fails_the_start_and_leaves_it_stopped(self):
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", self.port))
            taken.listen()
            with self.assertRaises(OSError):
                self.service.start()
        self.assertFalse(self.service.running)
        self.service.start()  # free again: starts fine
        self.assertTrue(self.service.running)

    def test_the_daemon_judges_away_with_the_presence_it_was_given(self):
        class Away:
            def is_away(self):
                return True

        service = Service(self.port, config=lambda: None, console=self.notices.put, presence=Away())
        self.addCleanup(service.stop)
        service.start()
        r = run_relay(self.port, {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "x"},
                      self.state.name)
        self.assertNotEqual(r.stdout, b"")  # the away reply; at the desk there is none


if __name__ == "__main__":
    unittest.main()
