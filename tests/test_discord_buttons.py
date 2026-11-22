"""All approvals here use temporary SQLite and fake Bluesky, never live credentials."""
import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
import test_discord_relay


class ButtonTests(unittest.TestCase):
    tearDown = test_discord_relay.DiscordTests.tearDown

    def setUp(self):
        test_discord_relay.DiscordTests.setUp(self)
        self.bot.bot_id = '777'
        self.bot.guild_id = '789'
        self.bot.update_draft = Mock(return_value='999')
        self.bot._call = Mock(return_value={'id':'999'})
        self.bot.send_draft = Mock(return_value='999')

    def interaction(self, action='approve', **kwargs):
        from moodful_responder.discord import components
        ids = {b['label'].lower(): b['custom_id'] for b in components(self.q, 'draft')[0]['components']}
        i = NS(id=10001, channel_id=123, guild_id=789, user=NS(id=456, bot=False),
               message=NS(id=999, channel=NS(id=123), author=NS(id=777, bot=True), webhook_id=None),
               data={'custom_id':ids[action], 'component_type':2}, type=NS(value=3),
               response=NS(defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock()),
               edit_original_response=AsyncMock())
        for k,v in kwargs.items(): setattr(i,k,v)
        return i

    async def bind(self):
        await self.relay.show_draft(self.store.get_pending(self.q))

    def test_buttons_payload(self):
        from moodful_responder.discord import components
        buttons = components(self.q, 'draft')[0]['components']
        self.assertEqual([x['label'] for x in buttons], ['Approve','Edit','Skip'])
        self.assertTrue(all(x['type']==2 and x['custom_id'].startswith('moodful:v1:') for x in buttons))
        self.assertTrue(all(x['disabled'] for x in components(self.q,'draft',True)[0]['components']))
        self.assertNotEqual(buttons, components(self.q,'edited')[0]['components'])

    def test_button_acks_before_post_and_replay_is_durable(self):
        async def scenario():
            await self.bind()
            i=self.interaction()
            self.client.post_reply.side_effect=lambda *a: (self.assertTrue(i.response.defer.await_count), 'at://posted')[1]
            await self.relay.handle_interaction(i)
            from moodful_responder.discord import DiscordRelay
            restarted=DiscordRelay(self.bot,self.store,lambda:self.client)
            await restarted.handle_interaction(i)
            await restarted.handle_interaction(self.interaction(id=10002))
            self.client.post_reply.assert_called_once()
            self.assertTrue(self.bot.update_draft.call_args.kwargs['disabled'])
        asyncio.run(scenario())

    def test_edit_modal_prefills_and_saves_only_then_explicit_approve(self):
        async def scenario():
            await self.bind()
            i=self.interaction('edit')
            await self.relay.handle_interaction(i)
            modal=i.response.send_modal.call_args.args[0]
            self.assertEqual(str(modal.children[0].default),'draft')
            submit=self.interaction('edit',id=10002,type=NS(value=5),data={'custom_id':modal.custom_id,'components':[{'type':1,'components':[{'type':4,'custom_id':'draft','value':'replacement'}]}]})
            await self.relay.handle_interaction(submit)
            self.assertEqual(self.store.get_pending(self.q)['draft_text'],'replacement')
            self.client.post_reply.assert_not_called()
            await self.relay.handle_interaction(submit)
            # Old revision cannot approve after an edit.
            await self.relay.handle_interaction(self.interaction(id=10003))
            self.client.post_reply.assert_not_called()
            from moodful_responder.discord import components
            approve=self.interaction(id=10004)
            approve.data['custom_id']=components(self.q,'replacement',control_revision=self.relay.control_revision(self.store.get_pending(self.q)))[0]['components'][0]['custom_id']
            await self.relay.handle_interaction(approve)
            self.assertEqual(self.client.post_reply.call_args.args[0],'replacement')
        asyncio.run(scenario())

    def test_interaction_strict_author_channel_guild_and_card_binding(self):
        async def scenario():
            await self.bind()
            bad=[{'user':NS(id=457,bot=False)}, {'user':NS(id=456,bot=True)}, {'channel_id':124}, {'guild_id':790},
                 {'message':NS(id=998,channel=NS(id=123),author=NS(id=777,bot=True),webhook_id=None)},
                 {'message':NS(id=999,channel=NS(id=123),author=NS(id=778,bot=True),webhook_id=None)},
                 {'message':NS(id=999,channel=NS(id=123),author=NS(id=777,bot=True),webhook_id=2)},
                 {'data':{'custom_id':'moodful:v1:approve:01:bad'}}, {'type':NS(value=2)}]
            for index,kw in enumerate(bad):
                await self.relay.handle_interaction(self.interaction(id=20000+index,**kw))
            self.client.post_reply.assert_not_called()
            self.assertEqual(self.store.get_pending(self.q)['draft_text'],'draft')
        asyncio.run(scenario())

    def test_skip_disables_card_and_never_posts(self):
        async def scenario():
            await self.bind()
            await self.relay.handle_interaction(self.interaction('skip'))
            self.assertIsNone(self.store.get_pending(self.q))
            self.assertTrue(self.bot.update_draft.call_args.kwargs['disabled'])
            self.client.post_reply.assert_not_called()
        asyncio.run(scenario())

    def test_card_updates_in_place_and_survives_restart(self):
        async def scenario():
            await self.bind()
            from moodful_responder.discord import DiscordRelay
            restarted=DiscordRelay(self.bot,self.store,lambda:self.client)
            await restarted.show_draft(self.store.get_pending(self.q))
            self.bot.send_draft.assert_called_once()
            self.bot.update_draft.assert_called_once()
            await restarted.handle_interaction(self.interaction('skip'))
            self.assertIsNone(self.store.get_pending(self.q))
        asyncio.run(scenario())

    def test_simultaneous_approvals_post_once(self):
        async def scenario():
            await self.bind()
            await asyncio.gather(*(self.relay.handle_interaction(self.interaction(id=10001+n)) for n in range(5)))
            self.client.post_reply.assert_called_once()
        asyncio.run(scenario())

    def test_failed_ack_never_posts(self):
        async def scenario():
            await self.bind()
            i=self.interaction(); i.response.defer.side_effect=TimeoutError()
            with self.assertRaises(TimeoutError): await self.relay.handle_interaction(i)
            self.client.post_reply.assert_not_called()
            self.assertIsNotNone(self.store.get_pending(self.q))
        asyncio.run(scenario())

    def test_uncertain_interaction_never_retries(self):
        from moodful_responder.bsky import BskyError
        async def scenario():
            await self.bind()
            self.client.post_reply.side_effect=BskyError('uncertain')
            await self.relay.handle_interaction(self.interaction())
            await self.relay.handle_interaction(self.interaction(id=10002))
            self.client.post_reply.assert_called_once()
            self.assertEqual(self.store.conn.execute('SELECT status FROM queue').fetchone()[0],'posting')
        asyncio.run(scenario())

    def test_unknown_namespace_is_not_acknowledged(self):
        async def scenario():
            i=self.interaction(data={'custom_id':'hermes:other'})
            await self.relay.handle_interaction(i)
            i.response.defer.assert_not_awaited();i.response.send_message.assert_not_awaited()
        asyncio.run(scenario())

    def test_ack_does_not_wait_for_busy_approval_lock(self):
        async def scenario():
            await self.bind()
            i=self.interaction()
            async with self.relay.lock:
                task=asyncio.create_task(self.relay.handle_interaction(i))
                await asyncio.sleep(.01)
                i.response.defer.assert_awaited_once()
                self.client.post_reply.assert_not_called()
            await task
        asyncio.run(scenario())

    def test_modal_survives_restart_but_expires_durably(self):
        async def scenario():
            await self.bind()
            i=self.interaction('edit');await self.relay.handle_interaction(i)
            modal=i.response.send_modal.call_args.args[0]
            self.store.conn.execute('UPDATE discord_modals SET expires=0');self.store.conn.commit()
            from moodful_responder.discord import DiscordRelay
            restarted=DiscordRelay(self.bot,self.store,lambda:self.client)
            submit=self.interaction(id=11001,type=NS(value=5),data={'custom_id':modal.custom_id,'components':[{'components':[{'custom_id':'draft','value':'not saved'}]}]})
            await restarted.handle_interaction(submit)
            self.assertEqual(self.store.get_pending(self.q)['draft_text'],'draft')
            self.client.post_reply.assert_not_called()
        asyncio.run(scenario())

    def test_resolved_card_refresh_retries_after_network_failure(self):
        from moodful_responder.discord import DiscordError
        async def scenario():
            await self.bind()
            self.bot.update_draft.side_effect=[DiscordError('offline'),{}]
            with self.assertRaises(DiscordError):
                await self.relay.handle_interaction(self.interaction('skip'))
            await self.relay.notify_pending()
            self.assertEqual(self.bot.update_draft.call_count,2)
            self.assertTrue(self.bot.update_draft.call_args.kwargs['disabled'])
        asyncio.run(scenario())

    def test_gateway_uses_no_privileged_intents_and_dispatches_namespace(self):
        from moodful_responder.discord import ApprovalGateway
        async def scenario():
            status=Mock()
            gateway=ApprovalGateway(self.relay,status)
            self.assertFalse(gateway.intents.message_content)
            self.assertFalse(gateway.intents.members)
            self.assertEqual(gateway.intents.value,0)
            self.relay.handle_interaction=AsyncMock()
            i=self.interaction()
            await gateway.on_interaction(i)
            self.relay.handle_interaction.assert_awaited_once_with(i)
            await gateway.close()
        asyncio.run(scenario())

    def test_rest_rate_limit_preserves_retry_after_without_retrying_write(self):
        from urllib.error import HTTPError
        from io import BytesIO
        from unittest.mock import patch
        from moodful_responder.discord import DiscordError
        error=HTTPError('https://discord.com',429,'limited',{},BytesIO(b'{"retry_after": 9.5}'))
        self.bot._call=type(self.bot)._call.__get__(self.bot)
        with patch('moodful_responder.discord.urlopen',side_effect=error) as request:
            with self.assertRaises(DiscordError) as caught:
                self.bot._call('POST','/channels/123/messages',{'content':'safe'})
            request.assert_called_once()
            self.assertEqual(caught.exception.retry_after,9.5)

    def test_cancelled_approval_locks_uncertain_write_on_restart(self):
        import threading
        async def scenario():
            await self.bind()
            started, release=threading.Event(),threading.Event()
            def slow_post(*args):
                started.set();release.wait(5);return 'at://accepted'
            self.client.post_reply.side_effect=slow_post
            task=asyncio.create_task(self.relay.handle_interaction(self.interaction()))
            await asyncio.to_thread(started.wait,2)
            self.assertTrue(started.is_set())
            task.cancel()
            try:
                with self.assertRaises(asyncio.CancelledError):await task
            finally:release.set()
            from moodful_responder.discord import DiscordRelay
            restarted=DiscordRelay(self.bot,self.store,lambda:self.client)
            await restarted.handle_interaction(self.interaction(id=10002))
            self.assertEqual(self.store.conn.execute('SELECT status FROM queue').fetchone()[0],'posting')
            self.client.post_reply.assert_called_once()
        asyncio.run(scenario())

    def test_editing_back_to_same_text_still_invalidates_old_approval(self):
        async def scenario():
            await self.bind()
            old=self.interaction()
            message={'id':'1003','channel_id':'123','author':{'id':'456','bot':False},'content':f'!moodful edit {self.q} replacement'}
            await self.relay.handle(message)
            message['id']='1004';message['content']=f'!moodful edit {self.q} draft'
            await self.relay.handle(message)
            await self.relay.handle_interaction(old)
            self.client.post_reply.assert_not_called()
        asyncio.run(scenario())

    def test_edit_before_first_card_uses_current_revision(self):
        async def scenario():
            message={'id':'1003','channel_id':'123','author':{'id':'456','bot':False},'content':f'!moodful edit {self.q} replacement'}
            await self.relay.handle(message)
            self.assertEqual(self.bot.send_draft.call_args.kwargs['control_revision'],self.relay.control_revision(self.store.get_pending(self.q)))
        asyncio.run(scenario())

    def test_gateway_close_cancels_inflight_handlers_before_store_close(self):
        from moodful_responder.discord import ApprovalGateway
        async def scenario():
            started=asyncio.Event()
            async def blocked(i):
                started.set();await asyncio.Event().wait()
            self.relay.handle_interaction=blocked
            gateway=ApprovalGateway(self.relay,Mock())
            task=asyncio.create_task(gateway.on_interaction(self.interaction()))
            await started.wait()
            await gateway.close()
            try:
                self.assertTrue(task.done())
            finally:
                task.cancel();await asyncio.gather(task,return_exceptions=True)
        asyncio.run(scenario())

    def test_real_sdk_interaction_payload_dispatch_without_registered_views(self):
        import discord
        from unittest.mock import patch
        from moodful_responder.discord import ApprovalGateway, components
        async def scenario():
            await self.bind()
            gateway=ApprovalGateway(self.relay,Mock())
            raw={'id':'10001','type':3,'application_id':'777','token':'synthetic-test-token','version':1,
                 'guild_id':'789','attachment_size_limit':1000000,
                 'channel':{'id':'123','type':11,'parent_id':'122','owner_id':'456','name':'test-thread',
                            'message_count':1,'member_count':1,'thread_metadata':{'archived':False,'auto_archive_duration':1440,'archive_timestamp':'2026-01-01T00:00:00+00:00','locked':False}},
                 'member':{'user':{'id':'456','username':'reviewer','discriminator':'0','avatar':None},'roles':[],'joined_at':'2026-01-01T00:00:00+00:00','flags':0},
                 'data':{'component_type':2,'custom_id':components(self.q,'draft')[0]['components'][2]['custom_id']},
                 'message':{'id':'999','channel_id':'123','author':{'id':'777','username':'bot','bot':True,'discriminator':'0','avatar':None},
                            'content':'test card','timestamp':'2026-01-01T00:00:00+00:00','edited_timestamp':None,'tts':False,'mention_everyone':False,
                            'mentions':[],'mention_roles':[],'attachments':[],'embeds':[],'pinned':False,'type':0,'flags':4}}
            gateway._connection.user=discord.ClientUser(state=gateway._connection,data={'id':'777','username':'bot','bot':True,'discriminator':'0','avatar':None})
            i=discord.Interaction(data=raw,state=gateway._connection)
            self.assertEqual(str(i.message.channel.id),'123')
            i._cs_response=NS(defer=AsyncMock(),send_message=AsyncMock(),send_modal=AsyncMock())
            with patch.object(discord.Interaction,'edit_original_response',new_callable=AsyncMock):
                await gateway.on_interaction(i)
            self.assertIsNone(self.store.get_pending(self.q))
            self.client.post_reply.assert_not_called()
            await gateway.close()
        asyncio.run(scenario())

    def test_sdk_http_logging_cannot_leak_interaction_token_urls(self):
        import logging
        from moodful_responder.discord import ApprovalGateway
        async def scenario():
            gateway=ApprovalGateway(self.relay,Mock())
            for name in ['discord.http','discord.webhook.async_','discord.client','discord.gateway','discord.ui.modal']:
                self.assertFalse(logging.getLogger(name).isEnabledFor(logging.WARNING),name)
            await gateway.close()
        asyncio.run(scenario())

    def test_invalid_or_expired_modal_does_not_save(self):
        async def scenario():
            await self.bind()
            i=self.interaction('edit');await self.relay.handle_interaction(i)
            modal=i.response.send_modal.call_args.args[0]
            for n,text in enumerate(['','x'*301]):
                submit=self.interaction(id=11000+n,type=NS(value=5),data={'custom_id':modal.custom_id,'components':[{'components':[{'custom_id':'draft','value':text}]}]})
                await self.relay.handle_interaction(submit)
            self.assertEqual(self.store.get_pending(self.q)['draft_text'],'draft')
            self.client.post_reply.assert_not_called()
        asyncio.run(scenario())
