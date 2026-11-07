"""Paquete de rutas del API.

Los routers se importan de forma diferida para que herramientas como
``unittest discover`` puedan cargar el paquete sin tener instalado todo el
runtime del bot/API. Al arrancar FastAPI, ``api.app`` sigue importando los
módulos concretos y se cargan con sus dependencias reales.
"""

from importlib import import_module

__all__ = [
    "guild", "moderation", "tickets", "tags",
    "reports", "schedules", "giveaways",
    "autoroles", "radio", "embeds", "channels",
    "voice_gen", "welcome", "suggestions_route",
    "invites_route", "ai_keys", "public_stats",
    "autoresponses", "custom_commands", "emojis", "channel_settings",
]


def __getattr__(name):
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(f"{__name__}.{name}")
    globals()[name] = module
    return module
