import json
import tempfile
import unittest
from pathlib import Path

from ccmonitor.notice import Level, Notice
from ccmonitor.sidechannel import SideChannel
from ccmonitor.telegram import TelegramError
from ccmonitor.topics import TopicMap
from tests.test_sidechannel import CHAT, FakeApi, FakePresence, FakeWriteBack, button_update, buttons


class TopicApi(FakeApi):
    """FakeApi that also creates topics and can pretend a topic was deleted."""

    def __init__(self):
        super().__init__()
        self._next_thread = 500
        self.deleted: set[int] = set()

    def call(self, method, **params):
        if method == "createForumTopic":
            self.calls.append((method, params))
            self._next_thread += 1
            return {"message_thread_id": self._next_thread, "name": params["name"]}
        if method == "sendMessage" and params.get("message_thread_id") in self.deleted:
            self.calls.append((method, params))
            raise TelegramError("sendMessage: 400 Bad Request: message thread not found")
        return super().call(method, **params)

    def created(self):
        return [p for m, p in self.calls if m == "createForumTopic"]


def completed(sid="s1", title="重构登录", options=("按刚才说的继续",)):
    return Notice(Level.DEFERRED, "completed", sid, title, "完成了。", list(options), title=title)


def topic_text(text, thread, chat=CHAT, reply_to=None):
    msg = {"message_id": 7, "chat": {"id": chat}, "text": text, "message_thread_id": thread, "is_topic_message": True}
    if reply_to:
        msg["reply_to_message"] = reply_to
    return {"update_id": 9, "message": msg}


class TopicMapTest(unittest.TestCase):
    def test_persists_ids_and_last_activity_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "topics.json"
            m = TopicMap(path, clock=lambda: 1000.0)
            m.set("cli-1", 501)
            again = TopicMap(path)
            self.assertEqual(again.thread_of("cli-1"), 501)
            self.assertEqual(again.session_of(501), "cli-1")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"cli-1": [501, 1000]})

    def test_old_file_without_times_counts_from_load(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "topics.json"
            path.write_text('{"cli-1": 501}', encoding="utf-8")
            now = [5000.0]
            m = TopicMap(path, clock=lambda: now[0])
            self.assertEqual(m.thread_of("cli-1"), 501)
            self.assertEqual(m.stale(), [])
            now[0] += 3 * 24 * 3600
            self.assertEqual(m.stale(), [("cli-1", 501)])

    def test_old_file_is_saved_at_once_so_a_restart_keeps_the_count(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "topics.json"
            path.write_text('{"cli-1": 501}', encoding="utf-8")
            TopicMap(path, clock=lambda: 5000.0)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"cli-1": [501, 5000]})
            restarted = TopicMap(path, clock=lambda: 5000.0 + 3 * 24 * 3600)
            self.assertEqual(restarted.stale(), [("cli-1", 501)])

    def test_touch_keeps_a_topic_fresh(self):
        now = [0.0]
        m = TopicMap(None, clock=lambda: now[0])
        m.set("a", 501)
        m.set("b", 502)
        now[0] = 2 * 24 * 3600
        m.touch("b")
        now[0] = 3 * 24 * 3600
        self.assertEqual(m.stale(), [("a", 501)])

    def test_unreadable_file_starts_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "topics.json"
            path.write_text("{", encoding="utf-8")
            self.assertIsNone(TopicMap(path).thread_of("x"))

    def test_forget(self):
        m = TopicMap(None)
        m.set("cli-1", 501)
        m.forget("cli-1")
        self.assertIsNone(m.thread_of("cli-1"))
        self.assertIsNone(m.session_of(501))


