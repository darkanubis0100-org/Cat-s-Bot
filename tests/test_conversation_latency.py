"""Mocked call-count and ordering regressions, never production speed claims."""
import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from api.routes.channel_settings import ChannelSettings
from cogs._conversation import Conversation, endpoint_config
from tests import test_conversation_memory as fixtures


class ConversationLatencyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.ConversationRuntimeTest()
        await self.fixture.asyncSetUp()
        self.engine, self.db = self.fixture.engine, self.fixture.db
        self.fixture.cog.bot.fetch_guild = AsyncMock(return_value=SimpleNamespace(id=10, owner_id=1))

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def counts(self):
        f = self.fixture
        return {"roles": f.guild.fetch_roles.await_count, "channels": f.guild.fetch_channel.await_count,
                "members": f.guild.fetch_member.await_count, "owner": f.cog.bot.fetch_guild.await_count,
                "messages": discord.TextChannel.fetch_message.await_count}

    async def test_global_deep_availability_does_not_enable_deep_for_normal_messages(self):
        self.db.set_ai_config(10, ai_deep_think=1)
        message = self.fixture.message(ping=True)
        await self.engine.listen(message)
        job = self.fixture.cog._queue.get_nowait()
        with patch("cogs._conversation.request_completion", new=AsyncMock(return_value={"role": "assistant", "content": "Hola"})) as provider:
            await self.engine.process(job)
        self.assertFalse(job.get("deep_think", False))
        self.assertEqual(provider.await_args.kwargs["max_output_tokens"], 1000)
        self.assertNotIn("Deep Think está activo", provider.await_args.args[2][0]["content"])

    async def test_control_stays_small_and_web_reserves_16k_without_overriding_selectors(self):
        self.db.set_automod_config(10, rules={"llm": {"endpoint_url": "https://provider.example/v1", "model": "strata",
            "chat_reasoning_effort": "high", "web_reasoning_effort": "high"}})
        await self.engine.listen(self.fixture.message(ping=True))
        job = self.fixture.cog._queue.get_nowait()
        with patch("cogs._conversation.request_completion", new=AsyncMock(return_value={"role": "assistant", "content": "Fixture"})) as provider:
            await self.engine.generate(job, [{"role": "system", "content": "control"}], internal=True)
            self.assertEqual(provider.await_args.kwargs["reasoning_effort"], "none")
            self.assertEqual(provider.await_args.kwargs["max_output_tokens"], 512)
            await self.engine.generate(job, [{"role": "system", "content": "quick research"}], task="web")
            self.assertEqual(provider.await_args.kwargs["reasoning_effort"], "high")
            self.assertEqual(provider.await_args.kwargs["max_output_tokens"], 16384)
            self.assertEqual(self.engine.budget_config(job, "web")["output_reserve"], 16384)
            await self.engine.generate(job, [{"role": "system", "content": "final"}])
            self.assertEqual(provider.await_args.kwargs["reasoning_effort"], "high")
            self.assertEqual(provider.await_args.kwargs["max_output_tokens"], 16384)
            job["deep_think"] = True
            await self.engine.generate(job, [{"role": "system", "content": "deep research"}], task="web")
            self.assertEqual(provider.await_args.kwargs["reasoning_effort"], "high")

    async def test_passive_capture_and_gateway_gates_do_not_consume_discord_rest(self):
        f = self.fixture
        await self.engine.listen(f.message())
        self.assertEqual(len(self.db.get_ai_chat_turns(10, 20)), 1)
        self.assertEqual(sum(self.counts().values()), 0)
        self.assertTrue(f.cog._queue.empty())
        self.db.clear_ai_memory(10, "chat", 20)
        self.db.set_ai_config(10, ai_enabled=0)
        await self.engine.listen(f.message(101))
        self.db.set_ai_config(10, ai_enabled=1)
        self.db.set_config(10, message_role_enabled=1, message_role_id=444)
        await self.engine.listen(f.message(102))
        self.db.set_config(10, message_role_enabled=0)
        local = ChannelSettings().model_dump()
        local["role"].update(mode="override", role_ids=["444"])
        self.db.set_channel_settings_batch(10, [20], local)
        await self.engine.listen(f.message(103))
        self.assertEqual(self.db.get_ai_chat_turns(10, 20), [])
        self.assertEqual(sum(self.counts().values()), 0)

    async def test_local_role_policy_change_before_dispatch_still_blocks_provider(self):
        f = self.fixture
        message = f.message(ping=True)
        await self.engine.listen(message)
        local = ChannelSettings().model_dump()
        local["role"].update(mode="override", role_ids=["444"])
        self.db.set_channel_settings_batch(10, [20], local)
        with patch("cogs._conversation.request_completion", new=AsyncMock(return_value="Must not export")) as provider:
            await self.engine.process(f.cog._queue.get_nowait())
        provider.assert_not_awaited()
        message.reply.assert_not_awaited()

    async def test_preparation_overlaps_owner_roles_sources_and_cold_model_metadata(self):
        f = self.fixture
        source = f.message(85, user_id=31)
        message = f.message(ping=True)
        message.content = "https://discord.com/channels/10/20/85"
        await self.engine.listen(message)
        self.engine.session = object()
        gate, all_started = asyncio.Event(), asyncio.Event()
        started = set()
        async def wait(name, value):
            started.add(name)
            if {"roles", "source", "metadata"} <= started:
                all_started.set()
            await gate.wait()
            return value
        async def roles(): return await wait("roles", [f.role])
        async def fetch(message_id):
            return await wait("source", source) if message_id == 85 else f.messages[message_id]
        async def metadata(*args, **kwargs):
            return await wait("metadata", {"data": [{"id": "strata", "meta": {"n_ctx": 262144}}]})
        f.guild.fetch_roles.side_effect = roles
        discord.TextChannel.fetch_message.side_effect = fetch
        async def provider(*args, **kwargs):
            self.assertGreaterEqual(f.guild.fetch_roles.await_count, 2)
            self.assertEqual(sum(call.args == (85,) for call in discord.TextChannel.fetch_message.await_args_list), 2)
            return {"role": "assistant", "content": "Fixture"}
        with patch("cogs._conversation_budget.endpoint_json", new=AsyncMock(side_effect=metadata)) as metadata_call, patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=provider)) as model:
            job = asyncio.create_task(self.engine.process(f.cog._queue.get_nowait()))
            try:
                await asyncio.wait_for(all_started.wait(), 2)
                model.assert_not_awaited()
            finally:
                gate.set()
            await job
        model.assert_awaited_once()
        metadata_call.assert_awaited_once()

    async def test_cached_reply_chain_is_discovered_without_fetch_and_refetched_at_each_egress(self):
        f = self.fixture
        ancestor = f.message(85, user_id=31)
        target = f.message(90, user_id=999, reference=SimpleNamespace(message_id=85, guild_id=10, channel_id=20, resolved=ancestor))
        message = f.message(reference=SimpleNamespace(message_id=90, guild_id=10, channel_id=20, resolved=target))
        await self.engine.listen(message)
        self.assertEqual(sum(self.counts().values()), 0)
        async def provider(*args, **kwargs):
            for message_id in (85, 90):
                self.assertEqual(sum(call.args == (message_id,) for call in discord.TextChannel.fetch_message.await_args_list), 1)
            return {"role": "assistant", "content": "Fixture"}
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=provider)) as model:
            await self.engine.process(f.cog._queue.get_nowait())
        model.assert_awaited_once()
        for message_id in (85, 90):
            self.assertEqual(sum(call.args == (message_id,) for call in discord.TextChannel.fetch_message.await_args_list), 2)

    async def test_cached_source_edit_or_delete_before_first_dispatch_cannot_reach_provider(self):
        f = self.fixture
        actual = f.message(85, user_id=31)
        stale = copy.copy(actual)
        stale.content = "Stale Gateway hint"
        message = f.message(ping=True, reference=SimpleNamespace(message_id=85, guild_id=10, channel_id=20, resolved=stale))
        await self.engine.listen(message)
        with patch("cogs._conversation.request_completion", new=AsyncMock(return_value="Must not export")) as provider:
            await self.engine.process(f.cog._queue.get_nowait())
        provider.assert_not_awaited()
        message.reply.assert_not_awaited()
        second = f.message(101, ping=True, reference=SimpleNamespace(message_id=85, guild_id=10, channel_id=20, resolved=actual))
        await self.engine.listen(second)
        def removed(message_id):
            if message_id == 85:
                raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "removed")
            return f.messages[message_id]
        discord.TextChannel.fetch_message.side_effect = removed
        with patch("cogs._conversation.request_completion", new=AsyncMock(return_value="Must not export")) as provider:
            await self.engine.process(f.cog._queue.get_nowait())
        provider.assert_not_awaited()
        second.reply.assert_not_awaited()

    async def test_in_place_gateway_reference_edit_after_enqueue_keeps_original_fingerprint(self):
        f = self.fixture
        target = f.message(90, user_id=999)
        message = f.message(reference=SimpleNamespace(message_id=90, guild_id=10, channel_id=20, resolved=target))
        await self.engine.listen(message)
        job = f.cog._queue.get_nowait()
        target.content = "Gateway updated the same cached object after enqueue"
        with patch("cogs._conversation.request_completion", new=AsyncMock(return_value="Must not export changed reference")) as provider:
            await self.engine.process(job)
        provider.assert_not_awaited()
        message.reply.assert_not_awaited()

    async def test_reference_editback_cannot_export_prepared_bytes_from_intermediate_edit(self):
        f = self.fixture
        target = f.message(90, user_id=999)
        original_server = copy.copy(target)
        message = f.message(reference=SimpleNamespace(message_id=90, guild_id=10, channel_id=20, resolved=target))
        await self.engine.listen(message)
        job = f.cog._queue.get_nowait()
        target.content = "Intermediate edited bytes must not be exported"
        fit = self.engine.budget.fit
        async def edit_back(*args, **kwargs):
            f.messages[90] = original_server
            return await fit(*args, **kwargs)
        with patch.object(self.engine.budget, "fit", new=edit_back), patch("cogs._conversation.request_completion", new=AsyncMock(return_value="Must not export intermediate reference")) as provider:
            await self.engine.process(job)
        provider.assert_not_awaited()
        message.reply.assert_not_awaited()

    async def test_queue_full_feedback_still_checks_fresh_authority(self):
        f = self.fixture
        f.cog._queue = asyncio.Queue(maxsize=1)
        f.cog._queue.put_nowait({"occupied": True})
        message = f.message(ping=True)
        await self.engine.listen(message)
        self.assertEqual(f.guild.fetch_roles.await_count, 1)
        self.assertEqual(discord.TextChannel.fetch_message.await_count, 1)
        message.reply.assert_awaited_once()

    async def test_missing_endpoint_feedback_cannot_bypass_a_new_role_gate(self):
        f = self.fixture
        self.db.set_automod_config(10, rules={"llm": {"enabled": False, "endpoint_url": "", "model": ""}})
        message = f.message(ping=True)
        await self.engine.listen(message)
        self.db.set_config(10, message_role_enabled=1, message_role_id=444)
        await self.engine.process(f.cog._queue.get_nowait())
        message.reply.assert_not_awaited()

    async def test_failed_authorization_cancels_parallel_discovery_and_metadata(self):
        f = self.fixture
        f.message(85, user_id=31)
        message = f.message(ping=True)
        message.content = "https://discord.com/channels/10/20/85"
        await self.engine.listen(message)
        self.engine.session = object()
        source_started, metadata_started = asyncio.Event(), asyncio.Event()
        source_cancelled, metadata_cancelled = asyncio.Event(), asyncio.Event()
        async def roles():
            await source_started.wait()
            await metadata_started.wait()
            raise ValueError("failed_fixture_snapshot")
        async def fetch(message_id):
            if message_id != 85:
                return f.messages[message_id]
            source_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                source_cancelled.set()
        async def metadata(*args, **kwargs):
            metadata_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                metadata_cancelled.set()
        f.guild.fetch_roles.side_effect = roles
        discord.TextChannel.fetch_message.side_effect = fetch
        with patch("cogs._conversation_budget.endpoint_json", new=AsyncMock(side_effect=metadata)), patch("cogs._conversation.request_completion", new=AsyncMock()) as provider:
            await asyncio.wait_for(self.engine.process(f.cog._queue.get_nowait()), 2)
        provider.assert_not_awaited()
        self.assertTrue(source_cancelled.is_set())
        self.assertTrue(metadata_cancelled.is_set())

    async def test_first_dispatch_metric_includes_uncached_reply_lookup_before_enqueue(self):
        f = self.fixture
        target = f.message(90, user_id=999)
        message = f.message(reference=SimpleNamespace(message_id=90, guild_id=10, channel_id=20))
        clock = [100.0]
        first_lookup = True
        def fetch(message_id):
            nonlocal first_lookup
            if message_id == 90 and first_lookup:
                first_lookup = False
                clock[0] = 105.0
            elif message_id == 100:
                clock[0] = 107.0
            return f.messages[message_id]
        discord.TextChannel.fetch_message.side_effect = fetch
        with patch("cogs._conversation.monotonic", side_effect=lambda: clock[0]):
            await self.engine.listen(message)
            job = f.cog._queue.get_nowait()
            self.assertEqual(job["queued_at"], 105)
            self.assertEqual(job["listen_started"], 100)
            with self.assertLogs("cogs._conversation", level="INFO") as logs, patch("cogs._conversation.request_completion", new=AsyncMock(return_value={"role": "assistant", "content": "Fixture"})):
                await self.engine.process(job)
        log = " ".join(logs.output)
        self.assertIn("listen_s=5.000", log)
        self.assertIn("queue_s=0.000", log)
        self.assertIn("first_dispatch_s=7.000", log)
        self.assertNotIn("hola", log)
        self.assertNotIn("secret", log)
        self.assertNotIn("https://", log)

    async def test_parallel_phase_timing_does_not_subtract_unrelated_tasks(self):
        clock = [0.0]
        gate, started = asyncio.Event(), asyncio.Event()
        job = {}
        async def slow_budget():
            started.set()
            await gate.wait()
        async def authorization():
            clock[0] = 3.0
        with patch("cogs._conversation.monotonic", side_effect=lambda: clock[0]):
            budget = asyncio.create_task(Conversation.timed(job, "budget", slow_budget()))
            await started.wait()
            clock[0] = 1.0
            await Conversation.timed(job, "authorization", authorization())
            clock[0] = 5.0
            gate.set()
            await budget
        self.assertEqual(job["_timings"]["authorization"], 2.0)
        self.assertEqual(job["_timings"]["budget"], 5.0)


