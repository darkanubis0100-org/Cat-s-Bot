"""Mocked call-count and ordering regressions, never production speed claims."""
import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from api.routes.channel_settings import ChannelSettings
from cogs._conversation import Conversation, current_channel_members, endpoint_config
from cogs._llm_moderation import fingerprint
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

    @staticmethod
    def rate_limited():
        return discord.HTTPException(SimpleNamespace(status=429, reason="Too Many Requests",
            headers={"Retry-After": "0.75"}), {"message": "Rate limited", "retry_after": 0.75})

    async def test_transient_429_before_send_revalidates_and_keeps_generated_answer(self):
        f = self.fixture
        message = f.message(ping=True)
        await self.engine.listen(message)
        reads = 0
        async def fetch(message_id):
            nonlocal reads
            reads += 1
            if reads == 3:
                raise self.rate_limited()
            return f.messages[message_id]
        discord.TextChannel.fetch_message.side_effect = fetch
        with patch("cogs._conversation.asyncio.sleep", new=AsyncMock()) as wait, patch(
                "cogs._conversation.request_completion", new=AsyncMock(return_value={"content": "La respuesta ya está preparada."})) as provider:
            await self.engine.process(f.cog._queue.get_nowait())
        wait.assert_awaited_once_with(0.75)
        provider.assert_awaited_once()
        self.assertEqual(message.reply.await_args.args[0], "La respuesta ya está preparada.")
        self.assertEqual(f.db.get_ai_chat_turns(10, 20)[-1]["content"], "Respuesta")

    async def test_429_wait_does_not_authorize_edited_deleted_or_revoked_source(self):
        f = self.fixture
        for change in ("edit", "delete", "revoke"):
            with self.subTest(change=change):
                f.role._permissions = discord.Permissions(view_channel=True, send_messages=True, read_message_history=True).value
                message = f.message(200 + len(f.messages), ping=True)
                await self.engine.listen(message)
                reads = 0
                async def fetch(message_id):
                    nonlocal reads
                    reads += 1
                    if reads == 3:
                        raise self.rate_limited()
                    if message_id not in f.messages:
                        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "deleted")
                    return f.messages[message_id]
                async def change_during_wait(seconds):
                    if change == "edit":
                        f.messages[message.id] = copy.copy(message)
                        f.messages[message.id].content = "Edited after generation"
                    elif change == "delete":
                        f.messages.pop(message.id)
                    else:
                        f.role._permissions = 0
                discord.TextChannel.fetch_message.side_effect = fetch
                before = len(f.db.get_ai_chat_turns(10, 20))
                with patch("cogs._conversation.asyncio.sleep", new=AsyncMock(side_effect=change_during_wait)), patch(
                        "cogs._conversation.request_completion", new=AsyncMock(return_value={"content": "Must be withheld"})):
                    await self.engine.process(f.cog._queue.get_nowait())
                message.reply.assert_not_awaited()
                self.assertEqual(len(f.db.get_ai_chat_turns(10, 20)), before)

    async def test_reply_chain_authorization_does_not_burst_message_gets(self):
        f = self.fixture
        ancestors = [f.message(message_id, user_id=31) for message_id in (82, 83, 84, 85)]
        target = f.message(90, user_id=999)
        message = f.message(reference=SimpleNamespace(message_id=90, guild_id=10, channel_id=20, resolved=target))
        await self.engine.listen(message)
        job = f.cog._queue.get_nowait()
        job["source_fingerprints"] = {source.id: fingerprint(source) for source in ancestors}
        active, peak, requested = 0, 0, []
        real_sleep = asyncio.sleep
        async def fetch(message_id):
            nonlocal active, peak
            requested.append(message_id)
            active += 1
            peak = max(peak, active)
            try:
                await real_sleep(0)
                return f.messages[message_id]
            finally:
                active -= 1
        discord.TextChannel.fetch_message.side_effect = fetch
        self.assertTrue(await self.engine.unchanged(job))
        self.assertEqual(set(requested), {82, 83, 84, 85, 90, 100})
        self.assertEqual(len(requested), 6)
        self.assertEqual(peak, 1)
        self.assertEqual(active, 0)

    async def test_failed_permission_snapshot_cancels_and_drains_other_rest_calls(self):
        f = self.fixture
        started, cancelled = set(), set()
        all_started = asyncio.Event()
        async def pending(label):
            started.add(label)
            if len(started) == 3:
                all_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.add(label)
        async def roles():
            await all_started.wait()
            raise self.rate_limited()
        f.guild.fetch_roles.side_effect = roles
        async def channel(channel_id): return await pending("channel")
        async def member(member_id): return await pending(str(member_id))
        f.guild.fetch_channel.side_effect = channel
        f.guild.fetch_member.side_effect = member
        with self.assertRaises(discord.HTTPException):
            await current_channel_members(f.guild, 20, (30, 999))
        self.assertEqual(cancelled, started)

    async def test_exhausted_429_does_not_launch_another_feedback_authorization(self):
        f = self.fixture
        message = f.message(ping=True)
        await self.engine.listen(message)
        reads = 0
        async def fetch(message_id):
            nonlocal reads
            reads += 1
            if reads >= 3:
                raise self.rate_limited()
            return f.messages[message_id]
        discord.TextChannel.fetch_message.side_effect = fetch
        with patch("cogs._conversation.asyncio.sleep", new=AsyncMock()) as wait, patch(
                "cogs._conversation.request_completion", new=AsyncMock(return_value={"content": "Withhold if not authorized"})) as provider, \
                self.assertLogs("cogs._conversation", level="WARNING") as logs:
            await self.engine.process(f.cog._queue.get_nowait())
        self.assertEqual(reads, 4)
        wait.assert_awaited_once_with(0.75)
        provider.assert_awaited_once()
        message.reply.assert_not_awaited()
        self.assertEqual(len(f.db.get_ai_chat_turns(10, 20)), 1)
        self.assertIn("discord_status=429", " ".join(logs.output))

    async def test_cancellation_during_429_wait_releases_authorization_lock(self):
        f = self.fixture
        message = f.message(ping=True)
        await self.engine.listen(message)
        job = f.cog._queue.get_nowait()
        entered, exited = asyncio.Event(), asyncio.Event()
        async def waiting(seconds):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                exited.set()
        discord.TextChannel.fetch_message.side_effect = self.rate_limited()
        with patch("cogs._conversation.asyncio.sleep", new=waiting):
            task = asyncio.create_task(self.engine.unchanged(job))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(exited.is_set())
        discord.TextChannel.fetch_message.side_effect = lambda message_id: f.messages[message_id]
        self.assertTrue(await asyncio.wait_for(self.engine.unchanged(job), 1))
        message.reply.assert_not_awaited()

    async def test_source_429_restarts_trigger_validation_instead_of_reusing_earlier_bytes(self):
        f = self.fixture
        source = f.message(85, user_id=31)
        message = f.message(ping=True)
        await self.engine.listen(message)
        job = f.cog._queue.get_nowait()
        job["source_fingerprints"] = {85: fingerprint(source)}
        source_attempts, trigger_reads = 0, 0
        async def fetch(message_id):
            nonlocal source_attempts, trigger_reads
            if message_id == 85:
                source_attempts += 1
                if source_attempts == 1:
                    raise self.rate_limited()
            else:
                trigger_reads += 1
            return f.messages[message_id]
        async def edit_during_wait(seconds):
            changed = copy.copy(message)
            changed.content = "Edited while an ancestor was rate limited"
            f.messages[message.id] = changed
        discord.TextChannel.fetch_message.side_effect = fetch
        with patch("cogs._conversation.asyncio.sleep", new=edit_during_wait):
            self.assertFalse(await self.engine.unchanged(job))
        self.assertEqual(trigger_reads, 2)

    async def test_deleted_trigger_does_not_start_obsolete_ancestor_gets(self):
        f = self.fixture
        target = f.message(90, user_id=999)
        message = f.message(reference=SimpleNamespace(message_id=90, guild_id=10, channel_id=20, resolved=target))
        await self.engine.listen(message)
        discord.TextChannel.fetch_message.reset_mock()
        discord.TextChannel.fetch_message.side_effect = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "deleted")
        with self.assertRaises(discord.NotFound):
            await self.engine.unchanged(f.cog._queue.get_nowait())
        discord.TextChannel.fetch_message.assert_awaited_once_with(message.id)


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
