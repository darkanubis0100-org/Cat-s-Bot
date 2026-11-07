import json
import unittest
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

import discord

from cogs.logging import Logging, PERMISSION_NAMES
from cogs.moderation import Moderation
from cogs.serverutils import ServerUtils


class _DB:
    def __init__(self, log_events):
        self.log_events = log_events
        self.serverlog_channel = None

    def get_server_config(self, guild_id: int):
        return {"guild_id": guild_id, "log_events": self.log_events, "serverlog_channel": self.serverlog_channel}

    def set_server_config(self, guild_id: int, **kwargs):
        if "serverlog_channel" in kwargs:
            self.serverlog_channel = kwargs["serverlog_channel"]


class _Channel:
    def __init__(self, channel_id: int):
        self.id = channel_id

    async def send(self, **kwargs):
        return None


class _Guild:
    def __init__(self, rounded_id: int, real_id: int):
        self.id = 123
        self.name = "Guild"
        self._rounded_id = rounded_id
        self._real_channel = _Channel(real_id)
        self.channels = [self._real_channel]

    def get_channel(self, channel_id: int):
        if channel_id == self._real_channel.id:
            return self._real_channel
        return None

    async def fetch_channel(self, channel_id: int):
        raise AssertionError("rounded channel should be repaired before fetch")


class ServerUtilsLoggingConfigTest(unittest.TestCase):
    def test_dashboard_log_keys_disable_legacy_defaults(self):
        cog = ServerUtils.__new__(ServerUtils)
        cog.db = _DB(json.dumps({
            "message_delete": False,
            "message_edit": False,
            "member_join": False,
            "member_leave": False,
            "voice_join": False,
            "voice_leave": False,
            "role_change": False,
            "channel_create": False,
            "channel_delete": False,
        }))

        events = cog._get_log_events(123)

        self.assertFalse(events["message_delete"])
        self.assertFalse(events["message_edit"])
        self.assertFalse(events["member_join"])
        self.assertFalse(events["member_leave"])
        self.assertFalse(events["voice_join_leave"])
        self.assertFalse(events["role_changes"])
        self.assertFalse(events["channel_updates"])

    def test_legacy_log_keys_still_supported(self):
        cog = ServerUtils.__new__(ServerUtils)
        cog.db = _DB(json.dumps({"voice_join_leave": False, "role_changes": False}))

        events = cog._get_log_events(123)

        self.assertFalse(events["voice_join_leave"])
        self.assertFalse(events["role_changes"])
        self.assertFalse(events["message_delete"])  # Explicit allowlists match the runtime.


class ServerUtilsChannelRepairTest(unittest.IsolatedAsyncioTestCase):
    async def test_repair_rounded_serverlog_channel_id(self):
        rounded_id = 1497055416062972000
        real_id = 1497055416062971949
        cog = ServerUtils.__new__(ServerUtils)
        cog.db = _DB(json.dumps({}))
        guild = _Guild(rounded_id, real_id)

        channel, resolved_id, error = await cog._resolve_serverlog_channel(guild, rounded_id)

        self.assertIs(channel, guild.channels[0])
        self.assertEqual(resolved_id, real_id)
        self.assertIsNone(error)
        self.assertEqual(cog.db.serverlog_channel, real_id)


class LoggingCogChannelRepairTest(unittest.IsolatedAsyncioTestCase):
    async def test_repair_rounded_serverlog_channel_id(self):
        rounded_id = 1497055416062972000
        real_id = 1497055416062971949
        cog = Logging.__new__(Logging)
        cog.db = _DB(json.dumps({}))
        guild = _Guild(rounded_id, real_id)

        channel, resolved_id, error = await cog._resolve_serverlog_channel(guild, rounded_id)

        self.assertIs(channel, guild.channels[0])
        self.assertEqual(resolved_id, real_id)
        self.assertIsNone(error)
        self.assertEqual(cog.db.serverlog_channel, real_id)


class ServerUtilsMessageListenerOwnershipTest(unittest.IsolatedAsyncioTestCase):
    async def test_message_edit_and_delete_are_noop_to_avoid_duplicate_logging(self):
        cog = ServerUtils.__new__(ServerUtils)
        cog._send_server_log = AsyncMock()

        author = type("Author", (), {"bot": False})()
        guild = type("Guild", (), {"id": 123})()
        message = type("Message", (), {"guild": guild, "author": author, "content": "before"})()
        after = type("Message", (), {"guild": guild, "author": author, "content": "after"})()

        await cog.on_message_delete(message)
        await cog.on_message_edit(message, after)

        cog._send_server_log.assert_not_called()


class ChannelUpdateLoggingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = Logging.__new__(Logging)
        self.cog._send_log = AsyncMock()
        self.guild = SimpleNamespace(id=123)

    def channel(self, **updates):
        values = dict(id=200, guild=self.guild, type=discord.ChannelType.text, name="general",
                      mention="<#200>", topic=None, position=0, category_id=None, overwrites={})
        values.update(updates)
        return SimpleNamespace(**values)

    def sent_text(self):
        return "\n".join(str(call.args[1].to_dict()) for call in self.cog._send_log.await_args_list)

    async def test_settings_and_unchanged_events(self):
        await self.cog.on_guild_channel_update(self.channel(), self.channel())
        self.cog._send_log.assert_not_awaited()
        await self.cog.on_guild_channel_update(self.channel(position=0), self.channel(position=1))
        self.cog._send_log.assert_not_awaited()
        before = self.channel(slowmode_delay=0, nsfw=False, bitrate=64000, user_limit=0)
        after = self.channel(slowmode_delay=10, nsfw=True, bitrate=96000, user_limit=5, position=3, category_id=300)
        await self.cog.on_guild_channel_update(before, after)
        text = self.sent_text()
        for value in ("Modo lento", "NSFW", "Bitrate", "Límite de usuarios", "Categoría", "Antes", "Después"):
            self.assertIn(value, text)
        self.assertNotIn("Posición", text)
        self.assertTrue(all(call.args[2] == "channel_update" for call in self.cog._send_log.await_args_list))

    async def test_category_permission_add_change_remove_use_tristate_and_uncached_targets(self):
        role = discord.Object(id=400, type=discord.Role)
        user = discord.Object(id=500, type=discord.User)
        before = self.channel(type=discord.ChannelType.category, overwrites={
            role: discord.PermissionOverwrite(view_channel=True, send_messages=False),
            user: discord.PermissionOverwrite(connect=False),
        })
        after = self.channel(type=discord.ChannelType.category, overwrites={
            role: discord.PermissionOverwrite(view_channel=False, send_messages=None, add_reactions=True),
        })
        await self.cog.on_guild_channel_update(before, after)
        text = self.sent_text()
        for value in ("Categoría", "Rol", "Usuario", "400", "500", "Permitidos → Bloqueados", "Bloqueados → Heredados", "Heredados → Permitidos"):
            self.assertIn(value, text)
        self.assertIn("eliminado", text.lower())
        self.cog._send_log.reset_mock()
        await self.cog.on_guild_channel_update(self.channel(), self.channel(overwrites={role: discord.PermissionOverwrite(view_channel=True)}))
        self.assertIn("añadido", self.sent_text().lower())

    async def test_permission_summary_groups_transitions_and_links_known_targets(self):
        role = discord.Role(guild=SimpleNamespace(id=123), state=None,
                            data={"id": "400", "name": "Miembros", "permissions": "0", "position": 1})
        user = Mock(id=500, display_name="Ana", mention="<@500>")
        before = self.channel(overwrites={role: discord.PermissionOverwrite(view_channel=True)})
        after = self.channel(overwrites={
            role: discord.PermissionOverwrite(view_channel=False, create_instant_invite=False, send_messages=True),
            user: discord.PermissionOverwrite(connect=True),
        })
        await self.cog.on_guild_channel_update(before, after)
        embed = self.cog._send_log.await_args.args[1]
        text = self.sent_text()
        self.assertEqual(embed.title, "✏️ Canal actualizado")
        self.assertEqual(embed.fields[0].value, "<#200>")
        self.assertIn("<@&400>", text)
        self.assertIn("<@500>", text)
        self.assertIn("Permitidos → Bloqueados: Ver canal", text)
        self.assertIn("Heredados → Bloqueados: Crear invitaciones", text)
        self.assertIn("Heredados → Permitidos: Enviar mensajes", text)
        self.assertNotIn("ID 400", text)
        self.assertNotIn("create_instant_invite", text)
        self.assertNotIn("read_messages", text)
        self.assertFalse(embed.footer.text)
        self.cog._send_log.reset_mock()
        await self.cog.on_guild_channel_update(self.channel(), self.channel(overwrites={
            role: discord.PermissionOverwrite(create_instant_invite=False, send_messages=False),
        }))
        self.assertIn("Heredados → Bloqueados: Crear invitaciones, Enviar mensajes", self.sent_text())

    async def test_forum_tags_and_defaults_detect_renames_without_false_changes(self):
        old = discord.ForumTag(name="Fotos", moderated=False)
        old.id = 88
        renamed = discord.ForumTag(name="Imágenes", moderated=True, emoji="✅")
        renamed.id = 88
        before = self.channel(available_tags=[old], default_thread_slowmode_delay=0, default_layout=discord.ForumLayoutType.not_set)
        equal = self.channel(available_tags=[old], default_thread_slowmode_delay=0, default_layout=discord.ForumLayoutType.not_set)
        await self.cog.on_guild_channel_update(before, equal)
        self.cog._send_log.assert_not_awaited()
        after = self.channel(available_tags=[renamed], default_thread_slowmode_delay=15, default_layout=discord.ForumLayoutType.gallery_view)
        await self.cog.on_guild_channel_update(before, after)
        for value in ("Fotos", "Imágenes", "Etiquetas", "Modo lento de hilos", "Vista del foro"):
            self.assertIn(value, self.sent_text())

    async def test_large_changes_are_split_without_losing_permissions_or_exceeding_limits(self):
        targets = {discord.Object(id=400 + i, type=discord.Role): discord.PermissionOverwrite(**{name: True for name in discord.PermissionOverwrite.PURE_FLAGS})
                   for i in range(10)}
        previous = {target: discord.PermissionOverwrite(**{
            name: False for name in sorted(discord.PermissionOverwrite.PURE_FLAGS)[::2]
        }) for target in targets}
        await self.cog.on_guild_channel_update(self.channel(topic="a" * 4096, overwrites=previous),
                                              self.channel(topic="😀" * 4096, overwrites=targets))
        self.assertGreater(self.cog._send_log.await_count, 1)
        text = self.sent_text()
        for name in discord.PermissionOverwrite.PURE_FLAGS:
            self.assertIn(PERMISSION_NAMES[name], text)
            self.assertNotIn(f"`{name}`", text)
        for call in self.cog._send_log.await_args_list:
            embed = call.args[1]
            self.assertLessEqual(len(embed), 6000)
            self.assertLessEqual(len(embed.fields), 25)
            for field in embed.fields:
                self.assertLessEqual(len(field.name.encode("utf-16-le")) // 2, 256)
                self.assertLessEqual(len(field.value.encode("utf-16-le")) // 2, 1024)

    def test_channel_event_alias_and_explicit_disable(self):
        self.assertTrue(Logging._event_enabled({"channel_updates": True}, "channel_update"))
        self.assertTrue(Logging._event_enabled(["channel_updates"], "channel_update"))
        self.assertFalse(Logging._event_enabled({"channel_updates": True, "channel_update": False}, "channel_update"))
        self.assertFalse(Logging._event_enabled({"channel_create": True}, "channel_update"))

    async def test_literal_topic_and_same_name_custom_emoji_changes_are_not_lost(self):
        before = self.channel(topic=None, default_reaction_emoji=discord.PartialEmoji(name="cat", id=101))
        after = self.channel(topic="Sin configurar", default_reaction_emoji=discord.PartialEmoji(name="cat", id=102))
        await self.cog.on_guild_channel_update(before, after)
        for value in ("Tema", "Reacción del foro", "101", "102"):
            self.assertIn(value, self.sent_text())

    async def test_send_respects_disabled_filter_destination_and_queues_continuation_without_pings(self):
        channel = SimpleNamespace(id=900, send=AsyncMock())
        guild = SimpleNamespace(id=123, get_channel=lambda _ident: channel)
        config = {"serverlog_channel": 900, "serverlog_enabled": 1,
                  "log_events": json.dumps({"channel_update": False, "channel_updates": True})}
        self.cog.db = SimpleNamespace(get_server_config=lambda _ident: config, enqueue_log_outbox=Mock())
        self.cog.bot = SimpleNamespace(db=self.cog.db)
        self.cog._last_log = {}
        embed = discord.Embed(title="Cambios")
        await Logging._send_log(self.cog, guild, embed, "channel_update")
        channel.send.assert_not_awaited()
        config["log_events"] = json.dumps({"channel_update": True})
        config["serverlog_enabled"] = 0
        await Logging._send_log(self.cog, guild, embed, "channel_update")
        channel.send.assert_not_awaited()
        config.update(serverlog_enabled=1, serverlog_channel=None)
        await Logging._send_log(self.cog, guild, embed, "channel_update")
        channel.send.assert_not_awaited()
        config["serverlog_channel"] = 900
        await Logging._send_log(self.cog, guild, embed, "channel_update")
        self.assertEqual(channel.send.await_args.kwargs["allowed_mentions"].to_dict(), {"parse": []})
        continuation = discord.Embed(title="Continuación")
        await Logging._send_log(self.cog, guild, continuation, "channel_update")
        payload = self.cog.db.enqueue_log_outbox.call_args.args[2]
        self.assertEqual(payload["embed"]["title"], "Continuación")
        self.assertEqual(payload["event"], "channel_update")

    async def test_outbox_replay_sends_without_mentions(self):
        channel = Mock(spec=discord.TextChannel)
        channel.id = 900
        channel.send = AsyncMock()
        guild = SimpleNamespace(get_channel=lambda _ident: channel)
        row = {"id": 1, "guild_id": 123, "channel_id": 900, "attempts": 0,
               "payload": json.dumps({"embed": discord.Embed(title="Permisos").to_dict()})}
        cog = Moderation.__new__(Moderation)
        cog.bot = SimpleNamespace(get_guild=lambda _ident: guild)
        cog.db = SimpleNamespace(get_pending_log_outbox=Mock(return_value=[row]), claim_log_outbox=Mock(return_value=True),
                                 mark_log_outbox=Mock())
        await Moderation._flush_log_outbox.coro(cog)
        self.assertEqual(channel.send.await_args.kwargs["allowed_mentions"].to_dict(), {"parse": []})
        cog.db.mark_log_outbox.assert_called_once_with(1, "sent")


if __name__ == "__main__":
    unittest.main()
