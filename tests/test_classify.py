import json
import os
import tempfile
import unittest

from ccmonitor.classify import Classifier, answered, tool_key
from ccmonitor.notice import Level


SID = "bd6e47a7-59bd-41ac-938d-abf542e4f558"


def payload(event, **fields):
    return {"hook_event_name": event, "session_id": SID, "cwd": "D:\\work\\ccmonitor", **fields}


class ClassifierTest(unittest.TestCase):
    def setUp(self):
        self.c = Classifier()

    def test_an_approval_carries_the_key_its_post_tool_use_will_have(self):
        n = self.c.handle(payload("PermissionRequest", tool_name="Bash", tool_input={"command": "make", "description": "build"}))
        post = payload("PostToolUse", tool_name="Bash", tool_input={"description": "build", "command": "make"},
                       tool_use_id="t1")
        self.assertEqual(n.key, tool_key(post))
        self.assertIsNone(self.c.handle(post))

    def test_bash_permission_request_is_urgent_approval_with_command(self):
        n = self.c.handle(payload("PermissionRequest", tool_name="Bash",
                                  tool_input={"command": "rm -rf build", "description": "Remove build"}))
        self.assertEqual(n.level, Level.URGENT)
        self.assertEqual(n.kind, "approval")
        self.assertEqual(n.session_id, SID)
        self.assertIn("rm -rf build", n.text)
        self.assertIn("Bash", n.text)
        self.assertEqual(n.command, "rm -rf build")

    def test_long_command_is_truncated_to_1000_chars(self):
        n = self.c.handle(payload("PermissionRequest", tool_name="Bash", tool_input={"command": "x" * 1500}))
        self.assertIn("x" * 1000 + "…", n.text)
        self.assertNotIn("x" * 1001, n.text)

    def test_non_bash_permission_shows_tool_input(self):
        n = self.c.handle(payload("PermissionRequest", tool_name="Edit",
                                  tool_input={"file_path": "D:\\a.py", "old_string": "a", "new_string": "b"}))
        self.assertEqual(n.kind, "approval")
        self.assertIn("Edit", n.text)
        self.assertIn("a.py", n.text)

    def test_ask_user_question_is_urgent_question(self):
        n = self.c.handle(payload("PermissionRequest", tool_name="AskUserQuestion", tool_input={"questions": [
            {"question": "先做哪个？", "header": "模块", "multiSelect": False,
             "options": [{"label": "事件接收", "description": "d1"}, {"label": "Telegram", "description": "d2"}]}]}))
        self.assertEqual(n.level, Level.URGENT)
        self.assertEqual(n.kind, "question")
        self.assertIn("先做哪个？", n.text)
        self.assertIn("事件接收", n.text)
        self.assertIn("Telegram", n.text)
        self.assertEqual(n.questions, [{"question": "先做哪个？", "multi": False, "options": [
            {"label": "事件接收", "description": "d1"}, {"label": "Telegram", "description": "d2"}]}])

    def test_question_text_numbers_questions_and_marks_multi_select(self):
        n = self.c.handle(payload("PermissionRequest", tool_name="AskUserQuestion", tool_input={"questions": [
            {"question": "早饭？", "multiSelect": False, "options": [{"label": "包子", "description": "包子"}]},
            {"question": "去哪？", "multiSelect": True, "options": [{"label": "公园", "description": "近"},
                                                                  {"label": "商场"}]}]}))
        self.assertEqual(n.text, "1/2 早饭？\n- 包子\n2/2 去哪？（可多选）\n- 公园：近\n- 商场")
        self.assertEqual([q["multi"] for q in n.questions], [False, True])
        self.assertEqual(n.questions[1]["options"][1], {"label": "商场", "description": ""})

    def test_stop_is_deferred_completion_with_options(self):
        n = self.c.handle(payload("Stop", last_assistant_message="完成了。\n\n选项：\n1. 按刚才说的继续\n2. 写测试"))
        self.assertEqual(n.level, Level.DEFERRED)
        self.assertEqual(n.kind, "completed")
        self.assertEqual(n.text, "完成了。")
        self.assertEqual(n.options, ["按刚才说的继续", "写测试"])
        self.assertEqual(n.options_label, "选项")

    def test_english_answer_gets_an_english_options_label(self):
        n = self.c.handle(payload("Stop", last_assistant_message="Done.\n\nOptions:\n1. Write tests\n2. Ship it"))
        self.assertEqual(n.options, ["Write tests", "Ship it"])
        self.assertEqual(n.options_label, "Options")

    def test_completion_body_is_kept_whole(self):
        n = self.c.handle(payload("Stop", last_assistant_message="字" * 7000 + "\n选项：\n1. 按刚才说的继续"))
        self.assertEqual(n.text, "字" * 7000)
        self.assertEqual(n.options, ["按刚才说的继续"])

    def test_injected_prompts_do_not_replace_the_users_last_prompt(self):
        self.c.handle(payload("UserPromptSubmit", prompt="推送并开 PR"))
        for injected in ('<ci-monitor-event>"Auto-fix pull requests" was just enabled</ci-monitor-event>',
                         "<task-notification>\n<task-id>b1</task-id>",
                         'Another Claude session sent a message:\n<cross-session-message from="x">hi',
                         "<system-reminder>x</system-reminder>"):
            self.c.handle(payload("UserPromptSubmit", prompt=injected))
        n = self.c.handle(payload("Stop", last_assistant_message="好了"))
        self.assertEqual((n.title, n.prompt), ("ccmonitor", "推送并开 PR"))

    def test_a_prompt_starting_with_a_less_than_sign_in_prose_still_counts(self):
        self.c.handle(payload("UserPromptSubmit", prompt="< 5 分钟能做完吗"))
        self.assertEqual(self.c.handle(payload("Stop", last_assistant_message="x")).prompt, "< 5 分钟能做完吗")

    def test_stop_without_message_still_notifies(self):
        n = self.c.handle(payload("Stop"))
        self.assertEqual(n.kind, "completed")
        self.assertEqual(n.text, "")

    def test_session_label_uses_title_and_last_prompt(self):
        self.c.handle(payload("SessionStart", source="startup", session_title="重构登录"))
        prompt = "把登录页的表单校验改成实时校验，并补上单元测试，最后跑一遍全部测试确认没有回归"
        self.c.handle(payload("UserPromptSubmit", prompt=prompt))
        n = self.c.handle(payload("Stop", last_assistant_message="好了"))
        self.assertEqual(n.session_label, "重构登录 · " + prompt[:30] + "…")

    def test_short_prompt_and_newlines_in_label(self):
        self.c.handle(payload("UserPromptSubmit", prompt="跑测试\n然后提交"))
        n = self.c.handle(payload("Stop", last_assistant_message="好了"))
        self.assertEqual(n.session_label, "ccmonitor · 跑测试 然后提交")

    def test_session_label_uses_desktop_title_lookup(self):
        c = Classifier(title_of=lambda sid: "桌面标题" if sid == SID else None)
        n = c.handle(payload("Stop", last_assistant_message="好了"))
        self.assertEqual(n.session_label, "桌面标题")

    def test_desktop_title_wins_over_session_start_slug(self):
        c = Classifier(title_of=lambda sid: "桌面标题")
        c.handle(payload("SessionStart", source="startup", session_title="ccmonitor-write-module-test-bd43f8"))
        self.assertEqual(c.handle(payload("Stop", last_assistant_message="x")).session_label, "桌面标题")

    def test_session_label_falls_back_to_cwd_name(self):
        n = self.c.handle(payload("Stop", last_assistant_message="好了"))
        self.assertEqual(n.session_label, "ccmonitor")

    def test_bookkeeping_events_produce_no_notice(self):
        for e in ("SessionStart", "UserPromptSubmit", "PreToolUse", "Notification", "PermissionDenied", "SessionEnd"):
            self.assertIsNone(self.c.handle(payload(e, prompt="hi", source="startup")), e)

    def test_payload_without_session_id_is_ignored(self):
        self.assertIsNone(self.c.handle({"hook_event_name": "Stop", "last_assistant_message": "x"}))

    def test_session_end_forgets_session(self):
        self.c.handle(payload("SessionStart", session_title="T"))
        self.c.handle(payload("SessionEnd"))
        n = self.c.handle(payload("Stop", last_assistant_message="x"))
        self.assertEqual(n.session_label, "ccmonitor")

    def test_subagent_stop_is_not_a_completion(self):
        self.assertIsNone(self.c.handle(payload("SubagentStop", last_assistant_message="x")))


