"""Explicit Deep Think, signed Discord anchors, quota and existing chat gates."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from cogs.ia import IA, ChannelJobQueue
from tests import test_conversation_memory as fixtures


class DeepThinkCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.ConversationRuntimeTest()
        await self.fixture.asyncSetUp()
        self.engine, self.db = self.fixture.engine, self.fixture.db
        self.fixture.cog.conversation = self.engine
        self.db.set_ai_config(10, ai_web_enabled=1, ai_deep_think=1, ai_deep_think_daily_limit=1)

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def interaction(self, request_id=900, anchor_id=500):
        anchor = self.fixture.message(anchor_id, user_id=999)
        anchor.webhook_id = 999
        anchor.interaction_metadata = SimpleNamespace(id=request_id, user=self.fixture.members[30],
            type=discord.InteractionType.application_command)
        async def edit(**kwargs):
            anchor.content = kwargs["content"]
            return anchor
        return SimpleNamespace(id=request_id, guild_id=10, guild=self.fixture.guild, user=self.fixture.members[30],
            channel_id=20, channel=self.fixture.channels[20],
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()), edit_original_response=AsyncMock(side_effect=edit)), anchor

    async def enqueue(self, request_id=900, anchor_id=500):
        interaction, anchor = self.interaction(request_id, anchor_id)
        self.assertTrue(await self.engine.enqueue_deepthink(interaction, "Compara las versiones de Example"))
        return self.fixture.cog._queue.get_nowait(), anchor

    async def test_registered_command_uses_explicit_job_and_preserves_real_anchor_attribution(self):
        interaction, anchor = self.interaction()
        await IA.deepthink.callback(self.fixture.cog, interaction, "Compara las versiones de Example")
        job = self.fixture.cog._queue.get_nowait()
        self.assertTrue(job["deep_think"])
        self.assertEqual(job["command_interaction_id"], interaction.id)
        self.assertEqual(job["message"].author.id, 30)
        self.assertEqual(anchor.author.id, 999)
        self.assertFalse(interaction.edit_original_response.await_args.kwargs["allowed_mentions"].users)
        stored = self.db.get_ai_chat_turns(10, 20)
        self.assertEqual(stored[0]["author_id"], 999)
        self.assertTrue(stored[0]["is_bot"])
        self.assertIn("/deepthink", stored[0]["content"])
        self.assertEqual(stored[0]["requester_id"], 30)
        await self.engine.listen(anchor)
        self.assertTrue(self.fixture.cog._queue.empty())

    async def test_duplicate_interaction_never_enqueues_twice(self):
        first, _ = self.interaction()
        second, _ = self.interaction(anchor_id=501)
        self.assertTrue(await self.engine.enqueue_deepthink(first, "Consulta"))
        self.assertFalse(await self.engine.enqueue_deepthink(second, "Consulta"))
        self.assertEqual(self.fixture.cog._queue.qsize(), 1)

    async def test_chat_restrictions_and_disabled_command_do_not_reserve(self):
        for kind in ("disabled", "bot", "restricted_role", "global_role", "web_off"):
            with self.subTest(kind=kind):
                self.db.set_ai_config(10, ai_deep_think=1, ai_web_enabled=1, ai_restricted_role_ids=[])
                self.db.set_config(10, message_role_enabled=0)
                self.fixture.members[30].bot = False
                if kind == "disabled":
                    self.db.set_ai_config(10, ai_deep_think=0)
                elif kind == "bot":
                    self.fixture.members[30].bot = True
                elif kind == "restricted_role":
                    self.db.set_ai_config(10, ai_restricted_role_ids=["10"])
                elif kind == "global_role":
                    self.db.set_config(10, message_role_enabled=1, message_role_id=444)
                else:
                    self.db.set_ai_config(10, ai_web_enabled=0)
                interaction, _ = self.interaction()
                with patch.object(self.db, "reserve_ai_deep_think", wraps=self.db.reserve_ai_deep_think) as reserve:
                    self.assertFalse(await self.engine.enqueue_deepthink(interaction, "Consulta"))
                reserve.assert_not_called()
                self.assertTrue(self.fixture.cog._queue.empty())

    async def test_forged_changed_or_deleted_anchor_cannot_reach_provider_and_is_refunded(self):
        for index, kind in enumerate(("author", "request_id", "user", "content", "deleted")):
            with self.subTest(kind=kind):
                job, anchor = await self.enqueue(900 + index, 500 + index)
                if kind == "author":
                    anchor.author = self.fixture.members[31]
                elif kind == "request_id":
                    anchor.interaction_metadata.id = 777
                elif kind == "user":
                    anchor.interaction_metadata.user = self.fixture.members[31]
                elif kind == "content":
                    anchor.content = "Changed query"
                else:
                    discord.TextChannel.fetch_message.side_effect = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")
                with patch("cogs._conversation.request_completion", new=AsyncMock()) as provider, patch.object(
                        self.db, "refund_ai_deep_think", wraps=self.db.refund_ai_deep_think) as refund:
                    await self.engine.process(job)
                provider.assert_not_awaited()
                refund.assert_called_once_with(10, 30, 900 + index)
                discord.TextChannel.fetch_message.side_effect = lambda mid: self.fixture.messages[mid]

    async def test_queue_full_and_provider_error_refund_without_a_completed_answer(self):
        self.fixture.cog._queue = asyncio.Queue(maxsize=1)
        self.fixture.cog._queue.put_nowait({"occupied": True})
        interaction, _ = self.interaction()
        with patch.object(self.db, "refund_ai_deep_think", wraps=self.db.refund_ai_deep_think) as refund:
            self.assertFalse(await self.engine.enqueue_deepthink(interaction, "Consulta"))
        refund.assert_called_once_with(10, 30, 900)
        self.fixture.cog._queue.get_nowait()
        second, anchor = self.interaction(901, 501)
        self.assertTrue(await self.engine.enqueue_deepthink(second, "Consulta"))
        job = self.fixture.cog._queue.get_nowait()
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=TimeoutError())), patch.object(
                self.db, "refund_ai_deep_think", wraps=self.db.refund_ai_deep_think) as refund:
            await self.engine.process(job)
        refund.assert_called_once_with(10, 30, 901)
        self.assertNotIn("_deep_answer_sent", job)

    async def test_success_keeps_quota_and_followup_sees_verified_research_with_bot_anchor_author(self):
        job, anchor = await self.enqueue()
        web = {"assessment": {"sufficient": True, "facts": [], "gaps": []}, "sources": [],
            "verified_source_urls": [], "complete": True, "pages_read": 1}
        intent = {"tool_calls": [{"id": "intent", "type": "function", "function": {
            "name": "request_public_web_research", "arguments": "{}"}}]}
        resolved = fixtures.web_selection("Example", job["message"].content)
        with patch.object(self.engine, "web_context", new=AsyncMock(return_value=web)), patch(
                "cogs._conversation.request_completion", new=AsyncMock(side_effect=[intent, resolved, "Explicación."])), patch.object(
                self.db, "refund_ai_deep_think", wraps=self.db.refund_ai_deep_think) as refund:
            await self.engine.process(job)
        refund.assert_not_called()
        self.assertTrue(job["_deep_answer_sent"])
        self.assertEqual(anchor.author.id, 999)
        text = anchor.reply.await_args_list[0].args[0]
        self.assertIn("<@30>", text)
        self.assertFalse(anchor.reply.await_args_list[0].kwargs["allowed_mentions"].replied_user)
        followup = self.fixture.message(600, reference=SimpleNamespace(message_id=anchor.id, guild_id=10, channel_id=20), ping=True)
        await self.engine.listen(followup)
        with patch("cogs._conversation.request_completion", new=AsyncMock(return_value="Seguimiento.")) as provider:
            await self.engine.process(self.fixture.cog._queue.get_nowait())
        context = json.loads(provider.await_args.args[2][1]["content"])
        self.assertIn("research_notes_untrusted", context)
        self.assertEqual(context["referenced_message_untrusted"]["author_id"], "999")

    async def test_cancelled_command_before_first_answer_refunds(self):
        job, _ = await self.enqueue()
        entered = asyncio.Event()
        async def pending(*_, **__):
            entered.set()
            await asyncio.Event().wait()
        with patch("cogs._conversation.request_completion", new=pending), patch.object(
                self.db, "refund_ai_deep_think", wraps=self.db.refund_ai_deep_think) as refund:
            task = asyncio.create_task(self.engine.process(job))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        refund.assert_called_once_with(10, 30, 900)

    async def test_header_does_not_add_the_requester_as_a_second_member_target(self):
        interaction, _ = self.interaction()
        self.assertTrue(await self.engine.enqueue_deepthink(interaction, "¿Qué permisos tiene <@31>?"))
        job = self.fixture.cog._queue.get_nowait()
        self.assertEqual(self.engine.explicit_moderation_target(job["message"], require_explicit=True), 31)

    async def test_fresh_role_revocation_blocks_a_queued_command_and_refunds(self):
        role = discord.Role(guild=self.fixture.guild, state=SimpleNamespace(), data={"id": "44", "name": "Access",
            "permissions": "0", "position": 1})
        self.fixture.guild._roles[44] = role
        self.fixture.guild.fetch_roles.return_value = [self.fixture.role, role]
        member = self.fixture.members[30]
        member.roles.append(role)
        member._roles = discord.utils.SnowflakeList([10, 44])
        self.db.set_config(10, message_role_enabled=1, message_role_id=44)
        job, _ = await self.enqueue()
        member.roles = [self.fixture.role]
        member._roles = discord.utils.SnowflakeList([10])
        with patch("cogs._conversation.request_completion", new=AsyncMock()) as provider, patch.object(
                self.db, "refund_ai_deep_think", wraps=self.db.refund_ai_deep_think) as refund:
            await self.engine.process(job)
        provider.assert_not_awaited()
        refund.assert_called_once_with(10, 30, 900)

    async def test_graceful_unload_refunds_a_command_still_in_the_queue(self):
        cog = self.fixture.cog
        cog._queue = ChannelJobQueue(maxsize=4)
        cog._workers, cog._http = [], None
        interaction, _ = self.interaction()
        self.assertTrue(await self.engine.enqueue_deepthink(interaction, "Consulta"))
        with patch.object(self.db, "refund_ai_deep_think", wraps=self.db.refund_ai_deep_think) as refund:
            await IA.cog_unload(cog)
        refund.assert_called_once_with(10, 30, 900)
        self.assertTrue(cog._queue.empty())
        await asyncio.wait_for(cog._queue.join(), 1)

    async def test_delivery_failure_after_the_first_chunk_does_not_refund_used_work(self):
        job, anchor = await self.enqueue()
        sent = anchor.reply.return_value
        failed = discord.HTTPException(SimpleNamespace(status=500, reason="Error"), "send failed")
        anchor.reply.side_effect = [sent, failed, failed]
        with patch("cogs._conversation.request_completion", new=AsyncMock(return_value="x" * 2500)), patch.object(
                self.db, "refund_ai_deep_think", wraps=self.db.refund_ai_deep_think) as refund:
            await self.engine.process(job)
        self.assertTrue(job["_deep_answer_sent"])
        refund.assert_not_called()

    async def test_research_failure_still_delivers_grounded_answer_with_saved_checkpoint(self):
        from cogs._llm_moderation import EndpointIncompleteResponse
        from tests.test_conversation_memory import research_report, research_fact
        job, anchor = await self.enqueue()
        url = "https://official.example/version"
        self.engine.web = SimpleNamespace(close=AsyncMock(), read_webpage=AsyncMock(return_value={"url": url, "text": "Release 9 is current."}))
        intent = {"tool_calls": [{"id": "intent", "type": "function", "function": {
            "name": "request_public_web_research", "arguments": "{}"}}]}
        resolved = fixtures.web_selection("Example", job["message"].content)
        replies = [intent, resolved, {"tool_calls": [self.fixture.tool_call("read_webpage", url)]},
                   research_report([research_fact(url, "Release 9 is current.")], gaps=["Performance remains unknown"]),
                   TimeoutError(), EndpointIncompleteResponse("length"), "La versión documentada es 9; la mejora de rendimiento no está verificada."]
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)) as provider:
            await self.engine.process(job)
        text = anchor.reply.await_args_list[0].args[0]
        self.assertIn("La versión documentada es 9", text)
        self.assertNotIn("No pude completar", text)
        final_context = json.loads(provider.await_args.args[2][1]["content"])
        self.assertFalse(final_context["web_research_untrusted"]["complete"])
        self.assertIn("Release 9 is current.", json.dumps(final_context))
        self.assertEqual(provider.await_args.kwargs["reasoning_effort"], "none")
        self.assertEqual(provider.await_args.kwargs["timeout_seconds"], 60)
        self.assertTrue(job["_deep_answer_sent"])
        self.assertIsNotNone(self.engine.research.load(self.engine.research_key(job), self.engine.research_token(job)))
