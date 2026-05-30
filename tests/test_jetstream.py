import asyncio
import json
import unittest
from unittest.mock import patch

import websockets

from moodful_responder import jetstream


class _RaisesOnEnter:
    async def __aenter__(self):
        raise websockets.InvalidMessage("bad upstream response")

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _OneMessageWebsocket:
    def __init__(self, message):
        self.message = message

    async def recv(self):
        return self.message


class _YieldsOnEnter:
    def __init__(self, ws):
        self.ws = ws

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, exc_type, exc, tb):
        return False


class JetstreamReconnects(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_handshake_response_reconnects_instead_of_crashing(self):
        stop = asyncio.Event()
        statuses = []
        calls = {"n": 0}
        event = {"time_us": 123, "kind": "commit"}

        def connect(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return _RaisesOnEnter()
            return _YieldsOnEnter(_OneMessageWebsocket(json.dumps(event)))

        async def no_sleep(_delay):
            return None

        with patch("moodful_responder.jetstream.websockets.connect", side_effect=connect), \
             patch("moodful_responder.jetstream.asyncio.sleep", side_effect=no_sleep):
            gen = jetstream.post_events("us-east-2", stop, on_status=statuses.append)
            self.assertEqual(await anext(gen), event)
            stop.set()
            await gen.aclose()

        self.assertEqual(calls["n"], 2)
        self.assertTrue(any("reconnecting" in msg for msg in statuses))


if __name__ == "__main__":
    unittest.main()
