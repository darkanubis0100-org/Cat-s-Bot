import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from cogs.logging import Logging
from cogs.serverutils import ServerUtils, ServerLogsView


class MemberAuditLoggingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.entry_id = 1566511831835742250
        self.config = {"serverlog_enabled": 1, "serverlog_channel": 900, "log_events": '{"role_change":true,"nickname_changes":true,"audit_events":true}'}
        self.output = SimpleNamespace(id=900, mention="<#900>", send=AsyncMock())
        self.guild = SimpleNamespace(id=10, owner_id=1, name="Test", _state=SimpleNamespace(get_emoji=lambda _ident: None),
                                     get_member=lambda _ident: None, get_thread=lambda _ident: None)
        self.default = self.role(10, 0)
        self.high = self.role(50, 142)
        self.managed = self.role(51, 2, managed=True)
        self.roles = {10: self.default, 50: self.high, 51: self.managed}
        self.guild.get_role = self.roles.get
        self.guild.get_channel = lambda ident: self.output if ident == 900 else None
        self.guild.get_channel_or_thread = self.guild.get_channel
        self.guild.me = SimpleNamespace(id=99, top_role=self.role(99, 141), guild_permissions=discord.Permissions(view_audit_log=True))
        self.bot = SimpleNamespace(intents=discord.Intents.default(), db=SimpleNamespace(
            get_server_config=lambda _ident: self.config, enqueue_log_outbox=Mock()), get_guild=lambda _ident: self.guild)
        self.cog = Logging(self.bot)
        self.cog._send_log = AsyncMock()

    def role(self, ident, position, managed=False):
        return discord.Role(guild=self.guild, state=None, data={"id": str(ident), "name": f"Role {ident}", "permissions": "0", "position": position, "managed": managed})

    def member(self, roles, nick=None):
        return SimpleNamespace(id=42, guild=self.guild, nick=nick, roles=roles,
                               display_name="Member", mention="<@42>", display_avatar=SimpleNamespace(url="https://example.test/avatar.png"))

    def entry(self, action, changes=None, **data):
        data.setdefault("id", str(self.entry_id))
        self.entry_id += 1
        return discord.AuditLogEntry(users={}, integrations={}, app_commands={}, automod_rules={}, webhooks={},
            guild=self.guild, data={"id": "1566511831835742250", "action_type": action.value,
                                   "user_id": "43", "target_id": "42", "changes": changes or [], **data})

    def text(self):
        return "\n".join(str(call.args[1].to_dict()) for call in self.cog._send_log.await_args_list)

    async def test_gateway_fallback_records_high_and_managed_roles_in_both_directions(self):
        self.bot.intents.moderation = False
        before = self.member([self.default])
        after = self.member([self.default, self.high, self.managed])
        await self.cog.on_member_update(before, after)
        await self.cog.on_member_update(after, before)
        self.assertIn("50", self.text())
        self.assertIn("51", self.text())
        self.assertEqual(self.cog._send_log.await_count, 2)

    async def test_native_timeout_gateway_fallback_without_audit_access(self):
        self.guild.me.guild_permissions = discord.Permissions.none()
        before, after = self.member([self.default]), self.member([self.default])
        before.timed_out_until = None
        after.timed_out_until = datetime(2026, 11, 7, 19, tzinfo=timezone.utc)
        await self.cog.on_member_update(before, after)
        await self.cog.on_member_update(after, before)
        await self.cog.on_member_update(after, after)
        self.assertEqual(self.cog._send_log.await_count, 2)
        self.assertIn("2026-11-07T19:00:00+00:00", self.text())
        self.assertIn("Sin configurar", self.text())
        self.assertNotIn("Autor", self.text())
        self.assertTrue(all(call.args[2] == "audit_events" for call in self.cog._send_log.await_args_list))

    async def test_native_timeout_uses_audit_source_once_when_available(self):
        before, after = self.member([self.default]), self.member([self.default])
        before.timed_out_until = None
        after.timed_out_until = datetime(2026, 11, 7, 19, tzinfo=timezone.utc)
        await self.cog.on_member_update(before, after)
        self.cog._send_log.assert_not_awaited()
        await self.cog.on_audit_log_entry_create(self.entry(discord.AuditLogAction.member_update,
            [{"key": "communication_disabled_until", "old_value": None,
              "new_value": after.timed_out_until.isoformat()}]))
        self.cog._send_log.assert_awaited_once()
        self.assertEqual(self.cog._send_log.await_args.args[2], "audit_events")
        self.assertIn("2026-11-07T19:00:00+00:00", self.text())

    async def test_native_timeout_gateway_fallback_respects_disabled_audit_filter(self):
        self.guild.me.guild_permissions = discord.Permissions.none()
        self.config["log_events"] = '{"audit_events":false,"role_change":true,"nickname_changes":true}'
        self.cog._send_log = Logging._send_log.__get__(self.cog, Logging)
        before, after = self.member([self.default]), self.member([self.default])
        before.timed_out_until = None
        after.timed_out_until = datetime(2026, 11, 7, 19, tzinfo=timezone.utc)
        await self.cog.on_member_update(before, after)
        self.output.send.assert_not_awaited()
        self.bot.db.enqueue_log_outbox.assert_not_called()

    async def test_audit_is_only_role_source_and_works_without_cached_actor_or_target(self):
        before, after = self.member([self.default]), self.member([self.default, self.high])
        await self.cog.on_member_update(before, after)
        self.cog._send_log.assert_not_awaited()
        for change in ("$add", "$remove"):
            entry = self.entry(discord.AuditLogAction.member_role_update, [{"key": change, "new_value": [{"id": "50", "name": "Role 50"}]}])
            await self.cog.on_audit_log_entry_create(entry)
        self.assertEqual(self.cog._send_log.await_count, 2)
        for value in ("42", "43", "50", "1566511831835742250"):
            self.assertIn(value, self.text())

    async def test_role_toggle_cannot_be_reenabled_by_general_audit_or_legacy_alias(self):
        self.config["log_events"] = '{"role_change":false,"role_changes":true,"audit_events":true}'
        self.cog._send_log = Logging._send_log.__get__(self.cog, Logging)
        for action in (discord.AuditLogAction.member_role_update, discord.AuditLogAction.role_update):
            changes = ([{"key": "$add", "new_value": [{"id": "50", "name": "Role 50"}]}]
                       if action == discord.AuditLogAction.member_role_update else [{"key": "name", "old_value": "old", "new_value": "new"}])
            await self.cog.on_audit_log_entry_create(self.entry(action, changes))
        self.output.send.assert_not_awaited()
        self.bot.db.enqueue_log_outbox.assert_not_called()
        await self.cog.on_audit_log_entry_create(self.entry(discord.AuditLogAction.guild_update, [{"key": "name", "old_value": "old", "new_value": "new"}]))
        self.output.send.assert_awaited_once()
        self.assertEqual(self.output.send.await_args.kwargs["allowed_mentions"].to_dict(), {"parse": []})

    async def test_other_families_are_logged_but_existing_gateway_families_are_not_duplicated(self):
        for action in (discord.AuditLogAction.guild_update, discord.AuditLogAction.webhook_update,
                       discord.AuditLogAction.emoji_update, discord.AuditLogAction.thread_update):
            await self.cog.on_audit_log_entry_create(self.entry(action, [{"key": "name", "old_value": "old", "new_value": "new"}]))
        self.assertEqual(self.cog._send_log.await_count, 4)
        self.cog._send_log.reset_mock()
        for action in (discord.AuditLogAction.channel_update, discord.AuditLogAction.overwrite_update,
                       discord.AuditLogAction.ban):
            await self.cog.on_audit_log_entry_create(self.entry(action))
        self.cog._send_log.assert_not_awaited()

    async def test_administrative_activity_logs_uncached_actor_once_and_keeps_category_filters(self):
        for index, (action, event, options) in enumerate((
            (discord.AuditLogAction.message_delete, "message_delete", {"count": "2", "channel_id": "700"}),
            (discord.AuditLogAction.message_bulk_delete, "bulk_message_delete", {"count": "4"}),
            (discord.AuditLogAction.member_move, "voice_state_update", {"count": "1", "channel_id": "700"}),
            (discord.AuditLogAction.member_disconnect, "voice_state_update", {"count": "3"}),
        )):
            self.cog._send_log.reset_mock()
            entry = self.entry(action, options=options, id=str(1566511831835742300 + index))
            await self.cog.on_audit_log_entry_create(entry)
            await self.cog.on_audit_log_entry_create(entry)
            self.cog._send_log.assert_awaited_once()
            self.assertEqual(self.cog._send_log.await_args.args[2], event)
            self.assertIn("Auditoría", self.cog._send_log.await_args.args[1].title)
            self.assertIn("ID 43", self.text())
            fields = {field.name: field.value for field in self.cog._send_log.await_args.args[1].fields}
            self.assertEqual(fields["Cantidad"], options["count"])
            if "channel_id" in options:
                self.assertIn("700", self.text())
        self.cog._send_log = Logging._send_log.__get__(self.cog, Logging)
        self.config["log_events"] = '{"message_delete":false,"voice_state_update":false,"audit_events":true}'
        for action in (discord.AuditLogAction.message_delete, discord.AuditLogAction.member_disconnect):
            await self.cog.on_audit_log_entry_create(self.entry(action, id=str(1566511831835742400 + action.value)))
        self.output.send.assert_not_awaited()
        self.bot.db.enqueue_log_outbox.assert_not_called()

    async def test_unknown_member_attributes_are_reported_without_their_values(self):
        entry = self.entry(discord.AuditLogAction.member_update,
                           [{"key": "future_attribute", "old_value": "SECRET_OLD", "new_value": "SECRET_NEW"}])
        await self.cog.on_audit_log_entry_create(entry)
        self.assertIn("future", self.text())
        self.assertNotIn("SECRET", self.text())
        self.assertEqual(self.cog._send_log.await_args.args[2], "audit_events")

    async def test_unknown_audit_action_from_bot_actor_gets_safe_summary(self):
        entry = self.entry(SimpleNamespace(value=99999))
        entry.user = SimpleNamespace(id=99, display_name="Other bot", bot=True)
        await self.cog.on_audit_log_entry_create(entry)
        self.cog._send_log.assert_awaited_once()
        self.assertIn("unknown 99999", self.text())
        self.assertIn("Other bot", self.text())
        self.assertIn("ID 99", self.text())

    async def test_audit_payload_omits_secrets_and_bounds_long_roles_and_changes(self):
        huge = [{"id": str(1000+i), "name": "😀" * 100} for i in range(100)]
        entry = self.entry(discord.AuditLogAction.member_role_update, [{"key": "$add", "new_value": huge}], reason="😀" * 1000)
        await self.cog.on_audit_log_entry_create(entry)
        await self.cog.on_audit_log_entry_create(self.entry(discord.AuditLogAction.webhook_update,
            [{"key": "token", "new_value": "NEVER_LOG_THIS_TOKEN"}, {"key": "name", "old_value": "old", "new_value": "new"}]))
        self.assertNotIn("NEVER_LOG_THIS_TOKEN", self.text())
        for call in self.cog._send_log.await_args_list:
            embed = call.args[1]
            text = embed.title + embed.footer.text + "".join(field.name + field.value for field in embed.fields)
            self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 6000)
            for field in embed.fields:
                self.assertLessEqual(len(field.name.encode("utf-16-le")) // 2, 256)
                self.assertLessEqual(len(field.value.encode("utf-16-le")) // 2, 1024)

    async def test_raw_message_fallbacks_do_not_duplicate_cached_messages(self):
        message = SimpleNamespace(id=101)
        await self.cog.on_raw_message_delete(SimpleNamespace(guild_id=10, channel_id=20, message_id=101, cached_message=message))
        self.cog._send_log.assert_not_awaited()
        await self.cog.on_raw_message_delete(SimpleNamespace(guild_id=10, channel_id=20, message_id=102, cached_message=None))
        await self.cog.on_raw_message_edit(SimpleNamespace(guild_id=10, channel_id=20, message_id=103, cached_message=None, data={"content": "changed"}))
        self.assertEqual(self.cog._send_log.await_count, 2)
        self.assertIn("102", self.text())
        self.assertIn("103", self.text())
        self.assertIn("caché", self.text())
        self.cog._send_log.reset_mock()
        identifiers = set(range(1566511831835742250, 1566511831835742350))
        cached_id = min(identifiers)
        await self.cog.on_raw_bulk_message_delete(SimpleNamespace(guild_id=10, channel_id=20,
            message_ids=identifiers, cached_messages=[SimpleNamespace(id=cached_id)]))
        text = self.text()
        self.assertNotIn(str(cached_id), text)
        for ident in identifiers - {cached_id}:
            self.assertEqual(text.count(str(ident)), 1)
        for call in self.cog._send_log.await_args_list:
            self.assertTrue(all(len(field.value) <= 1024 for field in call.args[1].fields))

    async def test_slash_preserves_other_flags_and_canonical_disabled_aliases(self):
        self.config["log_events"] = json.dumps({"role_change": False, "role_changes": True,
            "nickname_changes": None, "member_update": True, "channel_create": False, "future_event": True,
            "voice_state_update": True})
        server = ServerUtils(self.bot)
        events = server._get_log_events(10)
        self.assertFalse(events["role_changes"])
        self.assertFalse(events["nickname_changes"])
        self.assertFalse(events["audit_events"])
        self.bot.db.set_server_config = Mock()
        view = ServerLogsView(server, 43, 10, events)
        interaction = SimpleNamespace(user=SimpleNamespace(id=43, guild_permissions=discord.Permissions(administrator=True)),
            guild_id=10, guild=self.guild, data={"values": ["audit_events"]}, response=SimpleNamespace(edit_message=AsyncMock()))
        await view._toggle_callback(interaction)
        saved = json.loads(self.bot.db.set_server_config.call_args.kwargs["log_events"])
        self.assertTrue(saved["audit_events"])
        self.assertFalse(saved["role_change"])
        self.assertFalse(saved["channel_create"])
        self.assertTrue(saved["future_event"])
        self.assertFalse(saved["voice_state_update"])
        self.assertFalse(Logging._event_enabled(saved, "voice_state_update"))
        for raw in (["role_changes", "future_event"], {"role_changes": True}, {}):
            self.config["log_events"] = json.dumps(raw)
            events = server._get_log_events(10)
            self.assertTrue(events["role_changes"])
            self.assertEqual(events["audit_events"], raw == {})


if __name__ == "__main__":
    unittest.main()
