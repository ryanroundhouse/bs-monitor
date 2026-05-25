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
`bsky.py` (XRPC login + post), `cli.py` (commands).

## Setup

```bash
source .venv/bin/activate          # the repo venv (websockets installed)
```

No extra dependencies — `bsky.py` uses stdlib `urllib`.

To post, the `review` step needs an **app password** (not your real password):
Bluesky → Settings → Privacy and security → App passwords → *Add*.

```bash
export BSKY_IDENTIFIER="your-handle.bsky.social"   # or the bot account's handle
export BSKY_APP_PASSWORD="xxxx-xxxx-xxxx-xxxx"
```

These are read only at `review` time and are never written to disk. (`.env`,
`*.db`, `*.sqlite` are gitignored.)

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

### Options

| Command | Flag | Default | Meaning |
|---------|------|---------|---------|
| `watch` | `--endpoint` | `us-east-2` | which Jetstream instance |
| `watch` | `--lang` | `en` | only posts tagged this language |
| `watch` | `--min-confidence` | `0.6` | minimum intent score to queue |
| `review`| `--pds` | `bsky.social` | PDS host for login |
| `review`| `--dry-run` | off | preview without posting |
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
