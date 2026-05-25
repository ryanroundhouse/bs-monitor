#!/usr/bin/env python3
"""bs-monitor — subscribe to new Bluesky posts and print the ones that match keywords.

How it works
------------
Bluesky is built on the AT Protocol, which publishes a real-time "firehose" of
every event on the network. Decoding the raw firehose
(`com.atproto.sync.subscribeRepos`) means dealing with CBOR/CAR-encoded blocks.
Jetstream is Bluesky's official, simpler alternative: the same stream converted
to plain JSON over a WebSocket, with server-side filtering by collection (so we
only receive posts) and by repo DID. No authentication is required.

We connect to:

    wss://<host>/subscribe?wantedCollections=app.bsky.feed.post

and receive one JSON message per event. A "create" commit in the
`app.bsky.feed.post` collection is a brand-new post; its text lives at
`commit.record.text`. We match that text against the keywords you provide and
print the hits.

Docs: https://docs.bsky.app/docs/advanced-guides/firehose
      https://github.com/bluesky-social/jetstream
"""

import argparse
import asyncio
import json
import signal
import sys
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence
from urllib.parse import urlencode

import websockets

# The four official public Jetstream instances (no auth required).
JETSTREAM_HOSTS = {
    "us-east-1": "jetstream1.us-east.bsky.network",
    "us-east-2": "jetstream2.us-east.bsky.network",
    "us-west-1": "jetstream1.us-west.bsky.network",
    "us-west-2": "jetstream2.us-west.bsky.network",
}

POST_COLLECTION = "app.bsky.feed.post"

# When reconnecting we resume from the last cursor, rolled back slightly so a
# brief disconnect doesn't drop events. A little overlap (possible duplicates)
# is preferable to a gap for a monitor.
CURSOR_ROLLBACK_US = 5_000_000  # 5 seconds, in microseconds


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
class KeywordMatcher:
    """Case-insensitive substring matcher over post text."""

    def __init__(self, keywords: Sequence[str], require_all: bool = False):
        self.keywords = [k.lower() for k in keywords if k.strip()]
        self.require_all = require_all

    def matches(self, text: str) -> List[str]:
        """Return the keywords found in `text` ([] = no match)."""
        if not self.keywords:
            return ["*"]  # no keywords configured -> everything matches
        haystack = text.lower()
        hits = [k for k in self.keywords if k in haystack]
        if self.require_all:
            return hits if len(hits) == len(self.keywords) else []
        return hits


# --------------------------------------------------------------------------- #
# Event handling / output
# --------------------------------------------------------------------------- #
def post_url(did: str, rkey: str) -> str:
    return f"https://bsky.app/profile/{did}/post/{rkey}"


def highlight(text: str, terms: Iterable[str]) -> str:
    """Wrap matched terms in ANSI bold so they stand out in the terminal."""
    out = text
    for term in terms:
        if term == "*":
            continue
        idx = out.lower().find(term.lower())
        if idx >= 0:
            seg = out[idx : idx + len(term)]
            out = out[:idx] + f"\033[1;33m{seg}\033[0m" + out[idx + len(term) :]
    return out


def handle_event(event: dict, matcher: KeywordMatcher, args) -> None:
    """Inspect one Jetstream event; print it if it's a matching new post."""
    if event.get("kind") != "commit":
        return

    commit = event.get("commit") or {}
    if commit.get("operation") != "create":
        return
    if commit.get("collection") != POST_COLLECTION:
        return

    record = commit.get("record") or {}
    text = record.get("text") or ""
    if not text:
        return

    # Optional language filter (record.langs is a list like ["en", "pt"]).
    if args.lang:
        langs = [s.lower() for s in (record.get("langs") or [])]
        if not any(s.startswith(args.lang.lower()) for s in langs):
            return

    hits = matcher.matches(text)
    if not hits:
        return

    if args.json:
        print(json.dumps(event, ensure_ascii=False), flush=True)
        return

    did = event.get("did", "?")
    rkey = commit.get("rkey", "")
    created = record.get("createdAt", "")
    reply = " ↩" if record.get("reply") else ""
    has_media = " 🖼" if record.get("embed") else ""
    matched = "" if hits == ["*"] else f"  \033[2m[{', '.join(hits)}]\033[0m"

    print("\033[2m" + "─" * 72 + "\033[0m")
    print(highlight(text.strip(), hits))
    print(
        f"\033[36m@ {did}\033[0m{reply}{has_media}  "
        f"\033[2m{created}\033[0m{matched}"
    )
    print(f"\033[2m{post_url(did, rkey)}\033[0m", flush=True)


