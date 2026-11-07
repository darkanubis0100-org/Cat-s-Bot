"""
main.py
───────
Punto de entrada del bot. Carga los cogs, sincroniza los comandos slash
e inyecta la instancia de base de datos en el bot.

Variables de entorno requeridas (.env):
  TOKEN   – Token del bot de Discord
  DB_TYPE – 'sqlite' (único runtime soportado actualmente)
"""

import asyncio
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

from api import iniciar_api
from database import DatabaseManager
from cogs._channel_policy import apply_local_actions, local_actions, policy_active

# ── Cargar .env antes de cualquier otra inicialización ───────────────────────
load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────────────
class DiscordVoiceReconnectFilter(logging.Filter):
    """Baja ruido de reconnects normales del websocket de voz.

    discord.py registra los cierres 1006 del voice websocket como ERROR con
    traceback aunque luego reconecta automáticamente. Mantenerlo como WARNING
    evita que parezca un fallo fatal y conserva la señal en logs.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if (
            record.name == "discord.voice_state"
            and record.levelno >= logging.ERROR
            and str(record.getMessage()).startswith("Disconnected from voice... Reconnecting")
        ):
            record.levelno = logging.WARNING
            record.levelname = "WARNING"
            record.exc_info = None
            record.exc_text = None
        return True


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logging.getLogger("discord.voice_state").addFilter(DiscordVoiceReconnectFilter())
logger = logging.getLogger("Bot")

# ── Intents ───────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.voice_states = True
intents.presences = True


# ── Bot ───────────────────────────────────────────────────────────────────────


class BotCommandTree(discord.app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.type is not discord.InteractionType.application_command:
            return True
        command = interaction.command
        if not isinstance(command, discord.app_commands.Command):
            return True
        expected = {parameter.display_name for parameter in command.parameters}
        received = {name for name, _value in interaction.namespace}
        if received - expected:
            logger.warning(
                "Opciones desconocidas en /%s (guild=%s, options=%s)",
                command.qualified_name,
                interaction.guild_id,
                sorted(received - expected),
            )
            await interaction.response.send_message(
                "❌ Discord envió opciones de una versión anterior del comando. "
                "Ciérralo y vuelve a abrirlo para cargar las opciones actuales. "
                "No se ha ejecutado ninguna acción.",
                ephemeral=True,
            )
            return False
        return True


class BotES(commands.Bot):
    """
    Bot principal.
    - Usa commands.Bot para soporte nativo de cogs y app_commands.
    - La instancia de DB se inyecta como `bot.db` para que todos los
      cogs la compartan sin crear conexiones redundantes.
    """

    def __init__(self):
        super().__init__(
            command_prefix="!",  # Solo para !help
            intents=intents,
            help_command=None,
            tree_cls=BotCommandTree,
        )
        # Instancia única de DB compartida por todos los cogs
        self.db = DatabaseManager()
        self.start_time = discord.utils.utcnow()
        self._guild_command_cleanup_done = False
        self._message_role_checks = {}
        self._message_role_notices = set()
        self._channel_policy_notices = set()

    def dispatch(self, event_name, /, *args, **kwargs):
        # Aprobar antes de que discord.py distribuya a comandos, cogs y wait_for.
        message = None
        if event_name == "message":
            message = args[0]
        elif event_name == "message_edit":
            message = args[1]
        elif event_name == "raw_message_edit":
            message = args[0].message
        if message is None or message.guild is None:
            return super().dispatch(event_name, *args, **kwargs)
        cfg = self.db.get_config(message.guild.id)
        policy_channel_id = message.channel.parent_id if isinstance(message.channel, discord.Thread) else message.channel.id
        local = self.db.get_channel_settings(message.guild.id, policy_channel_id) if hasattr(self.db, "get_channel_settings") else None
        local = local if policy_active(local) else None
        resolve_channel = isinstance(message.channel, discord.PartialMessageable)
        if not cfg.get("message_role_enabled") and local is None and not resolve_channel:
            return super().dispatch(event_name, *args, **kwargs)
        # raw_message_edit y message_edit contienen el mismo objeto actualizado.
        key = id(message)
        check = self._message_role_checks.get(key)
        if check is None:
            check = asyncio.create_task(self._check_message_role(message, cfg, local, resolve_channel))
            self._message_role_checks[key] = check
            check.add_done_callback(lambda _task: self._message_role_checks.pop(key, None))
        self._schedule_event(
            self._dispatch_after_message_role, "on_message_role_gate",
            check, event_name, args, kwargs,
        )

    async def _dispatch_after_message_role(self, check, event_name, args, kwargs):
        if await check:
            super().dispatch(event_name, *args, **kwargs)

    async def _check_message_role(self, message, cfg, local=None, resolve_channel=False):
        # Los updates raw pueden omitir el autor: resolver antes de distribuirlos.
        if getattr(message, "author", None) is None:
            try:
                message = await message.channel.fetch_message(message.id)
            except discord.HTTPException as exc:
                logger.warning("No se pudo comprobar el rol del mensaje %s: %s", message.id, exc)
                return False
        if message.author.bot or message.webhook_id is not None or message.is_system():
            return True
        if resolve_channel:
            try:
                channel = await message.guild.fetch_channel(message.channel.id)
            except discord.HTTPException as exc:
                logger.warning("No se pudo resolver canal %s para las reglas: %s", message.channel.id, exc)
                return False
            message.channel = channel
            policy_channel_id = channel.parent_id if isinstance(channel, discord.Thread) else channel.id
            local = self.db.get_channel_settings(message.guild.id, policy_channel_id)
            local = local if policy_active(local) else None
        member = message.author
        if not isinstance(member, discord.Member):
            member = message.guild.get_member(member.id)
            if member is None:
                try:
                    member = await message.guild.fetch_member(message.author.id)
                except discord.HTTPException as exc:
                    logger.warning("No se pudo comprobar miembro %s: %s", message.author.id, exc)
                    return False
        role_id = cfg.get("message_role_id")
        global_required = cfg.get("message_role_enabled") and (local is None or local["role"]["mode"] in {"inherit", "additional"})
        if not global_required or (role_id and any(role.id == int(role_id) for role in member.roles)):
            if local is not None:
                return await apply_local_actions(self, message, local, local_actions(message, member, local))
            return True
        try:
            await message.delete()
        except discord.NotFound:
            return False
        except discord.HTTPException as exc:
            logger.warning("No se pudo borrar mensaje sin rol %s: %s", message.id, exc)
            return False
        key = (message.guild.id, message.author.id)
        if key not in self._message_role_notices:
            self._message_role_notices.add(key)
            asyncio.get_running_loop().call_later(10, self._message_role_notices.discard, key)
            notice = cfg.get("message_role_notice") or "Necesitas el rol requerido para hablar en este servidor."
            try:
                await message.channel.send(
                    f"<@{message.author.id}> {notice}",
                    allowed_mentions=discord.AllowedMentions(
                        everyone=False, roles=False,
                        users=[discord.Object(id=message.author.id)], replied_user=False,
                    ),
                )
            except discord.HTTPException as exc:
                logger.warning("No se pudo enviar aviso de rol en canal %s: %s", message.channel.id, exc)
        return False

    async def setup_hook(self) -> None:
        """
        Llamado automáticamente antes de conectar al WebSocket.
        Carga los cogs y sincroniza los comandos slash.
        """

        # Ignorar errores de ClientConnectionResetError en tareas en background (ej. discord.py voice)
        def custom_exception_handler(loop, context):
            exception = context.get("exception")
            if exception and type(exception).__name__ == "ClientConnectionResetError":
                return
            loop.default_exception_handler(context)

        self.loop.set_exception_handler(custom_exception_handler)

        from discord.ext import tasks

        @tasks.loop(seconds=30)
        async def bot_stats_updater():
            members_online = sum(
                1
                for g in self.guilds
                for m in g.members
                if m.status != discord.Status.offline
            )
            total_members = sum(g.member_count or 0 for g in self.guilds)

            uptime_seconds = int(
                (discord.utils.utcnow() - self.start_time).total_seconds()
            )

            # Count open tickets (método público)
            open_tickets = 0
            if hasattr(self, "db"):
                try:
                    open_tickets = self.db.count_all_open_tickets()
                    self.db.update_bot_stats(
                        members_online, total_members, open_tickets, uptime_seconds
                    )
                except Exception:
                    pass

        @bot_stats_updater.before_loop
        async def before_bot_stats_updater():
            await self.wait_until_ready()

        bot_stats_updater.start()
        self.tree.on_error = self.on_app_command_error

        cogs = [
            "cogs._catbot",
            "cogs.moderation",
            "cogs.channels",
            "cogs.users",
            "cogs.embeds",
            "cogs.serverutils",
            "cogs.ia",
            "cogs.welcomes",
            "cogs.suggestions",
            "cogs.giveaways",
            "cogs.autoroles",
            "cogs.radio",
            "cogs.tickets",
            "cogs.tags",
            "cogs.reports",
            "cogs.scheduler",
            "cogs.levels",
            "cogs.voice_gen",
            "cogs.help",
            "cogs.automod",
            "cogs.rolemenu",
            "cogs.autoresponses",
            "cogs.custom_commands",
            "cogs.logging",
            "cogs.utilities",
        ]
        for cog in cogs:
            try:
                await self.load_extension(cog)
                logger.info("Cog cargado: %s", cog)
            except Exception as exc:
                logger.error("Error cargando %s: %s", cog, exc, exc_info=True)

        synced = await self.tree.sync()
        logger.info("Comandos slash sincronizados: %d", len(synced))

    async def _clear_guild_command_overrides(self) -> None:
        """Elimina comandos específicos de guild para evitar duplicados.

        El bot publica comandos globales. Si alguna vez copiamos el árbol global
        a cada guild, Discord muestra dos entradas: global + guild. Esta limpieza
        borra esos overrides por servidor y deja solo los comandos globales.
        """
        for guild in list(self.guilds):
            try:
                target = discord.Object(id=guild.id)
                self.tree.clear_commands(guild=target)
                synced = await self.tree.sync(guild=target)
                logger.info(
                    "Overrides de comandos slash limpiados en guild %s (%s): %d quedan",
                    guild.name,
                    guild.id,
                    len(synced),
                )
            except Exception as exc:
                logger.warning(
                    "No se pudieron limpiar comandos de guild %s (%s): %s",
                    getattr(guild, "name", "?"),
                    getattr(guild, "id", "?"),
                    exc,
                    exc_info=True,
                )

    async def on_ready(self) -> None:
        logger.info(
            "%s despierta y ronronea | %d servidor(es)",
            self.user,
            len(self.guilds),
        )
        # No forzamos actividad/presencia en el perfil del bot.
        # La radio publica su estado únicamente en el canal de voz donde reproduce.
        if not self._guild_command_cleanup_done:
            self._guild_command_cleanup_done = True
            await self._clear_guild_command_overrides()

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ) -> None:
        """
        Manejador global de errores de comandos slash.
        Los cogs pueden tener sus propios handlers específicos.
        """
        command = interaction.command
        if interaction.extras.get("catsbot.app_command_error_handled"):
            # discord.py invoca CommandTree.on_error incluso después de un
            # @command.error. Los handlers locales marcan aquí su respuesta.
            return

        voice = getattr(self, "catbot_voice", None)
        line = voice.line if voice else (lambda role, text: text)

        if isinstance(error, discord.app_commands.CommandSignatureMismatch):
            msg = line(
                "error",
                "Discord envió un comando antiguo o incompleto. "
                "Ciérralo, vuelve a abrirlo y completa las opciones obligatorias. "
                "No se ha ejecutado ninguna acción.",
            )
        elif isinstance(error, discord.app_commands.CommandOnCooldown):
            msg = line(
                "loading",
                f"Espera, el gato está descansando. Vuelve en `{error.retry_after:.1f}s`.",
            )
        elif isinstance(error, discord.app_commands.MissingPermissions):
            msg = line(
                "error",
                f"Te faltan permisos: `{', '.join(error.missing_permissions)}`",
            )
        elif isinstance(error, discord.app_commands.BotMissingPermissions):
            msg = line(
                "error",
                f"Al gato le faltan permisos: `{', '.join(error.missing_permissions)}`",
            )
        elif isinstance(error, discord.app_commands.TransformerError):
            command_name = getattr(command, "qualified_name", "desconocido")
            if error.type is discord.AppCommandOptionType.user:
                logger.warning(
                    "No se pudo resolver el usuario de /%s "
                    "(guild=%s, value_type=%s)",
                    command_name,
                    interaction.guild_id,
                    type(error.value).__name__,
                )
                msg = line(
                    "error",
                    "No pude reconocer ese usuario para este comando. "
                    "Vuelve a abrir el comando y selecciónalo desde la lista de Discord; "
                    "si aún aparece como texto libre, reinicia Discord para actualizar los comandos.",
                )
            else:
                logger.warning(
                    "Valor inválido para /%s (option_type=%s, value_type=%s)",
                    command_name,
                    error.type,
                    type(error.value).__name__,
                )
                msg = line(
                    "error",
                    "Uno de los valores del comando no es válido. Vuelve a abrirlo e inténtalo de nuevo.",
                )
        elif isinstance(error, discord.app_commands.CommandInvokeError):
            logger.error("Error invocando comando: %s", error.original, exc_info=True)
            msg = line("error", "El gato tropezó con algo. Revisa los logs.")
        else:
            logger.error("Error de comando no manejado: %s", error, exc_info=True)
            msg = line("error", "Algo inesperado pasó. El gato se está reorganizando.")

        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except discord.NotFound:
            logger.warning("No se pudo responder error global: interacción expirada o desconocida")
        except discord.HTTPException as exc:
            logger.warning("No se pudo responder error global: %s", exc)

    async def on_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        """Silencia errores de comandos prefix no encontrados (ej. !hug)."""
        if isinstance(error, commands.CommandNotFound):
            return
        logger.debug("Error de prefix command (ignorado): %s", error)


# ── Arranque ──────────────────────────────────────────────────────────────────


def main() -> None:
    token = os.getenv("TOKEN")
    if not token:
        logger.critical("TOKEN no encontrado en el archivo .env")
        raise SystemExit(1)

    api_host = os.getenv("API_HOST", "0.0.0.0")
    try:
        api_port = int(os.getenv("API_PORT", "8080"))
    except ValueError:
        logger.warning("API_PORT inválido en .env; usando 8080")
        api_port = 8080

    bot = BotES()
    iniciar_api(db=bot.db, bot=bot, host=api_host, port=api_port)

    try:
        bot.run(token, log_handler=None)  # log_handler=None para usar nuestro logging
    except discord.LoginFailure:
        logger.critical("Token inválido. Verifica el archivo .env")
    except KeyboardInterrupt:
        logger.info("Bot detenido por el usuario")
    except Exception as exc:
        logger.critical("Error crítico al iniciar: %s", exc, exc_info=True)


if __name__ == "__main__":
    main()
