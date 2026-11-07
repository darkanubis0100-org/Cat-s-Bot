"""
cogs/logging.py
────────────────
Server event logging system.

Logs configurable Discord events to a designated channel.
Configuration is managed via the dashboard web panel.
"""

import json
import logging
import time
from datetime import datetime, timezone

import discord
from discord.ext import commands

logger = logging.getLogger(__name__)

CHANNEL_SETTINGS = (
    ("name", "Nombre"), ("topic", "Tema"), ("type", "Tipo"),
    ("position", "Posición"), ("category_id", "Categoría"),
    ("nsfw", "NSFW"), ("slowmode_delay", "Modo lento"),
    ("bitrate", "Bitrate"), ("user_limit", "Límite de usuarios"),
    ("rtc_region", "Región de voz"), ("video_quality_mode", "Calidad de vídeo"),
    ("default_auto_archive_duration", "Autoarchivo de hilos"),
    ("default_thread_slowmode_delay", "Modo lento de hilos"),
    ("default_sort_order", "Orden del foro"), ("default_layout", "Vista del foro"),
    ("default_reaction_emoji", "Reacción del foro"), ("available_tags", "Etiquetas del foro"),
    ("permissions_synced", "Permisos sincronizados con categoría"),
)
PERMISSION_NAMES = {
    "view_channel": "Ver canal", "read_messages": "Ver canal", "send_messages": "Enviar mensajes",
    "manage_channels": "Gestionar canal", "manage_roles": "Gestionar permisos",
    "read_message_history": "Leer historial", "add_reactions": "Añadir reacciones",
    "connect": "Conectar", "speak": "Hablar", "stream": "Vídeo",
    "attach_files": "Adjuntar archivos", "embed_links": "Insertar enlaces",
    "manage_messages": "Gestionar mensajes", "mention_everyone": "Mencionar a todos",
    "send_messages_in_threads": "Enviar en hilos", "create_public_threads": "Crear hilos públicos",
    "create_private_threads": "Crear hilos privados", "manage_threads": "Gestionar hilos",
    "create_instant_invite": "Crear invitaciones", "administrator": "Administrador",
    "kick_members": "Expulsar miembros", "ban_members": "Banear miembros",
    "manage_guild": "Gestionar servidor", "view_audit_log": "Ver registro de auditoría",
    "priority_speaker": "Prioridad de palabra", "send_tts_messages": "Enviar mensajes de voz sintetizada",
    "external_emojis": "Usar emojis externos", "external_stickers": "Usar stickers externos",
    "view_guild_insights": "Ver estadísticas del servidor", "mute_members": "Silenciar miembros",
    "deafen_members": "Ensordecer miembros", "move_members": "Mover miembros",
    "use_voice_activation": "Usar actividad de voz", "change_nickname": "Cambiar apodo",
    "manage_nicknames": "Gestionar apodos", "manage_webhooks": "Gestionar webhooks",
    "manage_expressions": "Gestionar emojis y stickers", "create_expressions": "Crear emojis y stickers",
    "use_application_commands": "Usar comandos de aplicaciones", "request_to_speak": "Pedir la palabra",
    "manage_events": "Gestionar eventos", "create_events": "Crear eventos",
    "use_embedded_activities": "Usar actividades", "moderate_members": "Aislar miembros",
    "view_creator_monetization_analytics": "Ver estadísticas de monetización",
    "use_soundboard": "Usar panel de sonidos", "use_external_sounds": "Usar sonidos externos",
    "send_voice_messages": "Enviar mensajes de voz", "set_voice_channel_status": "Cambiar estado del canal de voz",
    "send_polls": "Crear encuestas", "use_external_apps": "Usar aplicaciones externas",
    "pin_messages": "Fijar mensajes", "bypass_slowmode": "Omitir modo lento",
}
AUDIT_FIELDS = {
    "name": "Nombre", "description": "Descripción", "topic": "Tema", "nick": "Apodo",
    "permissions": "Permisos", "colour": "Color", "hoist": "Rol separado", "mentionable": "Rol mencionable",
    "position": "Posición", "type": "Tipo", "enabled": "Activo", "flags": "Opciones",
    "verification_level": "Verificación", "explicit_content_filter": "Filtro de contenido",
    "default_notifications": "Notificaciones", "afk_channel": "Canal AFK", "afk_timeout": "Tiempo AFK",
    "system_channel": "Canal del sistema", "rules_channel": "Canal de reglas", "owner": "Propietario",
    "timed_out_until": "Aislamiento hasta", "mute": "Silenciado", "deaf": "Ensordecido",
    "archived": "Archivado", "locked": "Bloqueado", "invitable": "Invitaciones permitidas",
    "slowmode_delay": "Modo lento", "auto_archive_duration": "Autoarchivo",
    "channel": "Canal", "roles": "Roles", "exempt_roles": "Roles exentos", "exempt_channels": "Canales exentos",
    "trigger_type": "Tipo de regla", "event_type": "Tipo de evento", "privacy_level": "Privacidad",
    "start_time": "Inicio", "end_time": "Fin", "status": "Estado", "unicode_emoji": "Emoji",
}
AUDIT_GATEWAY_ACTIONS = {
    "channel_create", "channel_update", "channel_delete", "overwrite_create", "overwrite_update", "overwrite_delete",
    "ban", "unban",
}
AUDIT_ACTIVITY_EVENTS = {
    "message_delete": "message_delete", "message_bulk_delete": "bulk_message_delete",
    "member_move": "voice_state_update", "member_disconnect": "voice_state_update",
}
AUDIT_ACTION_NAMES = {
    "guild_update": "Servidor actualizado", "member_role_update": "Roles de miembro", "member_update": "Miembro actualizado",
    "role_create": "Rol creado", "role_update": "Rol actualizado", "role_delete": "Rol eliminado",
    "kick": "Miembro expulsado", "member_prune": "Depuración de miembros", "bot_add": "Bot añadido",
    "message_pin": "Mensaje fijado", "message_unpin": "Mensaje desfijado",
    "message_delete": "Auditoría · Mensajes eliminados", "message_bulk_delete": "Auditoría · Eliminación en lote",
    "member_move": "Auditoría · Miembros movidos", "member_disconnect": "Auditoría · Miembros desconectados",
}


