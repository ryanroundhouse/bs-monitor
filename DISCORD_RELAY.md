# Discord approval relay

The deployed `bs-monitor-relay.service` serves thread `1574041693139902568`
(parent `1520609462853959680`, guild `1520609353772695602`). Only human reviewer
`190970180073947136` can act. Bot/application ID: `1520609659856359536`.

## Buttons (primary)

Every pending card has **Approve / Edit / Skip**:

- **Approve** posts exactly the current reviewed draft to Bluesky.
- **Edit** opens a modal prefilled with the current reply. Submit **saves only**,
  updates the same card, and requires a separate Approve to post. Limit: 1–300
  Unicode code points. Modal lifetime: five minutes; canceling saves nothing.
- **Skip** permanently discards the draft. Resolved/uncertain cards disable all
  buttons. Failed card PATCHes are retried by reconciliation without posting.

Controls are bound to the configured reviewer, guild, thread, bot's own non-webhook
message, durable queue/message mapping, and current revision. Every saved edit
increments a durable version, including edits back to identical text; old controls
and modal submissions cannot approve or overwrite newer revisions. Interactions
are durably deduplicated. Modal nonces, expiry and consumption survive restarts.
The generic Gateway handler does not depend on in-memory View registration.

Approval and modal-save interactions defer ephemerally **before** waiting for
locks or any Bluesky/network operation. Edit's modal response is itself the ACK.
Failed acknowledgements never post. State-changing decisions and notification
updates are serialized. SQLite's conditional `pending -> posting` claim remains
the final write gate. Success becomes `sent`; timeout, cancellation or uncertain
write stays `posting`. **Never reset an uncertain row to pending without manual
Bluesky reconciliation.** Approval writes have no automatic createRecord retry.

## Text-command fallback (not slash commands)

- `!moodful help` — help.
- `!moodful pending` — count and first ten pending cards; existing cards update
  in place rather than duplicating them.
- `!moodful approve ID` — approve a specific pending draft.
- `!moodful edit ID replacement text` — save only, then approve separately.
- `!moodful skip ID` — discard that draft.

Use the draft number, not the Discord message ID. Free-form text, other users,
bots, webhooks, and other channels are ignored. Cursor claims are durable and
precede processing; the first-ever connection skips historical commands. Restarts
may process commands arriving after the saved cursor during downtime.

## Architecture / shared-bot safety

REST polling remains for fallback commands and card delivery. A separate
`discord.py==2.7.1` Client receives interactions over the Gateway with **intents=0**
(no guild/message/member/presence subscriptions). The SDK handles heartbeat,
reconnect/resume, HTTP rate limits and interaction callbacks. REST requests time
out after 15 seconds; the consumer honors bounded HTTP 429 `retry_after` and
retries read/card synchronization failures. Discord POSTs are not retried inside
the REST transport; uncertain Bluesky writes are never retried anywhere.

Only `moodful:v1:*` controls are handled; other namespaces are not acknowledged.
Hermes' existing registered Views use different custom IDs, and its adapter has
no generic interaction catch-all. This app does **not** modify Hermes, register
slash commands, or change the shared application's global interaction endpoint.
Deployment verified that endpoint is unset. An HTTP interactions endpoint would
prevent Gateway delivery; do not set one for this shared app without coordination.

Official references consulted:
- Discord developer docs: `interactions/receiving-and-responding` (Gateway versus
  HTTP delivery are mutually exclusive; initial ACK deadline is three seconds).
- discord.py API docs: `discord.on_interaction`, `Client.start(reconnect=True)`,
  `Intents.none`, and the installed SDK's interaction parser / modal dispatch.

## Configuration / operation

Private app `.env` retains existing Bluesky settings and:

```dotenv
RELAY_TRANSPORT=discord
DISCORD_CHANNEL_ID=1574041693139902568
DISCORD_ALLOWED_USER_ID=190970180073947136
# DISCORD_GUILD_ID defaults to 1520609353772695602
# DISCORD_BOT_TOKEN is private; never print or paste it.
```

