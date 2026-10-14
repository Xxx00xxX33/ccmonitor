import unittest

from ccmonitor.desktop import DesktopWriteBack, MESSAGES
from ccmonitor.pending import PendingPrompts


class FakeLater:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds, fn):
        self.calls.append((seconds, fn))

    def fire(self):
        calls, self.calls = self.calls, []
        for _, fn in calls:
            fn()


class PendingPromptsTest(unittest.TestCase):
    def setUp(self):
        self.codes = []      # driver codes the next sends return, in order
        self.sent = []       # (session, text) each send attempt
        self.said = []
        self.later = FakeLater()
        self.away = True
        self.clock = 0.0
        self.q = PendingPrompts(send=self.send, say=self.said.append, later=self.later,
                                can_write=lambda: self.away, now=lambda: self.clock)

    def send(self, sid, text):
        self.sent.append((sid, text))
        code = self.codes.pop(0)
        return MESSAGES.get(code, code), code

    def test_busy_prompt_is_sent_when_the_turn_ends(self):
        self.assertIn("这一轮结束后自动发送", self.q.hold("s1", "继续", "busy"))
        self.codes = ["sent"]
        self.q.turn_ended("s1")
        self.later.fire()
        self.assertEqual(self.sent, [("s1", "继续")])
        self.assertIn("已发送", self.said[-1])
        self.assertIn("继续", self.said[-1])
        self.assertEqual(self.q.count(), 0)

    def test_still_busy_keeps_waiting(self):
        self.q.hold("s1", "继续", "busy")
        self.codes = ["busy"]
        self.later.fire()
        self.assertEqual(self.q.count(), 1)
        self.assertEqual(self.said, [])
        self.assertTrue(self.later.calls)  # a backup retry stays scheduled

    def test_turn_end_of_another_session_does_nothing(self):
        self.q.hold("s1", "继续", "busy")
        self.later.calls.clear()
        self.q.turn_ended("s2")
        self.assertEqual(self.later.calls, [])

    def test_prompts_for_one_session_go_out_in_order_one_per_turn(self):
        self.q.hold("s1", "一", "busy")
        self.q.hold("s1", "二", "busy")
        self.codes = ["sent"]
        self.q.turn_ended("s1")
        self.later.fire()
        self.assertEqual(self.sent, [("s1", "一")])
        self.codes = ["sent"]
        self.q.turn_ended("s1")
        self.later.fire()
        self.assertEqual(self.sent, [("s1", "一"), ("s1", "二")])

    def test_one_timer_per_session(self):
        self.q.hold("s1", "一", "busy")
        self.q.hold("s1", "二", "busy")
        self.assertEqual(len(self.later.calls), 1)

    def test_breakage_is_retried_then_given_up_with_the_text(self):
        self.assertIn("稍后自动重试", self.q.hold("s1", "继续", "no_window"))
        for _ in range(4):
            self.codes = ["no_window"]
            self.later.fire()
        self.assertEqual(self.q.count(), 1)
        self.codes = ["no_window"]
        self.later.fire()
        self.assertEqual(self.q.count(), 0)
        self.assertIn("放弃", self.said[-1])
        self.assertIn("继续", self.said[-1])

    def test_maybe_sent_is_never_retried(self):
        self.q.hold("s1", "继续", "busy")
        self.codes = ["send_failed"]
        self.later.fire()
        self.assertEqual(self.q.count(), 0)
        self.assertIn("可能未发送", self.said[-1])
        self.later.fire()
        self.assertEqual(len(self.sent), 1)

    def test_back_at_the_computer_drops_and_hands_the_text_back(self):
        self.q.hold("s1", "继续", "busy")
        self.away = False
        self.later.fire()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.q.count(), 0)
        self.assertIn("回到电脑前", self.said[-1])
        self.assertIn("继续", self.said[-1])

    def test_too_old_is_given_up(self):
        self.q.hold("s1", "继续", "busy")
        self.clock = 4 * 3600
        self.later.fire()
        self.assertEqual(self.sent, [])
        self.assertIn("放弃", self.said[-1])


class FallbackWiringTest(unittest.TestCase):
    def wb(self, code):
        self.held = []

        def fallback(sid, text, c):
            self.held.append((sid, text, c))
            return "已排队"
        return DesktopWriteBack(driver=lambda req: {"code": code}, title_of=lambda s: "T", fallback=fallback)

    def test_busy_and_breakage_go_to_the_fallback_with_their_code(self):
        for code in ("busy", "no_window", "fill_failed"):
            status = self.wb(code).send_prompt("cli-1", "继续")
            self.assertEqual(self.held, [("cli-1", "继续", code)])
            self.assertIn("已排队", status)

    def test_sent_and_maybe_sent_never_fall_back(self):
        for code in ("sent", "interjected", "sent_draft_lost", "send_failed"):
            self.wb(code).send_prompt("cli-1", "继续")
            self.assertEqual(self.held, [])

    def test_try_prompt_returns_the_code_without_falling_back(self):
        wb = self.wb("busy")
        self.assertEqual(wb.try_prompt("cli-1", "继续"), (MESSAGES["busy"], "busy"))
        self.assertEqual(self.held, [])

    def test_lost_draft_is_handed_back(self):
        wb = DesktopWriteBack(driver=lambda req: {"code": "sent_draft_lost", "text": "我的草稿"},
                              title_of=lambda s: "T")
        status = wb.send_prompt("cli-1", "继续")
        self.assertIn("已发送", status)
        self.assertIn("我的草稿", status)


if __name__ == "__main__":
    unittest.main()
