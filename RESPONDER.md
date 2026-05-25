# moodful_responder

Find people on Bluesky who are **actively asking for a mood/journaling tool**,
and reply to them about [moodful](https://moodful.ca) — with a human approving
every single post before it goes out.

This is built on the same Jetstream firehose as `bs_monitor.py`, but it is
deliberately narrow and conservative. Read the design stance below before
running it.

## What it does — and what it refuses to do

It responds **only to solicited requests**: posts where someone is looking for a
recommendation ("anyone know a good mood tracker?", "looking for a journaling
app with no streak pressure"). It does **not**:

- send unsolicited messages to people who didn't ask;
- target people by mood, sadness, or distress signals;
- post anything without a human approving it first.

### Safety rails (non-negotiable, built in)

| Rail | How |
|------|-----|
| **Intent, not distress** | A post must contain a *topic* signal **and** a *seeking* signal (or an unambiguous phrase) to count as an ask. See `intent.py`. |
| **Hard crisis gate** | Any post with distress/crisis markers is dropped and logged, never queued — even if it also looks like an ask. Biased toward over-exclusion. |
| **Human approval** | `watch` only drafts and queues. Nothing posts until you approve it in `review`. |
| **Disclosure** | Every reply states that the sender makes moodful. No astroturfing. Label the bot account as automated in its bio. |
| **Dedupe** | One reply per person, ever (tracked by DID). |
| **Audit log** | Every queue/send/skip — and every crisis exclusion — is recorded in the SQLite `audit` table. |

> A note on Bluesky's rules: replying once, with disclosure, to someone who
> publicly asked for a recommendation is welcome participation. Automated
> *unsolicited* replies are spam and risk getting the account — and the
> `moodful.ca` domain — labeled or blocked. This tool is built to stay firmly on
> the welcome side of that line. Keep the volume low and the relevance high.

## Architecture

```
                  ┌─────────────┐     genuine ask?      ┌──────────────┐
 Jetstream  ─────▶│   watch     │──── crisis-filter ───▶│  SQLite queue │
 (app.bsky.       │ classify +  │     dedupe            │  (drafts,     │
  feed.post)      │ draft reply │                       │   pending)    │
                  └─────────────┘                       └──────┬───────┘
                                                               │
                                          you approve / edit / skip each
                                                               │
                                                        ┌──────▼───────┐
                                                        │   review     │── post ─▶ Bluesky
                                                        │ (your login) │   (createRecord)
                                                        └──────────────┘
```

Modules: `jetstream.py` (stream), `intent.py` (classify + crisis gate),
`replies.py` (draft + link facets), `store.py` (queue/dedupe/audit),
`bsky.py` (XRPC login + post), `telegram.py` (push + approval bot),
`cli.py` (commands).

**Don't want to watch a console?** The `relay` command is `watch` + `review`
fused into one always-on process: it streams the firehose *and* pushes each
draft to you in Telegram with **Approve / Edit / Skip** buttons. The same
safety pipeline and the same human-approval rule apply — nothing posts until
you tap Approve (or reply with edited text). See *Approve from your phone*
below. The two-phase `watch` + `review` flow still works unchanged as a
console-only alternative.

## Setup

```bash
source .venv/bin/activate          # the repo venv (websockets installed)
```

No extra dependencies — `bsky.py` uses stdlib `urllib`.

To post, the `review` step needs an **app password** (not your real password):
Bluesky → Settings → Privacy and security → App passwords → *Add*.

Put your credentials in a `.env` file — the CLI **auto-loads it** from the
current directory or the repo root, so there's no `source` step:

```bash
cp example.env .env        # then edit .env and fill in the two required values
```

```ini
# .env  (gitignored — never committed)
BSKY_IDENTIFIER=your-handle.bsky.social
BSKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
BSKY_PDS=https://bsky.social        # optional; only for a custom PDS
MOODFUL_RESPONDER_DB=responder.db   # optional; queue/state file location

# Only needed for `relay` (approve from your phone):
TELEGRAM_BOT_TOKEN=123456789:AA...  # from @BotFather
TELEGRAM_CHAT_ID=                    # blank → relay prints it for you on first run
```

Real environment variables still take precedence over `.env`, so you can also
just `export BSKY_IDENTIFIER=… BSKY_APP_PASSWORD=…` instead. Either way the
credentials are read only at `review` time. `.env`, `*.db`, and `*.sqlite` are
gitignored.

## Usage

```bash
# 1. Watch the firehose and queue drafts (posts nothing). Leave it running.
python -m moodful_responder watch

# 2. When you have a moment, review the queue. Approve/edit/skip each draft.
python -m moodful_responder review

# Preview the queue without logging in or posting:
python -m moodful_responder review --dry-run

# See where things stand:
python -m moodful_responder stats
```

`review` shows you each original ask and the drafted reply, then waits:

```
[a]pprove / [e]dit / [s]kip / [q]uit
```

- **a** — post the draft as-is, from your account, as a reply to the ask.
- **e** — type a replacement, then post it.
- **s** — skip (logged; the person is *not* marked contacted, but the draft is resolved).
- **q** — stop reviewing; the rest stay pending.

### Approve from your phone (Telegram)

`relay` replaces the console with a Telegram chat, so you don't have to sit and
watch anything — you get a push the moment a real ask appears and approve it
from your phone.

**One-time setup:**

1. In Telegram, message **@BotFather** → `/newbot`, pick a name, copy the token
   into `TELEGRAM_BOT_TOKEN` in `.env`.
2. Open your new bot and send it any message (e.g. `/start`) so it can see you.
3. Run `relay` once with `TELEGRAM_CHAT_ID` blank — it prints your chat id.
   Paste that into `.env`.

**Run it (leave it running — one process does everything):**

```bash
python -m moodful_responder relay
```

For each genuine ask, the bot sends you the original post, a link to it, and the
drafted reply, with three buttons:

- **✓ Approve** — posts the draft as-is, from your account, as a reply.
- **✎ Edit** — the bot asks for replacement text; reply with it and that gets
  posted instead.
- **⊘ Skip** — drops the draft (logged; the person is *not* marked contacted).

Send `/pending` any time to see how many drafts are waiting. Only the chat id
you configured can drive the bot — messages from anyone else are ignored. The
relay keeps your Bluesky session fresh, so it can run for days; `Ctrl-C` stops
it (it may take a few seconds to finish the current long-poll).

### Options

| Command | Flag | Default | Meaning |
|---------|------|---------|---------|
| `watch` | `--endpoint` | `us-east-2` | which Jetstream instance |
| `watch` | `--lang` | `en` | only posts tagged this language |
| `watch` | `--min-confidence` | `0.6` | minimum intent score to queue |
| `review`| `--pds` | `bsky.social` | PDS host for login |
| `review`| `--dry-run` | off | preview without posting |
| `relay` | `--endpoint` / `--lang` / `--min-confidence` | (as `watch`) | streaming/classify filters |
| `relay` | `--pds` | `bsky.social` | PDS host for login |
| *(all)* | `--db` | `responder.db` | SQLite state file |

## Tuning the classifier

Edit the phrase lists in `moodful_responder/intent.py`:
`TOPIC_PHRASES`, `SEEKING_PHRASES`, `HIGH_CONFIDENCE_PHRASES`, and `CRISIS_TERMS`.
The crisis list is intentionally broad — when in doubt, add to it; a false
exclusion only costs you a reply you weren't going to regret skipping.

Run the safety tests after any change:

```bash
python -m unittest discover -s tests
```

## Good practice when running this

- Use a **dedicated, clearly-labeled account** and keep daily volume low.
- Read each draft before approving — the human step is the point, not a rubber stamp.
- If someone asks you to stop or seems to be having a hard time, don't reply.
- Reply *once*; never follow up.
