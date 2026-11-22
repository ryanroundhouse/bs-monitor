import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import URLError

from moodful_responder.store import Store
from moodful_responder.bsky import BskyError


class DiscordTests(unittest.TestCase):
    def setUp(self):
        from moodful_responder.discord import DiscordBot, DiscordRelay
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'queue.db')
        self.store = Store(self.db)
        self.bot = DiscordBot('secret', '123', '456')
        self.bot.send_message = Mock(return_value='999')
        self.bot.update_draft = Mock(return_value='999')
        self.client = Mock()
        self.client.post_reply.return_value = 'at://did:plc:us/app.bsky.feed.post/test'
        self.relay = DiscordRelay(self.bot, self.store, lambda: self.client)
        self.q = self.store.enqueue(dict(did='did:plc:ask', rkey='a', cid='cid', uri='at://ask/a', url='https://example.org', text='ask', draft_text='draft', confidence=.9))

    def tearDown(self):
        if hasattr(self, 'store'):
            self.store.close()
            self.tmp.cleanup()

    def message(self, text, **kwargs):
        m = dict(id='1000', channel_id='123', author=dict(id='456', bot=False), content=text)
        m.update(kwargs)
        return m

    def handle(self, text, **kwargs):
        asyncio.run(self.relay.handle(self.message(text, **kwargs)))

    def test_approve_posts_exact_draft_once(self):
        self.handle(f'!moodful approve {self.q}')
        self.handle(f'!moodful approve {self.q}')
        self.client.post_reply.assert_called_once()
        self.assertEqual(self.client.post_reply.call_args.args[0], 'draft')
        self.assertIsNone(self.store.get_pending(self.q))

    def test_edit_requires_separate_approval(self):
        self.handle(f'!moodful edit {self.q} replacement text')
        self.client.post_reply.assert_not_called()
        self.assertEqual(self.store.get_pending(self.q)['draft_text'], 'replacement text')
        self.handle(f'!moodful approve {self.q}', id='1001')
        self.assertEqual(self.client.post_reply.call_args.args[0], 'replacement text')

    def test_skip_is_terminal(self):
        self.handle(f'!moodful skip {self.q}')
        self.handle(f'!moodful approve {self.q}')
        self.client.post_reply.assert_not_called()
        self.assertIsNone(self.store.get_pending(self.q))

    def test_unauthorized_author_channel_and_bot_ignored(self):
        for extra in [dict(author=dict(id='789')), dict(channel_id='124'), dict(author=dict(id='456', bot=True)), dict(webhook_id='1')]:
            self.handle(f'!moodful approve {self.q}', **extra)
        self.client.post_reply.assert_not_called()
        self.bot.send_message.assert_not_called()

    def test_explicit_matching_ids_only(self):
        for text in ['approve', '!moodful approve', '!moodful approve -1', '!moodful approve 01', '!moodful approve 9999', f'!moodful approve {self.q} extra', f'!moodful edit {self.q}', f'!moodful edit {self.q} ' + 'x'*301]:
            self.handle(text)
        self.client.post_reply.assert_not_called()
        self.assertEqual(self.store.get_pending(self.q)['draft_text'], 'draft')

    def test_missing_cid_never_posts(self):
        self.store.conn.execute('UPDATE queue SET cid=NULL'); self.store.conn.commit()
        self.handle(f'!moodful approve {self.q}')
        self.client.post_reply.assert_not_called()

    def test_uncertain_post_not_retried_on_restart(self):
        self.client.post_reply.side_effect = BskyError('timeout')
        self.handle(f'!moodful approve {self.q}')
        from moodful_responder.discord import DiscordRelay
        self.relay = DiscordRelay(self.bot, self.store, lambda: self.client)
        self.handle(f'!moodful approve {self.q}', id='1001')
        self.client.post_reply.assert_called_once()
        self.assertEqual(self.store.conn.execute('SELECT status FROM queue').fetchone()[0], 'posting')

    def test_client_login_failure_keeps_pending(self):
        self.relay.client_factory = Mock(side_effect=BskyError('login timeout'))
        self.handle(f'!moodful approve {self.q}')
        self.assertIsNotNone(self.store.get_pending(self.q))
        self.client.post_reply.assert_not_called()

    def test_pending_republishes_existing_drafts(self):
        self.bot.send_draft = Mock()
        self.handle('!moodful pending')
        self.bot.send_draft.assert_called_once()
        self.assertEqual(self.bot.send_draft.call_args.args[0], self.q)

    def test_cursor_bootstrap_and_restart_no_replay(self):
        self.bot.messages = Mock(return_value=[self.message(f'!moodful approve {self.q}')])
        asyncio.run(self.relay.poll_once())
        self.client.post_reply.assert_not_called()
        self.assertEqual(self.relay.cursor(), '1000')
        self.bot.messages.return_value = [self.message(f'!moodful skip {self.q}', id='1001')]
        asyncio.run(self.relay.poll_once())
        self.assertIsNone(self.store.get_pending(self.q))
        from moodful_responder.discord import DiscordRelay
        restarted = DiscordRelay(self.bot, self.store, lambda: self.client)
        self.assertEqual(restarted.cursor(), '1001')
        asyncio.run(restarted.poll_once())
        self.client.post_reply.assert_not_called()

    def test_cursor_claim_precedes_processing(self):
        self.bot.messages = Mock(return_value=[])
        asyncio.run(self.relay.poll_once())
        self.bot.messages.return_value = [self.message('!moodful help')]
        async def failing(msg):
            self.assertEqual(self.relay.cursor(), '1000')
            raise RuntimeError('crash')
        self.relay.handle = failing
        with self.assertRaises(RuntimeError):
            asyncio.run(self.relay.poll_once())
        self.assertEqual(self.relay.cursor(), '1000')

    def test_rest_payload_no_mentions_or_previews(self):
        self.bot.send_message = type(self.bot).send_message.__get__(self.bot)
        self.bot._call = Mock(return_value={'id':'77'})
        self.assertEqual(self.bot.send_message('@everyone https://example.org'), '77')
        payload = self.bot._call.call_args.args[2]
        self.assertEqual(payload['allowed_mentions'], {'parse': [], 'replied_user': False})
        self.assertEqual(payload['flags'] & 4, 4)
        self.bot.send_draft(12, 'ask', 'draft', 'url', .9)
        self.assertIn('!moodful approve 12', self.bot._call.call_args.args[2]['content'])

    def test_http_timeout_normalized_without_token_leak(self):
        from moodful_responder.discord import DiscordError
        with patch('moodful_responder.discord.urlopen', side_effect=URLError('network')):
            with self.assertRaises(DiscordError) as ctx:
                self.bot._call('GET', '/users/@me')
        self.assertNotIn('secret', str(ctx.exception))

    def test_bluesky_transport_does_not_retry_uncertain_writes(self):
        from moodful_responder.discord import ApprovalBskyClient
        client = ApprovalBskyClient.__new__(ApprovalBskyClient)
        client._create_record = Mock(side_effect=BskyError('502 Bad Gateway'))
        with self.assertRaises(BskyError):
            client.post_reply('draft', {'uri':'at://post', 'cid':'cid'})
        client._create_record.assert_called_once()

    def test_unknown_transport_fails_closed(self):
        from moodful_responder.cli import _relay
        with patch.dict('os.environ', {'RELAY_TRANSPORT':'discorD-typo', 'BSKY_IDENTIFIER':'', 'BSKY_APP_PASSWORD':''}), patch('moodful_responder.cli.TelegramBot') as tg:
            with self.assertRaises(ValueError):
                asyncio.run(_relay(type('Args', (), {'db': self.db})()))
            tg.assert_not_called()

    def test_startup_network_failure_retries_without_bluesky_login(self):
        from moodful_responder.discord import run, DiscordBot, DiscordError
        from types import SimpleNamespace
        async def events(*args, **kwargs):
            await asyncio.Event().wait()
            yield None
        count = [0]
        gateway_started = []
        async def gateway_start(instance, token, **kwargs):
            gateway_started.append(token == 'secret')
            await asyncio.Event().wait()
        real_sleep = asyncio.sleep
        async def sleep(seconds):
            count[0] += 1
            if count[0] >= 2:
                raise asyncio.CancelledError()
            await real_sleep(0)
        with patch.dict('os.environ', {'DISCORD_BOT_TOKEN':'secret', 'DISCORD_CHANNEL_ID':'123', 'DISCORD_ALLOWED_USER_ID':'456'}), patch.object(DiscordBot, 'messages', side_effect=[DiscordError('offline'), []]) as messages, patch.object(DiscordBot, 'send_message', return_value='9'), patch('moodful_responder.jetstream.post_events', events), patch('moodful_responder.discord.BskyClient') as client, patch('moodful_responder.discord.asyncio.sleep', sleep), patch('moodful_responder.discord.ApprovalGateway.start', gateway_start):
            asyncio.run(run(SimpleNamespace(db=self.db, pds='unused', endpoint='us-east-2', lang='en', min_confidence=.6)))
            self.assertEqual(messages.call_count, 2)
            self.assertEqual(gateway_started, [True])
            client.assert_not_called()

    def test_notify_pending_is_durable(self):
        from moodful_responder.discord import DiscordRelay
        self.bot.send_draft = Mock()
        asyncio.run(self.relay.notify_pending())
        restarted = DiscordRelay(self.bot, self.store, lambda: self.client)
        asyncio.run(restarted.notify_pending())
        self.bot.send_draft.assert_called_once()

    def test_relay_selects_discord_without_constructing_telegram(self):
        from moodful_responder.cli import _relay
        with patch.dict('os.environ', {'RELAY_TRANSPORT':'discord'}), patch('moodful_responder.discord.run', new_callable=__import__('unittest.mock',fromlist=['AsyncMock']).AsyncMock) as run, patch('moodful_responder.cli.TelegramBot') as tg:
            asyncio.run(_relay(Mock()))
            run.assert_awaited_once()
            tg.assert_not_called()

if __name__ == '__main__':
    unittest.main()