class DispatchCallCountTest(unittest.IsolatedAsyncioTestCase):
    async def case_counts(self, case):
        f = fixtures.ConversationRuntimeTest()
        await f.asyncSetUp()
        try:
            f.cog.bot.fetch_guild = AsyncMock(return_value=SimpleNamespace(id=10, owner_id=1))
            f.engine.session = object()
            with patch("cogs._conversation_budget.endpoint_json", new=AsyncMock(return_value={"data": [{"id": "strata", "meta": {"n_ctx": 262144}}]})) as metadata:
                if case == "warm_mention":
                    await f.engine.budget.window(f.engine.session, endpoint_config(f.db, 10))
                    metadata.reset_mock()
                if case == "privileged":
                    f.configure_distinct_permission_subjects()
                ref = None
                if case == "reply":
                    ancestor = f.message(85, user_id=31)
                    target = f.message(90, user_id=999, reference=SimpleNamespace(message_id=85, channel_id=20, guild_id=10, resolved=ancestor))
                    ref = SimpleNamespace(message_id=90, channel_id=20, guild_id=10, resolved=target)
                message = f.message(ping=case not in {"reply", "passive"}, reference=ref)
                if case == "privileged":
                    message.content = "Qué roles y permisos tiene <@31>"
                def count():
                    return sum((f.guild.fetch_roles.await_count, f.guild.fetch_channel.await_count,
                        f.guild.fetch_member.await_count, f.cog.bot.fetch_guild.await_count, discord.TextChannel.fetch_message.await_count))
                dispatched = None
                async def provider(*args, **kwargs):
                    nonlocal dispatched
                    dispatched = count()
                    return {"role": "assistant", "content": "Fixture"}
                await f.engine.listen(message)
                enqueued = count()
                with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=provider)) as model:
                    if not f.cog._queue.empty():
                        await f.engine.process(f.cog._queue.get_nowait())
                return enqueued, dispatched, count(), metadata.await_count, model.await_count
        finally:
            await f.asyncTearDown()

    async def test_same_fixtures_reduce_discord_calls_without_more_model_calls(self):
        # Before-patch baseline captured with the SAME budget fastpath already enabled.
        baseline = {"cold_mention": 18, "warm_mention": 18, "reply": 25, "privileged": 26}
        expected = {"cold_mention": 12, "warm_mention": 12, "reply": 14, "privileged": 20}
        for case in (*expected, "passive"):
            with self.subTest(case=case):
                enqueued, dispatched, final, metadata, model_calls = await self.case_counts(case)
                self.assertEqual(enqueued, 0)
                if case == "passive":
                    self.assertEqual((dispatched, final, metadata, model_calls), (None, 0, 0, 0))
                else:
                    self.assertEqual(dispatched, expected[case])
                    self.assertLess(dispatched, baseline[case])
                    self.assertEqual(model_calls, 1)
                    self.assertEqual(metadata, 0 if case == "warm_mention" else 1)