def _log_clip(value, limit):
    text = discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))
    encoded = text.encode("utf-16-le")
    if len(encoded) > limit * 2:
        return encoded[:(limit - 1) * 2].decode("utf-16-le", errors="ignore") + "…"
    return text


def _channel_setting(channel, attribute):
    value = getattr(channel, attribute, None)
    if attribute == "available_tags" and value is not None:
        return "; ".join(f"{tag.name} (ID {tag.id}, emoji {tag.emoji or 'ninguno'}, moderada: {'sí' if tag.moderated else 'no'})" for tag in value) or "Sin etiquetas"
    if attribute == "category_id" and value is not None:
        category = getattr(channel, "category", None)
        return f"{category.name} (ID {value})" if category else str(value)
    if value is None:
        return "Sin configurar"
    if isinstance(value, bool):
        return "Sí" if value else "No"
    if attribute in {"slowmode_delay", "default_thread_slowmode_delay"}:
        return f"{value} s"
    if attribute == "default_auto_archive_duration":
        return f"{value} min"
    if attribute == "bitrate":
        return f"{value / 1000:g} kbps"
    if attribute == "user_limit" and value == 0:
        return "Sin límite"
    if attribute == "default_reaction_emoji":
        return str(value)
    return str(getattr(value, "name", value))


