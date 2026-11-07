import logging
import discord
from discord.ext import commands
from discord import app_commands

logger = logging.getLogger(__name__)

CATEGORIES = {
    "mod": {
        "emoji": "🛡️",
        "name": "Moderación",
        "commands": [
            ("/ban", "Banea por selector o ID, aunque el usuario ya haya salido"),
            ("/tempban [duración]", "Banea temporalmente"),
            ("/unban", "Desbanea por ID"),
            ("/kick", "Expulsa un usuario"),
            ("/mute [duración]", "Silencia un usuario"),
            ("/unmute", "Quita el silencio"),
            ("/timeout", "Aplica timeout nativo"),
            ("/untimeout", "Quita el timeout"),
            ("/warn", "Advierte un usuario"),
            ("/warns [usuario]", "Ver warns"),
            ("/unwarn [caso]", "Revoca un warn"),
            ("/clearwarns", "Limpia warns"),
            ("/purgar [N]", "Elimina mensajes en masa"),
            ("/cases", "Consulta casos moderativos"),
            ("/moderations", "Lista sanciones activas"),
            ("/appeals list", "Lista apelaciones"),
        ],
    },
    "info": {
        "emoji": "ℹ️",
        "name": "Información",
        "commands": [
            ("/ping", "Latencia del bot"),
            ("/bot_info", "Estado del bot"),
            ("/servidor_info", "Info del servidor"),
            ("/servidor_logs", "Configura logs"),
            ("/usuario_info [usuario]", "Info detallada"),
            ("/rol_info [rol]", "Info de un rol"),
            ("/avatar [usuario]", "Avatar en grande"),
            ("/icono_servidor", "Icono del servidor"),
        ],
    },
    "channels": {
        "emoji": "📝",
        "name": "Canales",
        "commands": [
            ("/bloquear", "Bloquea el canal"),
            ("/desbloquear", "Desbloquea el canal"),
            ("/modo_lento [s]", "Modo lento"),
            ("/limpiar [N]", "Borra N mensajes"),
            ("/limpiar_todo", "Borra todo (irreversible)"),
            ("/channelsetup", "Panel multimedia"),
        ],
    },
    "users": {
        "emoji": "👥",
        "name": "Usuarios",
        "commands": [
            ("/asignar_rol", "Añade un rol"),
            ("/quitar_rol", "Quita un rol"),
            ("/apodo [usuario]", "Cambia el apodo"),
        ],
    },
    "embeds": {
        "emoji": "🎨",
        "name": "Embeds",
        "commands": [
            ("/embed create", "Constructor interactivo"),
            ("/embed list", "Lista embeds guardados"),
            ("/embed load [nombre]", "Carga un embed"),
        ],
    },
    "ia": {
        "emoji": "🤖",
        "name": "IA / Gemini",
        "commands": [
            ("/ia_estado", "Métricas del servicio"),
            ("/ia_limpiar", "Borra tu historial"),
        ],
    },
    "tickets": {
        "emoji": "🎫",
        "name": "Tickets",
        "commands": [
            ("/añadir_usuario", "Añade usuario al ticket"),
        ],
    },
    "tags": {
        "emoji": "🏷️",
        "name": "Tags",
        "commands": [
            ("/tag get [nombre]", "Usa un tag"),
            ("/tag create", "Crea un tag"),
            ("/tag edit [nombre]", "Edita un tag"),
            ("/tag delete [nombre]", "Elimina un tag"),
            ("/tag list", "Lista todos"),
            ("/tag info [nombre]", "Info del tag"),
        ],
    },
    "levels": {
        "emoji": "🎮",
        "name": "Niveles / XP",
        "commands": [
            ("/rango [usuario]", "Tu rango o el de otro"),
            ("/clasificacion", "Top 10 del servidor"),
            ("/xp give [@] [N]", "[Admin] Dar XP"),
            ("/xp reset [@]", "[Admin] Resetear XP"),
        ],
    },
    "giveaways": {
        "emoji": "🎁",
        "name": "Sorteos",
        "commands": [
            ("/sorteo", "Crear sorteo interactivo"),
            ("/sorteo_terminar", "Terminar sorteo"),
            ("/sorteo_cancelar", "Cancelar sorteo"),
            ("/sorteo_rerollear", "Re-elegir ganador"),
            ("/sorteo_lista", "Sorteos activos"),
        ],
    },
    "radio": {
        "emoji": "📻",
        "name": "Radio",
        "commands": [
            ("/radio start [canal]", "[Admin] Activar la radio"),
            ("/radio status", "Estado actual"),
            ("/radio restart", "Reconectar stream"),
            ("/radio buscar [nombre]", "Buscar emisora"),
        ],
    },
    "autoroles": {
        "emoji": "🔐",
        "name": "Autoroles",
        "commands": [
            ("/autorolereact", "Rol por reacción"),
            ("/menu_roles", "Menú de roles dropdown"),
        ],
    },
    "reports": {
        "emoji": "📋",
        "name": "Reportes",
        "commands": [
            ("/report [@] [razón]", "Reportar usuario"),
            ("/reports list [estado]", "[Staff] Listar"),
            ("/reports view [id]", "[Staff] Ver detalle"),
        ],
    },
    "invites": {
        "emoji": "👋",
        "name": "Invitaciones",
        "commands": [
            ("/invites stats [user]", "Tus invitaciones"),
            ("/invites leaderboard", "Top 10 inviters"),
        ],
    },
    "schedule": {
        "emoji": "🗓️",
        "name": "Programador",
        "commands": [
            ("/schedule create [#canal]", "Crear mensaje"),
            ("/schedule list", "Listar schedules"),
            ("/schedule delete [nombre]", "Eliminar"),
            ("/schedule toggle [nombre]", "Activar/desactivar"),
            ("/schedule test [nombre]", "Enviar prueba"),
        ],
    },
    "vc": {
        "emoji": "🔊",
        "name": "Canales de Voz",
        "commands": [
            ("/vc lock", "Bloquear tu VC"),
            ("/vc unlock", "Desbloquear tu VC"),
            ("/vc limit [N]", "Límite de usuarios"),
            ("/vc rename [nombre]", "Renombrar"),
            ("/vc ban [@]", "Banear de tu VC"),
            ("/vc permit [@]", "Permitir acceso"),
            ("/vc claim", "Reclamar propiedad"),
            ("/vc transfer [@]", "Transferir propiedad"),
        ],
    },
    "automod": {
        "emoji": "🤖",
        "name": "Automoderación",
        "commands": [
            ("/automod status", "Estado y últimas acciones"),
            ("/automod toggle", "Activar/desactivar"),
            ("/automod log [N]", "Ver registro de acciones"),
        ],
    },
    "autoresponses": {
        "emoji": "💬",
        "name": "Auto-Respuestas",
        "commands": [
            ("/autoresponse add", "Agregar auto-respuesta"),
            ("/autoresponse remove", "Eliminar auto-respuesta"),
            ("/autoresponse list", "Listar todas"),
        ],
    },
    "config": {
        "emoji": "⚙️",
        "name": "Configuración",
        "commands": [
            ("/welcome channel", "Canal de bienvenidas"),
            ("/welcome toggle", "Activar/desactivar"),
            ("/welcome test", "Probar bienvenida"),
            ("/boost channel", "Canal de boosters"),
            ("/boost toggle", "Activar/desactivar"),
            ("/starboard channel", "Canal de starboard"),
            ("/starboard threshold", "Umbral de estrellas"),
            ("/starboard config", "Ver configuración"),
        ],
    },
}


class HelpSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label=data["name"],
                emoji=data["emoji"],
                value=key,
                description=f"{len(data['commands'])} comandos",
            )
            for key, data in CATEGORIES.items()
        ]
        super().__init__(
            placeholder="Selecciona una categoría...",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        data = CATEGORIES[key]
        embed = discord.Embed(
            title=f"{data['emoji']} {data['name']}",
            description="",
            color=discord.Color.blurple(),
        )
        for cmd, desc in data["commands"]:
            embed.add_field(name=f"`{cmd}`", value=desc, inline=False)
        embed.set_footer(text="Usa /help para ver este menú otra vez")
        await interaction.response.edit_message(embed=embed, view=self.view)


class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(HelpSelect())

    @discord.ui.button(label="General", emoji="📖", style=discord.ButtonStyle.secondary, row=1)
    async def general_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="📖 Ayuda General",
            description=(
                "**Cat's Bot** — Bot multifuncional para Discord.\n\n"
                "Usa el menú desplegable de arriba para ver comandos por categoría.\n\n"
                "También puedes usar **`!help`** con mensajes de texto para obtener la misma ayuda.\n\n"
                "📌 La mayoría de comandos se configuran desde el **Dashboard Web**."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="🔗 Dashboard",
            value="Accede al panel de control web para configurar módulos como:\n"
            "Tickets · Bienvenidas · Sugerencias · Niveles · Radio · IA · Autoroles · y más.",
            inline=False,
        )
        total_cmds = sum(len(c["commands"]) for c in CATEGORIES.values())
        embed.set_footer(text=f"{len(CATEGORIES)} categorías · {total_cmds} comandos slash")
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Cerrar", emoji="❌", style=discord.ButtonStyle.danger, row=1)
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Menú cerrado.", embed=None, view=None)
        self.stop()


class Help(commands.Cog):
    """Sistema de ayuda interactivo con botones y !help"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="Pide ayuda al gato")
    async def help_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        voice = getattr(self.bot, "catbot_voice", None)
        help_emoji = voice.get("help") if voice else "❓"
        paw = voice.get("paw") if voice else "🐾"

        embed = discord.Embed(
            title=f"{help_emoji} El gato te ayuda",
            description=(
                "Elige una categoría en el menú de abajo para ver sus comandos.\n"
                "También sirve **`!help`** en el chat normal."
            ),
            color=0xA855F7,
        )
        total_cmds = sum(len(c["commands"]) for c in CATEGORIES.values())
        embed.set_footer(
            text=f"{paw} {len(CATEGORIES)} categorías · {total_cmds} comandos · Cat's Bot"
        )
        await interaction.followup.send(embed=embed, view=HelpView(), ephemeral=True)

    @commands.command(name="help")
    async def help_prefix(self, ctx: commands.Context):
        voice = getattr(self.bot, "catbot_voice", None)
        prefix = voice.get("help") if voice else "❓"
        from cogs._managed_messages import send_managed
        await send_managed(self.bot, ctx.guild.id if ctx.guild else None, "help", ctx.send,
            f"{prefix} Usa **`/help`** para que el gato te muestre todo.",
            delete_after=10,
        )
        try:
            await ctx.message.delete()
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Help(bot))
