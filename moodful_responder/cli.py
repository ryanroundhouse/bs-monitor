"""moodful_responder CLI — `watch`, `review`, and `stats`.

Two-phase, approval-gated by design:

  watch   stream Jetstream → detect genuine asks → crisis-filter → dedupe →
          draft a reply → enqueue it. Posts NOTHING.
  review  walk the queue one at a time; you approve / edit / skip each draft.
          Only on approval is a reply posted, from your account.
"""

import argparse
import asyncio
import json
import os
import random
import signal
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from . import intent, replies
from .bsky import DEFAULT_PDS, BskyClient, BskyError
from .env import load_dotenv
from .jetstream import JETSTREAM_HOSTS, extract_post, post_events
from .store import Store
from .telegram import TelegramBot, TelegramError, decode_action

DIM = "\033[2m"
CYAN = "\033[36m"
YELLOW = "\033[1;33m"
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

DEFAULT_POSTS = Path(__file__).resolve().parent / "content" / "posts.json"

DAILY_HASHTAGS = {
    "base": ["#moodtracking", "#journaling", "#selfcare", "#mentalhealth"],
    "privacy": ["#privacy", "#digitalwellbeing"],
    "breath": ["#breathwork", "#mindfulness"],
    "habit": ["#habits", "#dailyreflection"],
    "sleep": ["#sleep", "#wellbeing"],
}
MOOD_SEARCH_QUERIES = [
    '"mood journal" -moodful',
    '"mood tracking" -moodful',
    '"feeling a bit" mood -moodful',
    '"how are you feeling" mood -moodful',
    '"daily reflection" mood -moodful',
]