def _channel_changes(before, after):
    changes = []
    for attribute, label in CHANNEL_SETTINGS:
        old, new = getattr(before, attribute, None), getattr(after, attribute, None)
        if attribute == "available_tags":
            old, new = [None if value is None else [(tag.id, tag.name, tag.moderated, str(tag.emoji)) for tag in value] for value in (old, new)]
        if old != new:
            old, new = _channel_setting(before, attribute), _channel_setting(after, attribute)
            changes.append((label, f"Antes: {_log_clip(old, 480)}\nDespués: {_log_clip(new, 480)}"))
    for flag, label in (("require_tag", "Exigir etiqueta"), ("hide_media_download_options", "Ocultar descarga multimedia")):
        old = getattr(getattr(before, "flags", None), flag, False)
        new = getattr(getattr(after, "flags", None), flag, False)
        if old != new:
            changes.append((label, f"Antes: {'Sí' if old else 'No'}\nDespués: {'Sí' if new else 'No'}"))

    def overwrites(channel):
        return {("Rol" if isinstance(target, discord.Role) or getattr(target, "type", None) is discord.Role else "Usuario", target.id): (target, dict(overwrite))
                for target, overwrite in getattr(channel, "overwrites", {}).items()}

    old_overwrites, new_overwrites = overwrites(before), overwrites(after)
    states = {None: "Heredados", True: "Permitidos", False: "Bloqueados"}
    for key in sorted(old_overwrites.keys() | new_overwrites.keys()):
        target, old = old_overwrites.get(key, (None, {}))
        new_target, new = new_overwrites.get(key, (None, {}))
        target = new_target or target
        transitions = {}
        for permission in sorted(old.keys() | new.keys()):
            if old.get(permission) != new.get(permission):
                transition = f"{states[old.get(permission)]} → {states[new.get(permission)]}"
                label = PERMISSION_NAMES.get(permission, permission.replace("_", " ").capitalize())
                transitions.setdefault(transition, []).append(label)
        if not transitions and key in old_overwrites and key in new_overwrites:
            continue
        action = "añadido" if key not in old_overwrites else "eliminado" if key not in new_overwrites else "modificado"
        name = getattr(target, "display_name", getattr(target, "name", None))
        identity = getattr(target, "mention", None) if name else None
        identity = identity or (_log_clip(name, 120) if name else f"ID {key[1]} (sin caché)")
        heading = f"Permisos · {key[0]} · {action}"
        chunk = identity
        for transition, labels in transitions.items():
            line = f"{transition}: "
            for label in labels:
                separator = "" if line.endswith(": ") else ", "
                if len((chunk + "\n" + line + separator + label).encode("utf-16-le")) // 2 > 1024:
                    changes.append((heading, chunk + "\n" + line if separator else chunk))
                    chunk, line = identity, f"{transition}: "
                    separator = ""
                line += separator + label
            chunk += "\n" + line
        changes.append((heading, chunk if transitions else identity + "\nSin permisos explícitos; se heredan."))
    return changes


def _audit_value(value):
    if value is None:
        return "Sin configurar"
    if isinstance(value, bool):
        return "Sí" if value else "No"
    if isinstance(value, discord.Permissions):
        return ", ".join(PERMISSION_NAMES.get(name, name) for name, enabled in value if enabled) or "Sin permisos"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return ", ".join(_audit_value(item) for item in value) or "Ninguno"
    if hasattr(value, "id"):
        return f"{getattr(value, 'display_name', getattr(value, 'name', 'Sin caché'))} (ID {value.id})"
    if isinstance(value, (str, int, float)):
        return str(value)
    if hasattr(value, "value"):
        return str(value.value)
    return "Valor actualizado"  # No volcar repr del SDK ni estructuras que puedan contener secretos.


def _role_change_fields(added, removed):
    fields = []
    for title, roles in (("Roles añadidos", added), ("Roles quitados", removed)):
        chunk = ""
        for role in roles:
            line = f"{_log_clip(getattr(role, 'name', 'Sin caché'), 120)} (ID {role.id})"
            if len((chunk + "\n" + line).encode("utf-16-le")) // 2 > 960:
                fields.append((title, chunk))
                chunk = ""
            chunk = f"{chunk}\n{line}" if chunk else line
        if chunk:
            fields.append((title, chunk))
    return fields


