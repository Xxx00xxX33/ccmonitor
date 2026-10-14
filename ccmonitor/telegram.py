"""Minimal Telegram Bot API client over HTTPS, standard library only."""
import json
import logging
import urllib.error
import urllib.request
from typing import Callable

Transport = Callable[[str, bytes, float], bytes]
HTTP_TIMEOUT = 15
ALLOWED_UPDATES = ["message", "callback_query"]

log = logging.getLogger("ccmonitor")


class TelegramError(Exception):
    """Raised for API or network failures. Never carries the token or URL."""


def urllib_transport(url: str, body: bytes, timeout: float) -> bytes:
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        # The Bot API answers errors with a JSON body ({"ok": false, "description": ...});
        # hand it on so the description reaches the log instead of a bare HTTPError.
        return e.read()


class BotApi:
    def __init__(self, token: str, transport: Transport = urllib_transport):
        self._url = f"https://api.telegram.org/bot{token}/"
        self._transport = transport

    def call(self, method: str, **params):
        timeout = HTTP_TIMEOUT + params.get("timeout", 0)
        body = json.dumps(params, ensure_ascii=False).encode("utf-8")
        try:
            raw = self._transport(self._url + method, body, timeout)
        except Exception as e:
            # Exceptions from urllib can embed the URL, which contains the token.
            raise TelegramError(f"{method}: {type(e).__name__}") from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise TelegramError(f"{method}: invalid response") from None
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('error_code')} {data.get('description', '')}")
        return data.get("result")


class UpdatePoller:
    """Long-polls getUpdates and hands each update to a handler."""

    def __init__(self, api, handler: Callable[[dict], None]):
        self._api = api
        self._handler = handler
        self._offset: int | None = None

    def skip_backlog(self) -> None:
        """Drop updates that arrived while the daemon was down; they refer to state we no longer have."""
        updates = self._api.call("getUpdates", offset=-1, timeout=0, allowed_updates=ALLOWED_UPDATES)
        if updates:
            self._offset = updates[-1]["update_id"] + 1

    def poll_once(self, timeout: int = 25) -> None:
        updates = self._api.call("getUpdates", offset=self._offset, timeout=timeout, allowed_updates=ALLOWED_UPDATES)
        for u in updates or []:
            self._offset = u["update_id"] + 1
            try:
                self._handler(u)
            except Exception as e:
                log.warning("update handler failed: %s", type(e).__name__)
