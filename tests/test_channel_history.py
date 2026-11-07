"""Bounded Discord history reads; all Discord/provider calls are mocked."""
import copy
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from tests import test_conversation_memory as fixtures


class ChannelHistoryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.ConversationRuntimeTest()
        await self.fixture.asyncSetUp()
        f = self.fixture
        self.engine = f.engine
        self.admin = discord.Role(guild=f.guild, state=None, data={
            "id": "44", "name": "Helper", "permissions": "8", "position": 1})
        f.guild._roles[44] = self.admin
        f.guild.fetch_roles.return_value = [f.role, self.admin]
        f.members[30]._roles = discord.utils.SnowflakeList([10, 44])
        f.channels[21].name = "📕│server-logs"
        f.channels[22].name = "another"
        f.guild.fetch_channels = AsyncMock(side_effect=lambda: list(f.channels.values()))
        self.source = SimpleNamespace(id=500, channel=f.channels[21], guild=f.guild,
            author=f.members[999], content="", attachments=[], stickers=[],
            created_at=datetime(2026, 10, 30, tzinfo=timezone.utc), edited_at=None,
            embeds=[discord.Embed(title="Audit entry", description="LOG_SECRET")])
        self.reads = []
        self.rows = [self.source]
        def history(channel, **kwargs):
            self.reads.append((channel.id, kwargs))
            async def rows():
                for row in self.rows:
                    if "before" not in kwargs or row.id < kwargs["before"].id:
                        yield row
            return rows()
        self.history_patch = patch.object(discord.TextChannel, "history", new=history)
        self.history_patch.start()

    async def asyncTearDown(self):
        self.history_patch.stop()
        await self.fixture.asyncTearDown()

    @staticmethod
    def intent(arguments="{}", name="read_channel_history"):
        return {"role": "assistant", "tool_calls": [{"id": "channel", "type": "function",
            "function": {"name": name, "arguments": arguments}}]}

    async def run_request(self, text="Busca el último mensaje en Server Logs", answer=None):
        f = self.fixture
        message = f.message(ping=True)
        message.content = text
        await self.engine.listen(message)
        async def completion(*args, **kwargs):
            tools = kwargs.get("tools") or []
            if any(tool["function"]["name"] == "confirm_channel_history_request" for tool in tools):
                return self.intent(name="confirm_channel_history_request")
            if tools:
                return self.intent()
            return answer or "El último registro dice LOG_SECRET."
        provider = AsyncMock(side_effect=completion)
        with patch("cogs._conversation.request_completion", new=provider):
            await self.engine.process(f.cog._queue.get_nowait())
        return message, provider

    def private_source(self):
        self.fixture.channels[21]._overwrites = [discord.abc._Overwrites({
            "id": "10", "type": 0, "allow": "0", "deny": "1024"}),
            discord.abc._Overwrites({"id": "999", "type": 1, "allow": "66560", "deny": "0"})]

    def private_destination(self):
        f = self.fixture
        f.channels[20]._overwrites = [discord.abc._Overwrites({
            "id": "10", "type": 0, "allow": "0", "deny": "1024"}),
            discord.abc._Overwrites({"id": "44", "type": 0, "allow": "1024", "deny": "0"})]
        f.members[999]._roles = discord.utils.SnowflakeList([10, 44])

    async def test_admin_reads_decorated_name_and_embedded_latest_message_without_shared_memory(self):
        message, provider = await self.run_request()
        self.assertTrue(message.reply.await_count)
        self.assertEqual(provider.await_count, 3)
        context = json.loads(provider.await_args.args[2][1]["content"])
        history = context["channel_history_untrusted"]
        self.assertEqual(history["channel_id"], "21")
        self.assertEqual(history["limit"], 50)
        self.assertEqual(history["messages"][0]["message_id"], "500")
        self.assertIn("LOG_SECRET", json.dumps(history))
        self.assertIn("https://discord.com/channels/10/21/500", json.dumps(history))
        self.assertTrue(all(kwargs["limit"] == 50 for _, kwargs in self.reads))
        self.assertFalse(any(row["is_bot"] for row in self.fixture.db.get_ai_chat_turns(10, 20)))
        self.assertIn("50", message.reply.await_args.args[0])

    async def test_mention_and_id_bind_only_requested_channel(self):
        for text in ("Lee el último mensaje de <#21>",
                     "¿Qué pone en Server Logs?", "¿A qué hora entró Pepe en Server Logs?",
                     "Busca los mensajes de <@!223456789012345678> en <#21>",
                     "Busca el ID 123456789012345678 en Server Logs",
                     "Busca el ID 123456789012345678 en <#21>",
                     "Busca en el canal 123456789012345678"):
            with self.subTest(text=text):
                f = self.fixture
                if text.endswith("canal 123456789012345678"):
                    channel = f.channels.pop(21)
                    channel.id = 123456789012345678
                    f.channels[channel.id] = channel
                message, provider = await self.run_request(text)
                history = json.loads(provider.await_args.args[2][1]["content"])["channel_history_untrusted"]
                self.assertEqual(history["channel_id"], str(self.source.channel.id))
                self.assertTrue(message.reply.await_count)

    async def test_isolated_confirmation_rejects_greeting_despite_induced_planner_tool(self):
        f = self.fixture
        f.db.set_ai_memory(10, "chat", 20, 20, "PRIVATE_PROFILE")
        message = f.message(ping=True)
        message.content = "Hola <#21>"
        await self.engine.listen(message)
        confirmations = []
        async def provider(session, cfg, messages, **kwargs):
            tools = kwargs.get("tools") or []
            if any(tool["function"]["name"] == "confirm_channel_history_request" for tool in tools):
                confirmations.append(messages)
                self.assertNotIn("PRIVATE_PROFILE", json.dumps(messages))
                self.assertNotIn("LOG_SECRET", json.dumps(messages))
                self.assertEqual(json.loads(messages[1]["content"]), {
                    "current_user_text_untrusted": "Hola <#21>", "channel_id": "21",
                    "requester_id": "30", "guild_id": "10"})
                return {"role": "assistant", "content": "DENY"}
            return self.intent() if tools else "No se pidió leer mensajes."
        with patch("cogs._conversation.request_completion", new=provider):
            await self.engine.process(f.cog._queue.get_nowait())
        self.assertEqual(len(confirmations), 1)
        self.assertEqual(self.reads, [])
        self.assertTrue(message.reply.await_count)

    async def test_public_research_intent_is_not_overridden_by_matching_channel_names(self):
        f = self.fixture
        f.db.set_ai_config(10, ai_web_enabled=1)
        f.channels[21].name = "📚┃información"
        f.channels[22] = discord.CategoryChannel(guild=f.guild, state=None, data={
            "id": "22", "type": 4, "name": "🍉 Información", "position": 0, "permission_overwrites": []})
        message = f.message(ping=True)
        message.content = "¿Ahora sí me puedes dar información sobre la RTX 5080 Super y cuándo crees que viene?"
        intent = self.intent(json.dumps({"public_brief": {"topic": "RTX 5080 Super", "question": message.content}}),
            name="request_public_web_research")
        bot = self.reply_message(101, "La petición identifica varios canales.", user_id=999, target=message)
        followup = self.reply_message(102, "Tu nomás has la investigación.", target=bot)
        for current in (message, followup):
            with self.subTest(text=current.content):
                await self.engine.listen(current)
                with patch.object(self.engine, "web_context", new=AsyncMock(return_value={"verified_source_urls": []})) as research, \
                        patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=[intent, "La investigación pública sigue su curso."])):
                    await self.engine.process(f.cog._queue.get_nowait())
                research.assert_awaited_once()
                self.assertEqual(self.reads, [])
                self.assertNotIn("varios canales", current.reply.await_args.args[0])

    async def test_ordinary_chat_matching_multiple_channels_checks_intent_before_target_error(self):
        f = self.fixture
        f.channels[21].name = "📚┃información"
        f.channels[22] = discord.CategoryChannel(guild=f.guild, state=None, data={
            "id": "22", "type": 4, "name": "🍉 Información", "position": 0, "permission_overwrites": []})
        confirmations = []
        async def provider(*args, **kwargs):
            names = [tool["function"]["name"] for tool in kwargs.get("tools", [])]
            if "confirm_channel_history_request" in names:
                confirmations.append(args[2])
                return {"content": "DENY"}
            return {"content": "De nada."} if names else "De nada."
        for index, text in enumerate(("Gracias por la información", "Gracias por la información sobre el acceso a Internet",
                "Gracias por la información sobre los permisos", "Ya arreglé mi acceso a Internet; gracias por la información")):
            with self.subTest(text=text):
                message = f.message(200 + index, ping=True)
                message.content = text
                await self.engine.listen(message)
                with patch("cogs._conversation.request_completion", new=provider):
                    await self.engine.process(f.cog._queue.get_nowait())
                self.assertEqual(len(confirmations), index + 1)
                self.assertEqual(self.reads, [])
                self.assertEqual(message.reply.await_args.args[0], "De nada.")

    async def test_failed_or_malformed_history_confirmation_never_reads(self):
        invalid = [None, "DENY", {}, self.intent(), self.intent("[]", "confirm_channel_history_request"),
            ValueError("verification_failed"), TimeoutError()]
        for result in invalid:
            with self.subTest(result=result):
                f = self.fixture
                message = f.message(ping=True)
                message.content = "Busca el último mensaje en Server Logs"
                await self.engine.listen(message)
                async def provider(*args, **kwargs):
                    tools = kwargs.get("tools") or []
                    if any(tool["function"]["name"] == "confirm_channel_history_request" for tool in tools):
                        if isinstance(result, Exception):
                            raise result
                        return result
                    return self.intent() if tools else "No se pudo verificar la petición."
                with patch("cogs._conversation.request_completion", new=provider):
                    await self.engine.process(f.cog._queue.get_nowait())
                self.assertEqual(self.reads, [])

    async def test_private_source_cannot_reach_provider_or_public_destination(self):
        self.private_source()
        message, provider = await self.run_request(answer="Consulta desde un canal exclusivo de administradores.")
        self.assertEqual(self.reads, [])
        self.assertNotIn("LOG_SECRET", json.dumps(provider.await_args_list, default=str))
        self.assertEqual(provider.await_count, 2)
        self.assertIn("administradores", message.reply.await_args.args[0])
        self.assertTrue(message.reply.await_count)

    async def test_admin_only_destination_can_read_private_source(self):
        self.private_source()
        self.private_destination()
        _, provider = await self.run_request()
        self.assertIn("LOG_SECRET", provider.await_args.args[2][1]["content"])

    async def test_bot_individual_allow_is_safe_but_another_member_allow_is_not(self):
        f = self.fixture
        self.private_source()
        self.private_destination()
        f.members[999]._roles = discord.utils.SnowflakeList([10])
        f.channels[20]._overwrites.append(discord.abc._Overwrites({
            "id": "999", "type": 1, "allow": "66560", "deny": "0"}))
        _, provider = await self.run_request()
        self.assertIn("LOG_SECRET", provider.await_args.args[2][1]["content"])
        self.reads.clear()
        f.channels[20]._overwrites.append(discord.abc._Overwrites({
            "id": "31", "type": 1, "allow": "66560", "deny": "0"}))
        _, provider = await self.run_request(answer="Consulta desde un canal exclusivo de administradores.")
        self.assertEqual(self.reads, [])
        self.assertNotIn("LOG_SECRET", provider.await_args.args[2][1]["content"])

    async def test_model_arguments_cannot_select_or_override_channel(self):
        f = self.fixture
        message = f.message(ping=True)
        message.content = "Lee el último mensaje de <#21>"
        await self.engine.listen(message)
        with patch("cogs._conversation.request_completion", new=AsyncMock(return_value=self.intent('{"channel_id":"22"}'))):
            await self.engine.process(f.cog._queue.get_nowait())
        self.assertEqual(self.reads, [])

    async def test_restrictive_public_overwrite_is_not_safe_for_other_audiences(self):
        self.fixture.channels[21]._overwrites = [discord.abc._Overwrites({
            "id": "31", "type": 1, "allow": "0", "deny": "1024"})]
        await self.run_request(answer="La audiencia de este canal no permite mostrar esos datos.")
        self.assertEqual(self.reads, [])

    async def test_nonadmin_bot_denial_ambiguous_and_unrequested_targets_never_read(self):
        for mode in ("nonadmin", "bot_denied", "ambiguous", "greeting", "other_guild"):
            with self.subTest(mode=mode):
                f = self.fixture
                f.members[30]._roles = discord.utils.SnowflakeList([10] if mode == "nonadmin" else [10, 44])
                f.channels[21]._overwrites = [discord.abc._Overwrites({
                    "id": "999", "type": 1, "allow": "0", "deny": "65536"})] if mode == "bot_denied" else []
                f.channels[22].name = "server_logs" if mode == "ambiguous" else "another"
                text = "Hola" if mode == "greeting" else "Busca el último mensaje en Server Logs"
                if mode == "other_guild":
                    text = "Lee https://discord.com/channels/11/21/500"
                message, _ = await self.run_request(text, answer="No pude consultar ese canal.")
                self.assertEqual(self.reads, [])
                if mode == "ambiguous":
                    self.assertIn("varios canales", message.reply.await_args.args[0])

    async def test_permission_revocation_or_embed_edit_during_final_generation_prevents_reply(self):
        f = self.fixture
        for change in (lambda: setattr(f.guild.fetch_roles, "return_value", [f.role]),
                       lambda: self.source.embeds[0].set_field_at(0, name="key", value="edited"),
                       lambda: self.rows.clear()):
            with self.subTest(change=change):
                f.guild.fetch_roles.return_value = [f.role, self.admin]
                self.source.embeds = [discord.Embed(description="LOG_SECRET").add_field(name="key", value="original")]
                self.rows = [self.source]
                message = f.message(ping=True)
                message.content = "Busca en Server Logs"
                await self.engine.listen(message)
                async def provider(*args, **kwargs):
                    if any(tool["function"]["name"] == "confirm_channel_history_request" for tool in kwargs.get("tools", [])):
                        return self.intent(name="confirm_channel_history_request")
                    if kwargs.get("tools"):
                        return self.intent()
                    change()
                    return "Do not send stale private data"
                with patch("cogs._conversation.request_completion", new=provider):
                    await self.engine.process(f.cog._queue.get_nowait())
                message.reply.assert_not_awaited()

    async def history_authority_change(self, change):
        f = self.fixture
        for read_number in (2, 3):
            with self.subTest(read_number=read_number):
                f.guild.fetch_roles.return_value = [f.role, self.admin]
                self.private_source()
                self.private_destination()
                original = discord.TextChannel.history
                reads = 0
                def history(channel, **kwargs):
                    nonlocal reads
                    reads += 1
                    current_read = reads
                    async def rows():
                        async for entry in original(channel, **kwargs):
                            yield entry
                        if current_read == read_number:
                            change()
                    return rows()
                with patch.object(discord.TextChannel, "history", new=history):
                    message, provider = await self.run_request()
                self.assertEqual(provider.await_count, 2 if read_number == 2 else 3)
                message.reply.assert_not_awaited()

    async def test_admin_revocation_during_history_blocks_egress_and_reply(self):
        await self.history_authority_change(lambda: setattr(self.fixture.guild.fetch_roles,
            "return_value", [self.fixture.role]))

    async def test_scope_change_during_history_blocks_egress_and_reply(self):
        revisions = iter(("first changed scope", "second changed scope"))
        await self.history_authority_change(lambda: self.fixture.db.set_ai_config(10,
            ai_system_prompt=next(revisions)))

    async def test_destination_audience_expansion_during_history_blocks_egress_and_reply(self):
        def expand():
            destination = copy.copy(self.fixture.channels[20])
            destination._overwrites = [*destination._overwrites, discord.abc._Overwrites({
                "id": "31", "type": 1, "allow": "66560", "deny": "0"})]
            self.fixture.channels[20] = destination
        await self.history_authority_change(expand)

    async def test_new_logs_do_not_invalidate_snapshot_and_same_channel_chunks_can_finish(self):
        f = self.fixture
        for same_channel in (False, True):
            with self.subTest(same_channel=same_channel):
                self.source.channel = f.channels[20 if same_channel else 21]
                self.rows = [self.source]
                message = f.message(ping=True)
                message.content = f"Lee el último mensaje de <#{self.source.channel.id}>"
                await self.engine.listen(message)
                async def provider(*args, **kwargs):
                    if any(tool["function"]["name"] == "confirm_channel_history_request" for tool in kwargs.get("tools", [])):
                        return self.intent(name="confirm_channel_history_request")
                    if kwargs.get("tools"):
                        return self.intent()
                    newer = SimpleNamespace(**{**vars(self.source), "id": 501, "content": "New log"})
                    self.rows.insert(0, newer)
                    return "Resumen " * 300
                async def reply(content, **kwargs):
                    sent = SimpleNamespace(**{**vars(self.source), "id": 10000 + message.reply.await_count,
                        "channel": message.channel, "content": content})
                    if same_channel:
                        self.rows.insert(0, sent)
                    return sent
                message.reply.side_effect = reply
                with patch("cogs._conversation.request_completion", new=provider):
                    await self.engine.process(f.cog._queue.get_nowait())
                self.assertEqual(message.reply.await_count, 2)

    def reply_message(self, message_id, text, *, user_id=30, target=None):
        f = self.fixture
        reference = SimpleNamespace(message_id=target.id, channel_id=20, guild_id=10) if target else None
        message = f.message(message_id, user_id=user_id, ping=user_id != 999, reference=reference)
        message.content = text
        return message

    async def test_reply_sequence_completes_channel_and_intent_despite_planner_omission(self):
        f = self.fixture
        original = self.reply_message(70, "¿Puedes leer Server Logs?")
        first_bot = self.reply_message(71, "No tengo ese permiso.", user_id=999, target=original)
        update = self.reply_message(72, "Ahora ya deberías tener ese permiso, te hice unos arreglos internos", target=first_bot)
        bot = self.reply_message(73, "Ahora sí tengo acceso a Server Logs.", user_id=999, target=update)
        latest = self.reply_message(74, "Para probar, ¿Me indicas el último mensaje?", target=bot)
        bot2 = self.reply_message(75, "Necesito el ID; no basta el nombre Server Logs.", user_id=999, target=latest)
        channel_id = 1397040503370940466
        channel = f.channels.pop(21)
        channel.id = channel_id
        f.channels[channel_id] = channel
        identify = self.reply_message(76, f"Bruh... Otro bug para arreglar. Btw: {channel_id}", target=bot2)
        bot3 = self.reply_message(77, "Ya puedo leerlo, dime qué quieres.", user_id=999, target=identify)
        again = self.reply_message(78, "Dame el último mensaje de ahí, wey", target=bot3)
        for message in (latest, identify, again):
            with self.subTest(text=message.content):
                await self.engine.listen(message)
                confirmations = []
                async def provider(session, cfg, messages, **kwargs):
                    tools = kwargs.get("tools") or []
                    if any(tool["function"]["name"] == "confirm_channel_history_request" for tool in tools):
                        context = json.loads(messages[1]["content"])
                        confirmations.append(context)
                        self.assertNotIn("Ahora sí tengo acceso", json.dumps(context))
                        return self.intent(name="confirm_latest_channel_message_request")
                    if not tools:
                        raise AssertionError("Latest-message rendering must not call the model again")
                    return {"role": "assistant", "content": "Necesito el ID y confirmar qué quieres leer."}
                with patch("cogs._conversation.request_completion", new=provider):
                    await self.engine.process(f.cog._queue.get_nowait())
                self.assertTrue(confirmations)
                result = "".join(call.args[0] for call in message.reply.await_args_list)
                self.assertIn("LOG_SECRET", result)
                self.assertIn(f"/10/{channel_id}/500", result)
                self.assertNotIn("Necesito el ID", result)

    async def test_other_user_or_invented_bot_channel_cannot_bind_followup(self):
        for foreign in (True, False):
            with self.subTest(foreign=foreign):
                original = self.reply_message(70, "Lee Server Logs" if foreign else "Hola", user_id=31 if foreign else 30)
                bot = self.reply_message(71, "Ahora sí tengo acceso a Server Logs.", user_id=999, target=original)
                current = self.reply_message(72, "¿Me indicas el último mensaje?", target=bot)
                await self.engine.listen(current)
                with patch("cogs._conversation.request_completion", new=AsyncMock(return_value=self.intent())):
                    await self.engine.process(self.fixture.cog._queue.get_nowait())
                self.assertEqual(self.reads, [])

    async def test_current_channel_and_filtered_intent_override_prior_latest(self):
        f = self.fixture
        old = self.reply_message(70, "Dame el último mensaje de Server Logs")
        bot = self.reply_message(71, "Indica qué quieres comprobar.", user_id=999, target=old)
        current = self.reply_message(72, "Busca el último mensaje de Pepe en <#22>", target=bot)
        self.source.channel = f.channels[22]
        await self.engine.listen(current)
        async def provider(session, cfg, messages, **kwargs):
            tools = kwargs.get("tools") or []
            if any(tool["function"]["name"] == "confirm_channel_history_request" for tool in tools):
                self.assertEqual(json.loads(messages[1]["content"])["channel_id"], "22")
                return self.intent(name="confirm_channel_history_request")
            if tools:
                return {"role": "assistant", "content": "Necesito el ID."}
            return "No encontré mensajes de Pepe entre las entradas consultadas."
        with patch("cogs._conversation.request_completion", new=provider):
            await self.engine.process(f.cog._queue.get_nowait())
        self.assertIn("No encontré mensajes de Pepe", current.reply.await_args.args[0])
        self.assertNotIn("LOG_SECRET", current.reply.await_args.args[0])

    async def test_search_id_in_followup_preserves_inherited_channel(self):
        f = self.fixture
        other_channel_id = 223456789012345678
        other = f.channels.pop(22)
        other.id = other_channel_id
        f.channels[other_channel_id] = other
        for text in (f"Busca el ID {other_channel_id} de ahí", "Busca los mensajes de 323456789012345678 de ahí"):
            with self.subTest(text=text):
                original = self.reply_message(70, "Lee <#21>")
                bot = self.reply_message(71, "¿Qué quieres buscar?", user_id=999, target=original)
                current = self.reply_message(72, text, target=bot)
                await self.engine.listen(current)
                selected, fitted = [], []
                async def provider(session, cfg, messages, **kwargs):
                    tools = kwargs.get("tools") or []
                    if any(tool["function"]["name"] == "confirm_channel_history_request" for tool in tools):
                        selected.append(json.loads(messages[1]["content"])["channel_id"])
                        return self.intent(name="confirm_channel_history_request")
                    if tools:
                        return self.intent()
                    fitted.append(json.loads(messages[1]["content"])["channel_history_untrusted"]["channel_id"])
                    return "Consulta filtrada del canal indicado."
                with patch("cogs._conversation.request_completion", new=provider):
                    await self.engine.process(f.cog._queue.get_nowait())
                self.assertEqual(selected, ["21"])
                self.assertEqual(fitted, ["21"])

    async def test_explicit_channel_change_overrides_inherited_target(self):
        f = self.fixture
        channel_id = 223456789012345678
        channel = f.channels.pop(22)
        channel.id = channel_id
        f.channels[channel_id] = channel
        self.source.channel = channel
        for text in (f"Lee <#{channel_id}>", f"Lee el canal {channel_id}", f"Lee el canal ID {channel_id}", str(channel_id)):
            with self.subTest(text=text):
                original = self.reply_message(70, "Lee <#21>")
                bot = self.reply_message(71, "¿Qué quieres buscar?", user_id=999, target=original)
                current = self.reply_message(72, text, target=bot)
                await self.engine.listen(current)
                selected, fitted = [], []
                async def provider(session, cfg, messages, **kwargs):
                    tools = kwargs.get("tools") or []
                    if any(tool["function"]["name"] == "confirm_channel_history_request" for tool in tools):
                        selected.append(json.loads(messages[1]["content"])["channel_id"])
                        return self.intent(name="confirm_channel_history_request")
                    return self.intent() if tools else "Consulta del nuevo canal."
                with patch("cogs._conversation.request_completion", new=provider):
                    await self.engine.process(f.cog._queue.get_nowait())
                self.assertEqual(selected, [str(channel_id)])
                self.assertTrue(current.reply.await_count)

        self.reads.clear()
        unknown = 923456789012345678
        current = self.reply_message(73, f"Lee <#{unknown}>", target=bot)
        await self.engine.listen(current)
        original_fetch = f.guild.fetch_channel.side_effect
        def fetch(channel_id):
            if channel_id == unknown:
                raise discord.NotFound(SimpleNamespace(status=404, reason="fixture"), "fixture")
            return original_fetch(channel_id)
        f.guild.fetch_channel.side_effect = fetch
        async def provider(*args, **kwargs):
            tools = kwargs.get("tools") or []
            return self.intent(name="confirm_channel_history_request") if any(
                tool["function"]["name"] == "confirm_channel_history_request" for tool in tools) else self.intent()
        with patch("cogs._conversation.request_completion", new=provider):
            await self.engine.process(f.cog._queue.get_nowait())
        self.assertEqual(self.reads, [])
        self.assertIn("Discord no encontró", current.reply.await_args.args[0])

    async def test_unverified_access_and_invented_diagnosis_are_not_reported_as_facts(self):
        f = self.fixture
        for deny_intent in (True, False):
            with self.subTest(deny_intent=deny_intent):
                current = self.reply_message(100, "Ahora ya tienes acceso a Server Logs" if deny_intent else "Lee Server Logs")
                if not deny_intent:
                    self.private_source()
                await self.engine.listen(current)
                async def provider(*args, **kwargs):
                    tools = kwargs.get("tools") or []
                    if any(tool["function"]["name"] == "confirm_channel_history_request" for tool in tools):
                        return {"role": "assistant", "content": "DENY"} if deny_intent else self.intent(name="confirm_channel_history_request")
                    if not tools:
                        raise AssertionError("Known lookup errors must not request model narration")
                    return {"role": "assistant", "content": "Ahora sí tengo acceso; es un canal de voz inválido."}
                with patch("cogs._conversation.request_completion", new=provider):
                    await self.engine.process(f.cog._queue.get_nowait())
                result = current.reply.await_args.args[0]
                self.assertNotIn("Ahora sí tengo acceso", result)
                self.assertNotIn("canal de voz", result)
                self.assertIn("no he comprobado" if deny_intent else "administradores", result)

    async def test_thanks_followup_does_not_repeat_read_or_ask_for_intent(self):
        original = self.reply_message(70, "Lee Server Logs")
        bot = self.reply_message(71, "Último mensaje consultado.", user_id=999, target=original)
        current = self.reply_message(72, "Gracias, bro", target=bot)
        await self.engine.listen(current)
        async def provider(*args, **kwargs):
            tools = kwargs.get("tools") or []
            if any(tool["function"]["name"] == "confirm_channel_history_request" for tool in tools):
                return {"role": "assistant", "content": "DENY"}
            return {"role": "assistant", "content": "¡De nada!"}
        with patch("cogs._conversation.request_completion", new=provider):
            await self.engine.process(self.fixture.cog._queue.get_nowait())
        self.assertEqual(self.reads, [])
        self.assertEqual(current.reply.await_args.args[0], "¡De nada!")

    async def test_lookup_errors_report_actual_type_bot_permission_and_rest_outcome(self):
        f = self.fixture
        text_channel = f.channels[21]
        for mode, expected in (("bot_permissions", "Mis permisos actuales"),
                               ("voice", "su tipo actual"), ("not_found", "Discord no encontró"),
                               ("forbidden", "Discord rechazó"), ("rest", "consulta actual a Discord falló")):
            with self.subTest(mode=mode):
                f.channels[21] = text_channel
                text_channel._overwrites = []
                if mode == "bot_permissions":
                    text_channel._overwrites = [discord.abc._Overwrites({
                        "id": "999", "type": 1, "allow": "0", "deny": "65536"})]
                elif mode == "voice":
                    f.channels[21] = discord.VoiceChannel(state=None, guild=f.guild,
                        data={"id": "21", "name": "server-logs", "type": 2, "position": 0,
                              "bitrate": 64000, "user_limit": 0})
                response = SimpleNamespace(status=404 if mode == "not_found" else 403 if mode == "forbidden" else 500,
                    reason="fixture")
                error = (discord.NotFound(response, "fixture") if mode == "not_found" else
                         discord.Forbidden(response, "fixture") if mode == "forbidden" else
                         discord.HTTPException(response, "fixture"))
                def fetch(channel_id):
                    if channel_id == 21 and mode in {"not_found", "forbidden", "rest"}:
                        raise error
                    return f.channels[channel_id]
                f.guild.fetch_channel.side_effect = fetch
                message, _ = await self.run_request("Lee <#21>")
                self.assertIn(expected, message.reply.await_args.args[0])
                self.assertEqual(self.reads, [])

    async def test_chain_edit_or_reply_reparent_during_confirmation_blocks_read(self):
        for change_link in (False, True):
            with self.subTest(change_link=change_link):
                original = self.reply_message(70, "Lee Server Logs")
                bot = self.reply_message(71, "¿Qué parte?", user_id=999, target=original)
                current = self.reply_message(72, "El último mensaje", target=bot)
                await self.engine.listen(current)
                async def provider(*args, **kwargs):
                    tools = kwargs.get("tools") or []
                    if any(tool["function"]["name"] == "confirm_channel_history_request" for tool in tools):
                        if change_link:
                            bot.reference = SimpleNamespace(message_id=69, channel_id=20, guild_id=10)
                        else:
                            original.content = "Lee otro canal"
                        return self.intent(name="confirm_latest_channel_message_request")
                    return {"role": "assistant", "content": "Dime el ID"}
                with patch("cogs._conversation.request_completion", new=provider):
                    await self.engine.process(self.fixture.cog._queue.get_nowait())
                self.assertEqual(self.reads, [])
                current.reply.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
