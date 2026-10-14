"""Side channel over Telegram (DESIGN.md 3.3): notices out, interventions in.

Everything here lives in memory: which message belongs to which session and the
full text behind each button. Each notice also carries a short session tag, so a
reply still finds its session after a restart has emptied that memory. Only the
configured chat id is listened to.
"""
import html
import itertools
import logging
import re
from collections import OrderedDict
from typing import Callable

from .away import Presence
from .classify import answered, tool_key
from .notice import Notice
from .sessions import find_cli_by_tag
from .telegram import TelegramError
from .topics import TopicMap
from .tgformat import LIMIT, md_to_html, split_markdown, visible_len
from .writeback import WriteBack

log = logging.getLogger("ccmonitor")

HEADERS = {"approval": ("🔴", "需要审批"), "question": ("🔴", "Claude 提问"), "completed": ("✅", "完成"),
           "stuck": ("⏳", "可能卡住"), "error": ("❌", "出错"), "offer": ("🔴", "等你选择"),
           "proposal": ("💬", "提议新会话")}
COMMANDS = "/away 声明离开  /back 回到电脑前  /pause 暂停通知  /resume 恢复通知  /status 状态  /mode a|b 转发模式"
# An /away button under the input box (a reply keyboard: tapping it sends the command),
# shown only while not away (user's call, 2026-10-12); every command is in the "/" menu.
# Telegram only changes the keyboard with a message, so every answer carries the one for
# the current state, and check_presence sends a silent message when the state changes.
AWAY_BUTTON = {"keyboard": [[{"text": "/away"}]], "resize_keyboard": True, "is_persistent": True}
NO_BUTTON = {"remove_keyboard": True}
MENU = [{"command": "away", "description": "声明离开，通知转到这里"},
        {"command": "back", "description": "回到电脑前，不再转发"},
        {"command": "status", "description": "状态"},
        {"command": "pause", "description": "暂停通知"},
        {"command": "resume", "description": "恢复通知"},
        {"command": "mode", "description": "转发模式 a|b"}]
