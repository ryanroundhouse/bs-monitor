"""Minimal Telegram Bot API client — push a draft, listen for your decision.

stdlib only (urllib), matching `bsky.py`, so the relay adds no dependency
beyond `websockets`. This is the remote stand-in for the `review` console: each
queued draft is pushed to your phone with Approve / Edit / Skip buttons, and the
relay long-polls for your tap (or your replacement text) and hands it back to
the same `BskyClient` path that `review` uses.

Two safety properties live here:
  * `authorized()` — only updates from the configured chat id are ever acted on
    (a bot can be messaged by anyone who finds it).
  * the bot never posts to Bluesky itself; it only relays your decision.
"""

import json
from typing import Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_ROOT = "https://api.telegram.org"

# Telegram limits: callback_data ≤ 64 bytes, message text ≤ 4096 chars.
_ASK_PREVIEW = 600


class TelegramError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Pure helpers (no network) — unit-tested in tests/test_telegram.py
# --------------------------------------------------------------------------- #
def encode_action(action: str, queue_id: int) -> str:
    """Pack a button action + queue id into callback_data (e.g. 'approve:12')."""
    return f"{action}:{queue_id}"


def decode_action(data: str):
    """Inverse of encode_action. Returns (action, queue_id) — queue_id is None
    if the payload is malformed."""
    action, _, raw = data.partition(":")
    try:
        return action, int(raw)
    except ValueError:
        return action, None


def draft_keyboard(queue_id: int) -> dict:
    """Inline keyboard carrying the queue id in each button's callback_data."""
    return {
        "inline_keyboard": [[
            {"text": "✓ Approve", "callback_data": encode_action("approve", queue_id)},
            {"text": "✎ Edit", "callback_data": encode_action("edit", queue_id)},
            {"text": "⊘ Skip", "callback_data": encode_action("skip", queue_id)},
        ]]
    }


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def format_draft(ask_text: str, draft_text: str, post_url: str, confidence) -> str:
    """The notification body: the original ask, a link to it, and the draft."""
    return (
        f"🆕 Someone's asking for a tool (confidence {confidence}):\n\n"
        f"{_truncate(ask_text, _ASK_PREVIEW)}\n"
        f"{post_url}\n\n"
        f"✍️ Drafted reply:\n"
        f"{draft_text}"
    )


# --------------------------------------------------------------------------- #
# Bot client
# --------------------------------------------------------------------------- #
class TelegramBot:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = str(chat_id) if chat_id else ""
        self._base = f"{API_ROOT}/bot{token}"

    def authorized(self, chat_id) -> bool:
        """True only for the one chat we're configured to talk to."""
        return bool(self.chat_id) and str(chat_id) == self.chat_id

    def _call(self, method: str, params: dict, timeout: int = 30) -> dict:
        data = json.dumps(params).encode("utf-8")
        req = Request(
            f"{self._base}/{method}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise TelegramError(f"{method}: {exc.code} {exc.reason}: {detail}") from exc
        except URLError as exc:
            raise TelegramError(f"{method}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise TelegramError(f"{method}: {exc}") from exc
        if not payload.get("ok"):
            raise TelegramError(f"{method}: {payload.get('description', payload)}")
        return payload["result"]

    # --- sending ----------------------------------------------------------
    def send_draft(self, queue_id: int, ask_text: str, draft_text: str,
                   post_url: str, confidence) -> int:
        result = self._call("sendMessage", {
            "chat_id": self.chat_id,
            "text": format_draft(ask_text, draft_text, post_url, confidence),
            "reply_markup": draft_keyboard(queue_id),
            "disable_web_page_preview": True,
        })
        return result["message_id"]

    def send_message(self, text: str, reply_markup: Optional[dict] = None,
                     force_reply: bool = False) -> int:
        params = {"chat_id": self.chat_id, "text": text,
                  "disable_web_page_preview": True}
        if reply_markup:
            params["reply_markup"] = reply_markup
        elif force_reply:
            params["reply_markup"] = {"force_reply": True}
        return self._call("sendMessage", params)["message_id"]

    def edit_message(self, message_id: int, text: str) -> None:
        """Best-effort: update a sent message (e.g. to mark it posted/skipped).
        Silently ignored if the message is too old or unchanged."""
        try:
            self._call("editMessageText", {
                "chat_id": self.chat_id, "message_id": message_id,
                "text": text, "disable_web_page_preview": True,
            })
        except TelegramError:
            pass

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        """Clear the button's loading spinner; best-effort."""
        try:
            self._call("answerCallbackQuery",
                       {"callback_query_id": callback_id, "text": text})
        except TelegramError:
            pass

    # --- receiving --------------------------------------------------------
    def get_updates(self, offset: Optional[int], timeout: int = 20) -> List[dict]:
        """Long-poll for new updates. Blocks up to `timeout` seconds."""
        params = {"timeout": timeout,
                  "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        # HTTP timeout must outlast the long-poll window.
        return self._call("getUpdates", params, timeout=timeout + 10)

    def discover_chats(self) -> List[Dict]:
        """Distinct chats that have messaged the bot — used to find your chat id
        during setup (when TELEGRAM_CHAT_ID isn't set yet)."""
        chats: Dict = {}
        for u in self.get_updates(None, timeout=0):
            msg = u.get("message") or u.get("edited_message") or {}
            chat = msg.get("chat")
            if chat:
                chats[chat["id"]] = chat
        return list(chats.values())