# --------------------------------------------------------------------------- #
# Connection loop
# --------------------------------------------------------------------------- #
def build_url(host: str, cursor: Optional[int]) -> str:
    params = [("wantedCollections", POST_COLLECTION)]
    if cursor is not None:
        params.append(("cursor", str(cursor)))
    return f"wss://{host}/subscribe?{urlencode(params)}"


async def stream(args, stop: asyncio.Event) -> None:
    host = JETSTREAM_HOSTS[args.endpoint]
    matcher = KeywordMatcher(args.keywords, require_all=(args.match == "all"))
    cursor: Optional[int] = args.cursor
    backoff = 1

    scope = "all new posts" if not args.keywords else (
        f"posts matching {args.match.upper()} of {args.keywords}"
    )
    print(
        f"bs-monitor: streaming {scope} from {host}"
        + (f" (lang={args.lang})" if args.lang else ""),
        file=sys.stderr,
    )

    while not stop.is_set():
        url = build_url(host, cursor)
        try:
            async with websockets.connect(
                url, max_size=None, ping_interval=20, ping_timeout=20
            ) as ws:
                backoff = 1  # reset after a successful connect
                while not stop.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    # Track cursor so we can resume after a disconnect.
                    ts = event.get("time_us")
                    if isinstance(ts, int):
                        cursor = ts
                    handle_event(event, matcher, args)
        except asyncio.TimeoutError:
            # No message for 30s — likely idle/stale; reconnect.
            continue
        except (websockets.ConnectionClosed, OSError) as exc:
            if stop.is_set():
                break
            # Roll the cursor back a little so we don't miss events that
            # arrived during the gap, then back off before retrying.
            if cursor is not None:
                cursor = max(0, cursor - CURSOR_ROLLBACK_US)
            print(
                f"bs-monitor: disconnected ({exc!s}); reconnecting in {backoff}s…",
                file=sys.stderr,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    print("bs-monitor: stopped.", file=sys.stderr)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="bs-monitor",
        description="Subscribe to new Bluesky posts (via Jetstream) and print "
        "those matching keywords.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  bs_monitor.py python rust golang        # any of these words\n"
            "  bs_monitor.py --match all claude code    # both words present\n"
            "  bs_monitor.py --lang en bluesky          # English posts only\n"
            "  bs_monitor.py                            # firehose of all new posts\n"
            "  bs_monitor.py --json earthquake > hits.ndjson\n"
        ),
    )
    p.add_argument(
        "keywords",
        nargs="*",
        help="keywords to match (case-insensitive substrings). "
        "Omit to print every new post.",
    )
    p.add_argument(
        "--match",
        choices=("any", "all"),
        default="any",
        help="match ANY keyword (default) or require ALL of them.",
    )
    p.add_argument(
        "--lang",
        metavar="CODE",
        help="only show posts tagged with this language (e.g. en, pt, ja).",
    )
    p.add_argument(
        "--endpoint",
        choices=sorted(JETSTREAM_HOSTS),
        default="us-east-2",
        help="which public Jetstream instance to use (default: us-east-2).",
    )
    p.add_argument(
        "--cursor",
        type=int,
        default=None,
        metavar="TIME_US",
        help="resume from a Unix-microseconds cursor instead of live-tailing.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="emit raw matching events as NDJSON instead of pretty output.",
    )
    return p.parse_args(argv)


async def _main_async(args) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # e.g. Windows
    await stream(args, stop)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
