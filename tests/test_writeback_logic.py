import json
import tempfile
import unittest
from pathlib import Path

from ccmonitor.away import Presence
from ccmonitor.desktop import DesktopWriteBack, MESSAGES
from ccmonitor.sessions import find_cli_by_tag, find_title


class FindTitleTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def put(self, account, name, **fields):
        d = self.root / account / "org"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.json").write_text(json.dumps(fields), encoding="utf-8")

    def test_finds_title_by_cli_session_id_across_accounts(self):
        self.put("a", "local_1", cliSessionId="x", title="别的")
        self.put("b", "local_2", cliSessionId="cli-1", title="重构登录", lastActivityAt=5)
        self.assertEqual(find_title("cli-1", self.root), "重构登录")

    def test_skips_archived_and_prefers_most_recent(self):
        self.put("a", "local_1", cliSessionId="cli-1", title="旧", lastActivityAt=1)
        self.put("a", "local_2", cliSessionId="cli-1", title="新", lastActivityAt=9)
        self.put("a", "local_3", cliSessionId="cli-1", title="归档", lastActivityAt=99, isArchived=True)
        self.assertEqual(find_title("cli-1", self.root), "新")

    def test_tag_lookup_needs_exactly_one_live_match(self):
        self.put("a", "local_1", cliSessionId="1a2b3c4d-aaaa", title="A")
        self.put("a", "local_2", cliSessionId="9f9f9f9f-bbbb", title="B")
        self.put("a", "local_3", cliSessionId="9f9f9f9f-cccc", title="C")
        self.put("a", "local_4", cliSessionId="5e5e5e5e-dddd", title="D", isArchived=True)
        self.assertEqual(find_cli_by_tag("1a2b3c4d", self.root), "1a2b3c4d-aaaa")
        self.assertIsNone(find_cli_by_tag("9f9f9f9f", self.root))  # ambiguous
        self.assertIsNone(find_cli_by_tag("5e5e5e5e", self.root))  # archived
        self.assertIsNone(find_cli_by_tag("00000000", self.root))

    def test_unknown_or_unreadable_returns_none(self):
        (self.root / "a" / "org").mkdir(parents=True)
        (self.root / "a" / "org" / "local_bad.json").write_text("{", encoding="utf-8")
        self.assertIsNone(find_title("cli-1", self.root))
        self.assertIsNone(find_title("cli-1", self.root / "missing"))


class FakeClock:
    """Ticks in milliseconds, like GetTickCount / GetLastInputInfo."""

    def __init__(self):
        self.now = 1_000_000
        self.last_input = self.now

    def input(self):
        self.last_input = self.now

    def wait(self, seconds):
        self.now += int(seconds * 1000)


class PresenceTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.p = Presence(idle_seconds=600, now=lambda: self.clock.now, last_input=lambda: self.clock.last_input)

    def test_at_desk_by_default(self):
        self.assertFalse(self.p.is_away())

    def test_away_after_three_idle_minutes_by_default(self):
        p = Presence(now=lambda: self.clock.now, last_input=lambda: self.clock.last_input)
        self.clock.wait(179)
        self.assertFalse(p.is_away())
        self.clock.wait(1)
        self.assertTrue(p.is_away())

    def test_quiet_seconds_counts_from_the_last_input(self):
        self.clock.wait(12)
        self.assertEqual(self.p.quiet_seconds(), 12)

    def test_away_after_ten_idle_minutes(self):
        self.clock.wait(599)
        self.assertFalse(self.p.is_away())
        self.clock.wait(2)
        self.assertTrue(self.p.is_away())

    def test_declared_away_holds_until_next_input(self):
        self.p.declare_away()
        self.clock.wait(5)
        self.assertTrue(self.p.is_away())
        self.clock.input()
        self.assertFalse(self.p.is_away())

    def test_back_cancels_declaration(self):
        self.p.declare_away()
        self.p.declare_back()
        self.assertFalse(self.p.is_away())

    def nudge(self):
        """The driver wakes the display with its own input, as desktop.ps1 does."""
        before = self.clock.last_input
        self.clock.input()
        self.p.ignore_input(before, self.clock.last_input)

    def test_own_input_keeps_the_idle_away(self):
        self.clock.wait(601)
        self.nudge()
        self.assertTrue(self.p.is_away())
        self.assertGreaterEqual(self.p.quiet_seconds(), 601)

    def test_own_input_keeps_a_declared_away(self):
        self.p.declare_away()
        self.clock.wait(5)
        self.nudge()
        self.clock.wait(5)
        self.nudge()  # nudges in a row
        self.assertTrue(self.p.is_away())

    def test_a_nudge_during_a_driver_run_does_not_cancel_a_declaration(self):
        # Seen live 2026-10-12: the driver nudges at the start but reports it on exit, so a
        # check in between read the nudge as the user and ended the away state.
        self.p.declare_away()
        self.p.begin_own_run()
        self.clock.wait(2)
        self.clock.input()        # the driver's nudge, not yet reported
        self.assertTrue(self.p.is_away())
        self.clock.wait(10)
        self.assertTrue(self.p.is_away())
        self.p.end_own_run()
        self.assertTrue(self.p.is_away())  # still away once the run folded into the chain

    def test_a_nudge_during_a_driver_run_does_not_end_an_idle_away(self):
        self.clock.wait(601)
        self.p.begin_own_run()
        self.clock.wait(2)
        self.clock.input()
        self.assertTrue(self.p.is_away())
        self.assertGreaterEqual(self.p.quiet_seconds(), 603)
        self.p.end_own_run()
        self.assertTrue(self.p.is_away())

    def test_real_input_after_a_driver_run_still_ends_away(self):
        self.p.declare_away()
        self.p.begin_own_run()
        self.clock.wait(2)
        self.clock.input()
        self.p.end_own_run()
        self.clock.wait(2)
        self.clock.input()        # the user, after the run
        self.assertFalse(self.p.is_away())

    def test_overlapping_runs_keep_the_window_open_until_the_last_ends(self):
        self.p.declare_away()
        self.p.begin_own_run()
        self.p.begin_own_run()
        self.clock.wait(1)
        self.clock.input()
        self.p.end_own_run()
        self.assertTrue(self.p.is_away())
        self.p.end_own_run()
        self.assertTrue(self.p.is_away())

    def test_real_input_after_own_input_still_ends_away(self):
        self.clock.wait(601)
        self.nudge()
        self.clock.wait(2)
        self.clock.input()
        self.assertFalse(self.p.is_away())


class FakeDriver:
    def __init__(self, result):
        self.result, self.requests = result, []

    def __call__(self, request):
        self.requests.append(request)
        return self.result