class AnsweredTest(unittest.TestCase):
    """answered(): has the transcript a result for the latest call with this key?"""

    KEY = tool_key({"tool_name": "Bash", "tool_input": {"command": "make"}})

    def path(self, *lines, raw=""):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(raw + "".join(json.dumps(line) + "\n" for line in lines))
        self.addCleanup(os.remove, path)
        return path

    @staticmethod
    def use(i, command="make"):
        return {"message": {"content": [{"type": "tool_use", "id": i, "name": "Bash", "input": {"command": command}}]}}

    @staticmethod
    def result(i):
        return {"message": {"content": [{"type": "tool_result", "tool_use_id": i, "is_error": True}]}}

    def test_a_rejected_call_is_answered(self):
        self.assertTrue(answered(self.path(self.use("a"), self.result("a")), self.KEY))

    def test_a_call_still_waiting_is_not(self):
        self.assertFalse(answered(self.path(self.use("a")), self.KEY))

    def test_the_latest_call_with_the_key_counts(self):
        # The same command ran before; the one now asking has no result yet.
        self.assertFalse(answered(self.path(self.use("a"), self.result("a"), self.use("b")), self.KEY))

    def test_other_calls_results_do_not_count(self):
        self.assertFalse(answered(self.path(self.use("a"), self.use("b", "ls"), self.result("b")), self.KEY))

    def test_unsure_is_not_answered(self):
        self.assertFalse(answered("", self.KEY))
        self.assertFalse(answered(os.path.join(tempfile.gettempdir(), "no-such-ccmonitor.jsonl"), self.KEY))
        self.assertFalse(answered(self.path(self.result("a")), self.KEY))  # call not in the file

    def test_junk_lines_are_skipped(self):
        path = self.path(self.use("a"), {"message": "text"}, [1], self.result("a"), raw="not json\n")
        self.assertTrue(answered(path, self.KEY))


if __name__ == "__main__":
    unittest.main()
