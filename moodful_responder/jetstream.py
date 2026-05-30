"""Stream new top-level Bluesky posts from Jetstream.

A thin, reusable async generator over the same public Jetstream firehose that
`bs_monitor.py` uses, filtered server-side to `app.bsky.feed.post`. It yields
normalized dicts for brand-new, *top-level* posts (replies are skipped — we
only ever want to answer someone's own question, not barge into a thread).
"""

import asyncio
import json
from typing import AsyncIterator, Callable, Dict, List, Optional
from urllib.parse import urlencode

import websockets

POST_COLLECTION = "app.bsky.feed.post"

JETSTREAM_HOSTS = {
    "us-east-1": "jetstream1.us-east.bsky.network",
    "us-east-2": "jetstream2.us-east.bsky.network",
    "us-west-1": "jetstream1.us-west.bsky.network",
    "us-west-2": "jetstream2.us-west.bsky.network",
}

CURSOR_ROLLBACK_US = 5_000_000  # roll back 5s on reconnect to avoid gaps


def extract_post(event: dict) -> Optional[Dict]:
    """Return a normalized new top-level post, or None if `event` isn't one.

    We keep only `create` commits in the post collection that are NOT replies,
    and surface the fields we need to both classify and (later) reply to them —
    including `cid`, which is required to build the reply's strong reference.
    """
    if event.get("kind") != "commit":
        return None
    commit = event.get("commit") or {}
    if commit.get("operation") != "create":
        return None
    if commit.get("collection") != POST_COLLECTION:
        return None

    record = commit.get("record") or {}
    text = record.get("text") or ""
    if not text:
        return None
    if record.get("reply"):
        return None  # only original asks, never thread interjections

    did = event.get("did")
    rkey = commit.get("rkey")
    return {
        "did": did,
        "rkey": rkey,
        "cid": commit.get("cid"),
        "text": text,
        "langs": [s.lower() for s in (record.get("langs") or [])],
        "created_at": record.get("createdAt"),
        "uri": f"at://{did}/{POST_COLLECTION}/{rkey}",
        "url": f"https://bsky.app/profile/{did}/post/{rkey}",
        "time_us": event.get("time_us"),
    }


async def post_events(
    endpoint: str,
    stop: asyncio.Event,
    cursor: Optional[int] = None,
    on_status: Optional[Callable[[str], None]] = None,
) -> AsyncIterator[dict]:
    """Yield raw Jetstream events for new posts, reconnecting as needed."""
    host = JETSTREAM_HOSTS[endpoint]
    backoff = 1
    while not stop.is_set():
        params: List = [("wantedCollections", POST_COLLECTION)]
        if cursor is not None:
            params.append(("cursor", str(cursor)))
        url = f"wss://{host}/subscribe?{urlencode(params)}"
        try:
            async with websockets.connect(
                url, max_size=None, ping_interval=20, ping_timeout=20
            ) as ws:
                backoff = 1
                while not stop.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    ts = event.get("time_us")
                    if isinstance(ts, int):
                        cursor = ts
                    yield event
        except asyncio.TimeoutError:
            continue
        except (websockets.WebSocketException, OSError) as exc:
            if stop.is_set():
                break
            if cursor is not None:
                cursor = max(0, cursor - CURSOR_ROLLBACK_US)
            if on_status:
                on_status(f"disconnected ({exc!s}); reconnecting in {backoff}s…")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
