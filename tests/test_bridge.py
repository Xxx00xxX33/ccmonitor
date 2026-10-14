import asyncio
import threading
import time
import unittest

from ccmonitor.daemon import TelegramBridge
from ccmonitor.health import Health
from ccmonitor.telegram import TelegramError
from ccmonitor.notice import Level, Notice
from ccmonitor.sidechannel import SideChannel
from ccmonitor.away import Presence
from ccmonitor.writeback import NotImplementedWriteBack


class Away(Presence):
    """Always away, so notices go out whoever is at this test machine."""

    def is_away(self):
        return True


class RecordingApi:
    def __init__(self):
        self.sent = []
        self.done = threading.Event()

    def call(self, method, **params):
        if method == "getUpdates":
            return []
        if method == "sendMessage":
            self.sent.append(params["text"])
            if len(self.sent) == 2:
                self.done.set()
            return {"message_id": len(self.sent)}
        return True


class TelegramBridgeTest(unittest.TestCase):
    def test_start_announces_and_notify_sends_off_the_event_loop(self):
        api = RecordingApi()
        bridge = TelegramBridge(SideChannel(api, 1, NotImplementedWriteBack(), Away()), api)
        asyncio.run(bridge.start())
        bridge.notify(Notice(Level.DEFERRED, "completed", "s", "L", "好了"))
        self.assertTrue(api.done.wait(5))
        self.assertEqual(api.sent[0], "ccmonitor 已启动")
        self.assertIn("好了", api.sent[1])


class CloseTest(unittest.TestCase):
    def test_nothing_reaches_telegram_after_close(self):
        api = RecordingApi()
        bridge = TelegramBridge(SideChannel(api, 1, NotImplementedWriteBack(), Away()), api)
        bridge.later(0.2, lambda: bridge.say("timed before the stop"))
        bridge.close()
        bridge.notify(Notice(Level.DEFERRED, "completed", "s", "L", "after the stop"))  # must not raise
        bridge.say("after the stop")
        time.sleep(0.5)
        self.assertEqual(api.sent, [])


class FlakyApi(RecordingApi):
    """sendMessage fails `failures` times, then works."""

    def __init__(self, failures):
        super().__init__()
        self.failures = failures

    def call(self, method, **params):
        if method == "sendMessage" and self.failures:
            self.failures -= 1
            raise TelegramError("sendMessage: ConnectionError")
        return super().call(method, **params)


class OutageTest(unittest.TestCase):
    def test_outage_is_reported_once_telegram_is_back(self):
        api = FlakyApi(failures=2)
        side = SideChannel(api, 1, NotImplementedWriteBack(), Away())
        bridge = TelegramBridge(side, api)
        reports = []
        bridge.health = Health(lambda text: reports.append(text))
        for i in range(3):
            bridge.notify(Notice(Level.DEFERRED, "completed", "s", "L", f"n{i}"))
        bridge._worker.shutdown(wait=True)
        self.assertEqual(len(reports), 1)
        self.assertIn("2 次请求失败", reports[0])


class NoGetMe(RecordingApi):
    """getMe fails (Telegram unreachable at start); everything else works."""

    def call(self, method, **params):
        if method == "getMe":
            raise TelegramError("getMe: ConnectionError")
        return super().call(method, **params)


class StartupGetMeTest(unittest.TestCase):
    def test_a_failed_get_me_at_start_shows_telegram_down_until_a_request_succeeds(self):
        api = NoGetMe()
        bridge = TelegramBridge(SideChannel(api, 1, NotImplementedWriteBack(), Away()), api)
        self.assertIsNone(asyncio.run(bridge.get_me()))
        self.assertTrue(bridge.telegram_down)  # not 已连接 on the status page
        bridge.notify(Notice(Level.DEFERRED, "completed", "s", "L", "好了"))
        bridge._worker.shutdown(wait=True)
        self.assertFalse(bridge.telegram_down)

    def test_a_working_get_me_returns_the_bot(self):
        class Bot(RecordingApi):
            def call(self, method, **params):
                return {"username": "cc_bot"} if method == "getMe" else super().call(method, **params)
        api = Bot()
        bridge = TelegramBridge(None, api)
        self.assertEqual(asyncio.run(bridge.get_me()), {"username": "cc_bot"})
        self.assertFalse(bridge.telegram_down)


if __name__ == "__main__":
    unittest.main()
