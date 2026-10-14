import json
import tempfile
import unittest
from pathlib import Path

from ccmonitor.classify import Classifier
from ccmonitor.notice import Level
from ccmonitor.stuck import ENDED, WORKING, StuckWatcher, read_tail, subagent_activity

T0 = 1_800_000_000.0  # epoch seconds


def iso(t):
    import datetime
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def rec(kind, t, content=None, stop_reason=None):
    r = {"type": kind, "timestamp": iso(t)}
    if kind in ("user", "assistant"):
        r["message"] = {"content": content if content is not None else [{"type": "text", "text": "x"}]}
        if stop_reason:
            r["message"]["stop_reason"] = stop_reason
    return r


class ReadTailTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "t.jsonl"

    def tearDown(self):
        self.dir.cleanup()

    def write(self, *rows, partial=""):
        self.path.write_text("".join(json.dumps(r) + "\n" for r in rows) + partial, encoding="utf-8")

    def test_tool_running_is_working_with_last_record_time(self):
        self.write(rec("user", T0, "做 A"), rec("assistant", T0 + 5, [{"type": "tool_use"}], "tool_use"),
                   rec("cost-state", T0 + 50))
        self.assertEqual(read_tail(str(self.path)), (T0 + 5, WORKING))

    def test_end_turn_is_ended(self):
        self.write(rec("user", T0, "做 A"), rec("assistant", T0 + 5, stop_reason="end_turn"), rec("system", T0 + 6))
        self.assertEqual(read_tail(str(self.path))[1], ENDED)

    def test_interrupt_marker_is_ended(self):
        self.write(rec("assistant", T0, [{"type": "tool_use"}], "tool_use"),
                   rec("user", T0 + 1, [{"type": "tool_result"}]),
                   rec("user", T0 + 2, [{"type": "text", "text": "[Request interrupted by user for tool use]"}]))
        self.assertEqual(read_tail(str(self.path))[1], ENDED)

    def test_tool_result_waiting_for_model_is_working(self):
        self.write(rec("assistant", T0, [{"type": "tool_use"}], "tool_use"), rec("user", T0 + 1, [{"type": "tool_result"}]))
        self.assertEqual(read_tail(str(self.path)), (T0 + 1, WORKING))

    def test_half_written_last_line_is_ignored(self):
        self.write(rec("user", T0, "做 A"), partial='{"type": "assistant", "timest')
        self.assertEqual(read_tail(str(self.path)), (T0, WORKING))

    def test_missing_file(self):
        self.assertIsNone(read_tail(str(self.path)))


class SubagentActivityTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.main = Path(self.dir.name) / "abc.jsonl"
        self.main.write_text("", encoding="utf-8")
        self.subs = Path(self.dir.name) / "abc" / "subagents"

    def tearDown(self):
        self.dir.cleanup()

    def write_sub(self, name, *rows):
        self.subs.mkdir(parents=True, exist_ok=True)
        (self.subs / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def test_newest_record_across_subagent_files(self):
        self.write_sub("agent-1", rec("user", T0), rec("assistant", T0 + 30, [{"type": "tool_use"}], "tool_use"))
        self.write_sub("agent-2", rec("assistant", T0 + 90, stop_reason="end_turn"))
        self.assertEqual(subagent_activity(str(self.main)), T0 + 90)

    def test_no_subagents(self):
        self.assertIsNone(subagent_activity(str(self.main)))
        self.assertIsNone(subagent_activity(""))


class FakeTail:
    def __init__(self):
        self.result = (T0, WORKING)

    def __call__(self, path):
        return self.result


class StuckWatcherTest(unittest.TestCase):
    def setUp(self):
        self.now = T0
        self.tail = FakeTail()
        self.sub = None
        self.w = StuckWatcher(Classifier(), read_tail=self.tail, clock=lambda: self.now, quiet_seconds=600,
                              subagent_activity=lambda path: self.sub)

    def ev(self, event, sid="s1", **f):
        self.w.on_event({"hook_event_name": event, "session_id": sid, "cwd": "D:\\p",
                         "transcript_path": f"D:/t/{sid}.jsonl", **f})

    def test_quiet_turn_is_reported_once_as_deferred(self):
        self.ev("UserPromptSubmit")
        self.now = T0 + 599
        self.assertEqual(self.w.check(), [])
        self.now = T0 + 601
        notices = self.w.check()
        self.assertEqual(len(notices), 1)
        n = notices[0]
        self.assertEqual((n.level, n.kind, n.session_id), (Level.DEFERRED, "stuck", "s1"))
        self.assertIn("10 分钟", n.text)
        self.now = T0 + 1500
        self.assertEqual(self.w.check(), [])

    def test_new_output_resets_and_allows_a_later_report(self):
        self.ev("UserPromptSubmit")
        self.now = T0 + 601
        self.assertEqual(len(self.w.check()), 1)
        self.tail.result = (T0 + 700, WORKING)
        self.now = T0 + 800
        self.assertEqual(self.w.check(), [])
        self.now = T0 + 1301
        self.assertEqual(len(self.w.check()), 1)

    def test_busy_subagent_is_not_stuck(self):
        # A subagent writes to its own transcript; the main one stays silent (seen live, 2026-10-11).
        self.ev("UserPromptSubmit")
        self.tail.result = (T0 + 5, WORKING)
        self.sub = T0 + 580
        self.now = T0 + 1000
        self.assertEqual(self.w.check(), [])

    def test_silent_subagent_is_still_stuck(self):
        self.ev("UserPromptSubmit")
        self.tail.result = (T0 + 5, WORKING)
        self.sub = T0 + 100
        self.now = T0 + 701
        self.assertEqual(len(self.w.check()), 1)

    def test_stop_ends_tracking(self):
        self.ev("UserPromptSubmit")
        self.ev("Stop")
        self.now = T0 + 9999
        self.assertEqual(self.w.check(), [])

    def test_turn_ended_without_stop_is_dropped(self):
        # Deny or Esc in Desktop ends the turn without a Stop event (VERIFY.md 1).
        self.ev("UserPromptSubmit")
        self.tail.result = (T0 + 3, ENDED)
        self.now = T0 + 9999
        self.assertEqual(self.w.check(), [])
        self.tail.result = (T0 + 3, WORKING)
        self.assertEqual(self.w.check(), [])

    def test_waiting_for_approval_is_not_stuck(self):
        self.ev("UserPromptSubmit")
        self.now = T0 + 10
        self.ev("PermissionRequest", tool_name="Bash")
        self.now = T0 + 9999
        self.assertEqual(self.w.check(), [])

    def test_activity_after_approval_resumes_watching(self):
        self.ev("UserPromptSubmit")
        self.now = T0 + 10
        self.ev("PermissionRequest", tool_name="Bash")
        self.tail.result = (T0 + 20, WORKING)
        self.now = T0 + 621
        self.assertEqual(len(self.w.check()), 1)

    def test_unreadable_transcript_uses_prompt_time(self):
        self.tail.result = None
        self.ev("UserPromptSubmit")
        self.now = T0 + 601
        self.assertEqual(len(self.w.check()), 1)

    def test_session_end_and_stop_failure_end_tracking(self):
        self.ev("UserPromptSubmit", sid="a")
        self.ev("UserPromptSubmit", sid="b")
        self.ev("SessionEnd", sid="a")
        self.ev("StopFailure", sid="b", error="rate_limit")
        self.now = T0 + 9999
        self.assertEqual(self.w.check(), [])


class StopFailureTest(unittest.TestCase):
    def test_stop_failure_is_deferred_error_notice(self):
        n = Classifier().handle({"hook_event_name": "StopFailure", "session_id": "s", "cwd": "D:\\p",
                                 "error": "rate_limit", "error_details": "429 Too Many Requests",
                                 "last_assistant_message": "API Error: Rate limit reached"})
        self.assertEqual((n.level, n.kind), (Level.DEFERRED, "error"))
        self.assertIn("rate_limit", n.text)
        self.assertIn("429 Too Many Requests", n.text)


if __name__ == "__main__":
    unittest.main()
