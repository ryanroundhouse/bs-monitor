"""Tests for the pure (no-network) helpers in telegram.py.

These cover the bits that decide what gets posted and who is allowed to act:
the callback round-trip, the authorization check, and message formatting.
The HTTP methods are not exercised here.
"""

import unittest
from unittest.mock import patch

from moodful_responder import telegram
from moodful_responder.telegram import TelegramBot, TelegramError


class CallbackData(unittest.TestCase):
    def test_round_trip(self):
        for action in ("approve", "edit", "skip"):
            data = telegram.encode_action(action, 42)
            self.assertEqual(telegram.decode_action(data), (action, 42))

    def test_fits_telegram_64_byte_limit(self):
        data = telegram.encode_action("approve", 9_999_999)
        self.assertLessEqual(len(data.encode("utf-8")), 64)

    def test_malformed_payload_yields_none_id(self):
        self.assertEqual(telegram.decode_action("approve:notanint"), ("approve", None))
        self.assertEqual(telegram.decode_action("garbage"), ("garbage", None))

    def test_keyboard_has_all_three_actions(self):
        kb = telegram.draft_keyboard(7)
        datas = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertEqual(set(datas), {"approve:7", "edit:7", "skip:7"})


class Authorization(unittest.TestCase):
    def test_only_configured_chat_is_authorized(self):
        bot = TelegramBot("token", "12345")
        self.assertTrue(bot.authorized(12345))     # int form (Telegram sends int)
        self.assertTrue(bot.authorized("12345"))   # str form
        self.assertFalse(bot.authorized(99999))
        self.assertFalse(bot.authorized(None))

    def test_empty_chat_id_authorizes_nobody(self):
        bot = TelegramBot("token", "")
        self.assertFalse(bot.authorized(12345))


class Formatting(unittest.TestCase):
    def test_draft_includes_ask_link_and_reply(self):
        msg = telegram.format_draft(
            "anyone know a good mood tracker?",
            "i make moodful (moodful.ca) …",
            "https://bsky.app/profile/x/post/y",
            0.8,
        )
        self.assertIn("anyone know a good mood tracker?", msg)
        self.assertIn("https://bsky.app/profile/x/post/y", msg)
        self.assertIn("moodful.ca", msg)
        self.assertIn("0.8", str(msg))

    def test_long_ask_is_truncated(self):
        long_ask = "x" * 5000
        msg = telegram.format_draft(long_ask, "draft", "url", 0.7)
        self.assertIn("…", msg)
        self.assertLess(len(msg), 5000)


class NetworkErrors(unittest.TestCase):
    def test_socket_timeout_is_wrapped_as_telegram_error(self):
        bot = TelegramBot("token", "12345")

        with patch("moodful_responder.telegram.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaisesRegex(TelegramError, "sendMessage: timed out"):
                bot.send_message("hello")


if __name__ == "__main__":
    unittest.main()
