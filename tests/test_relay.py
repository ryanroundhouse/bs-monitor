import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from moodful_responder import cli
from moodful_responder.telegram import TelegramError


class _Store:
    def close(self):
        pass


class _Client:
    handle = "moodful-ryan.bsky.social"


class _Bot:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id

    def send_message(self, text):
        raise TelegramError("sendMessage: timed out")


async def _producer(args, store, bot, stop, status):
    status("producer ran")


async def _consumer(bot, client, store, stop, status):
    status("consumer ran")


class RelayStartup(unittest.IsolatedAsyncioTestCase):
    async def test_startup_telegram_timeout_does_not_exit_service(self):
        args = SimpleNamespace(
            db=":memory:",
            pds="https://bsky.social",
            endpoint="us-east-2",
            lang="en",
            min_confidence=0.6,
        )
        env = {
            "BSKY_IDENTIFIER": "user",
            "BSKY_APP_PASSWORD": "pass",
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_CHAT_ID": "12345",
        }

        with patch.dict(os.environ, env, clear=False), \
             patch("moodful_responder.cli.Store", return_value=_Store()), \
             patch("moodful_responder.cli.BskyClient", return_value=_Client()), \
             patch("moodful_responder.cli.TelegramBot", _Bot), \
             patch("moodful_responder.cli._relay_producer", side_effect=_producer), \
             patch("moodful_responder.cli._relay_consumer", side_effect=_consumer):
            await cli._relay(args)


if __name__ == "__main__":
    unittest.main()