class ThreadedSideChannelTest(unittest.TestCase):
    def setUp(self):
        self.api = TopicApi()
        self.wb = FakeWriteBack()
        self.presence = FakePresence()
        self.now = [0.0]
        self.topics = TopicMap(None, clock=lambda: self.now[0])
        self.side = SideChannel(self.api, CHAT, self.wb, self.presence, topics=self.topics)

    DAY = 24 * 3600

    def test_topics_idle_for_three_days_are_deleted(self):
        self.side.notify(completed(sid="old"))
        self.side.notify(completed(sid="busy"))
        old, busy = self.topics.thread_of("old"), self.topics.thread_of("busy")
        self.now[0] = 2 * self.DAY
        self.side.handle_update(topic_text("继续", busy))  # the user acting in a topic keeps it
        self.now[0] = 3 * self.DAY
        self.side.sweep_topics()
        deleted = [p["message_thread_id"] for m, p in self.api.calls if m == "deleteForumTopic"]
        self.assertEqual(deleted, [old])
        self.assertIsNone(self.topics.thread_of("old"))
        self.assertEqual(self.topics.thread_of("busy"), busy)

    def test_a_deleted_session_gets_a_new_topic_with_its_next_notice(self):
        self.side.notify(completed(sid="s"))
        self.now[0] = 3 * self.DAY
        self.side.sweep_topics()
        self.side.notify(completed(sid="s"))
        self.assertEqual(len(self.api.created()), 2)

    def test_a_failed_delete_is_retried_on_the_next_sweep(self):
        self.side.notify(completed(sid="s"))
        self.now[0] = 3 * self.DAY
        real = self.api.call

        def refuse(method, **params):
            if method == "deleteForumTopic":
                raise TelegramError("deleteForumTopic: ConnectionError")
            return real(method, **params)
        self.api.call = refuse
        self.side.sweep_topics()
        self.assertIsNotNone(self.topics.thread_of("s"))
        self.api.call = real
        self.side.sweep_topics()
        self.assertIsNone(self.topics.thread_of("s"))

    def test_presence_change_goes_to_the_topic_the_user_last_acted_in(self):
        self.side.notify(completed(sid="s"))
        thread = self.topics.thread_of("s")
        self.side.handle_update(topic_text("/away", thread))
        self.presence.away = False
        self.side.check_presence()
        last = self.api.sent()[-1]
        self.assertEqual(last["message_thread_id"], thread)
        self.assertIn("回到电脑前", last["text"])
        self.assertEqual(last["reply_markup"]["keyboard"], [[{"text": "/away"}]])

    def test_presence_change_falls_back_to_the_main_chat_when_the_topic_is_gone(self):
        self.side.notify(completed(sid="s"))
        thread = self.topics.thread_of("s")
        self.side.handle_update(topic_text("/away", thread))
        self.api.deleted.add(thread)
        self.presence.away = False
        self.side.check_presence()
        last = self.api.sent()[-1]
        self.assertNotIn("message_thread_id", last)
        self.assertIn("回到电脑前", last["text"])

    def stray_command(self, text, thread=777, name=None):
        root = {"message_id": thread, "forum_topic_created": {"name": name or text}}
        return topic_text(text, thread, reply_to=root)

    def test_a_command_from_all_messages_is_answered_in_the_main_chat_and_its_topic_removed(self):
        self.side.handle_update(self.stray_command("/away"))
        self.assertTrue(self.presence.declared)
        answer = self.api.sent()[-1]
        self.assertNotIn("message_thread_id", answer)
        self.assertIn("已声明离开", answer["text"])
        deleted = [p["message_thread_id"] for m, p in self.api.calls if m == "deleteForumTopic"]
        self.assertEqual(deleted, [777])

    def test_a_command_in_a_session_topic_stays_there(self):
        self.side.notify(completed(sid="s1"))
        thread = self.topics.thread_of("s1")
        self.side.handle_update(topic_text("/status", thread))
        self.assertEqual(self.api.sent()[-1]["message_thread_id"], thread)
        self.assertEqual([p for m, p in self.api.calls if m == "deleteForumTopic"], [])

    def topic_created(self, name, thread=778, chat=CHAT):
        return {"update_id": 10, "message": {"message_id": thread, "chat": {"id": chat}, "message_thread_id": thread,
                                             "is_topic_message": True, "forum_topic_created": {"name": name}}}

    def deleted_topics(self):
        return [p["message_thread_id"] for m, p in self.api.calls if m == "deleteForumTopic"]

    def test_an_empty_topic_opened_under_a_command_name_is_removed_when_created(self):
        self.side.handle_update(self.topic_created("/away"))
        self.assertEqual(self.deleted_topics(), [778])
        self.assertEqual(self.api.sent(), [])  # nothing to answer: there was no command

    def test_the_command_that_follows_in_that_topic_is_answered_without_deleting_twice(self):
        self.side.handle_update(self.topic_created("/away", thread=777))
        self.side.handle_update(self.stray_command("/away"))
        self.assertTrue(self.presence.declared)
        self.assertNotIn("message_thread_id", self.api.sent()[-1])
        self.assertEqual(self.deleted_topics(), [777])

    def test_created_topics_that_are_not_command_names_are_kept(self):
        self.side.handle_update(self.topic_created("我的笔记"))
        self.side.handle_update(self.topic_created("/status #1a2b3c4d"))
        self.side.handle_update(self.topic_created("/away", chat=CHAT + 1))
        self.assertEqual(self.deleted_topics(), [])

    def test_a_created_topic_that_holds_a_session_is_kept(self):
        self.side.notify(completed(sid="s1"))
        thread = self.topics.thread_of("s1")
        self.side.handle_update(self.topic_created("/away", thread=thread))
        self.assertEqual(self.deleted_topics(), [])

    def test_a_topic_named_with_a_session_tag_is_kept(self):
        self.side.handle_update(self.stray_command("/status", name="重构登录 #1a2b3c4d"))
        self.assertEqual([p for m, p in self.api.calls if m == "deleteForumTopic"], [])

    def test_first_notice_creates_a_topic_and_goes_into_it(self):
        self.side.notify(completed(sid="1a2b3c4d-x", title="重构登录"))
        [created] = self.api.created()
        self.assertEqual(created["name"], "重构登录 #1a2b3c4d")
        self.assertEqual(self.api.sent()[0]["message_thread_id"], 501)

    def test_later_notices_reuse_the_topic(self):
        self.side.notify(completed(sid="s1"))
        self.side.notify(completed(sid="s1"))
        self.assertEqual(len(self.api.created()), 1)
        self.assertEqual({p["message_thread_id"] for p in self.api.sent()}, {501})

    def test_each_session_gets_its_own_topic(self):
        self.side.notify(completed(sid="a"))
        self.side.notify(completed(sid="b"))
        self.assertEqual([p["message_thread_id"] for p in self.api.sent()], [501, 502])

    def test_typing_in_a_topic_goes_to_its_session_without_replying(self):
        self.side.notify(completed(sid="a"))
        self.side.notify(completed(sid="b"))
        self.side.handle_update(topic_text("改用方案二", 501))
        self.assertEqual(self.wb.prompts, [("a", "改用方案二")])

    def test_answers_go_back_into_the_same_topic(self):
        self.side.notify(completed(sid="a"))
        self.side.handle_update(topic_text("继续", 501))
        self.assertEqual(self.api.sent()[-1]["message_thread_id"], 501)

    def test_button_answer_goes_into_the_buttons_topic(self):
        self.side.notify(completed(sid="a"))
        data = buttons(self.api.sent()[0])[0]["callback_data"]
        update = button_update(data, 101)
        update["callback_query"]["message"]["message_thread_id"] = 501
        self.side.handle_update(update)
        self.assertEqual(self.wb.prompts, [("a", "按刚才说的继续")])
        self.assertEqual(self.api.sent()[-1]["message_thread_id"], 501)

    def test_topic_mapping_survives_a_restart(self):
        self.side.notify(completed(sid="a"))
        fresh = SideChannel(self.api, CHAT, self.wb, self.presence, topics=self.topics)
        fresh.handle_update(topic_text("继续", 501))
        self.assertEqual(self.wb.prompts, [("a", "继续")])

    def test_unknown_topic_is_refused_with_a_hint(self):
        self.side.handle_update(topic_text("继续", 999))
        self.assertEqual(self.wb.prompts, [])
        self.assertIn("没有对应的会话", self.api.last_text())

    def test_unknown_topic_named_with_a_tag_is_recovered(self):
        side = SideChannel(self.api, CHAT, self.wb, self.presence, topics=TopicMap(None),
                           session_by_tag=lambda tag: "1a2b3c4d-full" if tag == "1a2b3c4d" else None)
        created = {"message_id": 3, "forum_topic_created": {"name": "重构登录 #1a2b3c4d"}}
        side.handle_update(topic_text("继续", 777, reply_to=created))
        self.assertEqual(self.wb.prompts, [("1a2b3c4d-full", "继续")])

    def test_reply_to_a_notice_wins_over_the_topic(self):
        self.side.notify(completed(sid="a"))   # message 101 in topic 501
        self.side.notify(completed(sid="b"))   # message 102 in topic 502
        self.side.handle_update(topic_text("继续", 502, reply_to={"message_id": 101, "text": "x"}))
        self.assertEqual(self.wb.prompts, [("a", "继续")])

    def test_deleted_topic_is_recreated(self):
        self.side.notify(completed(sid="a"))
        self.api.deleted.add(501)
        self.side.notify(completed(sid="a"))
        self.assertEqual(len(self.api.created()), 2)
        self.assertEqual(self.api.sent()[-1]["message_thread_id"], 502)
        self.assertEqual(self.topics.thread_of("a"), 502)

    def test_topic_is_renamed_when_the_title_changes(self):
        self.side.notify(completed(sid="1a2b3c4d-x", title="新会话"))
        self.side.notify(completed(sid="1a2b3c4d-x", title="重构登录"))
        edits = [p for m, p in self.api.calls if m == "editForumTopic"]
        self.assertEqual(edits, [{"chat_id": CHAT, "message_thread_id": 501, "name": "重构登录 #1a2b3c4d"}])

    def test_commands_answer_in_their_topic(self):
        self.side.notify(completed(sid="a"))  # topic 501 belongs to session a
        self.side.handle_update(topic_text("/status", 501))
        self.assertEqual(self.api.sent()[-1]["message_thread_id"], 501)

    def test_without_topics_nothing_changes(self):
        side = SideChannel(self.api, CHAT, self.wb, self.presence)
        side.notify(completed(sid="a"))
        self.assertEqual(self.api.created(), [])
        self.assertNotIn("message_thread_id", self.api.sent()[-1])


if __name__ == "__main__":
    unittest.main()