# --------------------------------------------------------------------------- #
# shared safety pipeline
# --------------------------------------------------------------------------- #
def _classify_and_enqueue(event, store, lang, min_confidence) -> Optional[dict]:
    """Run one Jetstream event through the full safety pipeline and enqueue it
    if it's a genuine, non-crisis, not-already-contacted ask.

    Returns the queued post dict (with `draft_text`, `confidence`, `queue_id`)
    or None. `watch` and `relay` both go through here so the rails — the crisis
    gate, the confidence floor, dedupe — can never drift apart between them.
    """
    post = extract_post(event)
    if not post:
        return None
    if lang and not any(l.startswith(lang) for l in post["langs"]):
        return None

    res = intent.evaluate(post["text"])

    # Hard safety gate: anything with crisis markers is dropped and logged,
    # never queued — even if it also looks like an ask.
    if res.is_crisis:
        store.log("skip_crisis", post["did"], post["uri"], ",".join(res.crisis_hits))
        return None
    if not res.should_reply or res.confidence < min_confidence:
        return None
    if "moodful" in post["text"].lower():
        return None  # they already know about us
    if store.already_contacted(post["did"]) or store.is_queued(post["did"]):
        return None

    post["draft_text"] = replies.draft_reply(post["text"])
    post["confidence"] = res.confidence
    queue_id = store.enqueue(post)
    if queue_id is None:
        return None
    post["queue_id"] = queue_id
    return post


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
            post = _classify_and_enqueue(event, store, args.lang, args.min_confidence)
            if post is None:
                continue
            queued += 1
            print(f"{GREEN}＋ queued{RESET} {DIM}(conf {post['confidence']}){RESET} "
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
# relay — watch + Telegram approval, in one process
# --------------------------------------------------------------------------- #
def _at_post_url(at_uri: str) -> str:
    """at://did/app.bsky.feed.post/rkey -> a tappable bsky.app URL."""
    try:
        rest = at_uri.split("at://", 1)[1]
        did, _, tail = rest.partition("/")
        rkey = tail.rsplit("/", 1)[-1]
        return f"https://bsky.app/profile/{did}/post/{rkey}"
    except (IndexError, ValueError):
        return at_uri


async def _approve_and_post(bot, client, store, row, text, message_id, status) -> None:
    """Post `text` as the reply for `row`, then confirm back in Telegram.

    DB writes stay on this (event-loop) thread; only the blocking network call
    is offloaded, so the single SQLite connection is never touched off-thread.
    """
    if not row["cid"]:
        store.mark_skipped(row["id"], row["did"], row["post_uri"], "missing cid")
        await asyncio.to_thread(bot.send_message,
                                "⚠️ Can't post — the original post is missing its cid.")
        return
    ref = {"uri": row["post_uri"], "cid": row["cid"]}
    try:
        reply_uri = await asyncio.to_thread(
            client.post_reply, text, ref, ref, replies.link_facets(text)
        )
    except BskyError as exc:
        store.log("post_error", row["did"], row["post_uri"], str(exc))
        await asyncio.to_thread(bot.send_message, f"❌ Post failed: {exc}")
        status(f"post failed for {row['post_url']}: {exc}")
        return
    store.mark_sent(row["id"], row["did"], row["post_uri"], reply_uri)
    if message_id is not None:
        await asyncio.to_thread(bot.edit_message, message_id,
                                f"✅ Posted.\n{_at_post_url(reply_uri)}")
    else:
        await asyncio.to_thread(bot.send_message,
                                f"✅ Posted (edited).\n{_at_post_url(reply_uri)}")
    status(f"posted via Telegram: {reply_uri}")


async def _handle_callback(bot, client, store, cb, awaiting_edit, status) -> None:
    message = cb.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    cb_id = cb["id"]
    if not bot.authorized(chat_id):
        await asyncio.to_thread(bot.answer_callback, cb_id, "Not authorized.")
        return

    action, queue_id = decode_action(cb.get("data", ""))
    message_id = message.get("message_id")
    body = message.get("text", "")
    if queue_id is None:
        await asyncio.to_thread(bot.answer_callback, cb_id, "Unrecognized action.")
        return

    row = store.get_pending(queue_id)
    if row is None:
        await asyncio.to_thread(bot.answer_callback, cb_id, "Already handled.")
        await asyncio.to_thread(bot.edit_message, message_id,
                                f"{body}\n\n— already handled —")
        return

    if action == "skip":
        store.mark_skipped(row["id"], row["did"], row["post_uri"], "skipped via Telegram")
        await asyncio.to_thread(bot.answer_callback, cb_id, "Skipped.")
        await asyncio.to_thread(bot.edit_message, message_id, f"{body}\n\n⊘ Skipped.")
        status(f"skipped via Telegram: {row['post_url']}")
    elif action == "edit":
        awaiting_edit[chat_id] = queue_id
        await asyncio.to_thread(bot.answer_callback, cb_id, "Send your replacement text.")
        await asyncio.to_thread(
            bot.send_message,
            f"✍️ Reply with the replacement text for:\n{row['post_url']}",
            force_reply=True,
        )
    elif action == "approve":
        await asyncio.to_thread(bot.answer_callback, cb_id, "Posting…")
        await _approve_and_post(bot, client, store, row, row["draft_text"],
                                message_id, status)
    else:
        await asyncio.to_thread(bot.answer_callback, cb_id, "Unrecognized action.")


async def _handle_message(bot, client, store, msg, awaiting_edit, status) -> None:
    chat_id = (msg.get("chat") or {}).get("id")
    if not bot.authorized(chat_id):
        return  # ignore anyone who isn't the configured operator
    text = (msg.get("text") or "").strip()
    if not text:
        return

    if chat_id in awaiting_edit:
        queue_id = awaiting_edit.pop(chat_id)
        row = store.get_pending(queue_id)
        if row is None:
            await asyncio.to_thread(bot.send_message, "That draft was already handled.")
            return
        await _approve_and_post(bot, client, store, row, text, None, status)
        return

    if text in ("/start", "/help"):
        await asyncio.to_thread(
            bot.send_message,
            "I'll send each drafted reply here as it's found. Use the buttons "
            "to Approve, Edit (reply with new text), or Skip. /pending shows the "
            "queue depth.",
        )
    elif text == "/pending":
        n = len(store.list_pending())
        await asyncio.to_thread(bot.send_message, f"{n} draft(s) pending.")


async def _relay_producer(args, store, bot, stop, status) -> None:
    """The firehose half: stream, classify, enqueue, push to Telegram."""
    async for event in post_events(args.endpoint, stop, on_status=status):
        post = _classify_and_enqueue(event, store, args.lang, args.min_confidence)
        if post is None:
            continue
        try:
            await asyncio.to_thread(
                bot.send_draft, post["queue_id"], post["text"],
                post["draft_text"], post["url"], post["confidence"],
            )
            status(f"queued + pushed to Telegram: {post['url']}")
        except TelegramError as exc:
            # The draft is safely queued either way; review/relay can pick it up.
            status(f"queued, but Telegram push failed: {exc}")


async def _relay_consumer(bot, client, store, stop, status) -> None:
    """The approval half: long-poll Telegram and act on your decisions."""
    offset: Optional[int] = None
    awaiting_edit: dict = {}  # chat_id -> queue_id awaiting replacement text
    while not stop.is_set():
        try:
            # Short long-poll: updates still arrive instantly (the server
            # returns the moment one lands); the modest window just bounds how
            # long a Ctrl-C has to wait for an idle poll to return.
            updates = await asyncio.to_thread(bot.get_updates, offset, 10)
        except TelegramError as exc:
            status(f"Telegram poll error: {exc}; retrying in 5s")
            await asyncio.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            if "callback_query" in u:
                await _handle_callback(bot, client, store, u["callback_query"],
                                       awaiting_edit, status)
            elif "message" in u:
                await _handle_message(bot, client, store, u["message"],
                                      awaiting_edit, status)


async def _relay(args) -> None:
    store = Store(args.db)

    def status(msg: str) -> None:
        print(f"{DIM}moodful-relay: {msg}{RESET}", file=sys.stderr)

    bsky_id = os.environ.get("BSKY_IDENTIFIER")
    bsky_pw = os.environ.get("BSKY_APP_PASSWORD")
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID")

    if not bsky_id or not bsky_pw:
        print(f"{RED}Missing Bluesky credentials.{RESET} Set BSKY_IDENTIFIER and "
              f"BSKY_APP_PASSWORD (an app password) — relay posts on your approval.",
              file=sys.stderr)
        store.close()
        sys.exit(1)
    if not tg_token:
        print(f"{RED}Missing TELEGRAM_BOT_TOKEN.{RESET} Create a bot with "
              f"@BotFather, then put its token in your .env.", file=sys.stderr)
        store.close()
        sys.exit(1)

    # Help the user find their chat id the first time (when it isn't set yet).
    if not tg_chat:
        try:
            chats = TelegramBot(tg_token, "").discover_chats()
        except TelegramError as exc:
            print(f"{RED}Telegram error:{RESET} {exc}", file=sys.stderr)
            store.close()
            sys.exit(1)
        print(f"{RED}TELEGRAM_CHAT_ID is not set.{RESET} Send your bot any "
              f"message in Telegram, then re-run. Recent chats seen:",
              file=sys.stderr)
        for c in chats:
            who = c.get("username") or c.get("first_name") or c.get("title") or "?"
            print(f"  TELEGRAM_CHAT_ID={c['id']}   ({who})", file=sys.stderr)
        if not chats:
            print("  (none yet — message the bot first, then re-run)", file=sys.stderr)
        store.close()
        sys.exit(1)

    bot = TelegramBot(tg_token, tg_chat)
    try:
        client = BskyClient(bsky_id, bsky_pw, pds=args.pds)
    except BskyError as exc:
        print(f"{RED}Bluesky login failed:{RESET} {exc}", file=sys.stderr)
        store.close()
        sys.exit(1)

    try:
        bot.send_message(
            f"🟢 moodful relay online as @{client.handle}. "
            f"I'll send drafts here for you to approve, edit, or skip."
        )
    except TelegramError as exc:
        print(f"{RED}Couldn't reach Telegram:{RESET} {exc}\nCheck TELEGRAM_BOT_TOKEN "
              f"and TELEGRAM_CHAT_ID.", file=sys.stderr)
        store.close()
        sys.exit(1)

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    tasks = [
        asyncio.create_task(_relay_producer(args, store, bot, stop, status)),
        asyncio.create_task(_relay_consumer(bot, client, store, stop, status)),
    ]

    # Ctrl-C must be responsive: the loops are usually parked inside a network
    # wait (the firehose recv, a Telegram long-poll), so we cancel the tasks
    # outright rather than wait for them to notice a flag. A second Ctrl-C
    # force-quits, in case an in-flight long-poll is still unwinding.
    interrupts = {"n": 0}

    def _on_signal() -> None:
        interrupts["n"] += 1
        if interrupts["n"] == 1:
            status("shutting down… (press Ctrl-C again to force-quit)")
            stop.set()
            for t in tasks:
                t.cancel()
        else:
            status("force-quit.")
            os._exit(130)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass

    status(f"online as @{client.handle}; watching {JETSTREAM_HOSTS[args.endpoint]} "
           f"(lang={args.lang}, min-confidence={args.min_confidence}). "
           f"Approve/edit/skip from Telegram. Ctrl-C to stop.")
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass  # expected on Ctrl-C
    finally:
        status("stopped.")
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
# post-daily — one moodful post per day
# --------------------------------------------------------------------------- #
def _load_posts(path: str) -> list:
    p = Path(path)
    if not p.is_file():
        print(f"{RED}posts file not found:{RESET} {p}", file=sys.stderr)
        sys.exit(1)
    data = json.loads(p.read_text(encoding="utf-8"))
    posts = [s.strip() for s in data if isinstance(s, str) and s.strip()]
    if not posts:
        print(f"{RED}no posts found in {p}{RESET}", file=sys.stderr)
        sys.exit(1)
    return posts


def _parse_window(spec: str):
    """Parse a 'START-END' local-hour window (e.g. '6-24'); END is exclusive."""
    try:
        a, b = spec.split("-", 1)
        start, end = int(a), int(b)
    except (ValueError, AttributeError):
        raise ValueError(f"invalid --window {spec!r} (use START-END, e.g. 6-24)")
    if not (0 <= start < end <= 24):
        raise ValueError(f"invalid --window {spec!r} (need 0 <= start < end <= 24)")
    return start, end


def _should_post_this_hour(hour: int, start: int, end: int, rng=random) -> bool:
    """Whether *this* hour is the one to post, for an [start, end) hour window.

    Outside the window: never. Inside: post with probability 1/(hours left,
    incl. now). Over the window that makes each hour equally likely and
    guarantees a post by the final hour (prob 1)."""
    if not (start <= hour < end):
        return False
    return rng.random() < 1.0 / (end - hour)


def _select_daily_hashtags(text: str, day_label: int, max_tags: int = 3) -> list:
    """Pick a small, rotating hashtag set for discoverability without spamminess."""
    low = text.lower()
    candidates = list(DAILY_HASHTAGS["base"])
    if any(w in low for w in ("private", "privacy", "sell", "train")):
        candidates += DAILY_HASHTAGS["privacy"]
    if any(w in low for w in ("breath", "breathing", "stillness", "pause")):
        candidates += DAILY_HASHTAGS["breath"]
    if any(w in low for w in ("habit", "ritual", "daily", "check-in", "check in")):
        candidates += DAILY_HASHTAGS["habit"]
    if "sleep" in low:
        candidates += DAILY_HASHTAGS["sleep"]

    # De-dupe while preserving order, then rotate deterministically by post day
    # so the account does not use the exact same tags every day.
    seen = set()
    unique = [t for t in candidates if not (t.lower() in seen or seen.add(t.lower()))]
    rng = random.Random(day_label)
    rng.shuffle(unique)
    return unique[:max_tags]


def _append_daily_hashtags(text: str, day_label: int, max_len: int = replies.MAX_GRAPHEMES) -> str:
    """Append up to three engagement hashtags while respecting Bluesky's limit."""
    tags = _select_daily_hashtags(text, day_label)
    if not tags or any("#" in part for part in text.split()):
        return text
    out = text
    chosen = []
    for tag in tags:
        candidate_tags = chosen + [tag]
        candidate = f"{text.rstrip()}\n\n{' '.join(candidate_tags)}"
        if len(candidate) <= max_len:
            out = candidate
            chosen = candidate_tags
    return out


def _post_daily(args) -> None:
    """Post the next scheduled moodful post. Idempotent per calendar day: if
    one already went out today it does nothing (unless --force)."""
    posts = _load_posts(args.posts)
    store = Store(args.db)
    today = date.today().isoformat()

    existing = store.posted_today(today)
    if existing and not args.force:
        print(f"already posted today ({today}). {existing['uri'] or ''}".rstrip())
        store.close()
        return

    # Randomized timing (for an hourly cron): fire every hour in a window and
    # let each hour roll whether it's the one. Robust to the machine sleeping —
    # the next awake hour re-rolls, and the final hour is guaranteed. Skipped
    # for --dry-run, which just previews the post.
    if args.window and not args.dry_run:
        try:
            wstart, wend = _parse_window(args.window)
        except ValueError as exc:
            print(f"{RED}{exc}{RESET}", file=sys.stderr)
            store.close()
            sys.exit(2)
        hour = datetime.now().hour
        if not (wstart <= hour < wend):
            print(f"{DIM}outside posting window {wstart}:00–{wend}:00 "
                  f"(now {hour:02d}:00) — skipping this run.{RESET}")
            store.close()
            return
        if not args.force and not _should_post_this_hour(hour, wstart, wend):
            print(f"{DIM}not this hour (rolled 1/{wend - hour}); will reconsider "
                  f"next hour.{RESET}")
            store.close()
            return
        if args.jitter > 0:
            delay = random.randint(0, min(args.jitter, 3300))
            print(f"{DIM}this is the hour — posting in {delay // 60}m "
                  f"{delay % 60}s.{RESET}", flush=True)
            time.sleep(delay)

    # Two ways to pick today's post:
    #   --start DATE : deterministic by calendar day (stateless — for cron/remote)
    #   default      : sequential by how many we've posted (uses the local db)
    if args.start:
        try:
            start = date.fromisoformat(args.start)
        except ValueError:
            print(f"{RED}invalid --start date:{RESET} {args.start} (use YYYY-MM-DD)",
                  file=sys.stderr)
            store.close()
            sys.exit(1)
        day_num = (date.today() - start).days
        if day_num < 0:
            print(f"start date {args.start} is in the future — nothing to post yet.")
            store.close()
            return
        idx, day_label = day_num % len(posts), day_num + 1
    else:
        idx, day_label = store.daily_count() % len(posts), store.daily_count() + 1

    text = _append_daily_hashtags(posts[idx], day_label)
    print(f"{DIM}day {day_label} · post {idx + 1}/{len(posts)}{RESET}")
    print(f"\n{text}\n")

    if args.dry_run:
        print(f"{DIM}[dry-run] not posting.{RESET}")
        store.close()
        return

    identifier = os.environ.get("BSKY_IDENTIFIER")
    password = os.environ.get("BSKY_APP_PASSWORD")
    if not identifier or not password:
        print(f"{RED}Missing credentials.{RESET} Set BSKY_IDENTIFIER and "
              f"BSKY_APP_PASSWORD (see .env / RESPONDER.md).", file=sys.stderr)
        store.close()
        sys.exit(1)
    try:
        client = BskyClient(identifier, password, pds=args.pds)
        uri = client.post(text, facets=replies.richtext_facets(text))
    except BskyError as exc:
        print(f"{RED}post failed:{RESET} {exc}", file=sys.stderr)
        store.log("daily_post_error", None, None, str(exc))
        store.close()
        sys.exit(1)
    store.record_daily_post(idx, text, today, uri)
    print(f"{GREEN}posted.{RESET} {_at_post_url(uri)}")
    store.close()


def _search_post_text(post: dict) -> str:
    record = post.get("record") or {}
    return str(record.get("text") or post.get("text") or "")


def _is_safe_mood_like_candidate(post: dict, client: BskyClient, store: Store) -> bool:
    uri = post.get("uri")
    cid = post.get("cid")
    author = post.get("author") or {}
    text = _search_post_text(post)
    if not uri or not cid or not text:
        return False
    if author.get("did") == client.did:
        return False
    if store.already_liked_post(uri):
        return False
    low = text.lower()
    if "moodful" in low or "moodful.ca" in low:
        return False
    # Avoid liking crisis/distress content from the brand account.
    res = intent.evaluate(text)
    if res.is_crisis:
        return False
    return True


def _like_mood_post(args) -> None:
    """Once per day, find a public post about moods and like one safe candidate."""
    store = Store(args.db)
    today = date.today().isoformat()
    existing = store.liked_today(today)
    if existing and not args.force:
        print(f"already liked a mood post today ({today}). {existing['post_uri']}")
        store.close()
        return

    identifier = os.environ.get("BSKY_IDENTIFIER")
    password = os.environ.get("BSKY_APP_PASSWORD")
    if not identifier or not password:
        print(f"{RED}Missing credentials.{RESET} Set BSKY_IDENTIFIER and "
              f"BSKY_APP_PASSWORD (see .env / RESPONDER.md).", file=sys.stderr)
        store.close()
        sys.exit(1)

    try:
        client = BskyClient(identifier, password, pds=args.pds)
        queries = list(args.query or MOOD_SEARCH_QUERIES)
        random.shuffle(queries)
        for query in queries:
            posts = client.search_posts(query, limit=args.limit, sort="latest")
            random.shuffle(posts)
            for post in posts:
                if not _is_safe_mood_like_candidate(post, client, store):
                    continue
                uri, cid = post["uri"], post["cid"]
                author_did = (post.get("author") or {}).get("did", "")
                text = _search_post_text(post).strip()
                print(f"candidate from {query!r}: {_at_post_url(uri)}")
                print(text[:280])
                if args.dry_run:
                    print(f"{DIM}[dry-run] not liking.{RESET}")
                    store.close()
                    return
                like_uri = client.like(uri, cid)
                store.record_daily_like(today, uri, cid, author_did, text, like_uri, query)
                print(f"{GREEN}liked.{RESET} {_at_post_url(uri)}")
                store.close()
                return
    except BskyError as exc:
        print(f"{RED}like task failed:{RESET} {exc}", file=sys.stderr)
        store.log("daily_like_error", None, None, str(exc))
        store.close()
        sys.exit(1)

    print("no safe mood post found to like today.")
    store.log("daily_like_none", None, None, "no safe candidate")
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

    rl = sub.add_parser(
        "relay",
        help="watch + push each draft to Telegram for approve/edit/skip from "
        "your phone (one always-on process).",
    )
    rl.add_argument("--endpoint", choices=sorted(JETSTREAM_HOSTS), default="us-east-2")
    rl.add_argument("--lang", default="en", help="language filter (default: en).")
    rl.add_argument("--min-confidence", type=float, default=0.6,
                    help="minimum intent confidence to queue (default: 0.6).")
    rl.add_argument("--pds", default=os.environ.get("BSKY_PDS", DEFAULT_PDS),
                    help="PDS host (env: BSKY_PDS; default: bsky.social).")
    rl.set_defaults(func=lambda a: asyncio.run(_relay(a)))

    d = sub.add_parser(
        "post-daily",
        help="post the next daily moodful post (idempotent per calendar day).",
    )
    d.add_argument(
        "--posts",
        default=os.environ.get("MOODFUL_POSTS_FILE", str(DEFAULT_POSTS)),
        help="JSON array of posts (env: MOODFUL_POSTS_FILE).",
    )
    d.add_argument("--pds", default=os.environ.get("BSKY_PDS", DEFAULT_PDS),
                   help="PDS host (env: BSKY_PDS).")
    d.add_argument("--start", default=os.environ.get("MOODFUL_START_DATE"),
                   metavar="YYYY-MM-DD",
                   help="anchor date for stateless calendar-based selection "
                        "(env: MOODFUL_START_DATE). Best for cron/remote runs.")
    d.add_argument("--window", default=os.environ.get("MOODFUL_WINDOW"),
                   metavar="START-END",
                   help="randomize timing across this local-hour window "
                        "(e.g. 6-24 = 6am–midnight); pair with an hourly cron. "
                        "(env: MOODFUL_WINDOW)")
    d.add_argument("--jitter", type=int, default=3300, metavar="SECS",
                   help="max seconds to scatter within the chosen hour "
                        "(default 3300; 0 = on the hour).")
    d.add_argument("--dry-run", action="store_true",
                   help="show the post that would go out, without posting.")
    d.add_argument("--force", action="store_true",
                   help="post even if one already went out today.")
    d.set_defaults(func=_post_daily)

    l = sub.add_parser(
        "like-mood-post",
        help="once per day, find a public mood-related post and like one safe candidate.",
    )
    l.add_argument("--pds", default=os.environ.get("BSKY_PDS", DEFAULT_PDS),
                   help="PDS host (env: BSKY_PDS).")
    l.add_argument("--query", action="append",
                   help="search query to try; may be repeated. Defaults to curated mood queries.")
    l.add_argument("--limit", type=int, default=20,
                   help="search results to inspect per query (default 20).")
    l.add_argument("--dry-run", action="store_true",
                   help="show the candidate without liking it.")
    l.add_argument("--force", action="store_true",
                   help="like even if one was already recorded today.")
    l.set_defaults(func=_like_mood_post)

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
