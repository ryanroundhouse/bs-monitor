"""moodful_responder CLI — `watch`, `review`, and `stats`.

Two-phase, approval-gated by design:

  watch   stream Jetstream → detect genuine asks → crisis-filter → dedupe →
          draft a reply → enqueue it. Posts NOTHING.
  review  walk the queue one at a time; you approve / edit / skip each draft.
          Only on approval is a reply posted, from your account.
"""

import argparse
import asyncio
import os
import signal
import sys
from typing import Optional

from . import intent, replies
from .bsky import DEFAULT_PDS, BskyClient, BskyError
from .env import load_dotenv
from .jetstream import JETSTREAM_HOSTS, extract_post, post_events
from .store import Store

DIM = "\033[2m"
CYAN = "\033[36m"
YELLOW = "\033[1;33m"
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"


# --------------------------------------------------------------------------- #
# watch
# --------------------------------------------------------------------------- #
async def _watch(args) -> None:
    store = Store(args.db)
    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    def status(msg: str) -> None:
        print(f"{DIM}moodful-responder: {msg}{RESET}", file=sys.stderr)

    status(
        f"watching {JETSTREAM_HOSTS[args.endpoint]} for tool-recommendation asks "
        f"(lang={args.lang}, min-confidence={args.min_confidence}). "
        f"Drafts queue to {args.db}; nothing is posted here."
    )
    queued = 0
    try:
        async for event in post_events(args.endpoint, stop, on_status=status):
            post = extract_post(event)
            if not post:
                continue
            if args.lang and not any(l.startswith(args.lang) for l in post["langs"]):
                continue

            res = intent.evaluate(post["text"])

            # Hard safety gate: anything with crisis markers is dropped and
            # logged, never queued — even if it also looks like an ask.
            if res.is_crisis:
                store.log("skip_crisis", post["did"], post["uri"],
                          ",".join(res.crisis_hits))
                continue
            if not res.should_reply or res.confidence < args.min_confidence:
                continue
            if "moodful" in post["text"].lower():
                continue  # they already know about us
            if store.already_contacted(post["did"]) or store.is_queued(post["did"]):
                continue

            post["draft_text"] = replies.draft_reply(post["text"])
            post["confidence"] = res.confidence
            if store.enqueue(post) is not None:
                queued += 1
                print(f"{GREEN}＋ queued{RESET} {DIM}(conf {res.confidence}){RESET} "
                      f"{post['url']}")
                print(f"  ask:   {post['text'].strip()[:120]}")
                print(f"  draft: {DIM}{post['draft_text'][:120]}…{RESET}", flush=True)
    finally:
        status(f"stopped. {queued} draft(s) queued this run. "
               f"Run `review` to approve them.")
        store.close()