class Logging(commands.Cog):
    """Logs server events to a configurable channel."""

    EVENT_ALIASES = {
        "channel_update": ("channel_update", "channel_updates"),
        "role_change": ("role_change", "role_changes", "member_update"),
        "nickname_changes": ("nickname_changes", "member_update"),
        "bulk_message_delete": ("bulk_message_delete", "message_delete"),
        "member_update": ("member_update", "role_change", "nickname_changes", "role_changes"),
        "voice_state_update": ("voice_state_update", "voice_join", "voice_leave", "voice_join_leave"),
        "member_unban": ("member_unban", "member_ban"),
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db  # type: ignore
        self._last_log: dict[int, float] = {}
        self._recent_audit_ids: dict[int, None] = {}

    @classmethod
    def _event_enabled(cls, raw, event_name: str) -> bool:
        aliases = cls.EVENT_ALIASES.get(event_name, (event_name,))
        if raw is None:
            return True
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return True
        if isinstance(raw, dict):
            if not raw:
                return True
            if event_name in {"channel_update", "role_change", "nickname_changes", "audit_events"} and event_name in raw:
                return bool(raw[event_name])
            return any(bool(raw.get(key)) for key in aliases)
        if isinstance(raw, list):
            return any(key in raw for key in aliases)
        return True

    def _nearby_messageable_channel(self, guild: discord.Guild, channel_id: int):
        candidates = [
            ch
            for ch in getattr(guild, "channels", [])
            if callable(getattr(ch, "send", None)) and abs(int(ch.id) - int(channel_id)) <= 4096
        ]
        return candidates[0] if len(candidates) == 1 else None

    async def _resolve_serverlog_channel(self, guild: discord.Guild, raw_channel_id):
        try:
            channel_id = int(raw_channel_id)
        except (TypeError, ValueError):
            return None, raw_channel_id, "invalid_channel_id"

        channel = guild.get_channel(channel_id)
        if callable(getattr(channel, "send", None)):
            return channel, channel.id, None

        repaired = self._nearby_messageable_channel(guild, channel_id)
        if repaired is not None:
            logger.warning(
                "serverlog channel_id redondeado reparado en %s: %s -> %s",
                guild.name,
                channel_id,
                repaired.id,
            )
            try:
                self.db.set_server_config(guild.id, serverlog_channel=repaired.id)
            except Exception:
                logger.debug("No se pudo persistir serverlog_channel reparado", exc_info=True)
            return repaired, repaired.id, None

        try:
            fetched = await guild.fetch_channel(channel_id)
            if callable(getattr(fetched, "send", None)):
                return fetched, fetched.id, None
            return fetched, channel_id, "target_is_not_messageable"
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            return None, channel_id, str(exc)

    async def _send_log(self, guild: discord.Guild, embed: discord.Embed, event_name: str) -> None:
        srv_cfg = self.db.get_server_config(guild.id)
        if not srv_cfg.get("serverlog_enabled", 1):
            return

        raw = srv_cfg.get("log_events", "[]")
        if not self._event_enabled(raw, event_name):
            return

        ch_id = srv_cfg.get("serverlog_channel")
        if not ch_id:
            return

        channel, resolved_ch_id, resolve_error = await self._resolve_serverlog_channel(guild, ch_id)
        if channel is None or not callable(getattr(channel, "send", None)):
            logger.warning("serverlog channel no accesible en %s (%s): %s", guild.name, ch_id, resolve_error)
            self._enqueue_serverlog_outbox(guild, embed, event_name, resolved_ch_id or ch_id, resolve_error or "channel_unavailable")
            return

        now = time.time()
        last = self._last_log.get(guild.id, 0.0)
        if now - last < 2.0:
            self._enqueue_serverlog_outbox(guild, embed, event_name, resolved_ch_id, "rate_limited")
            return
        self._last_log[guild.id] = now

        try:
            from cogs._managed_messages import send_managed
            await send_managed(self.bot, guild.id, "serverutils", channel.send, embed=embed,
                               allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            logger.warning("Sin permisos para enviar serverlog en %s", guild.name)
            self._enqueue_serverlog_outbox(guild, embed, event_name, resolved_ch_id, "missing_send_messages")
        except discord.HTTPException as exc:
            logger.warning("Error enviando serverlog en %s: %s", guild.name, exc)
            self._enqueue_serverlog_outbox(guild, embed, event_name, resolved_ch_id, str(exc))

    def _enqueue_serverlog_outbox(
        self,
        guild: discord.Guild,
        embed: discord.Embed,
        event_name: str,
        channel_id,
        error: str,
    ) -> None:
        try:
            self.db.enqueue_log_outbox(
                guild.id,
                event_name,
                {
                    "embed": embed.to_dict(),
                    "source": "logging_cog",
                    "event": event_name,
                    "last_error": (error or "")[:500],
                },
                channel_id=int(channel_id),
            )
        except Exception as exc:
            logger.warning("No se pudo encolar serverlog fallido en %s: %s", guild.name, exc)

    # ── Message events ──────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.author.bot or not after.guild:
            return
        if before.content == after.content:
            return

        embed = discord.Embed(
            title="✏️ Mensaje editado",
            color=discord.Color.yellow(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=after.author.display_name, icon_url=after.author.display_avatar.url)
        embed.add_field(name="Canal", value=after.channel.mention, inline=True)
        embed.add_field(name="Autor", value=after.author.mention, inline=True)
        if before.content:
            embed.add_field(name="Antes", value=before.content[:1024], inline=False)
        embed.add_field(name="Después", value=after.content[:1024], inline=False)
        embed.add_field(name="Enlace", value=f"[Ir al mensaje]({after.jump_url})", inline=False)
        embed.set_footer(text=f"ID: {after.id}")

        await self._send_log(after.guild, embed, "message_edit")

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        embed = discord.Embed(
            title="🗑️ Mensaje eliminado",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
        embed.add_field(name="Canal", value=message.channel.mention, inline=True)
        embed.add_field(name="Autor", value=message.author.mention, inline=True)
        if message.content:
            embed.add_field(name="Contenido", value=message.content[:1024], inline=False)
        embed.set_footer(text=f"ID: {message.id}")

        await self._send_log(message.guild, embed, "message_delete")

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]):
        if not messages:
            return
        guild = messages[0].guild
        if not guild:
            return

        embed = discord.Embed(
            title="🗑️ Mensajes eliminados en lote",
            color=discord.Color.dark_red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Cantidad", value=str(len(messages)), inline=True)
        embed.add_field(name="Canal", value=messages[0].channel.mention, inline=True)
        embed.set_footer(text=f"Canal ID: {messages[0].channel.id}")

        await self._send_log(guild, embed, "bulk_message_delete")

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        if payload.cached_message is not None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        embed = discord.Embed(title="🗑️ Mensaje eliminado (sin caché)", color=discord.Color.red(), timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Canal / mensaje", value=f"Canal ID {payload.channel_id}\nMensaje ID {payload.message_id}")
        embed.add_field(name="Contenido anterior", value="No disponible: el mensaje no estaba en caché.", inline=False)
        await self._send_log(guild, embed, "message_delete")

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload):
        uncached = set(payload.message_ids) - {message.id for message in payload.cached_messages}
        guild = self.bot.get_guild(payload.guild_id)
        if not uncached or guild is None:
            return
        fields = [("Canal", f"ID {payload.channel_id}"), ("Mensajes no disponibles en caché", str(len(uncached)))]
        identifiers = sorted(uncached)
        for start in range(0, len(identifiers), 40):
            fields.append(("IDs", ", ".join(str(ident) for ident in identifiers[start:start+40])))
        await self._send_fields(guild, "🗑️ Mensajes eliminados en lote (sin caché)", fields, "bulk_message_delete")

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload):
        if payload.cached_message is not None or "content" not in payload.data:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        embed = discord.Embed(title="✏️ Mensaje editado (sin caché)", color=discord.Color.yellow(), timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Canal / mensaje", value=f"Canal ID {payload.channel_id}\nMensaje ID {payload.message_id}")
        embed.add_field(name="Antes", value="No disponible: el mensaje no estaba en caché.", inline=False)
        embed.add_field(name="Después", value=_log_clip(payload.data.get("content") or "Sin texto", 960), inline=False)
        await self._send_log(guild, embed, "message_edit")

    # ── Member events ───────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        guild = after.guild
        # La auditoría incluye miembros no cacheados y el actor real; una sola fuente.
        if self._audit_available(guild):
            return
        added = [role for role in after.roles if role not in before.roles and role.id != guild.id]
        removed = [role for role in before.roles if role not in after.roles and role.id != guild.id]
        identity = [("Miembro", _log_clip(_audit_value(after), 240))]
        fields = _role_change_fields(added, removed)
        if fields:
            await self._send_fields(guild, "📝 Roles de miembro", identity + fields, "role_change")
        if before.nick != after.nick:
            await self._send_fields(guild, "📝 Apodo de miembro", identity + [
                ("Apodo", f"Antes: {_log_clip(before.nick or 'Ninguno', 450)}\nDespués: {_log_clip(after.nick or 'Ninguno', 450)}")], "nickname_changes")
        old_timeout, new_timeout = getattr(before, "timed_out_until", None), getattr(after, "timed_out_until", None)
        if old_timeout != new_timeout:
            await self._send_fields(guild, "⏱️ Aislamiento de miembro", identity + [
                ("Aislamiento hasta", f"Antes: {_log_clip(_audit_value(old_timeout), 450)}\nDespués: {_log_clip(_audit_value(new_timeout), 450)}")], "audit_events")

    def _audit_available(self, guild):
        permissions = getattr(getattr(guild, "me", None), "guild_permissions", None)
        return bool(getattr(getattr(self.bot, "intents", None), "moderation", False)
                    and permissions and (permissions.administrator or permissions.view_audit_log))

    async def _send_fields(self, guild, title, fields, event_name, *, entry_id=None):
        for start in range(0, len(fields), 4):
            embed = discord.Embed(title=title, color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
            for name, value in fields[start:start+4]:
                embed.add_field(name=name, value=value, inline=False)
            embed.set_footer(text=f"Auditoría ID {entry_id} · {start//4 + 1}/{(len(fields)+3)//4} · … = texto recortado" if entry_id else f"Gateway · {start//4 + 1}/{(len(fields)+3)//4}")
            await self._send_log(guild, embed, event_name)

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry):
        if not self._audit_available(entry.guild):
            return
        action = entry.action.name
        if action in AUDIT_GATEWAY_ACTIONS:
            return
        if entry.id in self._recent_audit_ids:
            return
        self._recent_audit_ids[entry.id] = None
        # shortcut: dedup keeps the latest 512 IDs in memory; use durable IDs if replay is added.
        if len(self._recent_audit_ids) > 512:
            self._recent_audit_ids.pop(next(iter(self._recent_audit_ids)))
        identity = [("Objetivo", _log_clip(_audit_value(entry.target), 240)),
                    ("Autor", _log_clip(_audit_value(entry.user) if entry.user else f"ID {entry.user_id}" if entry.user_id else "No informado", 240))]
        if entry.reason:
            identity.append(("Razón", _log_clip(entry.reason, 480)))
        title = "📋 " + AUDIT_ACTION_NAMES.get(action, "Auditoría · " + action.replace("_", " "))
        if action == "member_role_update":
            fields = _role_change_fields(getattr(entry.after, "roles", []), getattr(entry.before, "roles", []))
            if fields:
                await self._send_fields(entry.guild, title, identity + fields, "role_change", entry_id=entry.id)
            return
        old, new = dict(entry.before), dict(entry.after)
        changed = {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
        if action == "member_update" and "nick" in changed:
            fields = [("Apodo", f"Antes: {_log_clip(_audit_value(old.get('nick')), 450)}\nDespués: {_log_clip(_audit_value(new.get('nick')), 450)}")]
            await self._send_fields(entry.guild, title, identity + fields, "nickname_changes", entry_id=entry.id)
        fields = []
        for key, label in AUDIT_FIELDS.items():
            if key == "nick" or key not in old.keys() | new.keys() or old.get(key) == new.get(key):
                continue
            fields.append((label, f"Antes: {_log_clip(_audit_value(old.get(key)), 450)}\nDespués: {_log_clip(_audit_value(new.get(key)), 450)}"))
        omitted = sorted(changed - AUDIT_FIELDS.keys())
        if omitted:
            fields.append(("Otros atributos modificados (sin valores)", _log_clip(", ".join(omitted), 960)))
        if action in AUDIT_ACTIVITY_EVENTS:
            for key, label in (("count", "Cantidad"), ("channel", "Canal")):
                value = getattr(getattr(entry, "extra", None), key, None)
                if value is not None:
                    fields.append((label, _log_clip(_audit_value(value), 450)))
        if action == "member_update" and not fields:
            return
        if not fields:
            fields = [("Detalles", "Discord registró la acción; no proporcionó cambios compatibles para mostrar.")]
        event_name = AUDIT_ACTIVITY_EVENTS.get(action, "role_change" if action in {"role_create", "role_update", "role_delete"} else "audit_events")
        await self._send_fields(entry.guild, title, identity + fields, event_name, entry_id=entry.id)

    # ── Channel events ──────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        guild = channel.guild

        embed = discord.Embed(
            title="📢 Canal creado",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Nombre", value=channel.mention, inline=True)
        embed.add_field(name="Tipo", value=str(channel.type).title(), inline=True)
        embed.add_field(name="Categoría", value=channel.category.name if channel.category else "Ninguna", inline=True)
        embed.set_footer(text=f"ID: {channel.id}")

        await self._send_log(guild, embed, "channel_create")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        guild = channel.guild

        embed = discord.Embed(
            title="❌ Canal eliminado",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Nombre", value=f"#{channel.name}", inline=True)
        embed.add_field(name="Tipo", value=str(channel.type).title(), inline=True)
        embed.add_field(name="Categoría", value=channel.category.name if channel.category else "Ninguna", inline=True)
        embed.set_footer(text=f"ID: {channel.id}")

        await self._send_log(guild, embed, "channel_delete")

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        changes = _channel_changes(before, after)
        if not changes:
            return
        kind = "Categoría" if after.type == discord.ChannelType.category else "Canal"
        # Cuatro campos por embed mantienen el total por debajo de 6000 caracteres.
        pages = (len(changes) + 3) // 4
        for index in range(pages):
            embed = discord.Embed(
                title=f"✏️ {kind} {'actualizada' if kind == 'Categoría' else 'actualizado'}" + (f" · {index + 1}/{pages}" if pages > 1 else ""),
                color=discord.Color.yellow(), timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name=kind, value=getattr(after, "mention", None) or _log_clip(after.name, 180), inline=False)
            for label, value in changes[index * 4:(index + 1) * 4]:
                embed.add_field(name=label, value=value, inline=False)
            await self._send_log(after.guild, embed, "channel_update")

    # ── Voice events ────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return
        guild = member.guild

        if before.channel == after.channel:
            return

        if before.channel is None and after.channel is not None:
            embed = discord.Embed(
                title="🔊 Se unió a un canal de voz",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
            embed.add_field(name="Usuario", value=member.mention, inline=True)
            embed.add_field(name="Canal", value=after.channel.mention, inline=True)

        elif before.channel is not None and after.channel is None:
            embed = discord.Embed(
                title="🔇 Salió del canal de voz",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
            embed.add_field(name="Usuario", value=member.mention, inline=True)
            embed.add_field(name="Canal", value=before.channel.mention, inline=True)

        else:
            embed = discord.Embed(
                title="🔁 Se movió de canal de voz",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
            embed.add_field(name="Usuario", value=member.mention, inline=True)
            embed.add_field(name="De", value=before.channel.mention, inline=True)
            embed.add_field(name="A", value=after.channel.mention, inline=True)

        await self._send_log(guild, embed, "voice_state_update")

    # ── Ban events ──────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User | discord.Member):
        embed = discord.Embed(
            title="🔨 Usuario baneado",
            color=discord.Color.dark_red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Usuario", value=f"{user} (`{user.id}`)", inline=True)

        try:
            async for entry in guild.audit_logs(action=discord.AuditLogAction.ban, limit=1):
                if entry.target.id == user.id:
                    embed.add_field(name="Moderador", value=entry.user.mention if entry.user else "Desconocido", inline=True)
                    embed.add_field(name="Razón", value=entry.reason or "Sin razón", inline=False)
                    break
        except (discord.Forbidden, discord.HTTPException):
            pass

        embed.set_footer(text=f"ID: {user.id}")
        await self._send_log(guild, embed, "member_ban")

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        embed = discord.Embed(
            title="✅ Usuario desbaneado",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Usuario", value=f"{user} (`{user.id}`)", inline=True)

        try:
            async for entry in guild.audit_logs(action=discord.AuditLogAction.unban, limit=1):
                if entry.target.id == user.id:
                    embed.add_field(name="Moderador", value=entry.user.mention if entry.user else "Desconocido", inline=True)
                    embed.add_field(name="Razón", value=entry.reason or "Sin razón", inline=False)
                    break
        except (discord.Forbidden, discord.HTTPException):
            pass

        embed.set_footer(text=f"ID: {user.id}")
        await self._send_log(guild, embed, "member_unban")


async def setup(bot: commands.Bot):
    await bot.add_cog(Logging(bot))