The bot needs View Channel, Read Message History, Send Messages in Threads, and
Message Content access for REST command fallback. The thread must be accessible.
No Bluesky login occurs until approval. Mentions and embeds are disabled on
notifications. Error logging suppresses SDK exception details and payloads (which
can contain interaction tokens). Only restart the app service, never Hermes.

```sh
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
systemctl --user status bs-monitor-relay.service
journalctl --user -u bs-monitor-relay.service -n 40 --no-pager
```

Additional SQLite tables: `discord_cards` (message binding), `discord_versions`
(edit generation), `discord_rendered` (card reconciliation), `discord_interactions`
(replay claims), `discord_modals` (single-use expiring forms). Existing
`discord_state` and `discord_notified` remain intact. Migration binds and PATCHes
existing pending cards in place; no replacement card is sent for these drafts.

## Button rollback (keep Discord commands; do not restore old database)

Private pre-button backup directory (mode 700):
`/home/ryan/bs-monitor-backups/buttons-20261122-080113/`.
It includes the complete preexisting dirty files, private `.env`, requirements,
service unit and consistent `responder.db`. A stopped-service snapshot is saved as
`responder.pre-deploy.db`. Preserve the **live** DB: restoring an old snapshot would
lose approvals and could re-enable already-sent replies.

```sh
systemctl --user stop bs-monitor-relay.service
cd /home/ryan/bs-monitor
B=/home/ryan/bs-monitor-backups/buttons-20261122-080113
.venv/bin/python "$B/disable_buttons.py"
cp "$B/moodful_responder/discord.py" moodful_responder/discord.py
cp "$B/requirements.txt" requirements.txt
cp "$B/README.md" README.md
cp "$B/DISCORD_RELAY.md" DISCORD_RELAY.md
systemctl --user start bs-monitor-relay.service
systemctl --user status bs-monitor-relay.service
```

The rollback helper removes components from exact bound messages and reads each
back. It does not change draft status or send Bluesky writes. Restoring only the
runtime module and requirements is sufficient; the installed SDK and additive DB
tables can remain. Test files in the backup can be restored for the old suite;
`tests/test_discord_buttons.py` is new to this deployment. No git reset, commit or
push. To deliberately restore the earlier Telegram transport instead, consult
the older runbook in the pre-button backup; that is a separate rollback.

## Verification scope

Unit tests use temporary SQLite databases, synthetic interactions, and fake
Bluesky clients. They exercise approvals, save-only modal edits, replay/restart,
revision invalidation, unauthorized/forged inputs, concurrent approvals,
acknowledgement ordering/failure, cancellation/uncertain-write locks, durable
notifications and fallback routing. Live checks read REST bot/app/thread identity,
PATCH and GET exact pending messages, verify labels/custom IDs and enabled state,
and check the service's authenticated Gateway connection. Deployment does not
simulate Ryan or make any real Bluesky reply. A real authorized human click and
modal submission remain a manual end-to-end verification step. Daily posting and
like scheduling are untouched.

### Deployment evidence

The full suite passed **76 tests** (54 existing + 22 button regressions). The live
service authenticated its Gateway as bot `1520609659856359536` with `intents=0`;
verified deployed PID `19893`, `NRestarts=0`, started November 22, 2026 at 08:25:17
EST. Re-check current state rather than assuming the PID remains unchanged.

All ten original pending cards were updated and individually read back, with no
replacement messages, duplicate cards, queue decisions, or Bluesky writes:

| Draft | Original message ID (retained) |
|---|---|
| 4 | 1574043410711711936 |
| 24 | 1574043398871056407 |
| 35 | 1574043400242466867 |
| 36 | 1574043435978203247 |
| 40 | 1574043405577879646 |
| 41 | 1574043437164920873 |
| 42 | 1574043407976759297 |
| 44 | 1574043397642002564 |
| 47 | 1574043409247903786 |
| 55 | 1574043441225146479 |

Exact labels/custom IDs and post-start service verification are saved in
`buttons-verification.final.json` inside the private backup directory. Discord
returned HTTP 429 during migration; only idempotent PATCH/GET calls were retried
after the reported delay. No approval or write was retried. The rollback helper
was separately exercised using fake REST, a temporary DB and a simulated 429;
it was not run against live cards during verification.