MEMORY = 500  # notices remembered for replies and buttons
TAG_CHARS = 8
TAG = re.compile(r"#([0-9a-f]{%d})\b" % TAG_CHARS)
# A long session's suggestion can take longer than one read waits for (seen live
# 2026-10-12 13:26: none after ~35 s, there a while later), so a miss is read once more.
SUGGEST_AGAIN_SECONDS = 60


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _plain(html_text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", html_text))


class _Questions:
    """A question notice's progress through Desktop's card, which asks one question at a time."""

    def __init__(self, session_id: str, questions: list[dict]):
        self.session_id = session_id
        self.questions = questions
        self.current = 0          # the question the buttons are for
        self.picked: set[int] = set()  # multi-select ticks so far, kept only here until submitted
        self.message_id: int | None = None
        self.tokens: list[str] = []


class SideChannel:
    def __init__(self, api, chat_id: int, write_back: WriteBack, presence: Presence,
                 session_by_tag: Callable[[str], str | None] = find_cli_by_tag,
                 topics: TopicMap | None = None,
                 later: Callable[[float, Callable[[], None]], None] | None = None):
        self._api = api
        self._chat_id = chat_id
        self._wb = write_back
        self._presence = presence
        self._session_by_tag = session_by_tag
        self._topics = topics  # set when the bot runs in threaded mode: one topic per session
        self._later = later    # runs a callable on this worker after a delay
        self.paused = False
        self.status_extra: Callable[[], str] = lambda: ""
        self.on_mode: Callable[[str | None], str] | None = None  # /mode [a|b]: switch or show the relay mode
        self._ids = itertools.count(1)
        self._session_of_message: OrderedDict[int, str] = OrderedDict()
        self._actions: OrderedDict[str, tuple[str, str, object]] = OrderedDict()  # token -> (session, kind, payload)
        self._last_session: str | None = None
        self._topic_names: dict[str, str] = {}  # session -> topic name last set, to rename on title change
        self._reply_thread: int | None = None   # topic of the update being handled; answers go there
        # Notices whose buttons still show, per session: message id -> notice kind. A later
        # event of that session makes them stale, and they are taken off (session_event).
        self._live: dict[str, dict[int, tuple[str, str]]] = {}  # message id -> (kind, tool key)
        self._transcripts: dict[str, str] = {}  # session -> transcript path, to see a Deny (answered)
        self._last_thread: int | None = None    # topic the user last acted in; presence changes go there
        self._shown_away: bool | None = None    # the away state the keyboard on the phone matches
        self._gone_topics: set[int] = set()     # topics deleted here, so a later message in one isn't deleted again

    # --- outbound ---

    def notify(self, n: Notice) -> None:
        """Send a notice as one or more Telegram messages (DESIGN.md 3.3).

        Layout: status and session title, the user's last prompt, the body (Markdown
        rendered), then options; every message ends with the session tag. A long body
        is split into several messages; buttons go on the last one.
        Only while away: at the desk nothing goes to Telegram (user's call, 2026-10-12).
        """
        if self.paused or not self._away():
            return
        tag = f"<code>#{n.session_id[:TAG_CHARS]}</code>"
        header = self._header(n)
        rows, footer, card = [], "", None
        if n.kind == "question" and n.questions:
            card = _Questions(n.session_id, n.questions)
            rows = self._question_rows(card)
        elif n.kind == "approval":
            rows = [[self._button("允许一次", n.session_id, "approve", (True, n.command)),
                     self._button("拒绝", n.session_id, "approve", (False, n.command))]]
        elif n.options:
            footer = f"<b>{_esc(n.options_label)}</b>\n" + "\n".join(f"{i}. {_esc(o)}" for i, o in enumerate(n.options, 1))
            rows = [[self._button(f"{i}. {o}", n.session_id, "prompt", o)] for i, o in enumerate(n.options, 1)]
        if n.kind == "approval" and n.tool:
            bodies = [f"<b>{_esc(n.tool)}</b>\n<pre>{_esc(n.detail)}</pre>"]
        else:
            room = LIMIT - visible_len(header) - visible_len(footer) - 40
            bodies = [md_to_html(chunk) for chunk in split_markdown(n.text, room)] if n.text.strip() else [""]
        thread = self._topic_for(n)
        message_id, base = None, ""
        for i, body in enumerate(bodies):
            last = i == len(bodies) - 1
            top = header if i == 0 else f"<i>（续 {i + 1}/{len(bodies)}）</i>"
            base = "\n\n".join(part for part in (top, body, footer if last else "") if part)
            try:
                message_id = self._send(f"{base}\n\n{tag}", rows if last else None, thread)
            except TelegramError as e:
                if thread is None or "thread" not in str(e).lower():
                    raise
                # The user deleted the session's topic: open a new one and carry on there.
                self._topics.forget(n.session_id)
                thread = self._topic_for(n)
                message_id = self._send(f"{base}\n\n{tag}", rows if last else None, thread)
            self._remember(self._session_of_message, message_id, n.session_id)
        if self._topics is not None:
            self._topics.touch(n.session_id)
        if card:
            card.message_id = message_id
        if rows or n.kind == "completed":  # a completion may still get its 💡 button
            self._live.setdefault(n.session_id, {})[message_id] = (n.kind, n.key)
        self._last_session = n.session_id
        if n.kind == "completed":
            self._suggest_while_away(n.session_id, message_id, base, tag, rows)

    def _away(self) -> bool:
        try:
            return self._presence.is_away()
        except Exception as e:
            log.warning("presence check failed: %s", type(e).__name__)
            return True  # can't tell: better a notice too many than one lost

    @staticmethod
    def _header(n: Notice) -> str:
        emoji, word = HEADERS.get(n.kind, ("•", n.kind))
        title = n.title or n.session_label
        lines = [f"{emoji} <b>{_esc(word)}</b>｜<b>{_esc(title)}</b>"]
        if n.prompt:
            lines.append(f"<i>↳ {_esc(n.prompt)}</i>")
        return "\n".join(lines)

    def _topic_for(self, n: Notice) -> int | None:
        """The session's topic, created on its first notice and renamed when its title changes."""
        if self._topics is None:
            return None
        title = (n.title or n.session_label)[:110]
        name = f"{title} #{n.session_id[:TAG_CHARS]}"
        thread = self._topics.thread_of(n.session_id)
        if thread is None:
            thread = self._api.call("createForumTopic", chat_id=self._chat_id, name=name)["message_thread_id"]
            self._topics.set(n.session_id, thread)
        elif self._topic_names.get(n.session_id) != name:
            try:
                self._api.call("editForumTopic", chat_id=self._chat_id, message_thread_id=thread, name=name)
            except TelegramError:
                pass  # e.g. TOPIC_NOT_MODIFIED after a restart; the name is right either way
        self._topic_names[n.session_id] = name
        return thread

    def _send(self, text: str, rows: list | None, thread: int | None = None) -> int:
        params = {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML",
                  "link_preview_options": {"is_disabled": True}}
        if thread is not None:
            params["message_thread_id"] = thread
        if rows:
            params["reply_markup"] = {"inline_keyboard": rows}
        try:
            return self._api.call("sendMessage", **params)["message_id"]
        except TelegramError as e:
            if "parse" not in str(e).lower():
                raise
            # Formatting Telegram can't parse must not cost the message: resend it plain.
            params.pop("parse_mode")
            params["text"] = _plain(text)
            return self._api.call("sendMessage", **params)["message_id"]

    def _edit(self, message_id: int, text: str, rows: list) -> None:
        params = {"chat_id": self._chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
                  "link_preview_options": {"is_disabled": True}, "reply_markup": {"inline_keyboard": rows}}
        try:
            self._api.call("editMessageText", **params)
        except TelegramError as e:
            if "parse" not in str(e).lower():
                raise
            params.pop("parse_mode")
            params["text"] = _plain(text)
            self._api.call("editMessageText", **params)

    def _suggest_while_away(self, session_id: str, message_id: int, base: str, tag: str, rows: list,
                            again: bool = True) -> None:
        """Read the suggestion, and on a miss once more a minute later; only while away,
        since reading switches the visible session (user's call, 2026-10-12)."""
        if not self._away():
            return
        if not self._add_suggestion(session_id, message_id, base, tag, rows) and again and self._later:
            self._later(SUGGEST_AGAIN_SECONDS, lambda: self._suggest_while_away(
                session_id, message_id, base, tag, rows, again=False))

    def _add_suggestion(self, session_id: str, message_id: int, base: str, tag: str, rows: list) -> bool:
        """Read the session's official suggestion and add it to the notice (DESIGN.md 3.8).

        Runs after the notice went out, on the same worker as write-back, so screen work
        is never concurrent.
        """
        suggestion = self._wb.read_suggestion(session_id)
        if suggestion and message_id not in self._live.get(session_id, {}):
            return True  # the session moved on while reading: its buttons are gone, keep it so
        if suggestion:
            self._show_suggestion(session_id, message_id, (base, tag), rows, suggestion)
        return bool(suggestion)

    def _show_suggestion(self, session_id: str, message_id: int, base: tuple[str, str], rows: list,
                         suggestion: str) -> None:
        body, tag = base
        text = f"{body}\n\n💡 <b>官方建议</b>：{_esc(suggestion)}\n\n{tag}"
        # The button carries the text shown, so a tap sends exactly that or nothing.
        button = self._button("💡 官方建议", session_id, "suggest", (message_id, base, rows, suggestion))
        self._edit(message_id, text, rows + [[button]])
        self._live.setdefault(session_id, {})[message_id] = ("completed", "")

    # Hook events that make a session's earlier buttons stale: a new prompt (typed anywhere)
    # supersedes a completion's options and 💡; a new approval or question means the earlier
    # one was settled; a turn's end settles everything before it (user's call, 2026-10-13).
    # A tool call that ran or failed settles its own approval or question, answered on the
    # computer or anywhere else; matched by tool_key, so a parallel call doesn't clear it.
    # A Deny in Desktop fires no hook: its result shows in the transcript, checked on the
    # session's next prompt and once a minute (sweep_answered).
    STALE_KINDS = {"UserPromptSubmit": {"completed"}, "PermissionRequest": {"approval", "question"},
                   "Stop": None, "StopFailure": None, "SessionEnd": None,
                   "PostToolUse": "tool", "PostToolUseFailure": "tool"}

    def session_event(self, event: str, session_id: str, payload: dict | None = None) -> None:
        """Take buttons off this session's notices that the event made stale. Runs on the
        side channel's worker before the event's own notice, if any, is sent."""
        if event == "SessionEnd":
            self._transcripts.pop(session_id, None)
        elif payload and payload.get("transcript_path"):
            self._transcripts[session_id] = str(payload["transcript_path"])
        if event not in self.STALE_KINDS or session_id not in self._live:
            return
        kinds = self.STALE_KINDS[event]
        live = self._live[session_id]
        if kinds == "tool":
            key = tool_key(payload or {})
            stale = [m for m, (k, mkey) in live.items() if k in ("approval", "question") and mkey == key]
        else:
            stale = [m for m, (k, _) in live.items() if kinds is None or k in kinds]
        if event == "UserPromptSubmit":
            # A new prompt can come mid-turn while a request still waits, so an approval or
            # question goes only once the transcript shows its call answered: a Deny in
            # Desktop interrupts the turn and fires no hook at all.
            stale += self._answered(session_id)
        self._take_off(session_id, stale, event)

    def sweep_answered(self) -> None:
        """Every minute: take off approvals and questions answered without a hook (a Deny in
        Desktop), so they don't wait for the session's next prompt."""
        for session_id in list(self._live):
            self._take_off(session_id, self._answered(session_id), "transcript")

    def _answered(self, session_id: str) -> list[int]:
        path = self._transcripts.get(session_id)
        live = self._live.get(session_id, {})
        return [m for m, (k, key) in live.items()
                if path and k in ("approval", "question") and key and answered(path, key)]

    def _take_off(self, session_id: str, stale: list[int], why: str) -> None:
        live = self._live.get(session_id)
        if live is None:
            return
        if stale:  # which event cleared what, to tell a tool's own end from the turn's (no content)
            log.info("cleared %d notice buttons on %s (%s)", len(stale), why,
                     ",".join(live[m][0] for m in stale))
        for message_id in stale:
            del live[message_id]
            self._clear_buttons(message_id)
        if not live:
            del self._live[session_id]

    def _clear_buttons(self, message_id: int) -> None:
        try:
            self._api.call("editMessageReplyMarkup", chat_id=self._chat_id, message_id=message_id,
                           reply_markup={"inline_keyboard": []})
        except TelegramError as e:
            log.info("buttons not cleared: %s", e)  # e.g. already gone, or deleted with its topic

    def _question_rows(self, card: _Questions) -> list:
        """Buttons for the question the card is on (DESIGN.md 3.9); none once all are answered.

        Desktop asks one question at a time, so only that question gets buttons. Single-select:
        one button per option. Multi-select: a tick per option, toggled here only, and a submit.
        """
        for token in card.tokens:
            self._actions.pop(token, None)
        if card.current >= len(card.questions):
            card.tokens = []
            return []
        q = card.questions[card.current]
        prefix = f"问{card.current + 1}·" if len(card.questions) > 1 else ""
        if q["multi"]:
            rows = [[self._button(f"{'☑' if i in card.picked else '☐'} {prefix}{o['label']}", card.session_id,
                                  "pick", (card, i))] for i, o in enumerate(q["options"])]
            rows.append([self._button(f"提交{prefix.rstrip('·')}所选", card.session_id, "answer", (card, None))])
        else:
            rows = [[self._button(f"{prefix}{o['label']}", card.session_id, "answer", (card, i))]
                    for i, o in enumerate(q["options"])]
        card.tokens = [b["callback_data"][2:] for row in rows for b in row]
        return rows

    def _on_question(self, kind: str, card: _Questions, option: int | None) -> None:
        if kind == "pick":
            card.picked ^= {option}
        else:
            picked = sorted(card.picked) if option is None else [option]
            if not picked:
                self.say("未回答：先勾选至少一项，再点提交")
            else:
                status, answered = self._wb.answer(card.session_id, card.questions[card.current],
                                                   card.current + 1, len(card.questions), picked)
                if answered:
                    card.current += 1
                    card.picked = set()
                    left = len(card.questions) - card.current
                    if left:
                        status += f"（还有 {left} 个问题）"
                self.say(status)
        self._api.call("editMessageReplyMarkup", chat_id=self._chat_id, message_id=card.message_id,
                       reply_markup={"inline_keyboard": self._question_rows(card)})

    def _button(self, label: str, session_id: str, kind: str, payload) -> dict:
        token = str(next(self._ids))
        self._remember(self._actions, token, (session_id, kind, payload))
        return {"text": label, "callback_data": f"x:{token}"}

    @staticmethod
    def _remember(d: OrderedDict, key, value) -> None:
        d[key] = value
        while len(d) > MEMORY:
            d.popitem(last=False)

    def say(self, text: str, silent: bool = False) -> None:
        """A short answer, in the topic of whatever the user just did (or the main chat).
        It carries the keyboard for the current away state."""
        away = self._away()
        params = {"chat_id": self._chat_id, "text": text, "reply_markup": NO_BUTTON if away else AWAY_BUTTON}
        if self._reply_thread is not None:
            params["message_thread_id"] = self._reply_thread
        if silent:
            params["disable_notification"] = True
        self._api.call("sendMessage", **params)
        self._shown_away = away

    def greet(self, text: str) -> None:
        """The startup message: also fills the bot's "/" menu."""
        try:
            self._api.call("setMyCommands", commands=MENU)
        except TelegramError as e:
            log.warning("setMyCommands failed: %s", e)
        self.say(text)

    def check_presence(self) -> None:
        """Run periodically: when the away state changed since the last answer, say so
        silently, which also shows or hides the /away button. In threaded mode Telegram
        keeps a keyboard per topic, so this goes to the topic the user last acted in."""
        if self._shown_away is None or self._away() == self._shown_away:
            return
        text = "已离开电脑，通知转到这里" if not self._shown_away else "已回到电脑前，不再转发通知"
        log.info("presence message: %s", "away" if not self._shown_away else "at desk")
        self._reply_thread = self._last_thread
        try:
            self.say(text, silent=True)
        except TelegramError as e:
            if self._reply_thread is None or "thread" not in str(e).lower():
                raise
            self._last_thread = self._reply_thread = None  # that topic is gone: the main chat then
            self.say(text, silent=True)
        finally:
            self._reply_thread = None

    def sweep_topics(self) -> None:
        """Delete topics with no activity for 3 days (user's call, 2026-10-12). A session
        that becomes active again gets a new topic with its next notice."""
        if self._topics is None:
            return
        deleted = 0
        for session_id, thread in self._topics.stale():
            try:
                self._api.call("deleteForumTopic", chat_id=self._chat_id, message_thread_id=thread)
            except TelegramError as e:
                if not any(w in str(e).lower() for w in ("not found", "topic_id_invalid")):
                    log.warning("topic not deleted: %s", e)
                    continue  # try again on the next sweep
            self._topics.forget(session_id)
            self._topic_names.pop(session_id, None)
            if self._last_thread == thread:
                self._last_thread = None
            deleted += 1
        if deleted:
            log.info("deleted %d idle topics", deleted)

    # --- inbound ---

    def handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            q = update["callback_query"]
            message = q.get("message", {})
            if message.get("chat", {}).get("id") == self._chat_id:
                self._reply_thread = message.get("message_thread_id")
                self._user_acted()
                try:
                    self._on_button(q)
                finally:
                    self._reply_thread = None
        elif "message" in update:
            m = update["message"]
            if m.get("chat", {}).get("id") == self._chat_id and "forum_topic_created" in m:
                self._on_topic_created(m)
            elif m.get("chat", {}).get("id") == self._chat_id and m.get("text"):
                self._reply_thread = m.get("message_thread_id")
                self._user_acted()
                try:
                    self._on_text(m)
                finally:
                    self._reply_thread = None

    def _user_acted(self) -> None:
        self._last_thread = self._reply_thread
        if self._topics is not None and self._reply_thread is not None:
            session_id = self._topics.session_of(self._reply_thread)
            if session_id:
                self._topics.touch(session_id)

    def _on_button(self, q: dict) -> None:
        data = q.get("data", "")
        action = self._actions.pop(data[2:], None) if data.startswith("x:") else None
        if not action:
            self._answer(q, "按钮已失效（已用过，或 ccmonitor 重启过）。可以直接回复这条通知")
            if "message_id" in q.get("message", {}):
                self._clear_buttons(q["message"]["message_id"])  # so it can't be tapped again
            return
        self._answer(q)
        session_id, kind, payload = action
        if kind not in ("pick", "answer"):
            self._live.get(session_id, {}).pop(q["message"]["message_id"], None)
        # Every button is accepted whatever the away state (user's call, 2026-10-11), and a
        # tap declares away so the turn it starts still gets options. Free text does the
        # same in _write.
        self._presence.declare_away()
        if kind in ("pick", "answer"):
            self._on_question(kind, *payload)  # redraws its own buttons
            return
        self._api.call("editMessageReplyMarkup", chat_id=self._chat_id, message_id=q["message"]["message_id"],
                       reply_markup={"inline_keyboard": []})
        if kind == "approve":
            allow, expect = payload
            self.say(self._wb.approve(session_id, allow, expect))
        elif kind == "suggest":
            message_id, base, rows, shown = payload
            status, changed = self._wb.send_suggestion(session_id, shown)
            if changed:
                self._show_suggestion(session_id, message_id, base, rows, changed)
            self.say(status)
        else:
            self.say(self._wb.send_prompt(session_id, payload))

    def _answer(self, q: dict, text: str | None = None) -> None:
        """Acknowledge a button press. Telegram refuses presses that waited too long behind
        other work on this worker ("query is too old"); the press still takes effect."""
        try:
            if text:
                self._api.call("answerCallbackQuery", callback_query_id=q["id"], text=text)
            else:
                self._api.call("answerCallbackQuery", callback_query_id=q["id"])
        except TelegramError as e:
            log.warning("button answer failed: %s", e)

    def _on_text(self, m: dict) -> None:
        text = m["text"].strip()
        if text.startswith("/"):
            stray = self._stray_topic(m)
            if stray is not None:
                self._reply_thread = self._last_thread = None  # answer in the main chat instead
            parts = text.split()
            self._command(parts[0].split("@")[0], parts[1:])
            if stray is not None:
                self._delete_topic(stray)
            return
        replied = m.get("reply_to_message")
        topic_root = replied if replied and "forum_topic_created" in replied else None
        if topic_root:
            replied = None  # inside a topic, Telegram points plain messages at the topic's first message
        thread = m.get("message_thread_id") if self._topics is not None else None
        if replied:
            session_id = self._session_of_message.get(replied.get("message_id")) or self._session_from_tag(replied)
            if not session_id:
                self.say("找不到这条消息对应的会话：请回复带 # 标记的会话通知")
                return
        elif thread is not None:
            session_id = self._topics.session_of(thread)
            if not session_id and topic_root:
                # Pairing lost (e.g. topics file deleted): the topic's name carries the session tag.
                found = TAG.search(topic_root["forum_topic_created"].get("name", ""))
                session_id = self._session_by_tag(found.group(1)) if found else None
                if session_id:
                    self._topics.set(session_id, thread)
            if not session_id:
                self.say("这个话题没有对应的会话：请在会话自己的话题里发，或回复带 # 标记的会话通知")
                return
        else:
            session_id = self._last_session
            if not session_id:
                self.say("没有可发送的会话：回复某条会话通知，或等收到通知后再发")
                return
        self._write(lambda: self._wb.send_prompt(session_id, text))

    def _stray_topic(self, m: dict) -> int | None:
        """A topic that holds no session, e.g. the one Telegram opens when a command is sent
        from "All Messages" (named after the command; user's call, 2026-10-13, to remove it).
        A topic whose name carries a session tag is a session's, pairing lost or not."""
        thread = m.get("message_thread_id")
        if self._topics is None or thread is None or self._topics.session_of(thread):
            return None
        root = m.get("reply_to_message") or {}
        if TAG.search((root.get("forum_topic_created") or {}).get("name", "")):
            return None
        return thread

    def _on_topic_created(self, m: dict) -> None:
        """A topic opened under a command's name ("/away"), holding no session: delete it now.

        Telegram opens one when a command is sent from "All Messages", and the command does
        not always land in it (2026-10-13 13:52 and 15:26: two empty "/away" topics), so
        waiting for a command inside it (_stray_topic) can miss it."""
        thread = m.get("message_thread_id")
        name = (m.get("forum_topic_created") or {}).get("name", "")
        if (self._topics is None or thread is None or not name.startswith("/")
                or TAG.search(name) or self._topics.session_of(thread)):
            return
        self._delete_topic(thread)

    def _delete_topic(self, thread: int) -> None:
        if thread in self._gone_topics:
            return
        try:
            self._api.call("deleteForumTopic", chat_id=self._chat_id, message_thread_id=thread)
            self._gone_topics.add(thread)
            log.info("deleted a topic that held no session")
        except TelegramError as e:
            log.warning("stray topic not deleted: %s", e)

    def _session_from_tag(self, message: dict) -> str | None:
        # The tag is on the last line; notices from before 2026-10-11 15:30 had it on the first.
        lines = (message.get("text") or "").strip().split("\n")
        found = TAG.search(lines[-1]) or TAG.search(lines[0])
        return self._session_by_tag(found.group(1)) if found else None

    def _write(self, do: Callable[[], str]) -> None:
        # A phone reply declares away whatever the computer is doing (user's call, 2026-10-14).
        self._presence.declare_away()
        self.say(do())

    def _command(self, cmd: str, args: list[str] | None = None) -> None:
        if cmd == "/away":
            self._presence.declare_away()
            self.say("已声明离开，通知会转到这里；碰一下键盘或鼠标即视为回来")
        elif cmd == "/back":
            self._presence.declare_back()
            self.say("已回到电脑前，不再转发通知、不读官方建议")
        elif cmd == "/pause":
            self.paused = True
            self.say("通知已暂停")
        elif cmd == "/resume":
            self.paused = False
            self.say("通知已恢复")
        elif cmd == "/status":
            lines = ["离开状态：" + ("是" if self._presence.is_away() else "否（电脑前有人）"),
                     "通知：" + ("已暂停" if self.paused else "开启")]
            extra = self.status_extra()
            if extra:
                lines.append(extra)
            self.say("\n".join(lines))
        elif cmd == "/mode" and self.on_mode:
            arg = (args or [None])[0]
            if arg is not None and arg.lower() not in ("a", "b"):
                self.say("用法：/mode 查看，/mode a 不借助 agent，/mode b 借助转发会话")
                return
            self.say(self.on_mode(arg.lower() if arg else None))
        else:
            self.say(COMMANDS)
