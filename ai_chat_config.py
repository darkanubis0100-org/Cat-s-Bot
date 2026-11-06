"""Shared chat defaults and actor restrictions for providers and configuration storage."""

DEFAULT_WEB_TIMEOUT_SECONDS = 120


def normalize_restricted_roles(value):
    if not isinstance(value, list):
        raise ValueError("ai_restricted_role_ids debe ser una lista de IDs de roles")
    roles = []
    for role_id in value:
        text = str(role_id)
        if (type(role_id) not in (int, str) or not text.isascii() or not text.isdecimal()
                or not 0 < int(text) < 2**64):
            raise ValueError("ai_restricted_role_ids contiene un ID de rol inválido")
        roles.append(str(int(text)))
    return list(dict.fromkeys(roles))


def chat_actor_blocked(message, cfg, *, member=None):
    if message.author.bot or getattr(message, "webhook_id", None):
        return True
    if member is None:
        lookup = getattr(message.guild, "get_member", None)
        member = (lookup(message.author.id) if lookup else None) or message.author
    if member.bot:
        return True
    restricted = {int(role_id) for role_id in cfg.get("ai_restricted_role_ids", [])}
    if not restricted:
        return False
    role_ids = set(getattr(member, "_roles", ()))
    if not hasattr(member, "_roles"):
        role_ids.update(role.id for role in member.roles)
    role_ids.add(message.guild.id)  # Discord's implicit @everyone role.
    return not restricted.isdisjoint(role_ids)
