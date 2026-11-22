"""Discord approval: REST cards/command fallback, namespaced Gateway interactions.

Edits only save. Persistent card/revision binding and durable posting claims
prevent replay; uncertain Bluesky writes are never automatically retried.
"""
import asyncio
import json
import logging
import os
import re
import signal
import sys
import hashlib
import secrets
import time
import discord as discord_sdk


def revision(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def components(queue_id, text, disabled=False, control_revision=None):
    return [{'type': 1, 'components': [
        {'type': 2, 'style': style, 'label': label, 'disabled': disabled,
         'custom_id': f'moodful:v1:{label.lower()}:{queue_id}:{control_revision or revision(text)}'}
        for label, style in [('Approve', 3), ('Edit', 1), ('Skip', 4)]
    ]}]


def card_text(queue_id, ask_text, draft_text, post_url, confidence):
    return (f'Moodful draft #{queue_id} (confidence {confidence})\n'
            f'Original ask:\n{ask_text[:600]}\n<{post_url}>\n\n'
            f'Draft reply:\n{draft_text[:500]}\n\n'
            'Use the buttons below. Edit saves ONLY; approve separately.\n'
            f'Fallback: `!moodful approve {queue_id}` / '
            f'`!moodful edit {queue_id} replacement text` / `!moodful skip {queue_id}`')

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .bsky import BskyClient, BskyError
from .store import Store
from . import replies

HELP = ('Use Approve / Edit / Skip on each draft card. Edit opens a prefilled form and saves ONLY; approve separately.\n'
        'Fallback commands (replace ID with the draft number):\n'
        '`!moodful pending` — show pending drafts\n'
        '`!moodful approve ID` — post that draft to Bluesky\n'
        '`!moodful edit ID replacement text` — save only; then approve separately\n'
        '`!moodful skip ID` — discard that draft\n'
        '`!moodful help` — show this help. Only the configured reviewer can act.')


class ApprovalBskyClient(BskyClient):
    """Approval writes must never repeat a possibly accepted createRecord."""
    def _create_record_with_retries(self, collection, record):
        return self._create_record(collection, record)


class DiscordError(RuntimeError):
    def __init__(self, message, retry_after=5):
        super().__init__(message)
        self.retry_after = retry_after


class DiscordBot:
    def __init__(self, token, channel_id, allowed_user_id):
        if not token or not str(channel_id).isdigit() or not str(allowed_user_id).isdigit():
            raise ValueError('Discord token, channel ID and allowed user ID are required')
        self.token = token
        self.channel_id = str(channel_id)
        self.allowed_user_id = str(allowed_user_id)
        self.guild_id = os.environ.get('DISCORD_GUILD_ID', '1520609353772695602')
        self.bot_id = None

    def _call(self, method, path, payload=None):
        req = Request('https://discord.com/api/v10' + path,
                      data=None if payload is None else json.dumps(payload).encode(),
                      headers={'Authorization': 'Bot ' + self.token,
                               'Content-Type': 'application/json',
                               'User-Agent': 'MoodfulApprovalRelay/1.0'}, method=method)
        try:
            with urlopen(req, timeout=15) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except HTTPError as exc:
            # Never retry POST here, and never log response bodies/headers.
            delay = 5
            if exc.code == 429:
                try:
                    delay = max(5, min(3600, float(json.loads(exc.read(4096))['retry_after'])))
                except (ValueError, KeyError, TypeError, OSError):
                    pass
            raise DiscordError(f'Discord {method}: HTTP {exc.code}', retry_after=delay) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise DiscordError(f'Discord {method}: network/response error') from None

    def send_message(self, text, controls=None):
        payload = {'content': text[:2000], 'allowed_mentions': {'parse': [], 'replied_user': False},
                   'flags': 4, 'tts': False}
        if controls is not None:
            payload['components'] = controls
        return self._call('POST', f'/channels/{self.channel_id}/messages', payload)['id']

    def send_draft(self, queue_id, ask_text, draft_text, post_url, confidence, control_revision=None):
        return self.send_message(card_text(queue_id, ask_text, draft_text, post_url, confidence),
                                 components(queue_id, draft_text, control_revision=control_revision))

    def update_draft(self, message_id, row, disabled=False):
        return self._call('PATCH', f'/channels/{self.channel_id}/messages/{message_id}', {
            'content': card_text(row['id'], row['post_text'], row['draft_text'], row['post_url'], row['confidence']),
            'components': components(row['id'], row['draft_text'], disabled, dict(row).get('control_revision')),
            'allowed_mentions': {'parse': [], 'replied_user': False}, 'flags': 4,
        })

    def messages(self, after=None):
        query = '?limit=100' if after is not None else '?limit=1'
        if after is not None:
            query += '&after=' + after
        return self._call('GET', f'/channels/{self.channel_id}/messages' + query)

    def authorized(self, message):
        author = message.get('author') or {}
        return (str(message.get('channel_id')) == self.channel_id
                and str(author.get('id')) == self.allowed_user_id
                and not author.get('bot') and not message.get('webhook_id'))


class DiscordRelay:
    def __init__(self, bot, store, client_factory):
        self.bot, self.store, self.client_factory = bot, store, client_factory
        self.key = 'cursor:' + bot.channel_id
        self.lock = asyncio.Lock()
        store.conn.executescript('''
            CREATE TABLE IF NOT EXISTS discord_cards (
                channel TEXT, queue_id INTEGER, message_id TEXT NOT NULL,
                PRIMARY KEY(channel, queue_id), UNIQUE(channel, message_id));
            CREATE TABLE IF NOT EXISTS discord_rendered (
                channel TEXT, queue_id INTEGER, state TEXT,
                PRIMARY KEY(channel, queue_id));
            CREATE TABLE IF NOT EXISTS discord_versions (queue_id INTEGER PRIMARY KEY, version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS discord_interactions (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS discord_modals (
                token TEXT PRIMARY KEY, queue_id INTEGER, message_id TEXT,
                revision TEXT, expires REAL, used INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS discord_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS discord_notified (channel TEXT, queue_id INTEGER,
                PRIMARY KEY(channel, queue_id));
        ''')
        store.conn.commit()

    def cursor(self):
        row = self.store.conn.execute('SELECT value FROM discord_state WHERE key=?', (self.key,)).fetchone()
        return row[0] if row else None

    def checkpoint(self, value):
        self.store.conn.execute('INSERT OR REPLACE INTO discord_state VALUES (?,?)', (self.key, value))
        self.store.conn.commit()

    async def poll_once(self):
        cursor = self.cursor()
        messages = await asyncio.to_thread(self.bot.messages, cursor)
        if cursor is None:
            # First connection tails live; never execute historical commands.
            self.checkpoint(str(max((int(m['id']) for m in messages), default=0)))
            return
        for msg in sorted(messages, key=lambda m: int(m['id'])):
            if int(msg['id']) <= int(self.cursor()):
                continue
            # At-most-once command processing, even after a crash.
            self.checkpoint(str(msg['id']))
            await self.handle(msg)

    def card_id(self, queue_id):
        row = self.store.conn.execute('SELECT message_id FROM discord_cards WHERE channel=? AND queue_id=?',
                                      (self.bot.channel_id, queue_id)).fetchone()
        return row[0] if row else None

    def control_revision(self, row):
        version = self.store.conn.execute('SELECT version FROM discord_versions WHERE queue_id=?', (row['id'],)).fetchone()
        return revision(row['draft_text'] + ('\0' + str(version[0]) if version else ''))

    def card_row(self, row):
        return dict(row, control_revision=self.control_revision(row))

    def bind_card(self, queue_id, message_id):
        self.store.conn.execute('INSERT OR REPLACE INTO discord_cards VALUES (?,?,?)',
                                (self.bot.channel_id, queue_id, str(message_id)))
        self.store.conn.commit()

    async def show_draft(self, row):
        message_id = self.card_id(row['id'])
        if message_id:
            await asyncio.to_thread(self.bot.update_draft, message_id, self.card_row(row))
        else:
            message_id = await asyncio.to_thread(self.bot.send_draft, row['id'], row['post_text'],
                                                 row['draft_text'], row['post_url'], row['confidence'],
                                                 control_revision=self.control_revision(row))
            # A notification POST is not automatically retried by the transport.
            if isinstance(message_id, (str, int)):
                self.bind_card(row['id'], message_id)
        self.store.conn.execute('INSERT OR IGNORE INTO discord_notified VALUES (?,?)',
                                (self.bot.channel_id, row['id']))
        self.mark_rendered(row)

    def mark_rendered(self, row):
        self.store.conn.execute('INSERT OR REPLACE INTO discord_rendered VALUES (?,?,?)',
                                (self.bot.channel_id, row['id'], row['status'] + ':' + self.control_revision(row)))
        self.store.conn.commit()

    async def refresh_card(self, queue_id):
        message_id = self.card_id(queue_id)
        if message_id:
            row = self.store.conn.execute('SELECT * FROM queue WHERE id=?', (queue_id,)).fetchone()
            rendered = self.store.conn.execute('SELECT state FROM discord_rendered WHERE channel=? AND queue_id=?',
                                               (self.bot.channel_id, queue_id)).fetchone()
            if rendered and rendered[0] == row['status'] + ':' + self.control_revision(row):
                return
            await asyncio.to_thread(self.bot.update_draft, message_id, self.card_row(row), disabled=row['status'] != 'pending')
            self.mark_rendered(row)

    def interaction_binding(self, i, queue_id):
        message = i.message
        return (str(i.channel_id) == self.bot.channel_id
                and str(i.guild_id) == self.bot.guild_id
                and str(i.user.id) == self.bot.allowed_user_id and not i.user.bot
                and message is not None and str(message.channel.id) == self.bot.channel_id
                and str(message.author.id) == self.bot.bot_id and message.author.bot
                and not message.webhook_id and str(message.id) == self.card_id(queue_id))

    async def handle_interaction(self, i):
        custom_id = (i.data or {}).get('custom_id', '')
        if not isinstance(custom_id, str) or not custom_id.startswith('moodful:'):
            return  # Never acknowledge another application's controls.
        match = re.fullmatch(r'moodful:v1:(approve|edit|skip):([1-9][0-9]{0,17}):([0-9a-f]{16})', custom_id)
        modal_match = re.fullmatch(r'moodful:v1:save:([0-9a-f]{32})', custom_id)
        modal = None
        if modal_match and i.type.value == 5:
            modal = self.store.conn.execute('SELECT * FROM discord_modals WHERE token=?', (modal_match[1],)).fetchone()
            if modal and not modal['used'] and modal['expires'] >= time.time():
                action, queue_id, rev = 'save', modal['queue_id'], modal['revision']
            else:
                match = None
        elif match and i.type.value == 3 and (i.data or {}).get('component_type') == 2:
            action, queue_id, rev = match[1], int(match[2]), match[3]
        else:
            match = None
        if not ((match and i.type.value == 3) or (modal and not modal['used'] and modal['expires'] >= time.time())):
            await i.response.send_message('Invalid or expired Moodful control. No action taken.', ephemeral=True)
            return
        row = self.store.get_pending(queue_id)
        if (not row or not self.interaction_binding(i, queue_id) or self.control_revision(row) != rev
                or (modal and str(i.message.id) != modal['message_id'])):
            await i.response.send_message('Unauthorized or stale Moodful card. No action taken.', ephemeral=True)
            return
        if action == 'edit':
            # Opening a modal is itself the initial ACK. No network/lock wait first.
            claimed = self.store.conn.execute('INSERT OR IGNORE INTO discord_interactions VALUES (?)', (str(i.id),)).rowcount
            self.store.conn.commit()
            if not claimed:
                await i.response.send_message('Already handled.', ephemeral=True)
                return
            token = secrets.token_hex(16)
            self.store.conn.execute('INSERT INTO discord_modals(token,queue_id,message_id,revision,expires) VALUES (?,?,?,?,?)',
                                    (token, queue_id, str(i.message.id), rev, time.time() + 300))
            self.store.conn.commit()
            form = discord_sdk.ui.Modal(title=f'Edit draft #{queue_id} — save only', custom_id='moodful:v1:save:' + token, timeout=300)
            form.add_item(discord_sdk.ui.TextInput(label='Reply (save only; approve separately)', custom_id='draft',
                          style=discord_sdk.TextStyle.paragraph, default=row['draft_text'], min_length=1, max_length=300))
            await i.response.send_modal(form)
            return
        # ACK inside Discord's 3 second deadline BEFORE waiting on locks or Bluesky.
        await i.response.defer(ephemeral=True, thinking=True)
        async with self.lock:
            row = self.store.get_pending(queue_id)
            claimed = self.store.conn.execute('INSERT OR IGNORE INTO discord_interactions VALUES (?)', (str(i.id),)).rowcount
            self.store.conn.commit()
            if not claimed or not row or self.control_revision(row) != rev:
                await i.edit_original_response(content='Already handled or stale; nothing posted.')
                return
            if action == 'save':
                claimed = self.store.conn.execute('UPDATE discord_modals SET used=1 WHERE token=? AND used=0 AND expires>=?',
                                                  (modal_match[1], time.time())).rowcount
                self.store.conn.commit()
                fields = [field for group in i.data.get('components', []) for field in group.get('components', [])]
                if (not claimed or len(fields) != 1 or fields[0].get('custom_id') != 'draft'
                        or not isinstance(fields[0].get('value'), str) or not 1 <= len(fields[0]['value'].strip()) <= 300):
                    await i.edit_original_response(content='Invalid or expired edit. Nothing changed.')
                    return
                action, edited = 'edit', fields[0]['value'].strip()
            else:
                edited = None
            message = {'id': str(i.id), 'channel_id': self.bot.channel_id,
                       'author': {'id': self.bot.allowed_user_id, 'bot': False},
                       'content': f'!moodful {action} {queue_id}' + ((' ' + edited) if edited else '')}
            try:
                await self._handle(message)
            finally:
                await self.refresh_card(queue_id)
            await i.edit_original_response(content=('Saved ONLY. Approve separately to post.' if action == 'edit'
                                                     else 'Decision handled. See the thread for status.'))

    async def notify_pending(self):
        async with self.lock:
            for row in self.store.list_pending():
                seen = self.store.conn.execute('SELECT 1 FROM discord_notified WHERE channel=? AND queue_id=?',
                                               (self.bot.channel_id, row['id'])).fetchone()
                if not seen:
                    await self.show_draft(row)
            for card in self.store.conn.execute('SELECT queue_id FROM discord_cards WHERE channel=?',
                                                (self.bot.channel_id,)).fetchall():
                await self.refresh_card(card[0])

    async def handle(self, message):
        async with self.lock:
            await self._handle(message)
            match = re.fullmatch(r'!moodful (?:approve|skip) ([1-9][0-9]{0,17})', (message.get('content') or '').strip())
            if match and self.bot.authorized(message):
                await self.refresh_card(int(match[1]))

    async def _handle(self, message):
        if not self.bot.authorized(message):
            return
        text = (message.get('content') or '').strip()
        if text == '!moodful help':
            await asyncio.to_thread(self.bot.send_message, HELP)
            return
        if text == '!moodful pending':
            pending = self.store.list_pending()
            await asyncio.to_thread(self.bot.send_message, f'{len(pending)} draft(s) pending.')
            for row in pending[:10]:
                await self.show_draft(row)
            if len(pending) > 10:
                await asyncio.to_thread(self.bot.send_message, 'Showing first 10. Resolve these and request pending again.')
            return
        match = re.fullmatch(r'!moodful (approve|skip|edit) ([1-9][0-9]*)(?:\s+([\s\S]+))?', text)
        if not match:
            return
        action, raw_id, edited = match.groups()
        if (action == 'edit' and not edited) or (action != 'edit' and edited):
            return
        if len(raw_id) > 18:
            return
        row = self.store.get_pending(int(raw_id))
        if row is None:
            await asyncio.to_thread(self.bot.send_message, f'Draft #{raw_id} is missing or already handled. No action taken.')
            return
        if action == 'skip':
            self.store.mark_skipped(row['id'], row['did'], row['post_uri'], 'skipped via Discord by ' + self.bot.allowed_user_id)
            await asyncio.to_thread(self.bot.send_message, f'Draft #{raw_id} skipped.')
        elif action == 'edit':
            if not edited.strip() or len(edited) > replies.MAX_GRAPHEMES:
                await asyncio.to_thread(self.bot.send_message, 'Replacement must be 1–300 characters. Nothing changed.')
                return
            self.store.conn.execute("UPDATE queue SET draft_text=? WHERE id=? AND status='pending'", (edited.strip(), row['id']))
            self.store.conn.execute('INSERT INTO discord_versions VALUES (?,1) ON CONFLICT(queue_id) DO UPDATE SET version=version+1', (row['id'],))
            self.store.conn.commit()
            self.store.log('discord_edit', row['did'], row['post_uri'], 'reviewer=' + self.bot.allowed_user_id)
            await self.show_draft(self.store.get_pending(row['id']))
            await asyncio.to_thread(self.bot.send_message, f'Draft #{raw_id} saved, NOT posted. Approve separately to post.')
        else:
            if not row['cid']:
                await asyncio.to_thread(self.bot.send_message, f'Draft #{raw_id} missing cid. Not posted.')
                return
            try:
                client = await asyncio.to_thread(self.client_factory)
            except BskyError:
                await asyncio.to_thread(self.bot.send_message, 'Bluesky login failed; draft remains pending. Try later.')
                return
            claimed = self.store.conn.execute("UPDATE queue SET status='posting' WHERE id=? AND status='pending'", (row['id'],)).rowcount
            self.store.conn.commit()
            if not claimed:
                return
            self.store.log('discord_approve', row['did'], row['post_uri'], 'reviewer=' + self.bot.allowed_user_id + '; message=' + str(message['id']))
            ref = {'uri': row['post_uri'], 'cid': row['cid']}
            try:
                uri = await asyncio.to_thread(client.post_reply, row['draft_text'], ref, ref, replies.link_facets(row['draft_text']))
            except BskyError:
                self.store.log('discord_post_uncertain', row['did'], row['post_uri'], 'Manual reconciliation required; no automatic retry')
                await asyncio.to_thread(self.bot.send_message, f'Draft #{raw_id}: Bluesky result uncertain. Locked against retries; reconcile manually.')
                return
            self.store.mark_sent(row['id'], row['did'], row['post_uri'], uri)
            await asyncio.to_thread(self.bot.send_message, f'Draft #{raw_id} posted. {uri}')


class ApprovalGateway(discord_sdk.Client):
    """Independent minimal-intent session; no commands or global app edits."""
    def __init__(self, relay, status):
        # SDK HTTP warnings may include token-bearing webhook/interaction URLs.
        # This process uses sanitized lifecycle/error reports instead.
        for name in ['discord'] + [name for name in logging.Logger.manager.loggerDict if name.startswith('discord.')]:
            logging.getLogger(name).setLevel(logging.CRITICAL + 1)
        super().__init__(intents=discord_sdk.Intents.none(), max_messages=None,
                         allowed_mentions=discord_sdk.AllowedMentions.none())
        self.relay, self.report = relay, status
        self.interaction_tasks = set()

    async def on_ready(self):
        self.relay.bot.bot_id = str(self.user.id)
        self.report(f'Discord Gateway connected as bot {self.user.id}; intents=0; persistent Moodful controls ready')

    async def on_resumed(self):
        self.report('Discord Gateway resumed; persistent Moodful controls ready')

    async def on_disconnect(self):
        self.report('Discord Gateway disconnected; SDK reconnect/resume enabled')

    async def on_interaction(self, interaction):
        task = asyncio.current_task()
        self.interaction_tasks.add(task)
        try:
            await self.relay.handle_interaction(interaction)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never print SDK exceptions: URLs can embed interaction tokens.
            self.report(f'Moodful interaction failed ({type(exc).__name__}); no automatic approval retry')
        finally:
            self.interaction_tasks.discard(task)

    async def close(self):
        tasks = list(self.interaction_tasks - {asyncio.current_task()})
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await super().close()

    async def on_error(self, event_method, *args, **kwargs):
        self.report('Discord Gateway event failed; payload suppressed')


async def run(args):
    from .cli import _classify_and_enqueue
    from .jetstream import post_events
    def status(text):
        print('moodful-relay: ' + text, file=sys.stderr, flush=True)
    bot = DiscordBot(os.environ.get('DISCORD_BOT_TOKEN'), os.environ.get('DISCORD_CHANNEL_ID'),
                     os.environ.get('DISCORD_ALLOWED_USER_ID'))
    store = Store(args.db)
    def client_factory():
        return ApprovalBskyClient(os.environ['BSKY_IDENTIFIER'], os.environ['BSKY_APP_PASSWORD'], pds=args.pds)
    relay = DiscordRelay(bot, store, client_factory)
    gateway = ApprovalGateway(relay, status)
    stop = asyncio.Event()
    async def gateway_connection():
        try:
            await gateway.start(bot.token, reconnect=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise DiscordError(f'Discord Gateway stopped ({type(exc).__name__}); details suppressed') from None
    async def producer():
        async for event in post_events(args.endpoint, stop, on_status=status):
            _classify_and_enqueue(event, store, args.lang, args.min_confidence)
    async def consumer():
        initialized = False
        while not stop.is_set():
            delay = 5
            try:
                await relay.poll_once()
                if not initialized:
                    status(f'Discord thread {bot.channel_id} polling ready; reviewer {bot.allowed_user_id}; approval required')
                    initialized = True
                    await asyncio.to_thread(bot.send_message, 'Moodful relay online — human approval required.\n' + HELP)
                await relay.notify_pending()
            except DiscordError as exc:
                delay = exc.retry_after
                status(f'{exc}; retrying in {delay}s')
            await asyncio.sleep(delay)
    tasks = [asyncio.create_task(producer()), asyncio.create_task(consumer()),
             asyncio.create_task(gateway_connection())]
    def shutdown():
        stop.set()
        for task in tasks:
            task.cancel()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, shutdown)
    status(f'Discord relay starting for thread {bot.channel_id}; Telegram disabled')
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await gateway.close()
        store.close()
