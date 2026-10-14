import json
import os
import tempfile
import unittest
from pathlib import Path

from ccmonitor.config import load_config
from ccmonitor.telegram import BotApi, TelegramError, UpdatePoller

TOKEN = "123456:SECRET-token"


class BotApiTest(unittest.TestCase):
    def test_posts_json_to_method_url_and_returns_result(self):
        seen = {}

        def transport(url, body, timeout):
            seen.update(url=url, body=json.loads(body), timeout=timeout)
            return json.dumps({"ok": True, "result": {"message_id": 7}}).encode()

        r = BotApi(TOKEN, transport).call("sendMessage", chat_id=1, text="你好")
        self.assertEqual(r, {"message_id": 7})
        self.assertEqual(seen["url"], f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        self.assertEqual(seen["body"], {"chat_id": 1, "text": "你好"})

    def test_long_poll_timeout_extends_http_timeout(self):
        seen = {}

        def transport(url, body, timeout):
            seen["timeout"] = timeout
            return b'{"ok": true, "result": []}'

        BotApi(TOKEN, transport).call("getUpdates", timeout=25)
        self.assertGreater(seen["timeout"], 25)

    def test_api_error_raises_without_leaking_token(self):
        def transport(url, body, timeout):
            return b'{"ok": false, "error_code": 400, "description": "Bad Request: chat not found"}'

        with self.assertRaises(TelegramError) as cm:
            BotApi(TOKEN, transport).call("sendMessage", chat_id=1, text="x")
        self.assertIn("chat not found", str(cm.exception))
        self.assertNotIn("SECRET", str(cm.exception))

    def test_transport_failure_raises_without_leaking_token(self):
        def transport(url, body, timeout):
            raise OSError(f"cannot reach {url}")

        with self.assertRaises(TelegramError) as cm:
            BotApi(TOKEN, transport).call("getMe")
        self.assertNotIn("SECRET", str(cm.exception))


class ScriptedApi:
    def __init__(self, batches):
        self.batches, self.offsets = list(batches), []

    def call(self, method, **params):
        self.offsets.append(params.get("offset"))
        return self.batches.pop(0) if self.batches else []


class UpdatePollerTest(unittest.TestCase):
    def test_skip_backlog_then_advance_offset(self):
        api = ScriptedApi([[{"update_id": 5}, {"update_id": 6}], [{"update_id": 7}], []])
        seen = []
        poller = UpdatePoller(api, seen.append)
        poller.skip_backlog()
        poller.poll_once(timeout=0)
        poller.poll_once(timeout=0)
        self.assertEqual(seen, [{"update_id": 7}])
        self.assertEqual(api.offsets, [-1, 7, 8])

    def test_handler_failure_does_not_stop_later_updates(self):
        api = ScriptedApi([[{"update_id": 1}, {"update_id": 2}]])
        seen = []

        def handler(u):
            if u["update_id"] == 1:
                raise RuntimeError("boom")
            seen.append(u)

        UpdatePoller(api, handler).poll_once(timeout=0)
        self.assertEqual(seen, [{"update_id": 2}])


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "config.json"
        self.env = {}

    def tearDown(self):
        self.dir.cleanup()

    def test_reads_file(self):
        self.path.write_text(json.dumps({"telegram_token": TOKEN, "chat_id": 42}), encoding="utf-8")
        cfg = load_config(self.path, self.env)
        self.assertEqual((cfg.telegram_token, cfg.chat_id), (TOKEN, 42))

    def test_env_overrides_file(self):
        self.path.write_text(json.dumps({"telegram_token": "file", "chat_id": 1}), encoding="utf-8")
        cfg = load_config(self.path, {"CCMONITOR_TELEGRAM_TOKEN": TOKEN, "CCMONITOR_CHAT_ID": "42"})
        self.assertEqual((cfg.telegram_token, cfg.chat_id), (TOKEN, 42))

    def test_missing_config_returns_none(self):
        self.assertIsNone(load_config(self.path, self.env))

    def test_incomplete_config_returns_none(self):
        self.path.write_text(json.dumps({"telegram_token": TOKEN}), encoding="utf-8")
        self.assertIsNone(load_config(self.path, self.env))

    def test_repr_hides_token(self):
        self.path.write_text(json.dumps({"telegram_token": TOKEN, "chat_id": 42}), encoding="utf-8")
        self.assertNotIn("SECRET", repr(load_config(self.path, self.env)))

    def test_without_a_path_the_file_is_looked_up_at_each_load(self):
        # The Telegram tab saves to the storage directory while running; the restarted
        # daemon must read that file, not the one found when the module was imported.
        from unittest import mock
        from ccmonitor import paths
        nowhere = {"APPDATA": str(Path(self.dir.name) / "none"), "LOCALAPPDATA": str(Path(self.dir.name) / "none")}
        with mock.patch.object(paths, "DATA_DIR", Path(self.dir.name)), mock.patch.dict(os.environ, nowhere):
            self.assertIsNone(load_config(env=self.env))
            self.path.write_text(json.dumps({"telegram_token": TOKEN, "chat_id": 42}), encoding="utf-8")
            self.assertEqual(load_config(env=self.env).chat_id, 42)


class HttpErrorTest(unittest.TestCase):
    def test_telegram_error_description_survives_an_http_error(self):
        import io
        import urllib.error
        from ccmonitor import telegram

        body = b'{"ok":false,"error_code":400,"description":"Bad Request: query is too old"}'

        def urlopen(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(body))
        orig, telegram.urllib.request.urlopen = telegram.urllib.request.urlopen, urlopen
        try:
            with self.assertRaises(TelegramError) as cm:
                BotApi("123:secret").call("answerCallbackQuery", callback_query_id="1")
        finally:
            telegram.urllib.request.urlopen = orig
        self.assertIn("query is too old", str(cm.exception))
        self.assertNotIn("secret", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
