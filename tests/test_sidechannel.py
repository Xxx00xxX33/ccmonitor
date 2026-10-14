import json
import os
import tempfile
import unittest
from dataclasses import replace

from ccmonitor.notice import Level, Notice
from ccmonitor.classify import tool_key
from ccmonitor.sidechannel import SideChannel, _plain
from ccmonitor.telegram import TelegramError
from ccmonitor.tgformat import visible_len

CHAT = 1001
STRANGER = 666


class FakeApi:
    def __init__(self):
        self.calls = []
        self._next_id = 100

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "sendMessage":
            self._next_id += 1
            return {"message_id": self._next_id}
        return True

    def sent(self):
        return [p for m, p in self.calls if m == "sendMessage"]

    def last_text(self):
        return self.sent()[-1]["text"]


class RejectingApi(FakeApi):
    """Rejects the first HTML message, as Telegram does for entities it can't parse."""

    def call(self, method, **params):
        if method == "sendMessage" and params.get("parse_mode") and not getattr(self, "rejected", False):
            self.calls.append((method, params))
            self.rejected = True
            raise TelegramError("sendMessage: 400 Bad Request: can't parse entities")
        return super().call(method, **params)


class FakeWriteBack:
    def __init__(self, result="已写入"):
        self.prompts, self.approvals, self.result = [], [], result

    def send_prompt(self, session_id, text):
        self.prompts.append((session_id, text))
        return self.result

    def approve(self, session_id, allow, expect=""):
        self.approvals.append((session_id, allow, expect))
        return self.result

    answer_ok = True

    def answer(self, session_id, question, index, total, picked):
        self.answers = getattr(self, "answers", []) + [(session_id, question["question"], index, total, picked)]
        return ("已回答" if self.answer_ok else "未回答"), self.answer_ok

    suggestion = None

    def read_suggestion(self, session_id):
        self.suggestion_reads = getattr(self, "suggestion_reads", []) + [session_id]
        return self.suggestion

    changed_to = None

    def send_suggestion(self, session_id, expect):
        self.suggestion_sends = getattr(self, "suggestion_sends", []) + [(session_id, expect)]
        if self.changed_to:
            return "未发送：官方建议已经变了", self.changed_to
        return "已发送官方建议：" + expect, None


class FakePresence:
    def __init__(self, away=True):
        self.away, self.declared = away, None

    def is_away(self):
        return self.away

    quiet = 0  # seconds since the last keyboard or mouse input

    def quiet_seconds(self):
        return self.quiet

    def declare_away(self):
        self.declared = True

    def declare_back(self):
        self.declared = False


def completed(sid="s1", options=("按刚才说的继续", "写测试")):
    return Notice(Level.DEFERRED, "completed", sid, "ccmonitor · 做 A", "完成了。", list(options))


def approval(sid="s1"):
    return Notice(Level.URGENT, "approval", sid, "ccmonitor · 做 A", "Bash: rm -rf build", command="rm -rf build")


def question(sid="s1", *multi):
    """A question notice; one question per flag in `multi` (default: one single-select)."""
    qs = [{"question": f"问题{i}", "multi": m,
           "options": [{"label": f"甲{i}", "description": ""}, {"label": f"乙{i}", "description": "d"}]}
          for i, m in enumerate(multi or (False,), 1)]
    return Notice(Level.URGENT, "question", sid, "L", "问题", questions=qs)


def text_update(text, chat=CHAT, reply_to=None, reply_text=""):
    msg = {"message_id": 1, "chat": {"id": chat}, "text": text}
    if reply_to:
        msg["reply_to_message"] = {"message_id": reply_to, "text": reply_text}
    return {"update_id": 1, "message": msg}


def button_update(data, message_id, chat=CHAT):
    return {"update_id": 2, "callback_query": {"id": "cb1", "data": data,
                                                "message": {"message_id": message_id, "chat": {"id": chat}}}}


def buttons(params):
    return [b for row in params["reply_markup"]["inline_keyboard"] for b in row]


class SideChannelTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.wb = FakeWriteBack()
        self.presence = FakePresence()
        self.side = SideChannel(self.api, CHAT, self.wb, self.presence)

    # --- outbound ---

    def test_completion_shows_body_options_and_one_button_per_option(self):
        self.side.notify(completed())
        p = self.api.sent()[0]
        self.assertEqual(p["chat_id"], CHAT)
        self.assertIn("ccmonitor · 做 A", p["text"])
        self.assertIn("完成了。", p["text"])
        self.assertIn("1. 按刚才说的继续", p["text"])
        self.assertEqual([b["text"] for b in buttons(p)], ["1. 按刚才说的继续", "2. 写测试"])

    def test_completion_without_options_has_no_buttons(self):
        self.side.notify(completed(options=()))
        self.assertNotIn("reply_markup", self.api.sent()[0])

    def test_approval_has_allow_once_and_deny_only(self):
        self.side.notify(approval())
        p = self.api.sent()[0]
        self.assertIn("Bash: rm -rf build", p["text"])
        self.assertEqual([b["text"] for b in buttons(p)], ["允许一次", "拒绝"])

    def test_callback_data_fits_telegram_limit(self):
        self.side.notify(completed(options=("长" * 40,)))
        for b in buttons(self.api.sent()[0]):
            self.assertLessEqual(len(b["callback_data"].encode("utf-8")), 64)

    def test_long_answer_is_split_into_several_messages_with_buttons_on_the_last(self):
        body = "\n\n".join(f"第 {i} 段：" + "字" * 900 for i in range(9))
        self.side.notify(Notice(Level.DEFERRED, "completed", "s1", "L", body, ["按刚才说的继续"]))
        parts = self.api.sent()
        self.assertGreater(len(parts), 1)
        for p in parts:
            self.assertLessEqual(visible_len(p["text"]), 4096)
            self.assertIn("#s1", p["text"].split("\n")[-1])
            self.assertEqual(p["parse_mode"], "HTML")
        self.assertTrue(all("reply_markup" not in p for p in parts[:-1]))
        self.assertIn("reply_markup", parts[-1])
        self.assertIn(f"续 2/{len(parts)}", parts[1]["text"])
        seen = "".join(_plain(p["text"]) for p in parts)
        self.assertTrue(all(f"第 {i} 段" in seen for i in range(9)))  # nothing dropped

    def test_reply_to_any_part_reaches_the_session(self):
        body = "\n\n".join("字" * 900 for _ in range(9))
        self.side.notify(Notice(Level.DEFERRED, "completed", "s5", "L", body))
        first_part_id = 101
        self.side.handle_update(text_update("继续", reply_to=first_part_id))
        self.assertEqual(self.wb.prompts, [("s5", "继续")])

    def test_layout_header_prompt_body_options_tag(self):
        self.side.notify(Notice(Level.DEFERRED, "completed", "s1", "标题 · 做 A", "**粗体** 和 `代码`",
                                ["按刚才说的继续"], title="标题", prompt="做 A"))
        lines = self.api.sent()[0]["text"].split("\n")
        self.assertEqual(lines[0], "✅ <b>完成</b>｜<b>标题</b>")
        self.assertEqual(lines[1], "<i>↳ 做 A</i>")
        self.assertIn("<b>粗体</b> 和 <code>代码</code>", lines)
        self.assertIn("<b>选项</b>", lines)
        self.assertEqual(lines[-1], "<code>#s1</code>")

    def test_english_options_get_an_english_label(self):
        self.side.notify(Notice(Level.DEFERRED, "completed", "s1", "L", "Done.", ["Write tests"],
                                options_label="Options"))
        text = self.api.sent()[0]["text"]
        self.assertIn("<b>Options</b>\n1. Write tests", text)
        self.assertNotIn("选项", text)

    def test_approval_shows_the_command_as_code(self):
        self.side.notify(Notice(Level.URGENT, "approval", "s1", "L", "Bash: rm -rf <build>", command="rm -rf <build>",
                                tool="Bash", detail="rm -rf <build>"))
        self.assertIn("<b>Bash</b>\n<pre>rm -rf &lt;build&gt;</pre>", self.api.sent()[0]["text"])

    def test_formatting_telegram_rejects_is_resent_as_plain_text(self):
        api = RejectingApi()
        SideChannel(api, CHAT, self.wb, self.presence).notify(completed())
        [(_, first), (_, second)] = [c for c in api.calls if c[0] == "sendMessage"]
        self.assertEqual(first["parse_mode"], "HTML")
        self.assertNotIn("parse_mode", second)
        self.assertNotIn("<b>", second["text"])
        self.assertIn("完成了。", second["text"])

    def test_paused_sends_nothing_until_resumed(self):
        self.side.handle_update(text_update("/pause"))
        self.side.notify(completed())
        self.side.handle_update(text_update("/resume"))
        self.side.notify(approval())
        notices = [p for p in self.api.sent() if "ccmonitor · 做 A" in p["text"]]
        self.assertEqual(len(notices), 1)
        self.assertIn("rm -rf build", notices[0]["text"])

    # --- inbound: security ---

    def test_messages_from_other_chats_are_ignored_silently(self):
        self.side.notify(completed())
        n = len(self.api.calls)
        self.side.handle_update(text_update("rm -rf /", chat=STRANGER))
        self.side.handle_update(button_update("o:1:0", 101, chat=STRANGER))
        self.assertEqual(len(self.api.calls), n)
        self.assertEqual(self.wb.prompts, [])

    # --- inbound: options and approvals ---

    def test_option_button_writes_full_option_text_to_that_session(self):
        self.side.notify(completed(sid="s7"))
        data = buttons(self.api.sent()[0])[1]["callback_data"]
        self.side.handle_update(button_update(data, 101))
        self.assertEqual(self.wb.prompts, [("s7", "写测试")])

    def test_buttons_are_single_use(self):
        self.side.notify(completed())
        data = buttons(self.api.sent()[0])[0]["callback_data"]
        self.side.handle_update(button_update(data, 101))
        self.side.handle_update(button_update(data, 101))
        self.assertEqual(len(self.wb.prompts), 1)

    def test_buttons_removed_after_use(self):
        self.side.notify(completed())
        data = buttons(self.api.sent()[0])[0]["callback_data"]
        self.side.handle_update(button_update(data, 101))
        edits = [p for m, p in self.api.calls if m == "editMessageReplyMarkup"]
        self.assertEqual(edits, [{"chat_id": CHAT, "message_id": 101, "reply_markup": {"inline_keyboard": []}}])

    def test_approval_buttons(self):
        self.side.notify(approval(sid="s2"))
        allow, deny = buttons(self.api.sent()[0])
        self.side.handle_update(button_update(deny["callback_data"], 101))
        self.assertEqual(self.wb.approvals, [("s2", False, "rm -rf build")])

    # --- inbound: question buttons ---

    def tap(self, label):
        """Tap the button with this label on the latest keyboard of message 101."""
        edits = [p["reply_markup"] for m, p in self.api.calls if m == "editMessageReplyMarkup"]
        markup = edits[-1] if edits else self.api.sent()[0]["reply_markup"]
        [b] = [b for row in markup["inline_keyboard"] for b in row if b["text"] == label]
        self.side.handle_update(button_update(b["callback_data"], 101))

    def keyboard(self):
        edits = [p["reply_markup"] for m, p in self.api.calls if m == "editMessageReplyMarkup"]
        return [b["text"] for row in edits[-1]["inline_keyboard"] for b in row]

    def test_question_has_one_button_per_option(self):
        self.side.notify(question())
        self.assertEqual([b["text"] for b in buttons(self.api.sent()[0])], ["甲1", "乙1"])

    def test_option_tap_answers_that_question_in_that_session(self):
        self.side.notify(question("s3"))
        self.presence.away = False
        self.tap("乙1")
        self.assertEqual(self.wb.answers, [("s3", "问题1", 1, 1, [1])])
        self.assertTrue(self.presence.declared)  # taps ignore the away state and declare away
        self.assertEqual(self.wb.prompts, [])
        self.assertIn("已回答", self.api.last_text())
        self.assertEqual(self.keyboard(), [])

    def test_several_questions_are_offered_one_at_a_time_in_order(self):
        self.side.notify(question("s1", False, False))
        self.assertEqual([b["text"] for b in buttons(self.api.sent()[0])], ["问1·甲1", "问1·乙1"])
        self.tap("问1·甲1")
        self.assertIn("还有 1 个问题", self.api.last_text())
        self.assertEqual(self.keyboard(), ["问2·甲2", "问2·乙2"])
        self.tap("问2·乙2")
        self.assertEqual(self.wb.answers, [("s1", "问题1", 1, 2, [0]), ("s1", "问题2", 2, 2, [1])])
        self.assertEqual(self.keyboard(), [])

    def test_refused_answer_keeps_the_question_open(self):
        self.wb.answer_ok = False
        self.side.notify(question())
        self.tap("甲1")
        self.assertEqual(self.keyboard(), ["甲1", "乙1"])
        self.tap("甲1")  # fresh buttons, so the user can try again
        self.assertEqual(len(self.wb.answers), 2)

    def test_multi_select_ticks_locally_then_submits_the_ticked_options(self):
        self.side.notify(question("s1", True))
        self.assertEqual([b["text"] for b in buttons(self.api.sent()[0])], ["☐ 甲1", "☐ 乙1", "提交所选"])
        self.tap("☐ 乙1")
        self.tap("☐ 甲1")
        self.tap("☑ 乙1")
        self.tap("☐ 乙1")
        self.assertFalse(hasattr(self.wb, "answers"))  # ticking never touches the screen
        self.assertEqual(self.keyboard(), ["☑ 甲1", "☑ 乙1", "提交所选"])
        self.tap("提交所选")
        self.assertEqual(self.wb.answers, [("s1", "问题1", 1, 1, [0, 1])])

    def test_multi_select_submit_with_nothing_ticked_is_refused(self):
        self.side.notify(question("s1", True))
        self.tap("提交所选")
        self.assertFalse(hasattr(self.wb, "answers"))
        self.assertIn("至少一项", self.api.last_text())

    def test_used_question_buttons_expire(self):
        self.side.notify(question())
        data = buttons(self.api.sent()[0])[0]["callback_data"]
        self.side.handle_update(button_update(data, 101))
        self.side.handle_update(button_update(buttons(self.api.sent()[0])[1]["callback_data"], 101))
        self.assertEqual(len(self.wb.answers), 1)

    def test_every_button_press_is_answered(self):
        self.side.handle_update(button_update("o:999:0", 5))
        answers = [p for m, p in self.api.calls if m == "answerCallbackQuery"]
        self.assertEqual(answers[-1]["callback_query_id"], "cb1")
        self.assertIn("失效", answers[-1]["text"])

    def test_a_late_button_answer_still_runs_the_action(self):
        # Telegram refuses to answer a press that waited too long behind other work
        # ("query is too old"); the press itself must still take effect (seen live 2026-10-12).
        self.side.notify(completed())
        data = buttons(self.api.sent()[0])[0]["callback_data"]
        real = self.api.call

        def call(method, **params):
            if method == "answerCallbackQuery":
                raise TelegramError("answerCallbackQuery: 400 Bad Request: query is too old")
            return real(method, **params)
        self.api.call = call
        self.side.handle_update(button_update(data, 101))
        self.assertEqual(len(self.wb.prompts), 1)

    def test_write_result_is_reported_back(self):
        self.wb.result = "已发送"
        self.side.notify(completed())
        self.side.handle_update(button_update(buttons(self.api.sent()[0])[0]["callback_data"], 101))
        self.assertIn("已发送", self.api.last_text())

    # --- inbound: free prompt routing ---

    def test_reply_routes_to_that_messages_session(self):
        self.side.notify(completed(sid="a"))   # message 101
        self.side.notify(completed(sid="b"))   # message 102
        self.side.handle_update(text_update("改用方案二", reply_to=101))
        self.assertEqual(self.wb.prompts, [("a", "改用方案二")])

    def test_notice_carries_a_session_tag(self):
        self.side.notify(completed(sid="1a2b3c4d-0000-0000-0000-000000000000"))
        self.assertIn("#1a2b3c4d", self.api.sent()[0]["text"].split("\n")[-1])

    def test_reply_to_an_old_style_notice_with_the_tag_on_the_first_line(self):
        side = SideChannel(self.api, CHAT, self.wb, self.presence,
                           session_by_tag=lambda tag: "1a2b3c4d-full" if tag == "1a2b3c4d" else None)
        side.handle_update(text_update("继续", reply_to=55, reply_text="✅ 完成 · 标题 · #1a2b3c4d\n\n正文"))
        self.assertEqual(self.wb.prompts, [("1a2b3c4d-full", "继续")])

    def test_reply_to_a_notice_from_before_a_restart_uses_its_tag(self):
        side = SideChannel(self.api, CHAT, self.wb, self.presence,
                           session_by_tag=lambda tag: "1a2b3c4d-full-id" if tag == "1a2b3c4d" else None)
        side.handle_update(text_update("继续", reply_to=55, reply_text="✅ 完成 · 标题 · #1a2b3c4d\n\n就绪"))
        self.assertEqual(self.wb.prompts, [("1a2b3c4d-full-id", "继续")])

    def test_reply_to_an_unknown_message_never_falls_back_to_another_session(self):
        self.side.notify(completed(sid="b"))
        self.side.handle_update(text_update("继续", reply_to=55, reply_text="随便一条消息"))
        self.assertEqual(self.wb.prompts, [])
        self.assertIn("找不到", self.api.last_text())

    def test_expired_button_suggests_replying(self):
        self.side.handle_update(button_update("x:999", 5))
        answers = [p for m, p in self.api.calls if m == "answerCallbackQuery"]
        self.assertIn("回复", answers[-1]["text"])

    def test_plain_message_goes_to_most_recent_notified_session(self):
        self.side.notify(completed(sid="a"))
        self.side.notify(approval(sid="b"))
        self.side.handle_update(text_update("继续"))
        self.assertEqual(self.wb.prompts, [("b", "继续")])

    def test_plain_message_with_no_session_is_refused(self):
        self.side.handle_update(text_update("继续"))
        self.assertEqual(self.wb.prompts, [])
        self.assertIn("没有", self.api.last_text())

    # --- away state ---

    def test_free_text_writes_even_when_computer_in_use(self):
        # User's call, 2026-10-14: no more waiting for a quiet computer before a reply.
        self.side.notify(completed(sid="s5"))
        self.presence.away = False
        self.side.handle_update(text_update("继续"))
        self.assertEqual(self.wb.prompts, [("s5", "继续")])
        self.assertTrue(self.presence.declared)  # so the turn it starts still gets notices

    def test_option_button_writes_even_when_computer_in_use(self):
        self.side.notify(completed(sid="s7"))
        self.presence.away = False
        self.side.handle_update(button_update(buttons(self.api.sent()[0])[1]["callback_data"], 101))
        self.assertEqual(self.wb.prompts, [("s7", "写测试")])
        self.assertTrue(self.presence.declared)  # so the next answer still gets options

    def test_approval_buttons_work_even_when_computer_in_use(self):
        # User's call, 2026-10-11: every button is accepted regardless of the away state.
        self.side.notify(approval(sid="s2"))
        self.presence.away = False
        allow, deny = buttons(self.api.sent()[0])
        self.side.handle_update(button_update(allow["callback_data"], 101))
        self.assertEqual(self.wb.approvals, [("s2", True, "rm -rf build")])
        self.assertTrue(self.presence.declared)

    def test_commands_do_not_declare_away(self):
        self.side.handle_update(text_update("/status"))
        self.assertIsNone(self.presence.declared)

    def test_at_the_desk_nothing_goes_to_telegram(self):
        self.presence.away = False
        self.wb.suggestion = "继续"
        self.side.notify(completed())
        self.side.notify(approval())
        self.assertEqual(self.api.sent(), [])
        self.assertEqual(getattr(self.wb, "suggestion_reads", []), [])

    def test_a_presence_error_still_sends_the_notice(self):
        def broken():
            raise OSError
        self.presence.is_away = broken
        self.side.notify(completed())
        self.assertEqual(len(self.api.sent()), 1)

    def test_the_away_button_shows_only_while_not_away(self):
        self.presence.away = False
        self.side.handle_update(text_update("/status"))
        self.assertEqual(self.api.sent()[-1]["reply_markup"]["keyboard"], [[{"text": "/away"}]])
        self.presence.away = True
        self.side.handle_update(text_update("/away"))
        self.assertEqual(self.api.sent()[-1]["reply_markup"], {"remove_keyboard": True})

    def test_greeting_sets_the_menu_and_the_button(self):
        self.presence.away = False
        self.side.greet("ccmonitor 已启动")
        [menu] = [p for m, p in self.api.calls if m == "setMyCommands"]
        self.assertEqual([c["command"] for c in menu["commands"]][:2], ["away", "back"])
        self.assertEqual(self.api.sent()[0]["reply_markup"]["keyboard"], [[{"text": "/away"}]])

    def test_a_change_of_away_state_is_said_silently_with_the_new_keyboard(self):
        self.presence.away = False
        self.side.greet("ccmonitor 已启动")
        self.side.check_presence()
        self.assertEqual(len(self.api.sent()), 1)  # no change, no message
        self.presence.away = True
        self.side.check_presence()
        left = self.api.sent()[-1]
        self.assertTrue(left["disable_notification"])
        self.assertEqual(left["reply_markup"], {"remove_keyboard": True})
        self.side.check_presence()
        self.assertEqual(len(self.api.sent()), 2)
        self.presence.away = False
        self.side.check_presence()
        self.assertEqual(self.api.sent()[-1]["reply_markup"]["keyboard"], [[{"text": "/away"}]])
        self.assertIn("回到电脑前", self.api.last_text())

    def test_nothing_is_said_about_presence_before_the_first_message(self):
        self.side.check_presence()
        self.assertEqual(self.api.sent(), [])

    def test_away_and_back_commands_reach_presence(self):
        self.side.handle_update(text_update("/away"))
        self.assertTrue(self.presence.declared)
        self.side.handle_update(text_update("/back"))
        self.assertFalse(self.presence.declared)

    def test_status_command_reports_state(self):
        self.side.status_extra = lambda: "健康：write 2"
        self.side.handle_update(text_update("/pause"))
        self.side.handle_update(text_update("/status"))
        text = self.api.last_text()
        self.assertIn("离开", text)
        self.assertIn("暂停", text)
        self.assertIn("健康：write 2", text)

    def test_mode_command_shows_and_switches(self):
        calls = []
        self.side.on_mode = lambda arg: calls.append(arg) or f"模式 {arg}"
        self.side.handle_update(text_update("/mode"))
        self.side.handle_update(text_update("/mode B"))
        self.assertEqual(calls, [None, "b"])
        self.assertIn("模式 b", self.api.last_text())
        self.side.handle_update(text_update("/mode c"))
        self.assertEqual(calls, [None, "b"])
        self.assertIn("用法", self.api.last_text())

    # --- official suggestion (DESIGN.md 3.8) ---

    def edits(self):
        return [p for m, p in self.api.calls if m == "editMessageText"]

    def test_suggestion_is_added_to_the_completion_notice_while_away(self):
        self.wb.suggestion = "第三步：选什么语言和库来实现？"
        self.side.notify(completed(sid="s1"))
        [edit] = self.edits()
        self.assertEqual(edit["message_id"], 101)
        self.assertIn("💡 <b>官方建议</b>：第三步：选什么语言和库来实现？", edit["text"])
        self.assertIn("1. 按刚才说的继续", edit["text"])
        labels = [b["text"] for b in buttons(edit)]
        self.assertEqual(labels, ["1. 按刚才说的继续", "2. 写测试", "💡 官方建议"])

    def side_with_timer(self):
        self.pending = []
        return SideChannel(self.api, CHAT, self.wb, self.presence, later=lambda s, fn: self.pending.append((s, fn)))

    def test_coming_back_before_the_second_read_cancels_it(self):
        side = self.side_with_timer()
        side.notify(completed(sid="s1"))
        self.presence.away = False
        self.wb.suggestion = "晚到的建议"
        self.pending.pop(0)[1]()
        self.assertEqual(self.wb.suggestion_reads, ["s1"])
        self.assertEqual(self.edits(), [])

    def test_a_missed_suggestion_is_read_once_more_a_minute_later(self):
        side = self.side_with_timer()
        side.notify(completed(sid="s1"))
        self.assertEqual(self.wb.suggestion_reads, ["s1"])
        self.assertEqual(self.pending[0][0], 60)
        self.wb.suggestion = "晚到的建议"
        self.pending.pop(0)[1]()
        self.assertEqual(self.wb.suggestion_reads, ["s1", "s1"])
        self.assertIn("晚到的建议", self.edits()[0]["text"])
        self.assertEqual(self.pending, [])

    def test_a_second_miss_gives_up(self):
        side = self.side_with_timer()
        side.notify(completed(sid="s1"))
        self.pending.pop(0)[1]()
        self.assertEqual(self.wb.suggestion_reads, ["s1", "s1"])
        self.assertEqual(self.pending, [])
        self.assertEqual(self.edits(), [])

    def test_no_edit_when_there_is_no_suggestion(self):
        self.side.notify(completed(sid="s1"))
        self.assertEqual(self.wb.suggestion_reads, ["s1"])
        self.assertEqual(self.edits(), [])

    def test_only_completions_get_a_suggestion(self):
        self.wb.suggestion = "x"
        self.side.notify(approval())
        self.assertEqual(getattr(self.wb, "suggestion_reads", []), [])

    def test_suggestion_works_without_options(self):
        self.wb.suggestion = "继续写 README"
        self.side.notify(completed(options=()))
        [edit] = self.edits()
        self.assertEqual([b["text"] for b in buttons(edit)], ["💡 官方建议"])

    def test_tapping_the_suggestion_sends_it_even_at_the_desk(self):
        self.wb.suggestion = "继续写 README"
        self.side.notify(completed(sid="s9"))
        self.presence.away = False
        self.side.handle_update(button_update(buttons(self.edits()[0])[-1]["callback_data"], 101))
        self.assertEqual(self.wb.suggestion_sends, [("s9", "继续写 README")])
        self.assertTrue(self.presence.declared)
        self.assertIn("继续写 README", self.api.last_text())

    def test_changed_suggestion_is_not_sent_and_the_notice_shows_the_new_one(self):
        self.wb.suggestion = "列出第一版的具体任务清单"
        self.side.notify(completed(sid="s9"))
        self.wb.changed_to = "家人在大陆，就用微信小程序"
        self.side.handle_update(button_update(buttons(self.edits()[0])[-1]["callback_data"], 101))
        second = self.edits()[-1]
        self.assertIn("💡 <b>官方建议</b>：家人在大陆，就用微信小程序", second["text"])
        self.assertEqual(second["text"].count("官方建议</b>："), 1)
        self.assertIn("未发送", self.api.last_text())
        # A second tap sends the new text, and only that.
        self.wb.changed_to = None
        self.side.handle_update(button_update(buttons(second)[-1]["callback_data"], 101))
        self.assertEqual(self.wb.suggestion_sends[-1], ("s9", "家人在大陆，就用微信小程序"))

    # --- stale buttons come off (user's call, 2026-10-13) ---

    def cleared(self):
        return [p["message_id"] for m, p in self.api.calls
                if m == "editMessageReplyMarkup" and p["reply_markup"] == {"inline_keyboard": []}]

    def test_a_new_prompt_takes_the_options_off_that_sessions_completion(self):
        self.side.notify(completed(sid="s1"))  # message 101
        self.side.notify(completed(sid="s2"))  # message 102
        self.side.session_event("UserPromptSubmit", "s1")
        self.assertEqual(self.cleared(), [101])

    def test_a_new_prompt_leaves_a_pending_approval(self):
        self.side.notify(approval(sid="s1"))
        self.side.session_event("UserPromptSubmit", "s1")  # e.g. a mid-turn message
        self.assertEqual(self.cleared(), [])

    def test_a_new_approval_settles_the_earlier_one(self):
        self.side.notify(approval(sid="s1"))  # 101
        self.side.session_event("PermissionRequest", "s1")
        self.side.notify(approval(sid="s1"))  # 102 keeps its buttons
        self.assertEqual(self.cleared(), [101])

    def test_a_turn_end_settles_approvals_and_questions(self):
        self.side.notify(approval(sid="s1"))  # 101
        self.side.notify(question(sid="s1"))  # 102
        self.side.session_event("Stop", "s1")
        self.assertEqual(self.cleared(), [101, 102])
        self.side.session_event("Stop", "s1")
        self.assertEqual(self.cleared(), [101, 102])  # once only

    def approval_of(self, sid, command):
        return Notice(Level.URGENT, "approval", sid, "L", f"Bash: {command}", command=command,
                      key=tool_key({"tool_name": "Bash", "tool_input": {"command": command}}))

    def test_an_approval_settled_on_the_computer_comes_off_when_the_tool_runs(self):
        self.side.notify(self.approval_of("s1", "rm -rf build"))  # 101
        self.side.session_event("PostToolUse", "s1", {"tool_name": "Bash", "tool_input": {"command": "rm -rf build"}})
        self.assertEqual(self.cleared(), [101])

    def test_a_denied_or_failed_tool_call_also_settles_its_approval(self):
        self.side.notify(self.approval_of("s1", "make"))
        self.side.session_event("PostToolUseFailure", "s1", {"tool_name": "Bash", "tool_input": {"command": "make"}})
        self.assertEqual(self.cleared(), [101])

    def test_each_clear_is_logged_with_its_event(self):
        self.side.notify(self.approval_of("s1", "make"))
        with self.assertLogs("ccmonitor", level="INFO") as logs:
            self.side.session_event("PostToolUse", "s1", {"tool_name": "Bash", "tool_input": {"command": "make"}})
        self.assertIn("cleared 1 notice buttons on PostToolUse (approval)", logs.output[0])
        self.assertNotIn("make", logs.output[0])

    def test_another_tool_call_leaves_the_approval(self):
        self.side.notify(self.approval_of("s1", "rm -rf build"))
        self.side.notify(completed(sid="s1"))
        self.side.session_event("PostToolUse", "s1", {"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.side.session_event("PostToolUse", "s1", {"tool_name": "Read", "tool_input": {"file_path": "x"}})
        self.side.session_event("PostToolUse", "s2", {"tool_name": "Bash", "tool_input": {"command": "rm -rf build"}})
        self.assertEqual(self.cleared(), [])

    # A Deny pressed in Desktop interrupts the turn and fires no hook (live 2026-10-13 12:17);
    # Desktop writes the rejected result to the transcript, which is what's checked.

    def transcript(self, *lines):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("".join(json.dumps(line) + "\n" for line in lines))
        self.addCleanup(os.remove, path)
        return path

    def denied_after_approval(self, command="rm -rf build", denied=True):
        """An approval notice for s1, then its call in the transcript, rejected or still waiting."""
        lines = [call("t1", command)] + ([rejected("t1")] if denied else [])
        path = self.transcript(*lines)
        self.side.session_event("PermissionRequest", "s1", {"transcript_path": path})
        self.side.notify(self.approval_of("s1", command))  # 101
        return path

    def test_the_next_prompt_takes_off_an_approval_denied_in_desktop(self):
        self.denied_after_approval()
        self.side.session_event("UserPromptSubmit", "s1")
        self.assertEqual(self.cleared(), [101])

    def test_a_mid_turn_prompt_leaves_an_approval_still_waiting(self):
        self.denied_after_approval(denied=False)
        self.side.session_event("UserPromptSubmit", "s1")
        self.assertEqual(self.cleared(), [])

    def test_the_minute_sweep_takes_off_a_denied_approval_without_any_event(self):
        self.denied_after_approval()
        self.side.notify(completed(sid="s1"))  # 102: not the sweep's business
        with self.assertLogs("ccmonitor", level="INFO") as logs:
            self.side.sweep_answered()
        self.assertEqual(self.cleared(), [101])
        self.assertIn("cleared 1 notice buttons on transcript (approval)", logs.output[0])
        self.side.sweep_answered()
        self.assertEqual(self.cleared(), [101])  # once only

    def test_the_sweep_leaves_a_waiting_approval_and_sessions_without_a_transcript(self):
        self.denied_after_approval(denied=False)
        self.side.notify(self.approval_of("s2", "make"))  # no event of s2 seen: no path
        self.side.sweep_answered()
        self.assertEqual(self.cleared(), [])

    def test_a_denied_question_also_comes_off(self):
        path = self.transcript({"message": {"content": [
            {"type": "tool_use", "id": "q1", "name": "AskUserQuestion", "input": {"questions": []}}]}},
            rejected("q1"))
        self.side.session_event("PermissionRequest", "s1", {"transcript_path": path})
        self.side.notify(replace(question("s1"), key=tool_key(
            {"tool_name": "AskUserQuestion", "tool_input": {"questions": []}})))
        self.side.sweep_answered()
        self.assertEqual(len(self.cleared()), 1)

    def test_an_expired_button_is_taken_off_when_tapped(self):
        self.side.handle_update(button_update("x:999", 77))
        self.assertEqual(self.cleared(), [77])

    def test_a_suggestion_read_after_the_session_moved_on_is_not_shown(self):
        self.wb.suggestion = "晚到的建议"
        pending = []
        side = SideChannel(self.api, CHAT, self.wb, self.presence, later=lambda s, fn: pending.append(fn))
        self.wb.suggestion = None
        side.notify(completed(sid="s1"))
        side.session_event("UserPromptSubmit", "s1")
        self.wb.suggestion = "晚到的建议"
        pending.pop(0)()  # the second read, a minute later
        self.assertEqual(self.edits(), [])

    def test_unknown_command_lists_commands(self):
        self.side.handle_update(text_update("/what"))
        self.assertIn("/pause", self.api.last_text())
        self.assertIn("/status", self.api.last_text())


def call(tool_use_id, command):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_use_id, "name": "Bash", "input": {"command": command}}]}}


def rejected(tool_use_id):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tool_use_id, "is_error": True,
         "content": "The user doesn't want to proceed with this tool use."}]}}


if __name__ == "__main__":
    unittest.main()
