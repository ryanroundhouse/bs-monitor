# bs-monitor

Subscribe to **new Bluesky posts in real time** and print the ones that match
your keywords.

```
$ python bs_monitor.py --lang en earthquake quake
bs-monitor: streaming posts matching ANY of ['earthquake', 'quake'] from jetstream2.us-east.bsky.network (lang=en)
────────────────────────────────────────────────────────────────────────
Did anyone else just feel that earthquake?? 😳
@ did:plc:2tdpnzfvvxevmwbm67r6e6qm  2026-05-25T01:38:25.008Z  [earthquake]
https://bsky.app/profile/did:plc:2tdpnzfvvxevmwbm67r6e6qm/post/3mmndrnv2gs22
```

## How it works

Bluesky is built on the [AT Protocol](https://atproto.com/), which publishes a
real-time **firehose** of every event on the network — posts, likes, follows,
handle changes, etc. There are two ways to consume it:

| | Raw firehose (`com.atproto.sync.subscribeRepos`) | **Jetstream** (what we use) |
|---|---|---|
| Encoding | CBOR + CAR blocks (must be decoded) | Plain **JSON** |
| Filtering | None — you receive everything | Server-side by collection (NSID) and/or repo (DID) |
| Bandwidth | High | ~56% lower (optional `zstd`) |
| Auth | None | None |

`bs-monitor` connects a WebSocket to a public Jetstream instance and asks for
only the post collection:

```
wss://jetstream2.us-east.bsky.network/subscribe?wantedCollections=app.bsky.feed.post
```

Each message is one JSON event. A **`create` commit** in the
`app.bsky.feed.post` collection is a brand-new post, and its text lives at
`commit.record.text`. We match that text against your keywords and print the
hits. The connection auto-reconnects with exponential backoff, resuming from the
last `time_us` cursor so a brief drop doesn't lose posts.

```
event ──▶ kind == "commit"
       └▶ commit.operation == "create"
          └▶ commit.collection == "app.bsky.feed.post"
             └▶ commit.record.text  ──▶ keyword match ──▶ print
```

## Setup

Requires Python 3.8+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
# Match ANY of these words (case-insensitive)
python bs_monitor.py python rust golang

# Require ALL words to be present
python bs_monitor.py --match all claude code

# Only English-tagged posts
python bs_monitor.py --lang en bluesky

# No keywords = firehose of every new post
python bs_monitor.py

# Capture matches as newline-delimited JSON for later processing
python bs_monitor.py --json earthquake > hits.ndjson
```

### Options

| Flag | Description |
|------|-------------|
| `keywords...` | Case-insensitive substrings to match. Omit to print every post. |
| `--match {any,all}` | Match any keyword (default) or require all of them. |
| `--lang CODE` | Only posts tagged with this language (`en`, `pt`, `ja`, …). |
| `--endpoint NAME` | Jetstream instance: `us-east-1`, `us-east-2` (default), `us-west-1`, `us-west-2`. |
| `--cursor TIME_US` | Resume from a Unix-microseconds cursor instead of live-tailing. |
| `--json` | Emit raw matching events as NDJSON instead of pretty output. |

Press `Ctrl-C` to stop.

### Output

Each match prints the post text (matched terms highlighted), then a metadata
line — author DID, `↩` if it's a reply, `🖼` if it embeds media, the post's
`createdAt` timestamp, and which keywords matched — followed by a clickable
`bsky.app` link to the post.

> Note: events identify the author by **DID**, not handle. The link resolves to
> the author in a browser. Resolving DID→handle would mean an extra API lookup
> per post; see *Extending* below.

## Extending

This is a deliberately small, dependency-light base. Natural next steps:

- **Persist** — instead of (or in addition to) printing, append each match to
  SQLite or a file. The `--json` mode already gives you a clean NDJSON stream to
  pipe into other tools.
- **Resolve handles** — call `com.atproto.identity.resolveHandle` /
  `app.bsky.actor.getProfile` (or cache from Jetstream `identity` events) to show
  `@handle.bsky.social` instead of the raw DID.
- **Compression** — Jetstream supports `zstd` via `?compress=true` and a custom
  dictionary; add the `zstandard` package to cut bandwidth ~56% for high-volume
  feeds.
- **Richer filters** — match on `record.langs`, hashtags/facets, embedded links,
  or restrict to specific authors with Jetstream's `wantedDids` parameter.
- **Notifications / alerting** — fan matches out to a webhook, email, or queue.

## moodful_responder

Built on the same firehose: a narrow, **approval-gated** responder that finds
people *asking* for a mood/journaling tool and drafts a reply about
[moodful](https://moodful.ca) for a human to approve before it posts. It only
answers solicited requests, hard-excludes any post with crisis/distress markers,
dedupes per person, and discloses the sender. See **[RESPONDER.md](RESPONDER.md)**.

```bash
python -m moodful_responder watch     # detect asks, queue drafts (posts nothing)
python -m moodful_responder review     # approve/edit/skip each, then it posts
```

(There's also `relay`, which fuses `watch` + `review` into one always-on
process and pushes each draft to Telegram for one-tap approve/edit/skip — see
[RESPONDER.md](RESPONDER.md).)

## Daily posts

`moodful_responder` can also post **one original moodful post per day** from a
curated set of 365 (`moodful_responder/content/posts.json`; regenerate with
`scripts/assemble_posts.py`). Each goes out as a top-level Bluesky post with a
clickable `moodful.ca` link, using the same `.env` credentials.

```bash
python -m moodful_responder post-daily --dry-run   # preview today's post
python -m moodful_responder post-daily             # post it now
```

Schedule it with the bundled installer. It adds a single crontab entry that
`cd`s into the repo (so `.env` auto-loads) and logs to `logs/daily-post.log`:

```bash
scripts/install_daily_cron.sh                 # random time 6am–midnight (default)
scripts/install_daily_cron.sh --window=8-22   # random time, 8am–10pm
scripts/install_daily_cron.sh 09:00           # a fixed time every day instead
scripts/install_daily_cron.sh --dry-run       # print the crontab line, change nothing
scripts/install_daily_cron.sh --uninstall     # remove the job
```

The default fires the job **hourly** across the window; each hour rolls
`1/(hours left)` so the post lands at a uniformly random hour (plus a
within-hour minute jitter). The final hour is guaranteed, and if the machine is
asleep at the chosen hour the next awake hour simply re-rolls. cron uses your
machine's **local time**, so the job only fires while the machine is awake — for
guaranteed delivery, run it on an always-on host.

| `post-daily` flag | Meaning |
|---|---|
| `--window START-END` | randomize timing across these local hours (e.g. `6-24`); pair with an hourly cron |
| `--jitter SECS` | max within-hour scatter (default `3300`; `0` = on the hour) |
| `--start YYYY-MM-DD` | pick the post by calendar date (stateless) instead of a running count |
| `--dry-run` | show the post without posting |
| `--force` | post even if one already went out today |

## References

- [Firehose | Bluesky docs](https://docs.bsky.app/docs/advanced-guides/firehose)
- [Introducing Jetstream | Bluesky blog](https://docs.bsky.app/blog/jetstream)
- [bluesky-social/jetstream (GitHub)](https://github.com/bluesky-social/jetstream)
- [AT Protocol](https://atproto.com/)