# --------------------------------------------------------------------------- #
# review
# --------------------------------------------------------------------------- #
def _review(args) -> None:
    store = Store(args.db)
    pending = store.list_pending()
    if not pending:
        print("Nothing pending. The queue is empty.")
        store.close()
        return

    client: Optional[BskyClient] = None
    if not args.dry_run:
        identifier = os.environ.get("BSKY_IDENTIFIER")
        password = os.environ.get("BSKY_APP_PASSWORD")
        if not identifier or not password:
            print(f"{RED}Missing credentials.{RESET} Set BSKY_IDENTIFIER and "
                  f"BSKY_APP_PASSWORD (an app password from Bluesky settings), "
                  f"or pass --dry-run to preview without posting.", file=sys.stderr)
            store.close()
            sys.exit(1)
        try:
            client = BskyClient(identifier, password, pds=args.pds)
        except BskyError as exc:
            print(f"{RED}Login failed:{RESET} {exc}", file=sys.stderr)
            store.close()
            sys.exit(1)
        print(f"{DIM}Logged in as @{client.handle}. Replies will post from this "
              f"account.{RESET}\n")

    print(f"{len(pending)} draft(s) to review. "
          f"[a]pprove & post · [e]dit then post · [s]kip · [q]uit\n")

    sent = skipped = 0
    for row in pending:
        print(f"{DIM}{'─' * 72}{RESET}")
        print(f"{CYAN}ask{RESET} {DIM}(conf {row['confidence']}){RESET}  {row['post_url']}")
        print(f"  {row['post_text'].strip()}")
        print(f"\n{YELLOW}draft reply:{RESET}")
        print(f"  {row['draft_text']}\n")

        text_to_send = row["draft_text"]
        try:
            choice = input("[a]pprove / [e]dit / [s]kip / [q]uit > ").strip().lower()
        except EOFError:
            choice = "q"

        if choice == "q":
            break
        if choice == "s" or choice == "":
            store.mark_skipped(row["id"], row["did"], row["post_uri"], "skipped by reviewer")
            skipped += 1
            print(f"{DIM}skipped.{RESET}\n")
            continue
        if choice == "e":
            try:
                edited = input("new reply text:\n> ").strip()
            except EOFError:
                edited = ""
            if edited:
                text_to_send = edited
        elif choice != "a":
            print(f"{DIM}unrecognized — skipping for safety.{RESET}\n")
            store.mark_skipped(row["id"], row["did"], row["post_uri"], "ambiguous input")
            skipped += 1
            continue

        if args.dry_run:
            print(f"{DIM}[dry-run] would post:{RESET} {text_to_send}\n")
            continue

        ref = {"uri": row["post_uri"], "cid": row["cid"]}
        if not row["cid"]:
            print(f"{RED}missing post cid — cannot build reply ref. skipping.{RESET}\n")
            store.mark_skipped(row["id"], row["did"], row["post_uri"], "missing cid")
            skipped += 1
            continue
        try:
            reply_uri = client.post_reply(
                text_to_send, parent=ref, root=ref,
                facets=replies.link_facets(text_to_send),
            )
        except BskyError as exc:
            print(f"{RED}post failed:{RESET} {exc}\n")
            store.log("post_error", row["did"], row["post_uri"], str(exc))
            continue
        store.mark_sent(row["id"], row["did"], row["post_uri"], reply_uri)
        sent += 1
        print(f"{GREEN}posted.{RESET} {reply_uri}\n")

    print(f"\nDone — {sent} sent, {skipped} skipped.")
    store.close()


# --------------------------------------------------------------------------- #
# stats
# --------------------------------------------------------------------------- #
def _stats(args) -> None:
    store = Store(args.db)
    s = store.stats()
    print("queue:")
    for k in ("pending", "sent", "skipped"):
        print(f"  {k:<9} {s.get(k, 0)}")
    print(f"contacted (lifetime): {s.get('contacted_total', 0)}")
    store.close()


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="moodful_responder",
        description="Find people asking for a mood/journaling tool on Bluesky "
        "and reply about moodful — with human approval on every post.",
    )
    p.add_argument("--db", default=os.environ.get("MOODFUL_RESPONDER_DB", "responder.db"),
                   help="SQLite queue/state file (env: MOODFUL_RESPONDER_DB).")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("watch", help="stream and queue drafts (posts nothing).")
    w.add_argument("--endpoint", choices=sorted(JETSTREAM_HOSTS), default="us-east-2")
    w.add_argument("--lang", default="en", help="language filter (default: en).")
    w.add_argument("--min-confidence", type=float, default=0.6,
                   help="minimum intent confidence to queue (default: 0.6).")
    w.set_defaults(func=lambda a: asyncio.run(_watch(a)))

    r = sub.add_parser("review", help="approve/edit/skip queued drafts, then post.")
    r.add_argument("--pds", default=os.environ.get("BSKY_PDS", DEFAULT_PDS),
                   help="PDS host (env: BSKY_PDS; default: bsky.social).")
    r.add_argument("--dry-run", action="store_true",
                   help="preview without logging in or posting.")
    r.set_defaults(func=_review)

    s = sub.add_parser("stats", help="show queue + contact counts.")
    s.set_defaults(func=_stats)
    return p


def main(argv=None) -> int:
    load_dotenv()  # auto-load .env (cwd, then repo root) before reading defaults
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        pass
    return 0