class DesktopWriteBackTest(unittest.TestCase):
    def wb(self, result, title="重构登录"):
        self.driver = FakeDriver(result)
        return DesktopWriteBack(driver=self.driver, title_of=lambda sid: title)

    def test_send_prompt_passes_title_and_text_to_driver(self):
        status = self.wb({"code": "sent"}).send_prompt("cli-1", "继续")
        self.assertEqual(self.driver.requests,
                         [{"action": "prompt", "title": "重构登录", "text": "继续", "interject": True}])
        self.assertEqual(status, MESSAGES["sent"])

    def test_a_phone_prompt_into_a_running_session_is_interjected(self):
        reported = []
        status = DesktopWriteBack(driver=FakeDriver({"code": "interjected"}), title_of=lambda s: "T",
                                  on_failure=reported.append).send_prompt("cli-1", "顺便看下日志")
        self.assertEqual(status, MESSAGES["interjected"])
        self.assertEqual(reported, [])  # a good outcome, not breakage

    def test_step_times_are_logged_without_content(self):
        with self.assertLogs("ccmonitor", level="INFO") as logs:
            self.wb({"code": "suggestion", "text": "秘密建议", "timing": "wake=3,ready=40,switch=2282,suggest=584"})                 .read_suggestion("cli-1")
        [line] = [r.getMessage() for r in logs.records if r.getMessage().startswith("write-back suggestion")]
        self.assertIn("suggest", line)
        self.assertIn("switch=2282", line)
        self.assertNotIn("秘密", line)

    def test_running_out_of_time_is_reported_as_a_suggestion_failure(self):
        failures = []
        wb = DesktopWriteBack(driver=FakeDriver({"code": "out_of_time"}), title_of=lambda s: "T",
                              on_suggest_failure=failures.append)
        self.assertIsNone(wb.read_suggestion("cli-1"))
        self.assertEqual(len(failures), 1)

    def test_try_prompt_does_not_interject_by_default(self):
        self.wb({"code": "busy"}).try_prompt("cli-1", "继续")
        self.assertFalse(self.driver.requests[0]["interject"])

    def test_approve_passes_decision_and_expected_command(self):
        self.wb({"code": "denied"}).approve("cli-1", False, "rm -rf build")
        self.assertEqual(self.driver.requests,
                         [{"action": "approve", "title": "重构登录", "allow": False, "expect": "rm -rf build"}])

    def test_unknown_session_never_reaches_driver(self):
        status = self.wb({"code": "sent"}, title=None).send_prompt("cli-1", "继续")
        self.assertEqual(self.driver.requests, [])
        self.assertIn("找不到", status)

    def test_every_failure_code_has_a_message(self):
        for code in MESSAGES:
            self.assertTrue(self.wb({"code": code}).send_prompt("cli-1", "x"))

    def test_breakage_is_reported_but_refusals_are_not(self):
        reported = []
        for code in ("busy", "draft_present", "approval_mismatch", "no_approval", "sent"):
            DesktopWriteBack(driver=FakeDriver({"code": code}), title_of=lambda s: "T",
                             on_failure=reported.append).send_prompt("cli-1", "x")
        self.assertEqual(reported, [])
        for code in ("no_window", "session_not_found", "approval_failed", "error_COMException"):
            DesktopWriteBack(driver=FakeDriver({"code": code}), title_of=lambda s: "T",
                             on_failure=reported.append).send_prompt("cli-1", "x")
        self.assertEqual(len(reported), 4)

    def test_read_suggestion(self):
        failures = []
        wb = lambda result: DesktopWriteBack(driver=FakeDriver(result), title_of=lambda s: "T",
                                             on_suggest_failure=failures.append)
        self.assertEqual(wb({"code": "suggestion", "text": "第三步"}).read_suggestion("cli-1"), "第三步")
        for normal in ("no_suggestion", "busy", "draft_present", "ambiguous_session", "session_not_found"):
            self.assertIsNone(wb({"code": normal}).read_suggestion("cli-1"))
        self.assertEqual(failures, [])
        self.assertIsNone(DesktopWriteBack(driver=FakeDriver({"code": "suggestion", "text": "x"}), title_of=lambda s: None,
                                           on_suggest_failure=failures.append).read_suggestion("cli-1"))
        self.assertEqual(failures, [])  # not a Desktop session: nothing to read, not a failure
        self.assertIsNone(wb({"code": "no_window"}).read_suggestion("cli-1"))
        self.assertEqual(len(failures), 1)

    def test_read_suggestion_asks_driver_for_suggest(self):
        d = FakeDriver({"code": "no_suggestion"})
        DesktopWriteBack(driver=d, title_of=lambda s: "T").read_suggestion("cli-1")
        self.assertEqual(d.requests, [{"action": "suggest", "title": "T"}])

    def test_send_suggestion_echoes_the_exact_text(self):
        d = FakeDriver({"code": "sent", "text": "第三步：选什么语言"})
        status, changed = DesktopWriteBack(driver=d, title_of=lambda s: "T").send_suggestion("cli-1", "第三步：选什么语言")
        self.assertEqual(d.requests, [{"action": "accept", "title": "T", "expect": "第三步：选什么语言"}])
        self.assertIn("第三步：选什么语言", status)
        self.assertIsNone(changed)

    def test_send_suggestion_reports_a_changed_suggestion(self):
        failures = []
        d = FakeDriver({"code": "suggestion_changed", "text": "新的建议"})
        status, changed = DesktopWriteBack(driver=d, title_of=lambda s: "T",
                                           on_suggest_failure=failures.append).send_suggestion("x", "旧的建议")
        self.assertEqual(changed, "新的建议")
        self.assertIn("未发送", status)
        self.assertEqual(failures, [])

    def test_send_suggestion_refusals(self):
        for code, words in (("no_suggestion", "没有"), ("busy", "正在运行"), ("draft_present", "草稿")):
            status, _ = DesktopWriteBack(driver=FakeDriver({"code": code}), title_of=lambda s: "T").send_suggestion("x", "y")
            self.assertIn(words, status)

    QUESTION = {"question": "周末去哪？", "multi": True,
                "options": [{"label": "公园", "description": "近"}, {"label": "博物馆", "description": ""},
                            {"label": "商场", "description": "远"}]}

    def test_answer_passes_question_place_and_picked_options(self):
        status, answered = self.wb({"code": "answered"}).answer("cli-1", self.QUESTION, 2, 3, [0, 2])
        self.assertEqual(self.driver.requests, [{
            "action": "answer", "title": "重构登录", "question": "周末去哪？", "index": 2, "total": 3, "multi": True,
            "options": [{"label": "公园", "description": "近"}, {"label": "商场", "description": "远"}]}])
        self.assertTrue(answered)
        self.assertEqual(status, "已回答：公园、商场")

    def test_answer_refusals_are_not_breakage(self):
        reported = []
        for code, words in (("no_question", "没有等待回答"), ("question_mismatch", "不一致"),
                            ("option_mismatch", "找不到"), ("busy", "正在运行")):
            status, answered = DesktopWriteBack(driver=FakeDriver({"code": code}), title_of=lambda s: "T",
                                                on_failure=reported.append).answer("x", self.QUESTION, 1, 1, [0])
            self.assertFalse(answered)
            self.assertIn(words, status)
        self.assertEqual(reported, [])
        status, answered = DesktopWriteBack(driver=FakeDriver({"code": "answer_failed"}), title_of=lambda s: "T",
                                            on_failure=reported.append).answer("x", self.QUESTION, 1, 1, [0])
        self.assertFalse(answered)
        self.assertIn("可能未回答", status)
        self.assertEqual(len(reported), 1)

    def test_own_input_from_the_driver_is_reported(self):
        seen = []
        DesktopWriteBack(driver=FakeDriver({"code": "sent", "own_input": [100, 250]}), title_of=lambda s: "T",
                         on_own_input=lambda before, own: seen.append((before, own))).send_prompt("x", "y")
        self.assertEqual(seen, [(100, 250)])
        with self.assertLogs("ccmonitor", level="INFO") as logs:
            DesktopWriteBack(driver=FakeDriver({"code": "sent", "own_input": [1, 2]}), title_of=lambda s: "T",
                             on_own_input=lambda before, own: None).send_prompt("x", "y")
        self.assertIn("write-back own input", [r.getMessage() for r in logs.records])
        DesktopWriteBack(driver=FakeDriver({"code": "sent", "own_input": "junk"}), title_of=lambda s: "T",
                         on_own_input=lambda before, own: seen.append((before, own))).send_prompt("x", "y")
        self.assertEqual(len(seen), 1)

    def test_the_run_window_opens_before_the_driver_and_closes_after(self):
        order = []

        def driver(request):
            order.append("driver")
            return {"code": "sent"}

        DesktopWriteBack(driver=driver, title_of=lambda s: "T",
                         on_own_run=(lambda: order.append("begin"), lambda: order.append("end"))
                         ).send_prompt("x", "y")
        self.assertEqual(order, ["begin", "driver", "end"])

    def test_the_run_window_closes_even_when_the_driver_raises(self):
        order = []

        def driver(request):
            raise RuntimeError("boom")

        DesktopWriteBack(driver=driver, title_of=lambda s: "T",
                         on_own_run=(lambda: order.append("begin"), lambda: order.append("end"))
                         ).send_prompt("x", "y")
        self.assertEqual(order, ["begin", "end"])

    def test_unexpected_driver_output_is_reported_not_raised(self):
        self.assertIn("失败", self.wb({"code": "weird"}).send_prompt("cli-1", "x"))
        self.assertIn("失败", self.wb(None).send_prompt("cli-1", "x"))


if __name__ == "__main__":
    unittest.main()
