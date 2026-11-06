"""
database/manager.py
───────────────────
Capa de persistencia. El runtime soportado y verificado actualmente es SQLite.
Las ramas SQL de otros motores se conservan únicamente como base de una futura
migración; no se anuncian como una opción operativa hasta disponer de pruebas de
integración equivalentes para todo el bot.
"""

import json
import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from llm_moderation_config import DEFAULT_LLM_CONFIG
from ai_chat_config import DEFAULT_WEB_TIMEOUT_SECONDS, normalize_restricted_roles

logger = logging.getLogger("Database")

REMINDER_MAX_PENDING_PER_USER = 10
REMINDER_MAX_PENDING_PER_GUILD = 500

VALID_CONFIG_COLUMNS = frozenset(
    {
        "mute_role_id",
        "log_channel_id",
        "warn_mute_threshold",
        "warn_kick_threshold",
        "warn_ban_threshold",
        "warn_mute_enabled",
        "warn_kick_enabled",
        "warn_ban_enabled",
        "warn_mute_duration",
        "warn_embed_config",
        "staff_role_id",
        "message_role_enabled",
        "message_role_id",
        "message_role_notice",
    }
)

VALID_USER_COLUMNS = frozenset(
    {
        "warns",
        "mute_start",
        "mute_duration",
    }
)

VALID_CHANNEL_CONFIG_COLUMNS = frozenset(
    {
        "guild_id",
        "locked",
        "media_only",
        "media_config",
        "auto_react",
        "auto_react_enabled",
        "slowmode",
    }
)

VALID_SERVER_CONFIG_COLUMNS = frozenset(
    {
        "ai_message_categories",
        "staff_role_id",
        "mod_role_id",
        "modlog_channel",
        "serverlog_channel",
        "log_events",
        "embed_role_id",
        "channels_role_id",
        "users_role_id",
        "modlog_enabled",
        "serverlog_enabled",
        "starboard_channel_id",
        "starboard_threshold",
        "anti_raid_enabled",
        "anti_raid_threshold",
        "anti_raid_window",
        "anti_raid_lockdown_duration",
        "anti_alt_min_age",
        "anti_alt_action",
        "anti_alt_role_id",
    }
)

VALID_AI_CONFIG_COLUMNS = frozenset(
    {
        "ai_enabled",
        "ai_provider",
        "ai_web_enabled",
        "ai_web_max_sources",
        "ai_web_timeout_seconds",
        "ai_deep_think",
        "ai_deep_think_daily_limit",
        "ai_memory_messages",
        "ai_response_mode",
        "ai_channel_id",
        "ai_role_id",
        "ai_restricted_role_ids",
        "ai_model",
        "ai_system_prompt",
        "ai_limit_requests",
        "ai_limit_hours",
        "ai_imagine_enabled",
        "ai_webhook_name",
        "ai_webhook_icon",
    }
)

VALID_GENERIC_CONFIG_COLUMNS = {
    "welcome_config": frozenset({"channel_id", "embed_data", "enabled"}),
    "boost_config": frozenset({"channel_id", "embed_data", "gif_url", "enabled"}),
    "suggestions_config": frozenset(
        {
            "submit_channel_id",
            "review_channel_id",
            "public_channel_id",
            "enabled",
            "auto_publish",
            "min_length",
            "max_length",
            "cooldown_seconds",
        }
    ),
    "lofi_config": frozenset(
        {
            "channel_id",
            "volume",
            "enabled",
            "stream_url",
            "station_name",
            "auto_reconnect",
            "pause_on_empty",
        }
    ),
    "ticket_config": frozenset(
        {
            "panel_channel_id",
            "category_id",
            "log_channel_id",
            "allowed_roles",
            "immune_roles",
            "panel_embed_data",
            "channel_name_template",
            "max_tickets_per_user",
            "ticket_cooldown_seconds",
            "panel_select_template",
            "panel_inside_template",
            "msg_open_template",
            "msg_close_template",
        }
    ),
    "xp_config": frozenset(
        {
            "enabled",
            "xp_min",
            "xp_max",
            "cooldown_seconds",
            "ignored_channels",
            "channel_multipliers",
            "announcement_channel_id",
            "announcement_message",
            "stack_rewards",
            "levelup_persist",
            "levelup_autodelete",
            "levelup_delete_after_seconds",
            "levelup_embed_config",
            "announcement_mode",
        }
    ),
    "voice_gen_config": frozenset(
        {
            "generator_channel_id",
            "category_id",
            "panel_channel_id",
            "name_template",
            "default_limit",
            "enabled",
            "panel_title",
            "panel_description",
            "panel_color",
            "auto_send_panel",
            "panel_embed_data",
            "panel_message_channel_id",
            "panel_message_id",
        }
    ),
    "invite_config": frozenset({"channel_id", "enabled"}),
    "automod_config": frozenset({"enabled", "rules"}),
}

VALID_TICKET_COLUMNS = frozenset(
    {
        "channel_id",
        "staff_id",
        "status",
        "ai_summary",
        "closed_at",
    }
)

VALID_GIVEAWAY_COLUMNS = frozenset(
    {
        "prize",
        "end_time",
        "winners_count",
        "req_roles",
        "deny_roles",
        "participants",
        "ended",
        "cancelled",
        "winners",
    }
)

VALID_SUGGESTION_COLUMNS = frozenset(
    {
        "message_id",
        "content",
        "status",
        "upvotes",
        "downvotes",
        "denial_reason",
    }
)


# ── Schema por tipo de base de datos ─────────────────────────────────────────

_SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id     INTEGER PRIMARY KEY,
    mute_role_id INTEGER,
    log_channel_id INTEGER,
    warn_mute_threshold INTEGER DEFAULT 3,
    warn_kick_threshold INTEGER DEFAULT 5,
    warn_ban_threshold  INTEGER DEFAULT 7,
    warn_mute_enabled   INTEGER DEFAULT 1,
    warn_kick_enabled   INTEGER DEFAULT 0,
    warn_ban_enabled    INTEGER DEFAULT 0,
    warn_mute_duration  INTEGER DEFAULT 3600,
    warn_embed_config   TEXT,
    staff_role_id INTEGER
);

CREATE TABLE IF NOT EXISTS user_records (
    user_id      INTEGER NOT NULL,
    guild_id     INTEGER NOT NULL,
    warns        INTEGER DEFAULT 0,
    mute_start   TEXT,
    mute_duration INTEGER,
    PRIMARY KEY (user_id, guild_id)
);

CREATE TABLE IF NOT EXISTS mod_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    target_id    INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    action_type  TEXT    NOT NULL,
    reason       TEXT    DEFAULT 'Sin razón especificada',
    extra_data   TEXT,
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_config (
    channel_id   INTEGER PRIMARY KEY,
    guild_id     INTEGER NOT NULL,
    locked       INTEGER DEFAULT 0,
    media_only   INTEGER DEFAULT 0,
    media_config TEXT,
    auto_react   TEXT,
    slowmode     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS server_config (
    guild_id            INTEGER PRIMARY KEY,
    staff_role_id       INTEGER,
    modlog_channel      INTEGER,
    serverlog_channel   INTEGER,
    log_events          TEXT,
    embed_role_id       INTEGER,
    channels_role_id    INTEGER,
    users_role_id       INTEGER,
    modlog_enabled      INTEGER DEFAULT 1,
    serverlog_enabled   INTEGER DEFAULT 1,
    mod_role_id         INTEGER,
    starboard_channel_id INTEGER,
    starboard_threshold  INTEGER DEFAULT 3,
    anti_raid_enabled    INTEGER DEFAULT 0,
    anti_raid_threshold  INTEGER DEFAULT 10,
    anti_raid_window     INTEGER DEFAULT 30,
    anti_raid_lockdown_duration INTEGER DEFAULT 300,
    anti_alt_min_age     INTEGER DEFAULT 7,
    anti_alt_action      TEXT    DEFAULT 'log',
    anti_alt_role_id     INTEGER
);

CREATE TABLE IF NOT EXISTS ai_config (
    guild_id            INTEGER PRIMARY KEY,
    ai_enabled          INTEGER DEFAULT 1,
    ai_response_mode    TEXT    DEFAULT 'mentions',
    ai_channel_id       INTEGER,
    ai_role_id          INTEGER,
    ai_restricted_role_ids TEXT DEFAULT '[]',
    ai_model            TEXT    DEFAULT 'gemma-4-26b-a4b-it',
    ai_system_prompt    TEXT,
    ai_limit_requests   INTEGER DEFAULT 50,
    ai_limit_hours      INTEGER DEFAULT 12,
    ai_imagine_enabled  INTEGER DEFAULT 1,
    ai_webhook_name     TEXT,
    ai_webhook_icon     TEXT
);

-- Pool de API keys de IA. Cada key puede cubrir hasta 2 guilds.
CREATE TABLE IF NOT EXISTS ai_api_keys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT    NOT NULL,
    api_key     TEXT    NOT NULL UNIQUE,
    active      INTEGER DEFAULT 1,
    notes       TEXT,
    created_at  TEXT    NOT NULL
);

-- Asignación 1:1 guild → key. Restringido por aplicación a 2 guilds por key.
CREATE TABLE IF NOT EXISTS ai_guild_keys (
    guild_id    INTEGER PRIMARY KEY,
    key_id      INTEGER NOT NULL,
    assigned_at TEXT    NOT NULL,
    FOREIGN KEY (key_id) REFERENCES ai_api_keys(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS appeals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    case_id      INTEGER,
    action_type  TEXT NOT NULL,
    reason       TEXT,
    appeal_text  TEXT,
    status       TEXT DEFAULT 'PENDING',
    created_at   TEXT NOT NULL,
    UNIQUE (guild_id, case_id)
);

CREATE TABLE IF NOT EXISTS autoresponses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    channel_id    INTEGER,
    trigger       TEXT NOT NULL,
    response      TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tempbans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    moderator_id  INTEGER NOT NULL,
    reason        TEXT DEFAULT 'Sin razón especificada',
    ban_start     TEXT NOT NULL,
    ban_duration  INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS saved_embeds (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    creator_id   INTEGER NOT NULL,
    name         TEXT,
    embed_data   TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS welcome_config (
    guild_id     INTEGER PRIMARY KEY,
    channel_id   INTEGER,
    embed_data   TEXT,
    enabled      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS boost_config (
    guild_id     INTEGER PRIMARY KEY,
    channel_id   INTEGER,
    embed_data   TEXT,
    gif_url      TEXT,
    enabled      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS suggestions_config (
    guild_id           INTEGER PRIMARY KEY,
    submit_channel_id  INTEGER,
    review_channel_id  INTEGER,
    public_channel_id  INTEGER,
    enabled            INTEGER DEFAULT 1,
    auto_publish       INTEGER DEFAULT 0,
    min_length         INTEGER DEFAULT 10,
    max_length         INTEGER DEFAULT 2000,
    cooldown_seconds   INTEGER DEFAULT 300
);

CREATE TABLE IF NOT EXISTS suggestions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    message_id    INTEGER,
    content       TEXT NOT NULL,
    status        TEXT DEFAULT 'PENDING',
    upvotes       INTEGER DEFAULT 0,
    downvotes     INTEGER DEFAULT 0,
    denial_reason TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suggestion_votes (
    suggestion_id INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    vote          INTEGER NOT NULL,
    PRIMARY KEY (suggestion_id, user_id)
);

CREATE TABLE IF NOT EXISTS giveaways (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id       INTEGER NOT NULL,
    channel_id     INTEGER NOT NULL,
    message_id     INTEGER NOT NULL,
    prize          TEXT NOT NULL,
    end_time       INTEGER NOT NULL,
    winners_count  INTEGER DEFAULT 1,
    req_roles      TEXT,
    deny_roles     TEXT,
    participants   TEXT,
    ended          INTEGER DEFAULT 0,
    cancelled      INTEGER DEFAULT 0,
    winners        TEXT
);

CREATE TABLE IF NOT EXISTS autoroles (
    message_id   INTEGER PRIMARY KEY,
    guild_id     INTEGER NOT NULL,
    channel_id   INTEGER NOT NULL,
    mapping_data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS join_autoroles (
    guild_id   INTEGER NOT NULL,
    role_id    INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, role_id)
);

CREATE TABLE IF NOT EXISTS lofi_config (
    guild_id        INTEGER PRIMARY KEY,
    channel_id      INTEGER,
    volume          INTEGER DEFAULT 100,
    enabled         INTEGER DEFAULT 0,
    stream_url      TEXT,
    station_name    TEXT,
    auto_reconnect  INTEGER DEFAULT 1,
    pause_on_empty  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    description TEXT,
    applied_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_stats (
    id             INTEGER PRIMARY KEY DEFAULT 1,
    members_online INTEGER DEFAULT 0,
    total_members  INTEGER DEFAULT 0,
    open_tickets   INTEGER DEFAULT 0,
    uptime_seconds INTEGER DEFAULT 0,
    last_updated   TEXT
);

CREATE TABLE IF NOT EXISTS ticket_config (
    guild_id                INTEGER PRIMARY KEY,
    panel_channel_id        INTEGER,
    category_id             INTEGER,
    log_channel_id          INTEGER,
    allowed_roles           TEXT DEFAULT '[]',
    immune_roles            TEXT DEFAULT '[]',
    panel_embed_data        TEXT,
    channel_name_template   TEXT DEFAULT '{username}-{number}',
    max_tickets_per_user    INTEGER DEFAULT 0,
    ticket_cooldown_seconds INTEGER DEFAULT 0,
    -- Referencias opcionales a plantillas en ticket_template_embeds. NULL = inline.
    panel_select_template   TEXT,
    panel_inside_template   TEXT,
    msg_open_template       TEXT,
    msg_close_template      TEXT
);

CREATE TABLE IF NOT EXISTS ticket_categories (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id                    INTEGER NOT NULL,
    name                        TEXT NOT NULL,
    emoji                       TEXT,
    description                 TEXT,
    questions                   TEXT DEFAULT '[]',
    close_reasons               TEXT DEFAULT '[]',
    welcome_embed_data          TEXT,
    welcome_embed_template_key  TEXT,
    staff_role_id               INTEGER
);

-- Pool de plantillas reutilizables (panel_select, panel_inside, msg_open, msg_close, custom_*).
CREATE TABLE IF NOT EXISTS ticket_template_embeds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    template_key TEXT   NOT NULL,
    name         TEXT,
    embed_data   TEXT   NOT NULL,
    created_at   TEXT   NOT NULL,
    UNIQUE(guild_id, template_key)
);

CREATE TABLE IF NOT EXISTS tickets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    global_number INTEGER NOT NULL,
    guild_id      INTEGER NOT NULL,
    channel_id    INTEGER,
    user_id       INTEGER NOT NULL,
    category_name TEXT NOT NULL,
    staff_id      INTEGER,
    status        TEXT DEFAULT 'OPEN',
    ai_summary    TEXT,
    created_at    TEXT NOT NULL,
    closed_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_ur_guild   ON user_records(guild_id);
CREATE INDEX IF NOT EXISTS idx_ma_target  ON mod_actions(target_id, guild_id);
CREATE INDEX IF NOT EXISTS idx_ma_time    ON mod_actions(guild_id, created_at);
CREATE INDEX IF NOT EXISTS idx_mute_active ON user_records(mute_start)
    WHERE mute_start IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cc_guild   ON channel_config(guild_id);
CREATE INDEX IF NOT EXISTS idx_se_guild   ON saved_embeds(guild_id);

CREATE TABLE IF NOT EXISTS tags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    name       TEXT NOT NULL,
    content    TEXT NOT NULL,
    creator_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    uses       INTEGER DEFAULT 0,
    UNIQUE(guild_id, name)
);

CREATE TABLE IF NOT EXISTS reports (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    reporter_id      INTEGER NOT NULL,
    reported_user_id INTEGER NOT NULL,
    reason           TEXT NOT NULL,
    ticket_id        INTEGER,
    status           TEXT DEFAULT 'PENDING',
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduled_messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    name             TEXT NOT NULL,
    channel_id       INTEGER NOT NULL,
    content          TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL,
    last_sent        TEXT,
    enabled          INTEGER DEFAULT 1,
    created_by       INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE(guild_id, name)
);

CREATE TABLE IF NOT EXISTS user_levels (
    user_id       INTEGER NOT NULL,
    guild_id      INTEGER NOT NULL,
    xp            INTEGER DEFAULT 0,
    level         INTEGER DEFAULT 0,
    message_count INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, guild_id)
);

CREATE TABLE IF NOT EXISTS xp_config (
    guild_id                     INTEGER PRIMARY KEY,
    enabled                      INTEGER DEFAULT 0,
    xp_min                       INTEGER DEFAULT 15,
    xp_max                       INTEGER DEFAULT 25,
    cooldown_seconds             INTEGER DEFAULT 60,
    ignored_channels             TEXT    DEFAULT '[]',
    channel_multipliers          TEXT    DEFAULT '{}',
    announcement_channel_id      INTEGER,
    announcement_message         TEXT,
    stack_rewards                INTEGER DEFAULT 1,
    -- Comportamiento del mensaje de subida de nivel.
    levelup_persist              INTEGER DEFAULT 1,   -- 0: se elimina; 1: queda en el canal
    levelup_autodelete           INTEGER DEFAULT 0,   -- 0: nunca; 1: aplica delete_after
    levelup_delete_after_seconds INTEGER DEFAULT 30,
    levelup_embed_config         TEXT                 -- JSON embed; NULL = texto plano
);

CREATE TABLE IF NOT EXISTS level_rewards (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    level    INTEGER NOT NULL,
    role_id  INTEGER NOT NULL,
    UNIQUE(guild_id, level)
);

CREATE INDEX IF NOT EXISTS idx_tags_guild  ON tags(guild_id);
CREATE INDEX IF NOT EXISTS idx_rep_guild   ON reports(guild_id, status);
CREATE INDEX IF NOT EXISTS idx_sched_guild ON scheduled_messages(guild_id, enabled);
CREATE INDEX IF NOT EXISTS idx_ul_guild    ON user_levels(guild_id, xp);
CREATE INDEX IF NOT EXISTS idx_lr_guild    ON level_rewards(guild_id);

CREATE TABLE IF NOT EXISTS custom_commands (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    name          TEXT NOT NULL,
    enabled       INTEGER DEFAULT 1,
    trigger_type  TEXT NOT NULL,
    trigger_value TEXT NOT NULL,
    conditions    TEXT DEFAULT '{}',
    actions       TEXT DEFAULT '[]',
    creator_id    INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    uses          INTEGER DEFAULT 0,
    last_used     TEXT,
    UNIQUE(guild_id, name)
);

CREATE TABLE IF NOT EXISTS cc_variables (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id  INTEGER NOT NULL,
    key       TEXT NOT NULL,
    value     TEXT DEFAULT '0',
    scope     TEXT DEFAULT 'guild',
    UNIQUE(guild_id, key, scope)
);

CREATE INDEX IF NOT EXISTS idx_cc_guild  ON custom_commands(guild_id);
CREATE INDEX IF NOT EXISTS idx_cc_trigger ON custom_commands(guild_id, trigger_type, enabled);
CREATE INDEX IF NOT EXISTS idx_ccv_guild ON cc_variables(guild_id);

CREATE TABLE IF NOT EXISTS automod_config (
    guild_id     INTEGER PRIMARY KEY,
    enabled      INTEGER DEFAULT 1,
    rules        TEXT    DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS automod_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    rule         TEXT    NOT NULL,
    user_id      INTEGER NOT NULL,
    content      TEXT,
    action_taken TEXT    NOT NULL,
    created_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_aml_guild ON automod_log(guild_id, created_at);
"""

_SCHEMA_POSTGRESQL = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id     BIGINT PRIMARY KEY,
    mute_role_id BIGINT,
    log_channel_id BIGINT,
    warn_mute_threshold INTEGER DEFAULT 3,
    warn_kick_threshold INTEGER DEFAULT 5,
    warn_ban_threshold  INTEGER DEFAULT 7,
    warn_mute_enabled   SMALLINT DEFAULT 1,
    warn_kick_enabled   SMALLINT DEFAULT 0,
    warn_ban_enabled    SMALLINT DEFAULT 0,
    warn_mute_duration  INTEGER DEFAULT 3600,
    warn_embed_config   TEXT,
    staff_role_id BIGINT
);

CREATE TABLE IF NOT EXISTS user_records (
    user_id      BIGINT NOT NULL,
    guild_id     BIGINT NOT NULL,
    warns        INTEGER DEFAULT 0,
    mute_start   TEXT,
    mute_duration INTEGER,
    PRIMARY KEY (user_id, guild_id)
);

CREATE TABLE IF NOT EXISTS mod_actions (
    id           BIGSERIAL PRIMARY KEY,
    guild_id     BIGINT NOT NULL,
    target_id    BIGINT NOT NULL,
    moderator_id BIGINT NOT NULL,
    action_type  TEXT   NOT NULL,
    reason       TEXT   DEFAULT 'Sin razón especificada',
    extra_data   TEXT,
    created_at   TEXT   NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_config (
    channel_id   BIGINT PRIMARY KEY,
    guild_id     BIGINT NOT NULL,
    locked       SMALLINT DEFAULT 0,
    media_only   SMALLINT DEFAULT 0,
    media_config TEXT,
    auto_react   TEXT,
    slowmode     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS server_config (
    guild_id            BIGINT PRIMARY KEY,
    staff_role_id       BIGINT,
    modlog_channel      BIGINT,
    serverlog_channel   BIGINT,
    log_events          TEXT,
    embed_role_id       BIGINT,
    channels_role_id    BIGINT,
    users_role_id       BIGINT,
    modlog_enabled      SMALLINT DEFAULT 1,
    serverlog_enabled   SMALLINT DEFAULT 1,
    mod_role_id         BIGINT,
    starboard_channel_id BIGINT,
    starboard_threshold  INTEGER DEFAULT 3,
    anti_raid_enabled    SMALLINT DEFAULT 0,
    anti_raid_threshold  INTEGER DEFAULT 10,
    anti_raid_window     INTEGER DEFAULT 30,
    anti_raid_lockdown_duration INTEGER DEFAULT 300,
    anti_alt_min_age     INTEGER DEFAULT 7,
    anti_alt_action      TEXT    DEFAULT 'log',
    anti_alt_role_id     BIGINT
);

CREATE TABLE IF NOT EXISTS ai_config (
    guild_id            BIGINT  PRIMARY KEY,
    ai_enabled          SMALLINT DEFAULT 1,
    ai_response_mode    TEXT    DEFAULT 'mentions',
    ai_channel_id       BIGINT,
    ai_role_id          BIGINT,
    ai_restricted_role_ids TEXT DEFAULT '[]',
    ai_model            TEXT    DEFAULT 'gemma-4-26b-a4b-it',
    ai_system_prompt    TEXT,
    ai_limit_requests   INTEGER DEFAULT 50,
    ai_limit_hours      INTEGER DEFAULT 12,
    ai_imagine_enabled  SMALLINT DEFAULT 1,
    ai_webhook_name     TEXT,
    ai_webhook_icon     TEXT
);

-- Pool de API keys de IA. Cada key puede cubrir hasta 2 guilds.
CREATE TABLE IF NOT EXISTS ai_api_keys (
    id          BIGSERIAL PRIMARY KEY,
    label       TEXT     NOT NULL,
    api_key     TEXT     NOT NULL UNIQUE,
    active      SMALLINT DEFAULT 1,
    notes       TEXT,
    created_at  TEXT     NOT NULL
);

-- Asignación 1:1 guild → key. Restringido por aplicación a 2 guilds por key.
CREATE TABLE IF NOT EXISTS ai_guild_keys (
    guild_id    BIGINT PRIMARY KEY,
    key_id      BIGINT NOT NULL REFERENCES ai_api_keys(id) ON DELETE CASCADE,
    assigned_at TEXT   NOT NULL
);

CREATE TABLE IF NOT EXISTS appeals (
    id           BIGSERIAL PRIMARY KEY,
    guild_id     BIGINT NOT NULL,
    user_id      BIGINT NOT NULL,
    case_id      BIGINT,
    action_type  TEXT NOT NULL,
    reason       TEXT,
    appeal_text  TEXT,
    status       TEXT DEFAULT 'PENDING',
    created_at   TEXT NOT NULL,
    UNIQUE (guild_id, case_id)
);

CREATE TABLE IF NOT EXISTS autoresponses (
    id            BIGSERIAL PRIMARY KEY,
    guild_id      BIGINT NOT NULL,
    channel_id    BIGINT,
    trigger       TEXT NOT NULL,
    response      TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tempbans (
    id            BIGSERIAL PRIMARY KEY,
    guild_id      BIGINT NOT NULL,
    user_id       BIGINT NOT NULL,
    moderator_id  BIGINT NOT NULL,
    reason        TEXT DEFAULT 'Sin razón especificada',
    ban_start     TEXT NOT NULL,
    ban_duration  INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS saved_embeds (
    id           BIGSERIAL PRIMARY KEY,
    guild_id     BIGINT NOT NULL,
    creator_id   BIGINT NOT NULL,
    name         TEXT,
    embed_data   TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS welcome_config (
    guild_id     BIGINT PRIMARY KEY,
    channel_id   BIGINT,
    embed_data   TEXT,
    enabled      SMALLINT DEFAULT 0
);

CREATE TABLE IF NOT EXISTS boost_config (
    guild_id     BIGINT PRIMARY KEY,
    channel_id   BIGINT,
    embed_data   TEXT,
    gif_url      TEXT,
    enabled      SMALLINT DEFAULT 0
);

CREATE TABLE IF NOT EXISTS suggestions_config (
    guild_id           BIGINT PRIMARY KEY,
    submit_channel_id  BIGINT,
    review_channel_id  BIGINT,
    public_channel_id  BIGINT,
    enabled            SMALLINT DEFAULT 1,
    auto_publish       SMALLINT DEFAULT 0,
    min_length         INTEGER  DEFAULT 10,
    max_length         INTEGER  DEFAULT 2000,
    cooldown_seconds   INTEGER  DEFAULT 300
);

CREATE TABLE IF NOT EXISTS suggestions (
    id            BIGSERIAL PRIMARY KEY,
    guild_id      BIGINT NOT NULL,
    user_id       BIGINT NOT NULL,
    message_id    BIGINT,
    content       TEXT NOT NULL,
    status        TEXT DEFAULT 'PENDING',
    upvotes       INTEGER DEFAULT 0,
    downvotes     INTEGER DEFAULT 0,
    denial_reason TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suggestion_votes (
    suggestion_id BIGINT  NOT NULL,
    user_id       BIGINT  NOT NULL,
    vote          INTEGER NOT NULL,
    PRIMARY KEY (suggestion_id, user_id)
);

CREATE TABLE IF NOT EXISTS giveaways (
    id             BIGSERIAL PRIMARY KEY,
    guild_id       BIGINT NOT NULL,
    channel_id     BIGINT NOT NULL,
    message_id     BIGINT NOT NULL,
    prize          TEXT NOT NULL,
    end_time       BIGINT NOT NULL,
    winners_count  INTEGER DEFAULT 1,
    req_roles      TEXT,
    deny_roles     TEXT,
    participants   TEXT,
    ended          SMALLINT DEFAULT 0,
    cancelled      SMALLINT DEFAULT 0,
    winners        TEXT
);

CREATE TABLE IF NOT EXISTS autoroles (
    message_id   BIGINT PRIMARY KEY,
    guild_id     BIGINT NOT NULL,
    channel_id   BIGINT NOT NULL,
    mapping_data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS join_autoroles (
    guild_id   BIGINT    NOT NULL,
    role_id    BIGINT    NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, role_id)
);

CREATE TABLE IF NOT EXISTS lofi_config (
    guild_id        BIGINT PRIMARY KEY,
    channel_id      BIGINT,
    volume          INTEGER  DEFAULT 100,
    enabled         SMALLINT DEFAULT 0,
    stream_url      TEXT,
    station_name    TEXT,
    auto_reconnect  SMALLINT DEFAULT 1,
    pause_on_empty  SMALLINT DEFAULT 0
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    description TEXT,
    applied_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_stats (
    id             INTEGER PRIMARY KEY DEFAULT 1,
    members_online INTEGER DEFAULT 0,
    total_members  INTEGER DEFAULT 0,
    open_tickets   INTEGER DEFAULT 0,
    uptime_seconds INTEGER DEFAULT 0,
    last_updated   TEXT
);

CREATE TABLE IF NOT EXISTS ticket_config (
    guild_id                BIGINT PRIMARY KEY,
    panel_channel_id        BIGINT,
    category_id             BIGINT,
    log_channel_id          BIGINT,
    allowed_roles           TEXT DEFAULT '[]',
    immune_roles            TEXT DEFAULT '[]',
    panel_embed_data        TEXT,
    channel_name_template   TEXT    DEFAULT '{username}-{number}',
    max_tickets_per_user    INTEGER DEFAULT 0,
    ticket_cooldown_seconds INTEGER DEFAULT 0,
    panel_select_template   TEXT,
    panel_inside_template   TEXT,
    msg_open_template       TEXT,
    msg_close_template      TEXT
);

CREATE TABLE IF NOT EXISTS ticket_categories (
    id                          BIGSERIAL PRIMARY KEY,
    guild_id                    BIGINT NOT NULL,
    name                        TEXT NOT NULL,
    emoji                       TEXT,
    description                 TEXT,
    questions                   TEXT DEFAULT '[]',
    close_reasons               TEXT DEFAULT '[]',
    welcome_embed_data          TEXT,
    welcome_embed_template_key  TEXT,
    staff_role_id               BIGINT
);

CREATE TABLE IF NOT EXISTS ticket_template_embeds (
    id           BIGSERIAL PRIMARY KEY,
    guild_id     BIGINT NOT NULL,
    template_key TEXT   NOT NULL,
    name         TEXT,
    embed_data   TEXT   NOT NULL,
    created_at   TEXT   NOT NULL,
    UNIQUE(guild_id, template_key)
);

CREATE TABLE IF NOT EXISTS tickets (
    id            BIGSERIAL PRIMARY KEY,
    global_number INTEGER NOT NULL,
    guild_id      BIGINT NOT NULL,
    channel_id    BIGINT,
    user_id       BIGINT NOT NULL,
    category_name TEXT NOT NULL,
    staff_id      BIGINT,
    status        TEXT DEFAULT 'OPEN',
    ai_summary    TEXT,
    created_at    TEXT NOT NULL,
    closed_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_ur_guild  ON user_records(guild_id);
CREATE INDEX IF NOT EXISTS idx_ma_target ON mod_actions(target_id, guild_id);
CREATE INDEX IF NOT EXISTS idx_ma_time   ON mod_actions(guild_id, created_at);
CREATE INDEX IF NOT EXISTS idx_cc_guild  ON channel_config(guild_id);
CREATE INDEX IF NOT EXISTS idx_se_guild  ON saved_embeds(guild_id);

CREATE TABLE IF NOT EXISTS tags (
    id         BIGSERIAL PRIMARY KEY,
    guild_id   BIGINT NOT NULL,
    name       TEXT NOT NULL,
    content    TEXT NOT NULL,
    creator_id BIGINT NOT NULL,
    created_at TEXT NOT NULL,
    uses       INTEGER DEFAULT 0,
    UNIQUE(guild_id, name)
);

CREATE TABLE IF NOT EXISTS reports (
    id               BIGSERIAL PRIMARY KEY,
    guild_id         BIGINT NOT NULL,
    reporter_id      BIGINT NOT NULL,
    reported_user_id BIGINT NOT NULL,
    reason           TEXT NOT NULL,
    ticket_id        BIGINT,
    status           TEXT DEFAULT 'PENDING',
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduled_messages (
    id               BIGSERIAL PRIMARY KEY,
    guild_id         BIGINT NOT NULL,
    name             TEXT NOT NULL,
    channel_id       BIGINT NOT NULL,
    content          TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL,
    last_sent        TEXT,
    enabled          SMALLINT DEFAULT 1,
    created_by       BIGINT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE(guild_id, name)
);

CREATE TABLE IF NOT EXISTS user_levels (
    user_id       BIGINT NOT NULL,
    guild_id      BIGINT NOT NULL,
    xp            INTEGER DEFAULT 0,
    level         INTEGER DEFAULT 0,
    message_count INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, guild_id)
);

CREATE TABLE IF NOT EXISTS xp_config (
    guild_id                     BIGINT PRIMARY KEY,
    enabled                      SMALLINT DEFAULT 0,
    xp_min                       INTEGER  DEFAULT 15,
    xp_max                       INTEGER  DEFAULT 25,
    cooldown_seconds             INTEGER  DEFAULT 60,
    ignored_channels             TEXT     DEFAULT '[]',
    channel_multipliers          TEXT     DEFAULT '{}',
    announcement_channel_id      BIGINT,
    announcement_message         TEXT,
    stack_rewards                SMALLINT DEFAULT 1,
    levelup_persist              SMALLINT DEFAULT 1,
    levelup_autodelete           SMALLINT DEFAULT 0,
    levelup_delete_after_seconds INTEGER  DEFAULT 30,
    levelup_embed_config         TEXT
);

CREATE TABLE IF NOT EXISTS level_rewards (
    id       BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    level    INTEGER NOT NULL,
    role_id  BIGINT NOT NULL,
    UNIQUE(guild_id, level)
);

CREATE INDEX IF NOT EXISTS idx_tags_guild  ON tags(guild_id);
CREATE INDEX IF NOT EXISTS idx_rep_guild   ON reports(guild_id, status);
CREATE INDEX IF NOT EXISTS idx_sched_guild ON scheduled_messages(guild_id, enabled);
CREATE INDEX IF NOT EXISTS idx_ul_guild    ON user_levels(guild_id, xp);
CREATE INDEX IF NOT EXISTS idx_lr_guild    ON level_rewards(guild_id);

CREATE TABLE IF NOT EXISTS custom_commands (
    id            BIGSERIAL PRIMARY KEY,
    guild_id      BIGINT NOT NULL,
    name          TEXT NOT NULL,
    enabled       SMALLINT DEFAULT 1,
    trigger_type  TEXT NOT NULL,
    trigger_value TEXT NOT NULL,
    conditions    TEXT DEFAULT '{}',
    actions       TEXT DEFAULT '[]',
    creator_id    BIGINT NOT NULL,
    created_at    TEXT NOT NULL,
    uses          INTEGER DEFAULT 0,
    last_used     TEXT,
    UNIQUE(guild_id, name)
);

CREATE TABLE IF NOT EXISTS cc_variables (
    id        BIGSERIAL PRIMARY KEY,
    guild_id  BIGINT NOT NULL,
    key       TEXT NOT NULL,
    value     TEXT DEFAULT '0',
    scope     TEXT DEFAULT 'guild',
    UNIQUE(guild_id, key, scope)
);

CREATE INDEX IF NOT EXISTS idx_cc_guild  ON custom_commands(guild_id);
CREATE INDEX IF NOT EXISTS idx_cc_trigger ON custom_commands(guild_id, trigger_type, enabled);
CREATE INDEX IF NOT EXISTS idx_ccv_guild ON cc_variables(guild_id);

CREATE TABLE IF NOT EXISTS automod_config (
    guild_id     BIGINT PRIMARY KEY,
    enabled      SMALLINT DEFAULT 1,
    rules        TEXT    DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS automod_log (
    id           BIGSERIAL PRIMARY KEY,
    guild_id     BIGINT NOT NULL,
    rule         TEXT    NOT NULL,
    user_id      BIGINT NOT NULL,
    content      TEXT,
    action_taken TEXT    NOT NULL,
    created_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_aml_guild ON automod_log(guild_id, created_at);
"""

_SCHEMA_MARIADB = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id     BIGINT PRIMARY KEY,
    mute_role_id BIGINT,
    log_channel_id BIGINT,
    warn_mute_threshold INT DEFAULT 3,
    warn_kick_threshold INT DEFAULT 5,
    warn_ban_threshold  INT DEFAULT 7,
    warn_mute_enabled   TINYINT DEFAULT 1,
    warn_kick_enabled   TINYINT DEFAULT 0,
    warn_ban_enabled    TINYINT DEFAULT 0,
    warn_mute_duration  INT DEFAULT 3600,
    warn_embed_config   TEXT,
    staff_role_id BIGINT
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS user_records (
    user_id      BIGINT NOT NULL,
    guild_id     BIGINT NOT NULL,
    warns        INT DEFAULT 0,
    mute_start   VARCHAR(50),
    mute_duration INT,
    PRIMARY KEY (user_id, guild_id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS mod_actions (
    id           BIGINT NOT NULL AUTO_INCREMENT,
    guild_id     BIGINT NOT NULL,
    target_id    BIGINT NOT NULL,
    moderator_id BIGINT NOT NULL,
    action_type  VARCHAR(30) NOT NULL,
    reason       TEXT,
    extra_data   TEXT,
    created_at   VARCHAR(50) NOT NULL,
    PRIMARY KEY (id),
    INDEX idx_ma_target (target_id, guild_id),
    INDEX idx_ma_time   (guild_id, created_at)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS channel_config (
    channel_id   BIGINT PRIMARY KEY,
    guild_id     BIGINT NOT NULL,
    locked       TINYINT DEFAULT 0,
    media_only   TINYINT DEFAULT 0,
    media_config TEXT,
    auto_react   TEXT,
    slowmode     INT DEFAULT 0,
    INDEX idx_cc_guild (guild_id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS server_config (
    guild_id            BIGINT PRIMARY KEY,
    staff_role_id       BIGINT,
    modlog_channel      BIGINT,
    serverlog_channel   BIGINT,
    log_events          TEXT,
    embed_role_id       BIGINT,
    channels_role_id    BIGINT,
    users_role_id       BIGINT,
    modlog_enabled      TINYINT DEFAULT 1,
    serverlog_enabled   TINYINT DEFAULT 1,
    mod_role_id         BIGINT,
    starboard_channel_id BIGINT,
    starboard_threshold  INT DEFAULT 3,
    anti_raid_enabled    TINYINT DEFAULT 0,
    anti_raid_threshold  INT DEFAULT 10,
    anti_raid_window     INT DEFAULT 30,
    anti_raid_lockdown_duration INT DEFAULT 300,
    anti_alt_min_age     INT DEFAULT 7,
    anti_alt_action      VARCHAR(16) DEFAULT 'log',
    anti_alt_role_id     BIGINT
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ai_config (
    guild_id            BIGINT      PRIMARY KEY,
    ai_enabled          TINYINT     DEFAULT 1,
    ai_response_mode    VARCHAR(16) DEFAULT 'mentions',
    ai_channel_id       BIGINT,
    ai_role_id          BIGINT,
    ai_restricted_role_ids TEXT DEFAULT '[]',
    ai_model            VARCHAR(60) DEFAULT 'gemma-4-26b-a4b-it',
    ai_system_prompt    TEXT,
    ai_limit_requests   INT         DEFAULT 50,
    ai_limit_hours      INT         DEFAULT 12,
    ai_imagine_enabled  TINYINT     DEFAULT 1,
    ai_webhook_name     TEXT,
    ai_webhook_icon     TEXT
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Pool de API keys de IA. Cada key puede cubrir hasta 2 guilds.
CREATE TABLE IF NOT EXISTS ai_api_keys (
    id          BIGINT       NOT NULL AUTO_INCREMENT,
    label       VARCHAR(100) NOT NULL,
    api_key     VARCHAR(255) NOT NULL UNIQUE,
    active      TINYINT      DEFAULT 1,
    notes       TEXT,
    created_at  VARCHAR(50)  NOT NULL,
    PRIMARY KEY (id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Asignación 1:1 guild → key. Restringido por aplicación a 2 guilds por key.
CREATE TABLE IF NOT EXISTS ai_guild_keys (
    guild_id    BIGINT      PRIMARY KEY,
    key_id      BIGINT      NOT NULL,
    assigned_at VARCHAR(50) NOT NULL,
    CONSTRAINT fk_ai_guild_key FOREIGN KEY (key_id) REFERENCES ai_api_keys(id) ON DELETE CASCADE
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS appeals (
    id           BIGINT NOT NULL AUTO_INCREMENT,
    guild_id     BIGINT NOT NULL,
    user_id      BIGINT NOT NULL,
    case_id      BIGINT,
    action_type  VARCHAR(30) NOT NULL,
    reason       TEXT,
    appeal_text  TEXT,
    status       VARCHAR(20) DEFAULT 'PENDING',
    created_at   VARCHAR(50) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_appeals_guild_case (guild_id, case_id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS autoresponses (
    id            BIGINT NOT NULL AUTO_INCREMENT,
    guild_id      BIGINT NOT NULL,
    channel_id    BIGINT,
    trigger       TEXT NOT NULL,
    response      TEXT NOT NULL,
    created_at    VARCHAR(50) NOT NULL,
    PRIMARY KEY (id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS tempbans (
    id            BIGINT NOT NULL AUTO_INCREMENT,
    guild_id      BIGINT NOT NULL,
    user_id       BIGINT NOT NULL,
    moderator_id  BIGINT NOT NULL,
    reason        TEXT DEFAULT 'Sin razón especificada',
    ban_start     VARCHAR(50) NOT NULL,
    ban_duration  INT NOT NULL,
    created_at    VARCHAR(50) NOT NULL,
    PRIMARY KEY (id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS saved_embeds (
    id           BIGINT NOT NULL AUTO_INCREMENT,
    guild_id     BIGINT NOT NULL,
    creator_id   BIGINT NOT NULL,
    name         VARCHAR(100),
    embed_data   TEXT NOT NULL,
    created_at   VARCHAR(50) NOT NULL,
    PRIMARY KEY (id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS welcome_config (
    guild_id     BIGINT PRIMARY KEY,
    channel_id   BIGINT,
    embed_data   TEXT,
    enabled      TINYINT DEFAULT 0
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS boost_config (
    guild_id     BIGINT PRIMARY KEY,
    channel_id   BIGINT,
    embed_data   TEXT,
    gif_url      TEXT,
    enabled      TINYINT DEFAULT 0
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS suggestions_config (
    guild_id           BIGINT PRIMARY KEY,
    submit_channel_id  BIGINT,
    review_channel_id  BIGINT,
    public_channel_id  BIGINT,
    enabled            TINYINT DEFAULT 1,
    auto_publish       TINYINT DEFAULT 0,
    min_length         INT     DEFAULT 10,
    max_length         INT     DEFAULT 2000,
    cooldown_seconds   INT     DEFAULT 300
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS suggestions (
    id           BIGINT NOT NULL AUTO_INCREMENT,
    guild_id     BIGINT NOT NULL,
    user_id       BIGINT NOT NULL,
    message_id    BIGINT,
    content       TEXT NOT NULL,
    status        VARCHAR(20) DEFAULT 'PENDING',
    upvotes       INT DEFAULT 0,
    downvotes     INT DEFAULT 0,
    denial_reason TEXT,
    created_at    VARCHAR(50) NOT NULL,
    PRIMARY KEY (id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS suggestion_votes (
    suggestion_id BIGINT NOT NULL,
    user_id       BIGINT NOT NULL,
    vote          INT    NOT NULL,
    PRIMARY KEY (suggestion_id, user_id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS giveaways (
    id             BIGINT NOT NULL AUTO_INCREMENT,
    guild_id       BIGINT NOT NULL,
    channel_id     BIGINT NOT NULL,
    message_id     BIGINT NOT NULL,
    prize          TEXT NOT NULL,
    end_time       BIGINT NOT NULL,
    winners_count  INT DEFAULT 1,
    req_roles      TEXT,
    deny_roles     TEXT,
    participants   TEXT,
    ended          TINYINT DEFAULT 0,
    cancelled      TINYINT DEFAULT 0,
    winners        TEXT,
    PRIMARY KEY (id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS autoroles (
    message_id   BIGINT PRIMARY KEY,
    guild_id     BIGINT NOT NULL,
    channel_id   BIGINT NOT NULL,
    mapping_data TEXT NOT NULL
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS join_autoroles (
    guild_id   BIGINT    NOT NULL,
    role_id    BIGINT    NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, role_id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS lofi_config (
    guild_id        BIGINT  PRIMARY KEY,
    channel_id      BIGINT,
    volume          INT     DEFAULT 100,
    enabled         TINYINT DEFAULT 0,
    stream_url      TEXT,
    station_name    TEXT,
    auto_reconnect  TINYINT DEFAULT 1,
    pause_on_empty  TINYINT DEFAULT 0
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    description TEXT,
    applied_at  VARCHAR(50) NOT NULL
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS bot_stats (
    id             INT PRIMARY KEY DEFAULT 1,
    members_online INT DEFAULT 0,
    total_members  INT DEFAULT 0,
    open_tickets   INT DEFAULT 0,
    uptime_seconds INT DEFAULT 0,
    last_updated   VARCHAR(50)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ticket_config (
    guild_id                BIGINT PRIMARY KEY,
    panel_channel_id        BIGINT,
    category_id             BIGINT,
    log_channel_id          BIGINT,
    allowed_roles           TEXT,
    immune_roles            TEXT,
    panel_embed_data        TEXT,
    channel_name_template   VARCHAR(100) DEFAULT '{username}-{number}',
    max_tickets_per_user    INT DEFAULT 0,
    ticket_cooldown_seconds INT DEFAULT 0,
    panel_select_template   VARCHAR(100),
    panel_inside_template   VARCHAR(100),
    msg_open_template       VARCHAR(100),
    msg_close_template      VARCHAR(100)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ticket_categories (
    id                          BIGINT NOT NULL AUTO_INCREMENT,
    guild_id                    BIGINT NOT NULL,
    name                        VARCHAR(100) NOT NULL,
    emoji                       VARCHAR(50),
    description                 TEXT,
    questions                   TEXT,
    close_reasons               TEXT,
    welcome_embed_data          TEXT,
    welcome_embed_template_key  VARCHAR(100),
    staff_role_id               BIGINT,
    PRIMARY KEY (id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ticket_template_embeds (
    id           BIGINT NOT NULL AUTO_INCREMENT,
    guild_id     BIGINT NOT NULL,
    template_key VARCHAR(100) NOT NULL,
    name         VARCHAR(150),
    embed_data   TEXT NOT NULL,
    created_at   VARCHAR(50) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY ux_tpl_guild_key (guild_id, template_key)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS tickets (
    id            BIGINT NOT NULL AUTO_INCREMENT,
    global_number INT NOT NULL,
    guild_id      BIGINT NOT NULL,
    channel_id    BIGINT,
    user_id       BIGINT NOT NULL,
    category_name VARCHAR(100) NOT NULL,
    staff_id      BIGINT,
    status        VARCHAR(20) DEFAULT 'OPEN',
    ai_summary    TEXT,
    created_at    VARCHAR(50) NOT NULL,
    closed_at     VARCHAR(50),
    PRIMARY KEY (id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS tags (
    id         BIGINT NOT NULL AUTO_INCREMENT,
    guild_id   BIGINT NOT NULL,
    name       VARCHAR(100) NOT NULL,
    content    TEXT NOT NULL,
    creator_id BIGINT NOT NULL,
    created_at VARCHAR(50) NOT NULL,
    uses       INT DEFAULT 0,
    PRIMARY KEY (id),
    UNIQUE KEY unique_tag (guild_id, name)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS reports (
    id               BIGINT NOT NULL AUTO_INCREMENT,
    guild_id         BIGINT NOT NULL,
    reporter_id      BIGINT NOT NULL,
    reported_user_id BIGINT NOT NULL,
    reason           TEXT NOT NULL,
    ticket_id        BIGINT,
    status           VARCHAR(20) DEFAULT 'PENDING',
    created_at       VARCHAR(50) NOT NULL,
    PRIMARY KEY (id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS scheduled_messages (
    id               BIGINT NOT NULL AUTO_INCREMENT,
    guild_id         BIGINT NOT NULL,
    name             VARCHAR(100) NOT NULL,
    channel_id       BIGINT NOT NULL,
    content          TEXT NOT NULL,
    interval_seconds INT NOT NULL,
    last_sent        VARCHAR(50),
    enabled          TINYINT DEFAULT 1,
    created_by       BIGINT NOT NULL,
    created_at       VARCHAR(50) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY unique_schedule (guild_id, name)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS user_levels (
    user_id       BIGINT NOT NULL,
    guild_id      BIGINT NOT NULL,
    xp            INT DEFAULT 0,
    level         INT DEFAULT 0,
    message_count INT DEFAULT 0,
    PRIMARY KEY (user_id, guild_id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS xp_config (
    guild_id                     BIGINT PRIMARY KEY,
    enabled                      TINYINT DEFAULT 0,
    xp_min                       INT     DEFAULT 15,
    xp_max                       INT     DEFAULT 25,
    cooldown_seconds             INT     DEFAULT 60,
    ignored_channels             TEXT,
    channel_multipliers          TEXT,
    announcement_channel_id      BIGINT,
    announcement_message         TEXT,
    stack_rewards                TINYINT DEFAULT 1,
    levelup_persist              TINYINT DEFAULT 1,
    levelup_autodelete           TINYINT DEFAULT 0,
    levelup_delete_after_seconds INT     DEFAULT 30,
    levelup_embed_config         TEXT
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS level_rewards (
    id       BIGINT NOT NULL AUTO_INCREMENT,
    guild_id BIGINT NOT NULL,
    level    INT NOT NULL,
    role_id  BIGINT NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY unique_reward (guild_id, level)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS custom_commands (
    id            BIGINT NOT NULL AUTO_INCREMENT,
    guild_id      BIGINT NOT NULL,
    name          VARCHAR(100) NOT NULL,
    enabled       TINYINT DEFAULT 1,
    trigger_type  VARCHAR(50) NOT NULL,
    trigger_value TEXT NOT NULL,
    conditions    TEXT,
    actions       TEXT,
    creator_id    BIGINT NOT NULL,
    created_at    VARCHAR(50) NOT NULL,
    uses          INT DEFAULT 0,
    last_used     VARCHAR(50),
    PRIMARY KEY (id),
    UNIQUE KEY unique_cc (guild_id, name)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS cc_variables (
    id        BIGINT NOT NULL AUTO_INCREMENT,
    guild_id  BIGINT NOT NULL,
    `key`     VARCHAR(100) NOT NULL,
    value     TEXT,
    scope     VARCHAR(100) DEFAULT 'guild',
    PRIMARY KEY (id),
    UNIQUE KEY unique_ccv (guild_id, `key`, scope)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS automod_config (
    guild_id     BIGINT PRIMARY KEY,
    enabled      TINYINT DEFAULT 1,
    rules        TEXT
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS automod_log (
    id           BIGINT NOT NULL AUTO_INCREMENT,
    guild_id     BIGINT NOT NULL,
    rule         VARCHAR(50) NOT NULL,
    user_id      BIGINT NOT NULL,
    content      TEXT,
    action_taken VARCHAR(50) NOT NULL,
    created_at   VARCHAR(50) NOT NULL,
    PRIMARY KEY (id),
    INDEX idx_aml_guild (guild_id, created_at)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
"""


class DatabaseManager:
    """Gestor de base de datos con soporte multi-proveedor."""

    DEFAULT_CONFIG: Dict[str, Any] = {
        "guild_id": None,
        "mute_role_id": None,
        "log_channel_id": None,
        "warn_mute_threshold": 3,
        "warn_kick_threshold": 5,
        "warn_ban_threshold": 7,
        "warn_mute_enabled": 1,
        "warn_kick_enabled": 0,
        "warn_ban_enabled": 0,
        "warn_mute_duration": 3600,
        "warn_embed_config": None,
        "staff_role_id": None,
        "message_role_enabled": 0,
        "message_role_id": None,
        "message_role_notice": "Necesitas el rol requerido para hablar en este servidor.",
    }

    DEFAULT_SERVER_CONFIG: Dict[str, Any] = {
        "guild_id": None,
        "ai_message_categories": "{}",
        "staff_role_id": None,
        "mod_role_id": None,
        "modlog_channel": None,
        "serverlog_channel": None,
        "log_events": None,
        "embed_role_id": None,
        "channels_role_id": None,
        "users_role_id": None,
        "modlog_enabled": 1,
        "serverlog_enabled": 1,
    }

    DEFAULT_CHANNEL_CONFIG: Dict[str, Any] = {
        "channel_id": None,
        "guild_id": None,
        "locked": 0,
        "media_only": 0,
        "media_config": None,
        "auto_react": None,
        "auto_react_enabled": 1,
        "slowmode": 0,
    }

    def __init__(self):
        import threading

        self.db_type = os.getenv("DB_TYPE", "sqlite").lower()

        if self.db_type == "sqlite":
            import sqlite3

            data_dir = Path(__file__).parent.parent / "data"
            data_dir.mkdir(exist_ok=True)
            self.db_path = str(data_dir / "bot.db")
            # Conexión persistente con lock para acceso concurrente seguro
            self._sqlite_lock = threading.Lock()
            self._sqlite_conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._sqlite_conn.row_factory = sqlite3.Row
            self._sqlite_conn.execute("PRAGMA journal_mode=WAL")
            self._sqlite_conn.execute("PRAGMA foreign_keys=ON")
            self._sqlite_conn.commit()
            logger.info(f"Base de datos: SQLite → {self.db_path}")

        elif self.db_type in ("postgresql", "mariadb"):
            raise ValueError(
                f"DB_TYPE='{self.db_type}' todavía no está soportado por el runtime completo. "
                "Usa DB_TYPE=sqlite; PostgreSQL/MariaDB se habilitarán cuando todas las "
                "migraciones y módulos tengan pruebas de integración por motor."
            )
        else:
            raise ValueError(
                f"DB_TYPE inválido: '{self.db_type}'. Usa 'sqlite'."
            )

        self._init_schema()
        self.recover_processing_appeals()

    def __del__(self):
        """Cierra la conexión persistente de SQLite al destruir el objeto."""
        if self.db_type == "sqlite" and hasattr(self, "_sqlite_conn"):
            try:
                self._sqlite_conn.close()
            except Exception:
                pass

    # ── Utilidades internas ───────────────────────────────────────────────────

    @property
    def ph(self) -> str:
        """Placeholder de parámetro: '?' para SQLite, '%s' para los demás."""
        return "?" if self.db_type == "sqlite" else "%s"

    def _adapt(self, query: str) -> str:
        """Adapta los placeholders '?' según el tipo de DB."""
        if self.db_type == "sqlite":
            return query
        return query.replace("?", "%s")

    # ── Conexión ──────────────────────────────────────────────────────────────

    @contextmanager
    def _conn(self):
        """
        Context manager de conexión.
        SQLite: reutiliza la conexión persistente con lock de hilo.
        PostgreSQL/MariaDB: abre y cierra por operación.
        """
        if self.db_type == "sqlite":
            # Conexión compartida protegida por lock
            with self._sqlite_lock:
                try:
                    yield self._sqlite_conn
                    self._sqlite_conn.commit()
                except Exception as exc:
                    self._sqlite_conn.rollback()
                    logger.error(f"Error de base de datos: {exc}")
                    raise
            return

        # PostgreSQL y MariaDB: conexión por operación
        connection = None
        try:
            if self.db_type == "postgresql":
                import psycopg2
                from psycopg2.extras import RealDictCursor

                connection = psycopg2.connect(self.connection_url)
                connection.cursor_factory = RealDictCursor

            elif self.db_type == "mariadb":
                import pymysql
                import pymysql.cursors

                connection = pymysql.connect(
                    host=os.getenv("DB_HOST", "localhost"),
                    port=int(os.getenv("DB_PORT", "3306")),
                    user=os.getenv("DB_USER"),
                    password=os.getenv("DB_PASSWORD"),
                    database=os.getenv("DB_NAME"),
                    charset="utf8mb4",
                    cursorclass=pymysql.cursors.DictCursor,
                )

            yield connection
            connection.commit()

        except Exception as exc:
            if connection:
                connection.rollback()
            logger.error(f"Error de base de datos: {exc}")
            raise
        finally:
            if connection:
                connection.close()

    # ── Helpers de ejecución ──────────────────────────────────────────────────

    def _execute(self, query: str, params: tuple = ()) -> None:
        with self._conn() as conn:
            if self.db_type == "postgresql":
                from psycopg2.extras import RealDictCursor

                cur = conn.cursor(cursor_factory=RealDictCursor)
            else:
                cur = conn.cursor()
            cur.execute(self._adapt(query), params)

    def _execute_rowcount(self, query: str, params: tuple = ()) -> int:
        """Ejecuta una mutación y devuelve las filas afectadas en la misma transacción."""
        with self._conn() as conn:
            if self.db_type == "postgresql":
                from psycopg2.extras import RealDictCursor

                cur = conn.cursor(cursor_factory=RealDictCursor)
            else:
                cur = conn.cursor()
            cur.execute(self._adapt(query), params)
            return int(cur.rowcount or 0)

    def _fetchone(self, query: str, params: tuple = ()) -> Optional[Dict]:
        with self._conn() as conn:
            if self.db_type == "postgresql":
                from psycopg2.extras import RealDictCursor

                cur = conn.cursor(cursor_factory=RealDictCursor)
            else:
                cur = conn.cursor()
            cur.execute(self._adapt(query), params)
            row = cur.fetchone()
            return dict(row) if row else None

    def _fetchall(self, query: str, params: tuple = ()) -> List[Dict]:
        with self._conn() as conn:
            if self.db_type == "postgresql":
                from psycopg2.extras import RealDictCursor

                cur = conn.cursor(cursor_factory=RealDictCursor)
            else:
                cur = conn.cursor()
            cur.execute(self._adapt(query), params)
            return [dict(r) for r in cur.fetchall()]

    def _executemany(self, queries_params: List[tuple]) -> None:
        """Ejecuta múltiples queries en una única transacción."""
        with self._conn() as conn:
            if self.db_type == "postgresql":
                from psycopg2.extras import RealDictCursor

                cur = conn.cursor(cursor_factory=RealDictCursor)
            else:
                cur = conn.cursor()
            for query, params in queries_params:
                cur.execute(self._adapt(query), params)

    def _reserve_entity_number(
        self,
        cur,
        namespace: str,
        guild_id: int,
        source_table: str,
        source_column: str,
    ) -> int:
        """Reserva un contador por guild dentro de la transacción activa."""
        allowed_sources = {
            ("tickets", "global_number"),
            ("mod_actions", "case_number"),
        }
        if (source_table, source_column) not in allowed_sources:
            raise ValueError("Fuente de contador no permitida")
        cur.execute(
            self._adapt(
                "SELECT next_value FROM entity_counters WHERE namespace = ? AND guild_id = ?"
            ),
            (namespace, guild_id),
        )
        row = cur.fetchone()
        if row:
            value = int(row["next_value"] if hasattr(row, "keys") else row[0])
            cur.execute(
                self._adapt(
                    "UPDATE entity_counters SET next_value = ? WHERE namespace = ? AND guild_id = ?"
                ),
                (value + 1, namespace, guild_id),
            )
            return value

        cur.execute(
            self._adapt(
                f"SELECT MAX({source_column}) AS max_value FROM {source_table} WHERE guild_id = ?"
            ),
            (guild_id,),
        )
        max_row = cur.fetchone()
        raw_max = max_row["max_value"] if hasattr(max_row, "keys") else max_row[0]
        value = int(raw_max or 0) + 1
        cur.execute(
            self._adapt(
                "INSERT INTO entity_counters (namespace, guild_id, next_value) VALUES (?, ?, ?)"
            ),
            (namespace, guild_id, value + 1),
        )
        return value

    # ── Schema ────────────────────────────────────────────────────────────────

    def _init_schema(self):
        schema_map = {
            "sqlite": _SCHEMA_SQLITE,
            "postgresql": _SCHEMA_POSTGRESQL,
            "mariadb": _SCHEMA_MARIADB,
        }
        schema = schema_map[self.db_type]

        with self._conn() as conn:
            if self.db_type == "postgresql":
                from psycopg2.extras import RealDictCursor

                cur = conn.cursor(cursor_factory=RealDictCursor)
            else:
                cur = conn.cursor()
            # SQLite soporta executescript; los demás ejecutan statement a statement
            if self.db_type == "sqlite":
                conn.executescript(schema)
            else:
                for stmt in schema.split(";"):
                    stmt = stmt.strip()
                    if stmt:
                        cur.execute(stmt)

        logger.info("Schema de base de datos inicializado correctamente.")
        self._migrate_ai_config()
        self._migrate_tickets()
        self._run_migrations()
        self._ensure_moderation_mvp_tables()
        self._ensure_case_evidence_table()
        self._ensure_functional_runtime_tables()
        self._ensure_ai_memory_tables()
        self._ensure_ai_deep_think_tables()
        self.ensure_column("server_config", "ai_message_categories", "TEXT DEFAULT '{}'")
        self.ensure_column("automod_log", "extra_data", "TEXT")

    def _ensure_case_evidence_table(self):
        if self.db_type != "sqlite":
            return
        self._execute("""CREATE TABLE IF NOT EXISTS case_evidence (
            id TEXT PRIMARY KEY, guild_id BIGINT NOT NULL, case_id BIGINT NOT NULL,
            metadata TEXT NOT NULL, content BLOB,
            FOREIGN KEY(case_id) REFERENCES mod_actions(id) ON DELETE CASCADE)""")
        self._execute("CREATE INDEX IF NOT EXISTS idx_case_evidence_case ON case_evidence(guild_id, case_id)")

    def add_case_evidence(self, guild_id, case_id, records):
        """Append immutable bytes; serialize quota enforcement with the SQLite write."""
        import uuid
        from cogs._moderation_evidence import MAX_CASE_BYTES
        if self.db_type != "sqlite":
            raise ValueError("evidence_storage_unavailable")
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT id FROM mod_actions WHERE guild_id=? AND id=?", (guild_id, case_id)).fetchone():
                raise ValueError("case_not_found")
            used = conn.execute("SELECT COALESCE(SUM(LENGTH(content)),0), COUNT(*) FROM case_evidence WHERE guild_id=? AND case_id=?", (guild_id, case_id)).fetchone()
            total, count = int(used[0]), int(used[1])
            if count + len(records) > 128:
                raise ValueError("case_evidence_limit")
            for record in records:
                content = record.get("content")
                total += len(content) if content is not None else 0
                if total > MAX_CASE_BYTES:
                    raise ValueError("case_evidence_limit")
                metadata = {key: value for key, value in record.items() if key != "content"}
                conn.execute("INSERT INTO case_evidence(id,guild_id,case_id,metadata,content) VALUES(?,?,?,?,?)",
                    (uuid.uuid4().hex, guild_id, case_id, json.dumps(metadata, ensure_ascii=False), content))

    def _store_case_evidence(self, guild_id, case_id, evidence):
        if not evidence:
            return
        try:
            self.add_case_evidence(guild_id, case_id, evidence)
        except Exception:
            logger.warning("Evidence persistence failed guild=%s case=%s", guild_id, case_id)
            try:
                from cogs._moderation_evidence import artifact
                self.add_case_evidence(guild_id, case_id, [artifact("evidence", error="evidence_storage_failed")])
            except Exception:
                logger.error("Evidence failure record unavailable guild=%s case=%s", guild_id, case_id)

    def list_case_evidence(self, guild_id, case_id):
        case = self.get_case_by_id(guild_id, case_id)
        if not case:
            return []
        parent = self.get_case_by_id(guild_id, case.get("parent_case_id") or 0)
        parent_id = int(parent["id"]) if parent and parent["target_id"] == case["target_id"] else 0
        rows = self._fetchall("SELECT id,case_id,metadata FROM case_evidence WHERE guild_id=? AND case_id IN (?,?) ORDER BY rowid", (guild_id, case_id, parent_id))
        return [{**json.loads(row["metadata"]), "id": row["id"], "case_id": str(row["case_id"]),
                 "inherited": int(row["case_id"]) != int(case_id)} for row in rows]

    def get_case_evidence(self, guild_id, case_id, evidence_id):
        if not any(item["id"] == evidence_id for item in self.list_case_evidence(guild_id, case_id)):
            return None
        row = self._fetchone("SELECT content,metadata FROM case_evidence WHERE guild_id=? AND id=?", (guild_id, evidence_id))
        return {**json.loads(row["metadata"]), "content": bytes(row["content"]) if row["content"] is not None else None} if row else None

    def case_evidence_summary(self, guild_id, case_id):
        items = self.list_case_evidence(guild_id, case_id)
        available = sum(item["status"] == "available" for item in items)
        errors = [item["error"] for item in items if item.get("error")]
        return {"evidence_count": available,
            "evidence_status": "partial" if available and errors else "available" if available else "failed" if errors else "none",
            "evidence_errors": errors}

    def _ensure_ai_deep_think_tables(self) -> None:
        self._executemany([(sql, ()) for sql in (
            """CREATE TABLE IF NOT EXISTS ai_deep_think_requests (
                guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
                request_id VARCHAR(100) NOT NULL, usage_day VARCHAR(10) NOT NULL,
                refunded INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id, request_id))""",
            "CREATE INDEX IF NOT EXISTS idx_ai_deep_think_daily ON ai_deep_think_requests (guild_id, user_id, usage_day, refunded)",
        )])

    def reserve_ai_deep_think(self, guild_id: int, user_id: int, request_id: int | str,
                              *, now: datetime | None = None) -> Dict:
        """Reserve once per Discord request; only accepted=True may start work."""
        with self._conn() as conn:
            # Take the write lock before reading: other connections share this quota.
            conn.execute("BEGIN IMMEDIATE")
            day = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
            reset_at = datetime.combine(day + timedelta(days=1), datetime.min.time(), timezone.utc)
            cur = conn.cursor()
            cur.execute("SELECT ai_deep_think, ai_deep_think_daily_limit FROM ai_config WHERE guild_id = ?", (guild_id,))
            cfg = cur.fetchone()
            enabled = bool(cfg and cfg["ai_deep_think"])
            limit = int(cfg["ai_deep_think_daily_limit"]) if cfg else 3
            cur.execute("SELECT refunded FROM ai_deep_think_requests WHERE guild_id = ? AND user_id = ? AND request_id = ?",
                        (guild_id, user_id, str(request_id)))
            previous = cur.fetchone()
            cur.execute("SELECT COUNT(*) AS used FROM ai_deep_think_requests WHERE guild_id = ? AND user_id = ? AND usage_day = ? AND refunded = 0",
                        (guild_id, user_id, day.isoformat()))
            used = int(cur.fetchone()["used"])
            if previous:
                status = "refunded" if previous["refunded"] else "duplicate"
            elif not enabled or limit == 0:
                status = "disabled"
            elif used >= limit:
                status = "limit"
            else:
                cur.execute("INSERT INTO ai_deep_think_requests (guild_id, user_id, request_id, usage_day) VALUES (?, ?, ?, ?)",
                            (guild_id, user_id, str(request_id), day.isoformat()))
                used += 1
                status = "reserved"
            return {"accepted": status == "reserved", "status": status, "used": used,
                    "limit": limit, "remaining": max(0, limit - used), "reset_at": reset_at.isoformat()}

    def refund_ai_deep_think(self, guild_id: int, user_id: int, request_id: int | str) -> bool:
        """Refund a failed request once, retaining its ID to reject duplicate work."""
        return bool(self._execute_rowcount(
            "UPDATE ai_deep_think_requests SET refunded = 1 WHERE guild_id = ? AND user_id = ? AND request_id = ? AND refunded = 0",
            (guild_id, user_id, str(request_id)),
        ))

    def _ensure_ai_memory_tables(self) -> None:
        self._executemany([(sql, ()) for sql in (
            "CREATE TABLE IF NOT EXISTS ai_scope_revision (guild_id BIGINT NOT NULL, scope VARCHAR(8) NOT NULL, owner_id BIGINT NOT NULL, revision BIGINT NOT NULL, PRIMARY KEY (guild_id, scope, owner_id))",
            """CREATE TABLE IF NOT EXISTS ai_memories (
                guild_id BIGINT NOT NULL, scope VARCHAR(8) NOT NULL, owner_id BIGINT NOT NULL,
                source_channel BIGINT NOT NULL, public_at_capture INTEGER NOT NULL DEFAULT 0,
                content TEXT NOT NULL, updated_at VARCHAR(40) NOT NULL,
                PRIMARY KEY (guild_id, scope, owner_id, source_channel))""",
            """CREATE TABLE IF NOT EXISTS ai_chat_turns (
                guild_id BIGINT NOT NULL, channel_id BIGINT NOT NULL, message_id BIGINT NOT NULL,
                author_id BIGINT NOT NULL, display_name VARCHAR(100) NOT NULL,
                content TEXT NOT NULL, is_bot INTEGER NOT NULL, created_at VARCHAR(40) NOT NULL,
                requester_id BIGINT,
                origin_message_id BIGINT,
                PRIMARY KEY (guild_id, channel_id, message_id))""",
            "CREATE INDEX IF NOT EXISTS idx_ai_chat_turns_created ON ai_chat_turns (created_at)",
            """CREATE TABLE IF NOT EXISTS ai_personal_memories (
                guild_id BIGINT NOT NULL, owner_id BIGINT NOT NULL, memory_id VARCHAR(32) NOT NULL,
                source_channel BIGINT NOT NULL, source_message BIGINT NOT NULL,
                content TEXT NOT NULL, updated_at VARCHAR(40) NOT NULL,
                PRIMARY KEY (guild_id, owner_id, memory_id))""",
            """CREATE TABLE IF NOT EXISTS ai_personal_memory_settings (
                guild_id BIGINT NOT NULL, owner_id BIGINT NOT NULL, enabled INTEGER NOT NULL,
                after_message BIGINT NOT NULL DEFAULT 0, PRIMARY KEY (guild_id, owner_id))""",
        )])
        self.ensure_column("ai_chat_turns", "requester_id", "BIGINT")
        self.ensure_column("ai_chat_turns", "origin_message_id", "BIGINT")

    def _ai_revision(self, cur, guild_id, channel_id=0, user_id=0):
        cur.execute(self._adapt("SELECT scope, owner_id, revision FROM ai_scope_revision WHERE guild_id = ? AND ((scope = 'global' AND owner_id = 0) OR (scope = 'chat' AND owner_id = ?) OR (scope = 'user' AND owner_id = ?))"), (guild_id, channel_id, user_id))
        rows = {(row["scope"], row["owner_id"]): int(row["revision"]) for row in cur.fetchall()}
        return (rows.get(("global", 0), 0), rows.get(("chat", channel_id), 0), rows.get(("user", user_id), 0))

    def get_ai_memory_revision(self, guild_id: int, channel_id=0, user_id=0):
        with self._conn() as conn:
            return self._ai_revision(conn.cursor(), guild_id, channel_id, user_id)

    def _ai_bump_revision(self, cur, guild_id, scope, owner_id):
        cur.execute(self._adapt("UPDATE ai_scope_revision SET revision = revision + 1 WHERE guild_id = ? AND scope = ? AND owner_id = ?"), (guild_id, scope, owner_id))
        if not cur.rowcount:
            cur.execute(self._adapt("INSERT INTO ai_scope_revision (guild_id, scope, owner_id, revision) VALUES (?, ?, ?, 1)"), (guild_id, scope, owner_id))

    def get_ai_memories(self, guild_id: int, scope: str, owner_id: int) -> List[Dict]:
        return self._fetchall("SELECT * FROM ai_memories WHERE guild_id = ? AND scope = ? AND owner_id = ? ORDER BY updated_at DESC LIMIT 20", (guild_id, scope, owner_id))

    def get_ai_personal_memory_settings(self, guild_id, owner_id):
        return self._fetchone("SELECT enabled, after_message FROM ai_personal_memory_settings WHERE guild_id = ? AND owner_id = ?", (guild_id, owner_id)) or {"enabled": 1, "after_message": 0}

    def get_ai_personal_memories(self, guild_id, owner_id):
        return self._fetchall("SELECT * FROM ai_personal_memories WHERE guild_id = ? AND owner_id = ? ORDER BY updated_at DESC LIMIT 20", (guild_id, owner_id))

    def _ai_personal_barrier(self, cur, guild_id, owner_id, *, enabled=None):
        cur.execute(self._adapt("SELECT enabled, after_message FROM ai_personal_memory_settings WHERE guild_id = ? AND owner_id = ?"), (guild_id, owner_id))
        settings = cur.fetchone() or {"enabled": 1, "after_message": 0}
        cur.execute(self._adapt("SELECT MAX(source_message) AS latest FROM ai_personal_memories WHERE guild_id = ? AND owner_id = ?"), (guild_id, owner_id))
        latest = int(cur.fetchone()["latest"] or 0)
        cur.execute(self._adapt("SELECT MAX(message_id) AS latest FROM ai_chat_turns WHERE guild_id = ? AND author_id = ? AND is_bot = 0"), (guild_id, owner_id))
        # Discord snowflakes encode creation time: a later replay of any pre-control message stays blocked.
        control_message = (int(datetime.now(timezone.utc).timestamp() * 1000) - 1420070400000) << 22 | ((1 << 22) - 1)
        latest = max(latest, int(cur.fetchone()["latest"] or 0), int(settings["after_message"]), control_message)
        cur.execute(self._adapt("DELETE FROM ai_personal_memory_settings WHERE guild_id = ? AND owner_id = ?"), (guild_id, owner_id))
        cur.execute(self._adapt("INSERT INTO ai_personal_memory_settings (guild_id, owner_id, enabled, after_message) VALUES (?, ?, ?, ?)"),
            (guild_id, owner_id, int(settings["enabled"] if enabled is None else enabled), latest))
        self._ai_bump_revision(cur, guild_id, "user", owner_id)

    def set_ai_personal_memory_enabled(self, guild_id, owner_id, enabled):
        if type(enabled) is not bool:
            raise ValueError("Estado de memoria inválido")
        with self._conn() as conn:
            self._ai_personal_barrier(conn.cursor(), guild_id, owner_id, enabled=enabled)

    def save_ai_personal_memory(self, guild_id, owner_id, channel_id, message_id, content, expected_revision):
        import uuid
        if not isinstance(content, str) or not 5 <= len(content.strip()) <= 250:
            return None
        content = content.strip()
        with self._conn() as conn:
            cur = conn.cursor()
            if self._ai_revision(cur, guild_id, channel_id, owner_id) != expected_revision:
                return None
            cur.execute(self._adapt("SELECT enabled, after_message FROM ai_personal_memory_settings WHERE guild_id = ? AND owner_id = ?"), (guild_id, owner_id))
            settings = cur.fetchone()
            if settings and (not settings["enabled"] or message_id <= int(settings["after_message"])):
                return None
            cur.execute(self._adapt("SELECT content FROM ai_personal_memories WHERE guild_id = ? AND owner_id = ? AND source_channel = ?"), (guild_id, owner_id, channel_id))
            if any(row["content"].casefold() == content.casefold() for row in cur.fetchall()):
                return None
            cur.execute(self._adapt("SELECT memory_id FROM ai_personal_memories WHERE guild_id = ? AND owner_id = ?"), (guild_id, owner_id))
            if len(cur.fetchall()) >= 20:
                return None
            # SQLite _conn serializes this check/write with controls; additions must not cancel queued replies.
            memory_id = uuid.uuid4().hex[:12]
            cur.execute(self._adapt("INSERT INTO ai_personal_memories (guild_id, owner_id, memory_id, source_channel, source_message, content, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"),
                (guild_id, owner_id, memory_id, channel_id, message_id, content, datetime.now(timezone.utc).isoformat()))
            return memory_id

    def edit_ai_personal_memory(self, guild_id, owner_id, memory_id, content):
        if not isinstance(content, str) or not 1 <= len(content.strip()) <= 250:
            raise ValueError("Escribe un recuerdo de 1 a 250 caracteres")
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(self._adapt("SELECT memory_id FROM ai_personal_memories WHERE guild_id = ? AND owner_id = ? AND memory_id = ?"), (guild_id, owner_id, memory_id))
            if not cur.fetchone():
                return False
            self._ai_personal_barrier(cur, guild_id, owner_id)
            cur.execute(self._adapt("UPDATE ai_personal_memories SET content = ?, updated_at = ? WHERE guild_id = ? AND owner_id = ? AND memory_id = ?"),
                (content.strip(), datetime.now(timezone.utc).isoformat(), guild_id, owner_id, memory_id))
            return True

    def delete_ai_personal_memory(self, guild_id, owner_id, memory_id):
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(self._adapt("SELECT memory_id FROM ai_personal_memories WHERE guild_id = ? AND owner_id = ? AND memory_id = ?"), (guild_id, owner_id, memory_id))
            if not cur.fetchone():
                return False
            self._ai_personal_barrier(cur, guild_id, owner_id)
            cur.execute(self._adapt("DELETE FROM ai_personal_memories WHERE guild_id = ? AND owner_id = ? AND memory_id = ?"), (guild_id, owner_id, memory_id))
            return True

    def set_ai_memory(self, guild_id, scope, owner_id, source_channel, content, public_at_capture=False):
        if scope not in {"user", "chat", "global"} or not isinstance(content, str) or len(content) > 2000:
            raise ValueError("Memoria inválida (máximo 2000 caracteres)")
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(self._adapt("DELETE FROM ai_memories WHERE guild_id = ? AND scope = ? AND owner_id = ? AND source_channel = ?"), (guild_id, scope, owner_id, source_channel))
            if content.strip():
                cur.execute(self._adapt("INSERT INTO ai_memories (guild_id, scope, owner_id, source_channel, public_at_capture, content, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"), (guild_id, scope, owner_id, source_channel, int(public_at_capture), content.strip(), datetime.now(timezone.utc).isoformat()))
            if scope == "user":
                cur.execute(self._adapt("SELECT source_channel FROM ai_memories WHERE guild_id = ? AND scope = 'user' AND owner_id = ? ORDER BY updated_at DESC"), (guild_id, owner_id))
                for row in cur.fetchall()[20:]:
                    cur.execute(self._adapt("DELETE FROM ai_memories WHERE guild_id = ? AND scope = 'user' AND owner_id = ? AND source_channel = ?"), (guild_id, owner_id, row["source_channel"]))
            self._ai_bump_revision(cur, guild_id, scope, owner_id)

    def get_ai_chat_turns(self, guild_id, channel_id):
        limit = self.get_ai_config(guild_id)["ai_memory_messages"]
        if not limit:
            return []
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        rows = self._fetchall("SELECT * FROM ai_chat_turns WHERE guild_id = ? AND channel_id = ? AND created_at >= ? ORDER BY message_id DESC LIMIT ?", (guild_id, channel_id, cutoff, limit))
        return list(reversed(rows))

    def get_ai_chat_origin(self, guild_id, channel_id, message_id):
        row = self._fetchone("SELECT origin_message_id FROM ai_chat_turns WHERE guild_id = ? AND channel_id = ? AND message_id = ? AND is_bot = 1", (guild_id, channel_id, message_id))
        return int(row["origin_message_id"]) if row and row["origin_message_id"] else None

    def append_ai_chat_turn(self, guild_id, channel_id, message_id, author_id, display_name, content, is_bot=False, expected_revision=None, requester_id=None, origin_message_id=None):
        with self._conn() as conn:
            cur = conn.cursor()
            revision = self._ai_revision(cur, guild_id, channel_id, requester_id if is_bot else author_id)
            if expected_revision is not None and revision != expected_revision:
                return False
            cur.execute(self._adapt("SELECT ai_memory_messages FROM ai_config WHERE guild_id = ?"), (guild_id,))
            config = cur.fetchone()
            limit = int(config["ai_memory_messages"]) if config else 40
            if not limit:
                cur.execute(self._adapt("DELETE FROM ai_chat_turns WHERE guild_id = ? AND channel_id = ?"), (guild_id, channel_id))
                return True
            cur.execute(self._adapt("DELETE FROM ai_chat_turns WHERE guild_id = ? AND channel_id = ? AND message_id = ?"), (guild_id, channel_id, message_id))
            cur.execute(self._adapt("INSERT INTO ai_chat_turns (guild_id, channel_id, message_id, author_id, display_name, content, is_bot, created_at, requester_id, origin_message_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"), (guild_id, channel_id, message_id, author_id, display_name[:100], content[:4000], int(is_bot), datetime.now(timezone.utc).isoformat(), requester_id, origin_message_id))
            cur.execute(self._adapt("SELECT message_id FROM ai_chat_turns WHERE guild_id = ? AND channel_id = ? ORDER BY message_id DESC LIMIT 1 OFFSET ?"), (guild_id, channel_id, limit - 1))
            cutoff_row = cur.fetchone()
            if cutoff_row:
                cur.execute(self._adapt("DELETE FROM ai_chat_turns WHERE guild_id = ? AND channel_id = ? AND message_id < ?"), (guild_id, channel_id, int(cutoff_row["message_id"])))
            cur.execute(self._adapt("DELETE FROM ai_chat_turns WHERE created_at < ?"), ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),))
            return True

    def clear_ai_memory(self, guild_id, scope, owner_id):
        if scope not in {"user", "chat", "global"}:
            raise ValueError("Ámbito inválido")
        with self._conn() as conn:
            cur = conn.cursor()
            if scope == "user":
                self._ai_personal_barrier(cur, guild_id, owner_id)
                cur.execute(self._adapt("DELETE FROM ai_personal_memories WHERE guild_id = ? AND owner_id = ?"), (guild_id, owner_id))
            elif scope == "chat":
                cur.execute(self._adapt("SELECT DISTINCT owner_id FROM ai_personal_memories WHERE guild_id = ? AND source_channel = ?"), (guild_id, owner_id))
                for row in cur.fetchall():
                    self._ai_personal_barrier(cur, guild_id, int(row["owner_id"]))
                cur.execute(self._adapt("DELETE FROM ai_personal_memories WHERE guild_id = ? AND source_channel = ?"), (guild_id, owner_id))
            cur.execute(self._adapt("DELETE FROM ai_memories WHERE guild_id = ? AND scope = ? AND owner_id = ?"), (guild_id, scope, owner_id))
            if scope == "chat":
                cur.execute(self._adapt("DELETE FROM ai_chat_turns WHERE guild_id = ? AND channel_id = ?"), (guild_id, owner_id))
            elif scope == "user":
                cur.execute(self._adapt("SELECT DISTINCT channel_id FROM ai_chat_turns WHERE guild_id = ? AND (author_id = ? OR requester_id = ?)"), (guild_id, owner_id, owner_id))
                channels = [int(row["channel_id"]) for row in cur.fetchall()]
                cur.execute(self._adapt("DELETE FROM ai_chat_turns WHERE guild_id = ? AND (author_id = ? OR requester_id = ?)"), (guild_id, owner_id, owner_id))
                for channel_id in channels:
                    self._ai_bump_revision(cur, guild_id, "chat", channel_id)
            self._ai_bump_revision(cur, guild_id, scope, owner_id)

    def invalidate_ai_chat(self, guild_id, channel_id, message_id):
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(self._adapt("SELECT DISTINCT owner_id FROM ai_personal_memories WHERE guild_id = ? AND source_channel = ? AND source_message = ?"), (guild_id, channel_id, message_id))
            for row in cur.fetchall():
                self._ai_personal_barrier(cur, guild_id, int(row["owner_id"]))
            cur.execute(self._adapt("DELETE FROM ai_personal_memories WHERE guild_id = ? AND source_channel = ? AND source_message = ?"), (guild_id, channel_id, message_id))
            cur.execute(self._adapt("DELETE FROM ai_chat_turns WHERE guild_id = ? AND channel_id = ? AND (message_id = ? OR origin_message_id = ?)"), (guild_id, channel_id, message_id, message_id))
            if cur.rowcount:
                self._ai_bump_revision(cur, guild_id, "chat", channel_id)

    def _ensure_functional_runtime_tables(self) -> None:
        """Tablas idempotentes para estado runtime que debe sobrevivir reinicios."""
        statements = [
            """
            CREATE TABLE IF NOT EXISTS channel_settings (
                guild_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                settings_json TEXT NOT NULL,
                PRIMARY KEY (guild_id, channel_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS starboard_entries (
                guild_id BIGINT NOT NULL,
                source_message_id BIGINT NOT NULL,
                source_channel_id BIGINT NOT NULL,
                starboard_message_id BIGINT NOT NULL,
                star_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, source_message_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS role_menus (
                message_id BIGINT PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                role_ids TEXT NOT NULL,
                placeholder TEXT NOT NULL,
                max_values INTEGER NOT NULL DEFAULT 1,
                title TEXT,
                description TEXT,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS persistent_reminders (
                reminder_id VARCHAR(64) PRIMARY KEY,
                guild_id BIGINT,
                user_id BIGINT NOT NULL,
                message TEXT NOT NULL,
                due_at TEXT NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS raid_lockdowns (
                guild_id BIGINT PRIMARY KEY,
                original_verification_level INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS giveaway_participants (
                message_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                joined_at TEXT NOT NULL,
                PRIMARY KEY (message_id, user_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS entity_counters (
                namespace VARCHAR(64) NOT NULL,
                guild_id BIGINT NOT NULL,
                next_value INTEGER NOT NULL,
                PRIMARY KEY (namespace, guild_id)
            )
            """,
        ]
        for statement in statements:
            self._execute(statement)
        self._execute(
            "UPDATE ai_config SET ai_model = ? WHERE ai_model = ?",
            ("gemma-4-26b-a4b-it", "gemma-4-26b-it"),
        )
        if self.db_type != "mariadb":
            for statement in (
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_mod_actions_guild_case "
                "ON mod_actions(guild_id, case_number)",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_guild_number "
                "ON tickets(guild_id, global_number)",
            ):
                try:
                    self._execute(statement)
                except Exception as exc:
                    logger.warning("No se pudo crear índice funcional %s: %s", statement, exc)

    def _migrate_tickets(self) -> None:
        """
        Migración no destructiva de tablas de tickets para BBDDs creadas
        antes de Fase 7 del refactor.
        """
        cfg_cols = [
            ("max_tickets_per_user", "INTEGER DEFAULT 0"),
            ("ticket_cooldown_seconds", "INTEGER DEFAULT 0"),
            ("panel_select_template", "TEXT"),
            ("panel_inside_template", "TEXT"),
            ("msg_open_template", "TEXT"),
            ("msg_close_template", "TEXT"),
        ]
        for col, col_def in cfg_cols:
            try:
                self.ensure_column("ticket_config", col, col_def)
            except Exception as e:
                logger.warning("ensure_column ticket_config.%s: %s", col, e)

        cat_cols = [
            ("description", "TEXT"),
            ("welcome_embed_template_key", "TEXT"),
            ("staff_role_id", "INTEGER"),
            ("staff_role_ids", "TEXT"),
        ]
        for col, col_def in cat_cols:
            try:
                self.ensure_column("ticket_categories", col, col_def)
            except Exception as e:
                logger.warning("ensure_column ticket_categories.%s: %s", col, e)

        # Niveles (Fase 9): comportamiento del mensaje de subida.
        xp_cols = [
            ("levelup_persist", "INTEGER DEFAULT 1"),
            ("levelup_autodelete", "INTEGER DEFAULT 0"),
            ("levelup_delete_after_seconds", "INTEGER DEFAULT 30"),
            ("levelup_embed_config", "TEXT"),
            # "same" (canal del mensaje) | "channel" (canal predeterminado).
            ("announcement_mode", "TEXT"),
        ]

        # Autorespuestas: trigger avanzado + response embed.
        ar_cols = [
            # "contains" | "exact" | "word" | "starts_with" | "regex"
            ("match_type", "TEXT DEFAULT 'contains'"),
            ("case_sensitive", "INTEGER DEFAULT 0"),
            # JSON con la forma de MessageEditor para respuestas con embed.
            ("response_data", "TEXT"),
            ("enabled", "INTEGER DEFAULT 1"),
        ]
        for col, col_def in ar_cols:
            try:
                self.ensure_column("autoresponses", col, col_def)
            except Exception as e:
                logger.warning("ensure_column autoresponses.%s: %s", col, e)

        # Custom commands: permisos por rol + auto-delete invocación + embed response.
        cc_cols = [
            # JSON: {role_ids: [ids permitidos], everyone: bool}
            ("permission_data", "TEXT"),
            ("delete_invocation", "INTEGER DEFAULT 0"),
            # JSON con la forma de MessageEditor.
            ("response_data", "TEXT"),
        ]
        for col, col_def in cc_cols:
            try:
                self.ensure_column("custom_commands", col, col_def)
            except Exception as e:
                logger.warning("ensure_column custom_commands.%s: %s", col, e)

        # Scheduled messages: modo cron por zona horaria + embed editor.
        sch_cols = [
            # "interval" (cada N seg) | "cron" (hora local en timezone).
            ("schedule_mode", "TEXT DEFAULT 'interval'"),
            ("cron_hour", "INTEGER"),
            ("cron_minute", "INTEGER"),
            # JSON list [0..6] (0=lunes); NULL = todos los días.
            ("cron_weekdays", "TEXT"),
            # IANA tz name (ej. "America/Mexico_City"). NULL = UTC.
            ("timezone", "TEXT"),
            # JSON con la forma de MessageEditor (override del campo content legacy).
            ("message_data", "TEXT"),
        ]
        for col, col_def in sch_cols:
            try:
                self.ensure_column("scheduled_messages", col, col_def)
            except Exception as e:
                logger.warning("ensure_column scheduled_messages.%s: %s", col, e)
        for col, col_def in xp_cols:
            try:
                self.ensure_column("xp_config", col, col_def)
            except Exception as e:
                logger.warning("ensure_column xp_config.%s: %s", col, e)

        # Sorteos (Fase 11): cancelled flag + winners persistidos.
        gw_cols = [
            ("cancelled", "INTEGER DEFAULT 0"),
            ("winners", "TEXT"),
        ]
        for col, col_def in gw_cols:
            try:
                self.ensure_column("giveaways", col, col_def)
            except Exception as e:
                logger.warning("ensure_column giveaways.%s: %s", col, e)

        # Sugerencias (Fase 15): flexibilidad de config + razón de denegación.
        sug_cfg_cols = [
            ("enabled", "INTEGER DEFAULT 1"),
            ("auto_publish", "INTEGER DEFAULT 0"),
            ("min_length", "INTEGER DEFAULT 10"),
            ("max_length", "INTEGER DEFAULT 2000"),
            ("cooldown_seconds", "INTEGER DEFAULT 300"),
        ]
        for col, col_def in sug_cfg_cols:
            try:
                self.ensure_column("suggestions_config", col, col_def)
            except Exception as e:
                logger.warning("ensure_column suggestions_config.%s: %s", col, e)
        try:
            self.ensure_column("suggestions", "denial_reason", "TEXT")
        except Exception as e:
            logger.warning("ensure_column suggestions.denial_reason: %s", e)

        # Radio (Fase 5): flags auto_reconnect / pause_on_empty.
        lofi_cols = [
            ("auto_reconnect", "INTEGER DEFAULT 1"),
            ("pause_on_empty", "INTEGER DEFAULT 0"),
        ]
        for col, col_def in lofi_cols:
            try:
                self.ensure_column("lofi_config", col, col_def)
            except Exception as e:
                logger.warning("ensure_column lofi_config.%s: %s", col, e)
        # Nota: voice_gen_config ya tiene su propia migración idempotente
        # en _ensure_voice_gen_tables (Fase 6).

    def _migrate_ai_config(self) -> None:
        """
        Migración no destructiva de ai_config.
        Añade columnas nuevas a bases de datos existentes sin perder datos.
        Seguro de ejecutar múltiples veces (ignora si la columna ya existe).
        """
        migrations = [
            ("ai_imagine_enabled", "INTEGER DEFAULT 1"),
            ("ai_enabled", "INTEGER DEFAULT 1"),
            ("ai_provider", "VARCHAR(16) DEFAULT 'auto'"),
            ("ai_web_enabled", "INTEGER DEFAULT 0"),
            ("ai_web_max_sources", "INTEGER DEFAULT 3"),
            ("ai_web_timeout_seconds", f"INTEGER DEFAULT {DEFAULT_WEB_TIMEOUT_SECONDS}"),
            ("ai_deep_think", "INTEGER DEFAULT 0"),
            ("ai_deep_think_daily_limit", "INTEGER DEFAULT 3"),
            ("ai_memory_messages", "INTEGER DEFAULT 40"),
            ("ai_response_mode", "VARCHAR(16) DEFAULT 'all'"),
            ("ai_restricted_role_ids", "TEXT DEFAULT '[]'"),
        ]
        for col, col_def in migrations:
            try:
                self.ensure_column("ai_config", col, col_def)
            except Exception as e:
                logger.warning("ensure_column ai_config.%s: %s", col, e)

    # ── Sistema de Migraciones ──────────────────────────────────────────────

    # Lista de migraciones: (version, descripcion, sql)
    # El SQL es el mismo para los 3 motores; se adaptan placeholders automáticamente.
    _MIGRATIONS: List[tuple] = [
        (
            1,
            "lofi_config: añadir stream_url",
            "ALTER TABLE lofi_config ADD COLUMN stream_url TEXT",
        ),
        (
            2,
            "lofi_config: añadir station_name",
            "ALTER TABLE lofi_config ADD COLUMN station_name TEXT",
        ),
        (
            3,
            "ticket_config: máx tickets por usuario",
            "ALTER TABLE ticket_config ADD COLUMN max_tickets_per_user INTEGER DEFAULT 0",
        ),
        (
            4,
            "ticket_config: cooldown entre tickets",
            "ALTER TABLE ticket_config ADD COLUMN ticket_cooldown_seconds INTEGER DEFAULT 0",
        ),
        (
            5,
            "invite_config: tabla de configuración de invitaciones",
            "CREATE TABLE IF NOT EXISTS invite_config (guild_id INTEGER PRIMARY KEY, channel_id INTEGER, enabled INTEGER DEFAULT 1)",
        ),
        (
            6,
            "invite_stats: tabla de estadísticas de invitaciones",
            "CREATE TABLE IF NOT EXISTS invite_stats (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, inviter_id INTEGER NOT NULL, invited_id INTEGER NOT NULL, invite_code TEXT, created_at TEXT, UNIQUE(guild_id, invited_id))",
        ),
        (
            7,
            "server_config: server_log_channel para eventos del servidor",
            "ALTER TABLE server_config ADD COLUMN server_log_channel INTEGER",
        ),
        (
            8,
            "server_config: server_log_enabled",
            "ALTER TABLE server_config ADD COLUMN server_log_enabled INTEGER DEFAULT 0",
        ),
        (
            9,
            "server_config: starboard_channel_id",
            "ALTER TABLE server_config ADD COLUMN starboard_channel_id INTEGER",
        ),
        (
            10,
            "server_config: starboard_threshold",
            "ALTER TABLE server_config ADD COLUMN starboard_threshold INTEGER DEFAULT 3",
        ),
        (
            11,
            "autoresponses: tabla de autorespuestas",
            "CREATE TABLE IF NOT EXISTS autoresponses (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, channel_id INTEGER, trigger TEXT NOT NULL, response TEXT NOT NULL, created_at TEXT NOT NULL)",
        ),
        (
            12,
            "server_config: anti_raid_enabled",
            "ALTER TABLE server_config ADD COLUMN anti_raid_enabled INTEGER DEFAULT 0",
        ),
        (
            13,
            "server_config: anti_raid_threshold",
            "ALTER TABLE server_config ADD COLUMN anti_raid_threshold INTEGER DEFAULT 10",
        ),
        (
            14,
            "server_config: anti_raid_window",
            "ALTER TABLE server_config ADD COLUMN anti_raid_window INTEGER DEFAULT 30",
        ),
        (
            15,
            "server_config: anti_raid_lockdown_duration",
            "ALTER TABLE server_config ADD COLUMN anti_raid_lockdown_duration INTEGER DEFAULT 300",
        ),
        (
            16,
            "server_config: anti_alt_min_age",
            "ALTER TABLE server_config ADD COLUMN anti_alt_min_age INTEGER DEFAULT 7",
        ),
        (
            17,
            "server_config: anti_alt_action",
            "ALTER TABLE server_config ADD COLUMN anti_alt_action TEXT DEFAULT 'log'",
        ),
        (
            18,
            "server_config: anti_alt_role_id",
            "ALTER TABLE server_config ADD COLUMN anti_alt_role_id INTEGER",
        ),
        (
            19,
            "guild_config: message_role_enabled",
            "ALTER TABLE guild_config ADD COLUMN message_role_enabled INTEGER DEFAULT 0",
        ),
        (
            20,
            "guild_config: message_role_id",
            "ALTER TABLE guild_config ADD COLUMN message_role_id BIGINT",
        ),
        (
            21,
            "guild_config: message_role_notice",
            "ALTER TABLE guild_config ADD COLUMN message_role_notice TEXT DEFAULT 'Necesitas el rol requerido para hablar en este servidor.'",
        ),
        (
            22,
            "channel_config: activar o pausar autorreacciones sin perder emojis",
            "ALTER TABLE channel_config ADD COLUMN auto_react_enabled INTEGER DEFAULT 1",
        ),
    ]

    def _run_migrations(self) -> None:
        """
        Ejecuta las migraciones pendientes de forma secuencial e idempotente.
        Cada migración se registra en la tabla schema_migrations para no repetirse.
        """
        try:
            applied = {
                r["version"]
                for r in self._fetchall("SELECT version FROM schema_migrations", ())
            }
        except Exception:
            applied = set()

        for version, description, sql in self._MIGRATIONS:
            if version in applied:
                continue
            try:
                self._execute(sql, ())
                self._execute(
                    "INSERT INTO schema_migrations (version, description, applied_at) VALUES (?, ?, ?)",
                    (version, description, datetime.now(timezone.utc).isoformat()),
                )
                logger.info(f"Migración v{version} aplicada: {description}")
            except Exception as exc:
                # Ignorar si la columna ya existe (bases de datos antiguas con ensure_column aplicado)
                if (
                    "duplicate column" in str(exc).lower()
                    or "already exists" in str(exc).lower()
                ):
                    # Registrar igualmente para no reintentar
                    try:
                        self._execute(
                            "INSERT INTO schema_migrations (version, description, applied_at) VALUES (?, ?, ?)",
                            (
                                version,
                                description,
                                datetime.now(timezone.utc).isoformat(),
                            ),
                        )
                    except Exception:
                        pass
                else:
                    logger.warning(
                        f"Error en migración v{version} ('{description}'): {exc}"
                    )

    def _ensure_moderation_mvp_tables(self) -> None:
        """Migraciones idempotentes para el MVP de moderación/Nekotina parity."""
        text_type = "VARCHAR(50)" if self.db_type == "mariadb" else "TEXT"
        case_ref_type = "INTEGER" if self.db_type == "sqlite" else "BIGINT"
        mod_columns = [
            ("case_number", "INTEGER"),
            ("status", f"{text_type} DEFAULT 'active'"),
            ("evidence_url", "TEXT"),
            ("duration_seconds", "INTEGER"),
            ("expires_at", text_type),
            ("parent_case_id", case_ref_type),
            ("updated_at", text_type),
        ]
        for col, col_def in mod_columns:
            self.ensure_column("mod_actions", col, col_def)
        self.ensure_column("tempbans", "case_id", case_ref_type)
        self.ensure_column("appeals", "case_id", case_ref_type)
        try:
            if self.db_type == "mariadb":
                existing_appeal_index = self._fetchone(
                    "SELECT 1 AS present FROM information_schema.statistics "
                    "WHERE table_schema = DATABASE() AND table_name = 'appeals' "
                    "AND index_name = 'uq_appeals_guild_case' LIMIT 1"
                )
                if not existing_appeal_index:
                    self._execute(
                        "CREATE UNIQUE INDEX uq_appeals_guild_case "
                        "ON appeals (guild_id, case_id)"
                    )
            else:
                self._execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_appeals_guild_case "
                    "ON appeals (guild_id, case_id)"
                )
        except Exception as exc:
            logger.warning("No se pudo asegurar unicidad de appeals.case_id: %s", exc)

        if self.db_type == "sqlite":
            self._execute(
                """
                CREATE TABLE IF NOT EXISTS pending_moderation_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    action_type TEXT NOT NULL,
                    case_id INTEGER,
                    execute_at TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    attempts INTEGER DEFAULT 0,
                    last_error TEXT,
                    extra_data TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT
                )
                """,
                (),
            )
            self._execute(
                """
                CREATE TABLE IF NOT EXISTS log_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    log_type TEXT NOT NULL,
                    channel_id INTEGER,
                    payload TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    attempts INTEGER DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT,
                    sent_at TEXT
                )
                """,
                (),
            )
        elif self.db_type == "postgresql":
            self._execute(
                """
                CREATE TABLE IF NOT EXISTS pending_moderation_actions (
                    id BIGSERIAL PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    action_type TEXT NOT NULL,
                    case_id BIGINT,
                    execute_at TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    attempts INTEGER DEFAULT 0,
                    last_error TEXT,
                    extra_data TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT
                )
                """,
                (),
            )
            self._execute(
                """
                CREATE TABLE IF NOT EXISTS log_outbox (
                    id BIGSERIAL PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    log_type TEXT NOT NULL,
                    channel_id BIGINT,
                    payload TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    attempts INTEGER DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT,
                    sent_at TEXT
                )
                """,
                (),
            )
        else:
            self._execute(
                """
                CREATE TABLE IF NOT EXISTS pending_moderation_actions (
                    id BIGINT NOT NULL AUTO_INCREMENT,
                    guild_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    action_type VARCHAR(40) NOT NULL,
                    case_id BIGINT,
                    execute_at VARCHAR(50) NOT NULL,
                    status VARCHAR(20) DEFAULT 'pending',
                    attempts INT DEFAULT 0,
                    last_error TEXT,
                    extra_data TEXT,
                    created_at VARCHAR(50) NOT NULL,
                    updated_at VARCHAR(50),
                    PRIMARY KEY (id),
                    INDEX idx_pma_due (status, execute_at),
                    INDEX idx_pma_guild_user (guild_id, user_id)
                ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
                """,
                (),
            )
            self._execute(
                """
                CREATE TABLE IF NOT EXISTS log_outbox (
                    id BIGINT NOT NULL AUTO_INCREMENT,
                    guild_id BIGINT NOT NULL,
                    log_type VARCHAR(50) NOT NULL,
                    channel_id BIGINT,
                    payload TEXT NOT NULL,
                    status VARCHAR(20) DEFAULT 'pending',
                    attempts INT DEFAULT 0,
                    last_error TEXT,
                    created_at VARCHAR(50) NOT NULL,
                    updated_at VARCHAR(50),
                    sent_at VARCHAR(50),
                    PRIMARY KEY (id),
                    INDEX idx_log_outbox_due (status, created_at),
                    INDEX idx_log_outbox_guild (guild_id)
                ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
                """,
                (),
            )

        for sql in (
            "CREATE INDEX IF NOT EXISTS idx_mod_actions_case ON mod_actions (guild_id, case_number)",
            "CREATE INDEX IF NOT EXISTS idx_mod_actions_status ON mod_actions (guild_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_pma_due ON pending_moderation_actions (status, execute_at)",
            "CREATE INDEX IF NOT EXISTS idx_pma_guild_user ON pending_moderation_actions (guild_id, user_id)",
            "CREATE INDEX IF NOT EXISTS idx_log_outbox_due ON log_outbox (status, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_log_outbox_guild ON log_outbox (guild_id)",
        ):
            try:
                if self.db_type == "mariadb" and "IF NOT EXISTS" in sql:
                    continue
                self._execute(sql, ())
            except Exception:
                pass

        self._backfill_case_numbers()

    def _backfill_case_numbers(self) -> None:
        """Asigna case_number por servidor a acciones antiguas sin número."""
        try:
            rows = self._fetchall(
                "SELECT id, guild_id FROM mod_actions "
                "WHERE case_number IS NULL ORDER BY guild_id ASC, created_at ASC, id ASC"
            )
        except Exception:
            return
        next_by_guild: Dict[int, int] = {}
        for row in rows:
            guild_id = int(row["guild_id"])
            if guild_id not in next_by_guild:
                current = self._fetchone(
                    "SELECT MAX(case_number) AS max_case FROM mod_actions WHERE guild_id = ?",
                    (guild_id,),
                )
                next_by_guild[guild_id] = int(current.get("max_case") or 0) + 1 if current else 1
            case_number = next_by_guild[guild_id]
            next_by_guild[guild_id] += 1
            try:
                self._execute(
                    "UPDATE mod_actions SET case_number = ?, status = COALESCE(status, 'active'), "
                    "updated_at = COALESCE(updated_at, created_at) WHERE id = ?",
                    (case_number, row["id"]),
                )
            except Exception:
                pass

    def _has_column(self, table: str, column: str) -> bool:
        """Comprueba si una tabla tiene una columna (multi-DB)."""
        try:
            if self.db_type == "sqlite":
                rows = self._fetchall(f"PRAGMA table_info('{table}')")
                return any(r.get("name") == column for r in rows)

            if self.db_type == "mariadb":
                row = self._fetchone(
                    "SELECT COUNT(*) as c FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ? AND COLUMN_NAME = ?",
                    (table, column),
                )
                return bool(row and row.get("c", 0))

            if self.db_type == "postgresql":
                row = self._fetchone(
                    "SELECT COUNT(*) as c FROM information_schema.columns "
                    "WHERE table_name = ? AND column_name = ?",
                    (table, column),
                )
                return bool(row and row.get("c", 0))

        except Exception:
            return False

        return False

    def ensure_column(self, table: str, column: str, column_def: str) -> None:
        """Añade una columna si no existe (silencioso si ya existe).

        Uso seguro desde código que puede ejecutarse repetidamente.
        """
        try:
            if self._has_column(table, column):
                return
            self._execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}", ())
            logger.info(f"Columna '{column}' añadida en tabla '{table}'")
        except Exception:
            # Ignorar si ya existe o si no es soportado por el motor
            pass

    # ── Guild Config ──────────────────────────────────────────────────────────

    def get_channel_settings(self, guild_id: int, channel_id: int):
        row = self._fetchone("SELECT settings_json FROM channel_settings WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id))
        return json.loads(row["settings_json"]) if row else None

    def list_channel_settings(self, guild_id: int):
        rows = self._fetchall("SELECT channel_id, settings_json FROM channel_settings WHERE guild_id = ? ORDER BY channel_id", (guild_id,))
        return [{"channel_id": str(row["channel_id"]), "config": json.loads(row["settings_json"])} for row in rows]

    def set_channel_settings_batch(self, guild_id: int, channel_ids: list[int], config: dict):
        encoded = json.dumps(config, ensure_ascii=False)
        with self._conn() as conn:
            cur = conn.cursor()
            for channel_id in channel_ids:
                # DELETE/INSERT en la misma transacción conserva atomicidad del lote.
                cur.execute(self._adapt("DELETE FROM channel_settings WHERE guild_id = ? AND channel_id = ?"), (guild_id, channel_id))
                cur.execute(self._adapt("INSERT INTO channel_settings (guild_id, channel_id, settings_json) VALUES (?, ?, ?)"), (guild_id, channel_id, encoded))

    def delete_channel_settings(self, guild_id: int, channel_id: int):
        self._execute("DELETE FROM channel_settings WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id))

    def get_config(self, guild_id: int) -> Dict:
        """Retorna la config del servidor, con valores por defecto si no existe."""
        row = self._fetchone(
            "SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)
        )
        result = dict(self.DEFAULT_CONFIG)
        result["guild_id"] = guild_id
        if row:
            result.update(row)
        return result

    def set_config(self, guild_id: int, **kwargs) -> None:
        """Crea o actualiza campos de configuración de un servidor."""
        invalid = set(kwargs) - VALID_CONFIG_COLUMNS
        if invalid:
            raise ValueError(f"Columnas inválidas: {invalid}")

        ops = []
        # Asegurar que el registro exista
        if self.db_type == "sqlite":
            ops.append(
                (
                    "INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)",
                    (guild_id,),
                )
            )
        elif self.db_type == "postgresql":
            ops.append(
                (
                    "INSERT INTO guild_config (guild_id) VALUES (?) "
                    "ON CONFLICT (guild_id) DO NOTHING",
                    (guild_id,),
                )
            )
        else:  # mariadb
            ops.append(
                (
                    "INSERT IGNORE INTO guild_config (guild_id) VALUES (?)",
                    (guild_id,),
                )
            )

        for col, val in kwargs.items():
            ops.append(
                (
                    f"UPDATE guild_config SET {col} = ? WHERE guild_id = ?",
                    (val, guild_id),
                )
            )

        self._executemany(ops)

    # ── User Records ──────────────────────────────────────────────────────────

    def get_user(self, user_id: int, guild_id: int) -> Dict:
        row = self._fetchone(
            "SELECT * FROM user_records WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        )
        if row:
            return row
        return {
            "user_id": user_id,
            "guild_id": guild_id,
            "warns": 0,
            "mute_start": None,
            "mute_duration": None,
        }

    def _upsert_user(self, user_id: int, guild_id: int, **kwargs) -> None:
        invalid = set(kwargs) - VALID_USER_COLUMNS
        if invalid:
            raise ValueError(f"Columnas inválidas: {invalid}")

        ops = []
        if self.db_type == "sqlite":
            ops.append(
                (
                    "INSERT OR IGNORE INTO user_records (user_id, guild_id) VALUES (?, ?)",
                    (user_id, guild_id),
                )
            )
        elif self.db_type == "postgresql":
            ops.append(
                (
                    "INSERT INTO user_records (user_id, guild_id) VALUES (?, ?) "
                    "ON CONFLICT (user_id, guild_id) DO NOTHING",
                    (user_id, guild_id),
                )
            )
        else:
            ops.append(
                (
                    "INSERT IGNORE INTO user_records (user_id, guild_id) VALUES (?, ?)",
                    (user_id, guild_id),
                )
            )

        for col, val in kwargs.items():
            ops.append(
                (
                    f"UPDATE user_records SET {col} = ? WHERE user_id = ? AND guild_id = ?",
                    (val, user_id, guild_id),
                )
            )

        self._executemany(ops)

    def add_warn(self, user_id: int, guild_id: int) -> int:
        """Incrementa el contador de warns y retorna el nuevo total."""
        current = self.get_user(user_id, guild_id)
        new_count = current["warns"] + 1
        self._upsert_user(user_id, guild_id, warns=new_count)
        return new_count

    def add_warn_with_action(
        self,
        user_id: int,
        guild_id: int,
        moderator_id: int,
        action_type: str,
        reason: str,
        *,
        evidence_url: Optional[str] = None,
        parent_case_id: Optional[int] = None,
        extra: Optional[Dict] = None,
        evidence: Optional[List[Dict]] = None,
    ) -> tuple[int, int]:
        """Incrementa el warn y crea su caso exacto en una transacción."""
        normalized_action = str(action_type or "").upper()
        if normalized_action not in {"WARN", "AUTO_WARN"}:
            raise ValueError("invalid_warn_action_type")
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            if self.db_type == "postgresql":
                from psycopg2.extras import RealDictCursor

                cur = conn.cursor(cursor_factory=RealDictCursor)
            else:
                cur = conn.cursor()
            if self.db_type == "sqlite":
                insert_user_sql = (
                    "INSERT OR IGNORE INTO user_records (user_id, guild_id) VALUES (?, ?)"
                )
            elif self.db_type == "postgresql":
                insert_user_sql = (
                    "INSERT INTO user_records (user_id, guild_id) VALUES (?, ?) "
                    "ON CONFLICT (user_id, guild_id) DO NOTHING"
                )
            else:
                insert_user_sql = (
                    "INSERT IGNORE INTO user_records (user_id, guild_id) VALUES (?, ?)"
                )
            cur.execute(self._adapt(insert_user_sql), (user_id, guild_id))
            cur.execute(
                self._adapt(
                    "UPDATE user_records SET warns = COALESCE(warns, 0) + 1 "
                    "WHERE user_id = ? AND guild_id = ?"
                ),
                (user_id, guild_id),
            )
            cur.execute(
                self._adapt(
                    "SELECT warns FROM user_records WHERE user_id = ? AND guild_id = ?"
                ),
                (user_id, guild_id),
            )
            warn_row = cur.fetchone()
            if not warn_row:
                raise RuntimeError("warn_counter_not_persisted")
            warns = int(
                warn_row["warns"] if hasattr(warn_row, "keys") else warn_row[0]
            )
            case_number = self._reserve_entity_number(
                cur, "moderation_case", guild_id, "mod_actions", "case_number"
            )
            cur.execute(
                self._adapt(
                    "INSERT INTO mod_actions "
                    "(guild_id, target_id, moderator_id, action_type, reason, extra_data, "
                    "created_at, case_number, status, evidence_url, parent_case_id, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)"
                ),
                (
                    guild_id,
                    user_id,
                    moderator_id,
                    normalized_action,
                    reason,
                    json.dumps(extra, ensure_ascii=False) if extra else None,
                    now,
                    case_number,
                    evidence_url,
                    parent_case_id,
                    now,
                ),
            )
            cur.execute(
                self._adapt(
                    "SELECT id FROM mod_actions WHERE guild_id = ? AND case_number = ?"
                ),
                (guild_id, case_number),
            )
            case_row = cur.fetchone()
            if not case_row:
                raise RuntimeError("warn_case_not_persisted")
            case_id = int(
                case_row["id"] if hasattr(case_row, "keys") else case_row[0]
            )
        self._store_case_evidence(guild_id, case_id, evidence)
        return warns, case_id

    def clear_warns(self, user_id: int, guild_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            if self.db_type == "postgresql":
                from psycopg2.extras import RealDictCursor

                cur = conn.cursor(cursor_factory=RealDictCursor)
            else:
                cur = conn.cursor()
            cur.execute(
                self._adapt(
                    "UPDATE user_records SET warns = 0 WHERE user_id = ? AND guild_id = ?"
                ),
                (user_id, guild_id),
            )
            cur.execute(
                self._adapt(
                    "UPDATE mod_actions SET status = 'revoked', updated_at = ? "
                    "WHERE guild_id = ? AND target_id = ? "
                    "AND UPPER(action_type) IN ('WARN', 'AUTO_WARN') "
                    "AND UPPER(COALESCE(status, 'active')) = 'ACTIVE'"
                ),
                (now, guild_id, user_id),
            )

    def decrement_warn(self, user_id: int, guild_id: int) -> int:
        """Resta un warn activo sin bajar de cero y retorna el nuevo total."""
        current = self.get_user(user_id, guild_id)
        new_count = max(0, int(current.get("warns") or 0) - 1)
        self._upsert_user(user_id, guild_id, warns=new_count)
        return new_count

    def set_mute(
        self, user_id: int, guild_id: int, duration_secs: Optional[int]
    ) -> None:
        self._upsert_user(
            user_id,
            guild_id,
            mute_start=datetime.now(timezone.utc).isoformat(),
            mute_duration=duration_secs,
        )

    def clear_mute(self, user_id: int, guild_id: int) -> None:
        self._upsert_user(user_id, guild_id, mute_start=None, mute_duration=None)

    def get_active_mutes(self) -> List[Dict]:
        """Retorna todos los registros con mutes activos y duración definida."""
        return self._fetchall(
            "SELECT * FROM user_records "
            "WHERE mute_start IS NOT NULL AND mute_duration IS NOT NULL"
        )

    # ── Mod Actions ───────────────────────────────────────────────────────────

    def log_action(
        self,
        guild_id: int,
        target_id: int,
        moderator_id: int,
        action_type: str,
        reason: str = "Sin razón especificada",
        extra: Optional[Dict] = None,
        *,
        extra_data: Optional[Any] = None,
        status: str = "active",
        evidence_url: Optional[str] = None,
        duration_seconds: Optional[int] = None,
        expires_at: Optional[str] = None,
        parent_case_id: Optional[int] = None,
        evidence: Optional[List[Dict]] = None,
    ) -> int:
        """Registra una acción de moderación y retorna el ID/case interno.

        Mantiene compatibilidad con los call sites antiguos: los parámetros
        extra siguen siendo opcionales y los nuevos campos se pasan solo por
        keyword. Cada guild recibe `case_number` incremental propio.
        """
        now = datetime.now(timezone.utc).isoformat()
        if extra_data is not None:
            stored_extra = extra_data if isinstance(extra_data, str) else json.dumps(extra_data, ensure_ascii=False)
        else:
            stored_extra = json.dumps(extra, ensure_ascii=False) if extra else None
        with self._conn() as conn:
            if self.db_type == "postgresql":
                from psycopg2.extras import RealDictCursor

                cur = conn.cursor(cursor_factory=RealDictCursor)
            else:
                cur = conn.cursor()
            case_number = self._reserve_entity_number(
                cur, "moderation_case", guild_id, "mod_actions", "case_number"
            )
            cur.execute(
                self._adapt(
                    "INSERT INTO mod_actions "
                    "(guild_id, target_id, moderator_id, action_type, reason, extra_data, created_at, "
                    "case_number, status, evidence_url, duration_seconds, expires_at, parent_case_id, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                (
                    guild_id,
                    target_id,
                    moderator_id,
                    action_type,
                    reason,
                    stored_extra,
                    now,
                    case_number,
                    status,
                    evidence_url,
                    duration_seconds,
                    expires_at,
                    parent_case_id,
                    now,
                ),
            )
            cur.execute(
                self._adapt(
                    "SELECT id FROM mod_actions WHERE guild_id = ? AND case_number = ?"
                ),
                (guild_id, case_number),
            )
            row = cur.fetchone()
            if not row:
                return 0
            case_id = int(row["id"] if hasattr(row, "keys") else row[0])
        self._store_case_evidence(guild_id, case_id, evidence)
        return case_id

    def get_applying_ban_cases(self) -> List[Dict]:
        """Intenciones de ban cuyo efecto Discord aún debe reconciliarse."""
        return self._fetchall(
            "SELECT * FROM mod_actions "
            "WHERE (UPPER(COALESCE(status, 'active')) = 'APPLYING' "
            "AND UPPER(action_type) IN ('BAN', 'TEMPBAN', 'FORCEBAN', 'AUTO_BAN', 'SOFTBAN')) "
            "OR (UPPER(status) = 'EFFECT_CONFIRMED' AND UPPER(action_type) = 'SOFTBAN') "
            "ORDER BY id ASC"
        )

    def has_applying_ban_case(self, guild_id: int, user_id: int) -> bool:
        return bool(
            self._fetchone(
                "SELECT id FROM mod_actions WHERE guild_id = ? AND target_id = ? "
                "AND ((UPPER(COALESCE(status, 'active')) = 'APPLYING' "
                "AND UPPER(action_type) IN ('BAN', 'TEMPBAN', 'FORCEBAN', 'AUTO_BAN', 'SOFTBAN')) "
                "OR (UPPER(status) = 'EFFECT_CONFIRMED' AND UPPER(action_type) = 'SOFTBAN')) "
                "LIMIT 1",
                (guild_id, user_id),
            )
        )

    def confirm_prepared_softban_effect(
        self, guild_id: int, user_id: int, case_id: int
    ) -> bool:
        """Persiste que el ban del softban existió antes de intentar retirarlo."""
        return self._execute_rowcount(
            "UPDATE mod_actions SET status = 'effect_confirmed', updated_at = ? "
            "WHERE id = ? AND guild_id = ? AND target_id = ? "
            "AND UPPER(action_type) = 'SOFTBAN' AND UPPER(status) = 'APPLYING'",
            (
                datetime.now(timezone.utc).isoformat(),
                case_id,
                guild_id,
                user_id,
            ),
        ) == 1

    def get_applying_mute_cases(self) -> List[Dict]:
        """Intenciones de mute cuyo rol Discord aún debe reconciliarse."""
        return self._fetchall(
            "SELECT * FROM mod_actions "
            "WHERE UPPER(COALESCE(status, 'active')) = 'APPLYING' "
            "AND UPPER(action_type) IN ('MUTE', 'AUTO_MUTE', 'HARDMUTE') "
            "ORDER BY id ASC"
        )

    def has_applying_mute_case(self, guild_id: int, user_id: int) -> bool:
        return bool(
            self._fetchone(
                "SELECT id FROM mod_actions WHERE guild_id = ? AND target_id = ? "
                "AND UPPER(COALESCE(status, 'active')) = 'APPLYING' "
                "AND UPPER(action_type) IN ('MUTE', 'AUTO_MUTE', 'HARDMUTE') "
                "LIMIT 1",
                (guild_id, user_id),
            )
        )

    def has_active_mute_case(self, guild_id: int, user_id: int) -> bool:
        """Evita reemplazar silenciosamente un rol de mute todavía rastreado."""
        return bool(
            self._fetchone(
                "SELECT id FROM mod_actions WHERE guild_id = ? AND target_id = ? "
                "AND UPPER(COALESCE(status, 'active')) = 'ACTIVE' "
                "AND UPPER(action_type) IN ('MUTE', 'AUTO_MUTE', 'HARDMUTE') "
                "LIMIT 1",
                (guild_id, user_id),
            )
        )

    def get_active_mute_case(self, guild_id: int, user_id: int) -> Optional[Dict]:
        """Find the current mute independently of later moderation history."""
        return self._fetchone(
            "SELECT * FROM mod_actions WHERE guild_id = ? AND target_id = ? "
            "AND UPPER(COALESCE(status, 'active')) = 'ACTIVE' "
            "AND UPPER(action_type) IN ('MUTE', 'AUTO_MUTE', 'HARDMUTE') "
            "ORDER BY id DESC LIMIT 1", (guild_id, user_id),
        )

    def activate_prepared_mute_case(
        self,
        guild_id: int,
        user_id: int,
        case_id: int,
    ) -> bool:
        """Activa un mute preparado y reemplaza su tracking en una transacción."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            if self.db_type == "postgresql":
                from psycopg2.extras import RealDictCursor

                cur = conn.cursor(cursor_factory=RealDictCursor)
            else:
                cur = conn.cursor()
            cur.execute(
                self._adapt(
                    "SELECT action_type, extra_data, duration_seconds, expires_at, created_at "
                    "FROM mod_actions WHERE id = ? AND guild_id = ? AND target_id = ? "
                    "AND UPPER(COALESCE(status, 'active')) = 'APPLYING' LIMIT 1"
                ),
                (case_id, guild_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                return False
            if hasattr(row, "keys"):
                action_type = str(row["action_type"] or "").upper()
                raw_extra = row["extra_data"]
                duration_raw = row["duration_seconds"]
                expires_at = row["expires_at"]
                mute_start = row["created_at"]
            else:
                action_type = str(row[0] or "").upper()
                raw_extra = row[1]
                duration_raw = row[2]
                expires_at = row[3]
                mute_start = row[4]
            if action_type not in {"MUTE", "AUTO_MUTE", "HARDMUTE"}:
                return False
            try:
                extra = (
                    json.loads(raw_extra)
                    if isinstance(raw_extra, str)
                    else dict(raw_extra or {})
                )
                role_id = int(extra["role_id"])
                effect_token = str(extra["mute_effect_token"]).strip()
                mute_start = str(extra["mute_start"])
                duration_seconds = (
                    int(duration_raw) if duration_raw is not None else None
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                return False
            if role_id <= 0 or not effect_token:
                return False
            expires_at = str(expires_at) if expires_at is not None else None
            job_action = str(
                extra.get("mute_job_action")
                or ("HARDMUTE_EXPIRE" if action_type == "HARDMUTE" else "MUTE_EXPIRE")
            ).upper()
            if job_action not in {"MUTE_EXPIRE", "HARDMUTE_EXPIRE"}:
                return False
            if duration_seconds is not None and not expires_at:
                return False

            cur.execute(
                self._adapt(
                    "SELECT id FROM mod_actions WHERE guild_id = ? AND target_id = ? "
                    "AND id <> ? AND UPPER(COALESCE(status, 'active')) = 'ACTIVE' "
                    "AND UPPER(action_type) IN ('MUTE', 'AUTO_MUTE', 'HARDMUTE') "
                    "LIMIT 1"
                ),
                (guild_id, user_id, case_id),
            )
            if cur.fetchone():
                return False

            cur.execute(
                self._adapt(
                    "UPDATE pending_moderation_actions SET status = 'cancelled', "
                    "last_error = ?, updated_at = ? WHERE guild_id = ? AND user_id = ? "
                    "AND action_type IN ('MUTE_EXPIRE', 'HARDMUTE_EXPIRE', 'UNMUTE') "
                    "AND status IN ('pending', 'processing')"
                ),
                ("mute_replaced", now, guild_id, user_id),
            )
            if self.db_type == "sqlite":
                insert_user_sql = (
                    "INSERT OR IGNORE INTO user_records (user_id, guild_id) VALUES (?, ?)"
                )
            elif self.db_type == "postgresql":
                insert_user_sql = (
                    "INSERT INTO user_records (user_id, guild_id) VALUES (?, ?) "
                    "ON CONFLICT (user_id, guild_id) DO NOTHING"
                )
            else:
                insert_user_sql = (
                    "INSERT IGNORE INTO user_records (user_id, guild_id) VALUES (?, ?)"
                )
            cur.execute(self._adapt(insert_user_sql), (user_id, guild_id))
            cur.execute(
                self._adapt(
                    "UPDATE user_records SET mute_start = ?, mute_duration = ? "
                    "WHERE user_id = ? AND guild_id = ?"
                ),
                (mute_start, duration_seconds, user_id, guild_id),
            )
            cur.execute(
                self._adapt(
                    "UPDATE mod_actions SET status = 'active', updated_at = ? "
                    "WHERE id = ? AND guild_id = ? AND target_id = ? "
                    "AND UPPER(status) = 'APPLYING'"
                ),
                (now, case_id, guild_id, user_id),
            )
            if int(cur.rowcount or 0) != 1:
                raise RuntimeError("prepared_mute_case_changed")
            if duration_seconds is not None:
                cur.execute(
                    self._adapt(
                        "INSERT INTO pending_moderation_actions "
                        "(guild_id, user_id, action_type, case_id, execute_at, status, "
                        "attempts, extra_data, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)"
                    ),
                    (
                        guild_id,
                        user_id,
                        job_action,
                        case_id,
                        expires_at,
                        json.dumps(
                            {"role_id": role_id, "expires_at": expires_at},
                            ensure_ascii=False,
                        ),
                        now,
                        now,
                    ),
                )
            return True

    def abandon_prepared_mute_case(
        self, guild_id: int, user_id: int, case_id: int
    ) -> None:
        """Marca fallida una intención de mute sin tocar la sanción anterior."""
        self._execute(
            "UPDATE mod_actions SET status = 'failed', updated_at = ? "
            "WHERE id = ? AND guild_id = ? AND target_id = ? "
            "AND UPPER(status) = 'APPLYING'",
            (
                datetime.now(timezone.utc).isoformat(),
                case_id,
                guild_id,
                user_id,
            ),
        )

    def activate_prepared_ban_case(
        self,
        guild_id: int,
        user_id: int,
        case_id: int,
        moderator_id: int,
        reason: str,
        duration_seconds: Optional[int],
        expires_at: Optional[str],
    ) -> bool:
        """Activa un ban preparado y reemplaza su familia en una transacción."""
        now = datetime.now(timezone.utc).isoformat()
        has_tempban_case = self._has_column("tempbans", "case_id")
        with self._conn() as conn:
            if self.db_type == "postgresql":
                from psycopg2.extras import RealDictCursor

                cur = conn.cursor(cursor_factory=RealDictCursor)
            else:
                cur = conn.cursor()
            cur.execute(
                self._adapt(
                    "SELECT id, action_type FROM mod_actions "
                    "WHERE id = ? AND guild_id = ? AND target_id = ? "
                    "AND UPPER(COALESCE(status, 'active')) = 'APPLYING' LIMIT 1"
                ),
                (case_id, guild_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                return False
            action_type = str(
                row["action_type"] if hasattr(row, "keys") else row[1]
            ).upper()
            if action_type not in {"BAN", "TEMPBAN", "FORCEBAN", "AUTO_BAN"}:
                return False

            cur.execute(
                self._adapt(
                    "UPDATE pending_moderation_actions SET status = 'cancelled', "
                    "last_error = ?, updated_at = ? "
                    "WHERE guild_id = ? AND user_id = ? "
                    "AND action_type IN ('TEMPBAN_EXPIRE', 'UNBAN') "
                    "AND status IN ('pending', 'processing')"
                ),
                ("ban_replaced", now, guild_id, user_id),
            )
            cur.execute(
                self._adapt(
                    "UPDATE mod_actions SET status = 'revoked', updated_at = ? "
                    "WHERE guild_id = ? AND target_id = ? AND id <> ? "
                    "AND UPPER(COALESCE(status, 'active')) = 'ACTIVE' "
                    "AND UPPER(action_type) IN ('BAN', 'TEMPBAN', 'FORCEBAN', 'AUTO_BAN')"
                ),
                (now, guild_id, user_id, case_id),
            )
            cur.execute(
                self._adapt(
                    "DELETE FROM tempbans WHERE guild_id = ? AND user_id = ?"
                ),
                (guild_id, user_id),
            )
            cur.execute(
                self._adapt(
                    "UPDATE mod_actions SET status = 'active', duration_seconds = ?, "
                    "expires_at = ?, updated_at = ? "
                    "WHERE id = ? AND guild_id = ? AND target_id = ? "
                    "AND UPPER(status) = 'APPLYING'"
                ),
                (duration_seconds, expires_at, now, case_id, guild_id, user_id),
            )
            if int(cur.rowcount or 0) != 1:
                raise RuntimeError("prepared_ban_case_changed")
            if duration_seconds is not None:
                if not expires_at or not has_tempban_case:
                    raise RuntimeError("tempban_identity_schema_unavailable")
                cur.execute(
                    self._adapt(
                        "INSERT INTO tempbans "
                        "(guild_id, user_id, moderator_id, reason, ban_start, "
                        "ban_duration, created_at, case_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                    ),
                    (
                        guild_id,
                        user_id,
                        moderator_id,
                        reason,
                        now,
                        int(duration_seconds),
                        now,
                        case_id,
                    ),
                )
                cur.execute(
                    self._adapt(
                        "INSERT INTO pending_moderation_actions "
                        "(guild_id, user_id, action_type, case_id, execute_at, status, "
                        "attempts, extra_data, created_at, updated_at) "
                        "VALUES (?, ?, 'TEMPBAN_EXPIRE', ?, ?, 'pending', 0, ?, ?, ?)"
                    ),
                    (
                        guild_id,
                        user_id,
                        case_id,
                        expires_at,
                        json.dumps({"expires_at": expires_at}, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            return True

    def complete_prepared_softban_case(
        self, guild_id: int, user_id: int, case_id: int
    ) -> bool:
        """Finaliza el softban sólo después de confirmar que su efecto fue retirado."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            if self.db_type == "postgresql":
                from psycopg2.extras import RealDictCursor

                cur = conn.cursor(cursor_factory=RealDictCursor)
            else:
                cur = conn.cursor()
            cur.execute(
                self._adapt(
                    "SELECT id FROM mod_actions WHERE id = ? AND guild_id = ? "
                    "AND target_id = ? AND UPPER(action_type) = 'SOFTBAN' "
                    "AND UPPER(status) = 'EFFECT_CONFIRMED' LIMIT 1"
                ),
                (case_id, guild_id, user_id),
            )
            if not cur.fetchone():
                return False
            cur.execute(
                self._adapt(
                    "UPDATE pending_moderation_actions SET status = 'cancelled', "
                    "last_error = ?, updated_at = ? WHERE guild_id = ? AND user_id = ? "
                    "AND action_type IN ('TEMPBAN_EXPIRE', 'UNBAN') "
                    "AND status IN ('pending', 'processing')"
                ),
                ("softban_completed", now, guild_id, user_id),
            )
            cur.execute(
                self._adapt(
                    "UPDATE mod_actions SET status = 'revoked', updated_at = ? "
                    "WHERE guild_id = ? AND target_id = ? AND id <> ? "
                    "AND UPPER(COALESCE(status, 'active')) = 'ACTIVE' "
                    "AND UPPER(action_type) IN ('BAN', 'TEMPBAN', 'FORCEBAN', 'AUTO_BAN')"
                ),
                (now, guild_id, user_id, case_id),
            )
            cur.execute(
                self._adapt("DELETE FROM tempbans WHERE guild_id = ? AND user_id = ?"),
                (guild_id, user_id),
            )
            cur.execute(
                self._adapt(
                    "UPDATE mod_actions SET status = 'expired', updated_at = ? "
                    "WHERE id = ? AND guild_id = ? AND target_id = ? "
                    "AND UPPER(status) = 'EFFECT_CONFIRMED'"
                ),
                (now, case_id, guild_id, user_id),
            )
            if int(cur.rowcount or 0) != 1:
                raise RuntimeError("prepared_softban_case_changed")
            return True

    def abandon_prepared_ban_case(
        self,
        guild_id: int,
        user_id: int,
        case_id: int,
        reason: str,
        *,
        retire_previous: bool = False,
    ) -> None:
        """Falla una intención; opcionalmente invalida identidades de ban antiguas."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            if self.db_type == "postgresql":
                from psycopg2.extras import RealDictCursor

                cur = conn.cursor(cursor_factory=RealDictCursor)
            else:
                cur = conn.cursor()
            cur.execute(
                self._adapt(
                    "UPDATE mod_actions SET status = 'failed', updated_at = ?, "
                    "extra_data = COALESCE(extra_data, '{}') "
                    "WHERE id = ? AND guild_id = ? AND target_id = ? "
                    "AND (UPPER(status) = 'APPLYING' OR "
                    "(UPPER(action_type) = 'SOFTBAN' AND UPPER(status) = 'EFFECT_CONFIRMED'))"
                ),
                (now, case_id, guild_id, user_id),
            )
            if int(cur.rowcount or 0) != 1:
                return
            if retire_previous:
                cur.execute(
                    self._adapt(
                        "UPDATE pending_moderation_actions SET status = 'cancelled', "
                        "last_error = ?, updated_at = ? "
                        "WHERE guild_id = ? AND user_id = ? "
                        "AND action_type IN ('TEMPBAN_EXPIRE', 'UNBAN') "
                        "AND status IN ('pending', 'processing')"
                    ),
                    (reason, now, guild_id, user_id),
                )
                cur.execute(
                    self._adapt(
                        "UPDATE mod_actions SET status = 'revoked', updated_at = ? "
                        "WHERE guild_id = ? AND target_id = ? "
                        "AND UPPER(COALESCE(status, 'active')) = 'ACTIVE' "
                        "AND UPPER(action_type) IN ('BAN', 'TEMPBAN', 'FORCEBAN', 'AUTO_BAN')"
                    ),
                    (now, guild_id, user_id),
                )
                cur.execute(
                    self._adapt(
                        "DELETE FROM tempbans WHERE guild_id = ? AND user_id = ?"
                    ),
                    (guild_id, user_id),
                )

    def get_case(self, guild_id: int, case_ref: int) -> Optional[Dict]:
        """Obtiene un caso público por case_number, con fallback a id interno."""
        row = self._fetchone(
            "SELECT * FROM mod_actions WHERE guild_id = ? AND case_number = ? LIMIT 1",
            (guild_id, case_ref),
        )
        if row:
            return row
        return self._fetchone(
            "SELECT * FROM mod_actions WHERE guild_id = ? AND id = ? LIMIT 1",
            (guild_id, case_ref),
        )

    def get_case_by_id(self, guild_id: int, case_id: int) -> Optional[Dict]:
        """Obtiene un caso por ID interno exacto para jobs/schedulers."""
        return self._fetchone(
            "SELECT * FROM mod_actions WHERE guild_id = ? AND id = ? LIMIT 1",
            (guild_id, case_id),
        )

    def update_case(self, guild_id: int, case_ref: int, **kwargs) -> Optional[Dict]:
        """Actualiza campos editables de un caso manteniendo auditoría básica."""
        allowed = {"reason", "evidence_url", "status", "extra_data", "parent_case_id"}
        payload = {k: v for k, v in kwargs.items() if k in allowed}
        if not payload:
            return self.get_case(guild_id, case_ref)
        case = self.get_case(guild_id, case_ref)
        if not case:
            return None
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        assignments = ", ".join(f"{k} = ?" for k in payload)
        self._execute(
            f"UPDATE mod_actions SET {assignments} WHERE id = ? AND guild_id = ?",
            tuple(payload.values()) + (case["id"], guild_id),
        )
        return self.get_case_by_id(guild_id, int(case["id"]))

    def update_case_by_id(self, guild_id: int, case_id: int, **kwargs) -> Optional[Dict]:
        """Actualiza un caso usando ID interno exacto, evitando colisiones con case_number."""
        allowed = {"reason", "evidence_url", "status", "extra_data", "parent_case_id"}
        payload = {k: v for k, v in kwargs.items() if k in allowed}
        case = self.get_case_by_id(guild_id, case_id)
        if not case:
            return None
        if not payload:
            return case
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        assignments = ", ".join(f"{k} = ?" for k in payload)
        self._execute(
            f"UPDATE mod_actions SET {assignments} WHERE id = ? AND guild_id = ?",
            tuple(payload.values()) + (case_id, guild_id),
        )
        return self.get_case_by_id(guild_id, case_id)

    def get_active_moderations(self, guild_id: int, limit: int = 100) -> List[Dict]:
        return self._fetchall(
            "SELECT * FROM mod_actions WHERE guild_id = ? AND status = 'active' "
            "AND action_type IN ('TEMPBAN', 'MUTE', 'AUTO_MUTE', 'HARDMUTE', 'TIMEOUT') "
            "ORDER BY expires_at ASC, created_at DESC LIMIT ?",
            (guild_id, limit),
        )

    def list_cases(
        self,
        guild_id: int,
        user_id: Optional[int] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict]:
        if user_id is not None:
            return self._fetchall(
                "SELECT * FROM mod_actions WHERE guild_id = ? AND target_id = ? "
                "ORDER BY case_number DESC, created_at DESC LIMIT ? OFFSET ?",
                (guild_id, user_id, limit, offset),
            )
        return self._fetchall(
            "SELECT * FROM mod_actions WHERE guild_id = ? "
            "ORDER BY case_number DESC, created_at DESC LIMIT ? OFFSET ?",
            (guild_id, limit, offset),
        )

    def add_pending_moderation_action(
        self,
        guild_id: int,
        user_id: int,
        action_type: str,
        execute_at: str,
        case_id: Optional[int] = None,
        extra: Optional[Dict] = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            "INSERT INTO pending_moderation_actions "
            "(guild_id, user_id, action_type, case_id, execute_at, status, attempts, extra_data, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)",
            (
                guild_id,
                user_id,
                action_type,
                case_id,
                execute_at,
                json.dumps(extra, ensure_ascii=False) if extra else None,
                now,
                now,
            ),
        )
        row = self._fetchone(
            "SELECT id FROM pending_moderation_actions WHERE guild_id = ? AND user_id = ? "
            "AND action_type = ? AND execute_at = ? ORDER BY id DESC LIMIT 1",
            (guild_id, user_id, action_type, execute_at),
        )
        return int(row["id"]) if row else 0

    def get_due_pending_moderation_actions(self, now_iso: str, limit: int = 100) -> List[Dict]:
        return self._fetchall(
            "SELECT * FROM pending_moderation_actions WHERE status = 'pending' AND execute_at <= ? "
            "ORDER BY execute_at ASC LIMIT ?",
            (now_iso, limit),
        )

    def recover_processing_moderation_actions(self) -> int:
        return self._execute_rowcount(
            "UPDATE pending_moderation_actions SET status = 'pending', updated_at = ? "
            "WHERE status = 'processing'",
            (datetime.now(timezone.utc).isoformat(),),
        )

    def claim_pending_moderation_action(self, action_id: int) -> bool:
        return self._execute_rowcount(
            "UPDATE pending_moderation_actions SET status = 'processing', updated_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (datetime.now(timezone.utc).isoformat(), action_id),
        ) == 1

    def get_pending_moderation_actions(self, guild_id: int, limit: int = 100) -> List[Dict]:
        return self._fetchall(
            "SELECT * FROM pending_moderation_actions WHERE guild_id = ? AND status = 'pending' "
            "ORDER BY execute_at ASC LIMIT ?",
            (guild_id, limit),
        )

    def update_pending_moderation_action(self, action_id: int, status: str, last_error: Optional[str] = None) -> None:
        self._execute(
            "UPDATE pending_moderation_actions SET status = ?, last_error = ?, attempts = attempts + 1, "
            "updated_at = ? WHERE id = ?",
            (status, last_error, datetime.now(timezone.utc).isoformat(), action_id),
        )

    def has_pending_moderation_action(
        self,
        guild_id: int,
        user_id: int,
        action_types: Optional[List[str]] = None,
    ) -> bool:
        if action_types:
            placeholders = ", ".join("?" for _ in action_types)
            row = self._fetchone(
                "SELECT id FROM pending_moderation_actions "
                "WHERE guild_id = ? AND user_id = ? AND status = 'pending' "
                f"AND action_type IN ({placeholders}) LIMIT 1",
                (guild_id, user_id, *action_types),
            )
        else:
            row = self._fetchone(
                "SELECT id FROM pending_moderation_actions "
                "WHERE guild_id = ? AND user_id = ? AND status = 'pending' LIMIT 1",
                (guild_id, user_id),
            )
        return bool(row)

    def cancel_pending_moderation_actions(
        self,
        guild_id: int,
        user_id: int,
        action_types: Optional[List[str]] = None,
        case_id: Optional[int] = None,
        reason: str = "cancelled",
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        where_params: List[Any] = [guild_id, user_id]
        where = "guild_id = ? AND user_id = ? AND status = 'pending'"
        if action_types:
            placeholders = ", ".join("?" for _ in action_types)
            where += f" AND action_type IN ({placeholders})"
            where_params.extend(action_types)
        if case_id is not None:
            where += " AND case_id = ?"
            where_params.append(case_id)

        rows = self._fetchall(
            f"SELECT id FROM pending_moderation_actions WHERE {where}",
            tuple(where_params),
        )
        if not rows:
            return 0

        self._execute(
            "UPDATE pending_moderation_actions SET status = 'cancelled', last_error = ?, updated_at = ? "
            f"WHERE {where}",
            (reason, now, *where_params),
        )
        return len(rows)

    def revoke_active_moderations(
        self,
        guild_id: int,
        user_id: int,
        action_types: List[str],
        status: str = "revoked",
    ) -> List[Dict]:
        if not action_types:
            return []
        placeholders = ", ".join("?" for _ in action_types)
        rows = self._fetchall(
            "SELECT * FROM mod_actions WHERE guild_id = ? AND target_id = ? AND status = 'active' "
            f"AND action_type IN ({placeholders})",
            (guild_id, user_id, *action_types),
        )
        now = datetime.now(timezone.utc).isoformat()
        for row in rows:
            self._execute(
                "UPDATE mod_actions SET status = ?, updated_at = ? WHERE id = ? AND guild_id = ?",
                (status, now, row["id"], guild_id),
            )
        return rows

    def enqueue_log_outbox(
        self,
        guild_id: int,
        log_type: str,
        payload: Dict,
        channel_id: Optional[int] = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            "INSERT INTO log_outbox (guild_id, log_type, channel_id, payload, status, attempts, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)",
            (guild_id, log_type, channel_id, json.dumps(payload, ensure_ascii=False), now, now),
        )
        row = self._fetchone(
            "SELECT id FROM log_outbox WHERE guild_id = ? AND log_type = ? AND created_at = ? "
            "ORDER BY id DESC LIMIT 1",
            (guild_id, log_type, now),
        )
        return int(row["id"]) if row else 0

    def get_pending_log_outbox(self, limit: int = 50) -> List[Dict]:
        return self._fetchall(
            "SELECT * FROM log_outbox WHERE status = 'pending' ORDER BY created_at ASC LIMIT ?",
            (limit,),
        )

    def recover_processing_log_outbox(self) -> int:
        return self._execute_rowcount(
            "UPDATE log_outbox SET status = 'pending', updated_at = ? WHERE status = 'processing'",
            (datetime.now(timezone.utc).isoformat(),),
        )

    def claim_log_outbox(self, log_id: int) -> bool:
        return self._execute_rowcount(
            "UPDATE log_outbox SET status = 'processing', updated_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (datetime.now(timezone.utc).isoformat(), log_id),
        ) == 1

    def mark_log_outbox(self, log_id: int, status: str, last_error: Optional[str] = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        sent_at = now if status == "sent" else None
        self._execute(
            "UPDATE log_outbox SET status = ?, last_error = ?, attempts = attempts + 1, "
            "updated_at = ?, sent_at = COALESCE(?, sent_at) WHERE id = ?",
            (status, last_error, now, sent_at, log_id),
        )

    def get_user_history(
        self, user_id: int, guild_id: int, limit: int = 10
    ) -> List[Dict]:
        return self._fetchall(
            "SELECT * FROM mod_actions "
            "WHERE target_id = ? AND guild_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, guild_id, limit),
        )

    def get_user_action_summary(self, user_id: int, guild_id: int) -> Dict[str, int]:
        """Cuenta warns, kicks, bans, mutes de un usuario para /userinfo."""
        rows = self._fetchall(
            "SELECT action_type, COUNT(*) as cnt FROM mod_actions "
            "WHERE target_id = ? AND guild_id = ? "
            "GROUP BY action_type",
            (user_id, guild_id),
        )
        summary = {"WARN": 0, "KICK": 0, "BAN": 0, "MUTE": 0, "UNMUTE": 0}
        for r in rows:
            if r["action_type"] in summary:
                summary[r["action_type"]] = r["cnt"]
        return summary

    # ── Channel Config ────────────────────────────────────────────────────────

    def get_channel_config(self, channel_id: int) -> Dict:
        row = self._fetchone(
            "SELECT * FROM channel_config WHERE channel_id = ?", (channel_id,)
        )
        result = dict(self.DEFAULT_CHANNEL_CONFIG)
        result["channel_id"] = channel_id
        if row:
            result.update(row)
        return result

    def set_channel_config(self, channel_id: int, guild_id: int, **kwargs) -> None:
        invalid = set(kwargs) - VALID_CHANNEL_CONFIG_COLUMNS
        if invalid:
            raise ValueError(f"Columnas inválidas: {invalid}")

        ops = []
        if self.db_type == "sqlite":
            ops.append(
                (
                    "INSERT OR IGNORE INTO channel_config (channel_id, guild_id) VALUES (?, ?)",
                    (channel_id, guild_id),
                )
            )
        elif self.db_type == "postgresql":
            ops.append(
                (
                    "INSERT INTO channel_config (channel_id, guild_id) VALUES (?, ?) "
                    "ON CONFLICT (channel_id) DO NOTHING",
                    (channel_id, guild_id),
                )
            )
        else:
            ops.append(
                (
                    "INSERT IGNORE INTO channel_config (channel_id, guild_id) VALUES (?, ?)",
                    (channel_id, guild_id),
                )
            )

        for col, val in kwargs.items():
            ops.append(
                (
                    f"UPDATE channel_config SET {col} = ? WHERE channel_id = ? AND guild_id = ?",
                    (val, channel_id, guild_id),
                )
            )

        self._executemany(ops)

    def delete_channel_config(self, channel_id: int) -> None:
        self._execute("DELETE FROM channel_config WHERE channel_id = ?", (channel_id,))

    def get_all_channel_configs(self, guild_id: int) -> List[Dict]:
        return self._fetchall(
            "SELECT * FROM channel_config WHERE guild_id = ?", (guild_id,)
        )

    # ── Server Config ─────────────────────────────────────────────────────────

    def get_server_config(self, guild_id: int) -> Dict:
        row = self._fetchone(
            "SELECT * FROM server_config WHERE guild_id = ?", (guild_id,)
        )
        result = dict(self.DEFAULT_SERVER_CONFIG)
        result["guild_id"] = guild_id
        if row:
            result.update(row)
        return result

    def set_server_config(self, guild_id: int, **kwargs) -> None:
        invalid = set(kwargs) - VALID_SERVER_CONFIG_COLUMNS
        if invalid:
            raise ValueError(f"Columnas inválidas: {invalid}")

        ops = []
        if self.db_type == "sqlite":
            ops.append(
                (
                    "INSERT OR IGNORE INTO server_config (guild_id) VALUES (?)",
                    (guild_id,),
                )
            )
        elif self.db_type == "postgresql":
            ops.append(
                (
                    "INSERT INTO server_config (guild_id) VALUES (?) "
                    "ON CONFLICT (guild_id) DO NOTHING",
                    (guild_id,),
                )
            )
        else:
            ops.append(
                (
                    "INSERT IGNORE INTO server_config (guild_id) VALUES (?)",
                    (guild_id,),
                )
            )

        for col, val in kwargs.items():
            ops.append(
                (
                    f"UPDATE server_config SET {col} = ? WHERE guild_id = ?",
                    (val, guild_id),
                )
            )

        self._executemany(ops)

    # ── Saved Embeds ──────────────────────────────────────────────────────────

    def save_embed(
        self, guild_id: int, creator_id: int, name: str, embed_data: str
    ) -> None:
        self._execute(
            "INSERT INTO saved_embeds (guild_id, creator_id, name, embed_data, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                guild_id,
                creator_id,
                name,
                embed_data,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    def get_saved_embeds(self, guild_id: int) -> List[Dict]:
        return self._fetchall(
            "SELECT * FROM saved_embeds WHERE guild_id = ? ORDER BY created_at DESC",
            (guild_id,),
        )

    def get_saved_embed_by_name(self, guild_id: int, name: str) -> Optional[Dict]:
        return self._fetchone(
            "SELECT * FROM saved_embeds WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )

    def delete_saved_embed(self, embed_id: int, guild_id: int) -> None:
        self._execute(
            "DELETE FROM saved_embeds WHERE id = ? AND guild_id = ?",
            (embed_id, guild_id),
        )

    # ── AI Config ─────────────────────────────────────────────────────────────

    DEFAULT_AI_CONFIG: Dict[str, Any] = {
        "guild_id": None,
        "ai_enabled": 1,
        "ai_provider": "auto",
        "ai_web_enabled": 0,
        "ai_web_max_sources": 3,
        "ai_web_timeout_seconds": DEFAULT_WEB_TIMEOUT_SECONDS,
        "ai_deep_think": 0,
        "ai_deep_think_daily_limit": 3,
        "ai_memory_messages": 40,
        "ai_response_mode": "mentions",
        "ai_channel_id": None,
        "ai_role_id": None,
        "ai_restricted_role_ids": [],
        "ai_model": "gemma-4-26b-a4b-it",
        "ai_system_prompt": None,
        "ai_limit_requests": 50,
        "ai_limit_hours": 12,
        "ai_imagine_enabled": 1,
        "ai_webhook_name": None,
        "ai_webhook_icon": None,
    }

    def get_ai_config(self, guild_id: int) -> Dict:
        row = self._fetchone("SELECT * FROM ai_config WHERE guild_id = ?", (guild_id,))
        result = dict(self.DEFAULT_AI_CONFIG)
        result["guild_id"] = guild_id
        if row:
            result.update(row)
        if result["ai_web_timeout_seconds"] is None:
            result["ai_web_timeout_seconds"] = DEFAULT_WEB_TIMEOUT_SECONDS
        restricted = result["ai_restricted_role_ids"]
        result["ai_restricted_role_ids"] = json.loads(restricted) if isinstance(restricted, str) else list(restricted or [])
        return result

    def set_ai_config(self, guild_id: int, **kwargs) -> None:
        invalid = set(kwargs) - VALID_AI_CONFIG_COLUMNS
        if invalid:
            raise ValueError(f"Columnas inválidas en ai_config: {invalid}")

        if "ai_restricted_role_ids" in kwargs:
            kwargs["ai_restricted_role_ids"] = json.dumps(normalize_restricted_roles(kwargs["ai_restricted_role_ids"]))

        if "ai_provider" in kwargs and kwargs["ai_provider"] not in {"auto", "strata", "gemini"}:
            raise ValueError("ai_provider debe ser auto, strata o gemini")

        if "ai_memory_messages" in kwargs and (type(kwargs["ai_memory_messages"]) is not int or not 0 <= kwargs["ai_memory_messages"] <= 5000):
            raise ValueError("ai_memory_messages debe ser un entero entre 0 y 5000")

        if "ai_web_max_sources" in kwargs and (
            type(kwargs["ai_web_max_sources"]) is not int or not 1 <= kwargs["ai_web_max_sources"] <= 20
        ):
            raise ValueError("ai_web_max_sources debe ser un entero entre 1 y 20")

        if "ai_web_timeout_seconds" in kwargs and (
            type(kwargs["ai_web_timeout_seconds"]) is not int or not 30 <= kwargs["ai_web_timeout_seconds"] <= 600
        ):
            raise ValueError("ai_web_timeout_seconds debe ser un entero entre 30 y 600")

        if "ai_deep_think_daily_limit" in kwargs and (
            type(kwargs["ai_deep_think_daily_limit"]) is not int
            or not 0 <= kwargs["ai_deep_think_daily_limit"] <= 1000
        ):
            raise ValueError("ai_deep_think_daily_limit debe ser un entero entre 0 y 1000")

        for field in ("ai_web_enabled", "ai_deep_think"):
            if field not in kwargs:
                continue
            enabled = kwargs[field]
            if isinstance(enabled, bool):
                enabled = int(enabled)
            elif isinstance(enabled, str) and enabled.strip() in {"0", "1"}:
                enabled = int(enabled.strip())
            elif not isinstance(enabled, int) or enabled not in {0, 1}:
                raise ValueError(f"{field} debe ser 0 o 1")
            kwargs[field] = enabled

        if "ai_enabled" in kwargs:
            enabled = kwargs["ai_enabled"]
            if isinstance(enabled, bool):
                enabled = int(enabled)
            elif isinstance(enabled, str) and enabled.strip() in {"0", "1"}:
                enabled = int(enabled.strip())
            elif not isinstance(enabled, int) or enabled not in {0, 1}:
                raise ValueError("ai_enabled debe ser 0 o 1")
            kwargs["ai_enabled"] = enabled

        if "ai_response_mode" in kwargs:
            response_mode = kwargs["ai_response_mode"]
            if not isinstance(response_mode, str):
                raise ValueError("ai_response_mode debe ser 'all' o 'mentions'")
            response_mode = response_mode.strip().lower()
            if response_mode not in {"all", "mentions"}:
                raise ValueError("ai_response_mode debe ser 'all' o 'mentions'")
            kwargs["ai_response_mode"] = response_mode

        ops = []
        if self.db_type == "sqlite":
            ops.append(
                (
                    "INSERT OR IGNORE INTO ai_config (guild_id) VALUES (?)",
                    (guild_id,),
                )
            )
        elif self.db_type == "postgresql":
            ops.append(
                (
                    "INSERT INTO ai_config (guild_id) VALUES (?) ON CONFLICT (guild_id) DO NOTHING",
                    (guild_id,),
                )
            )
        else:
            ops.append(
                (
                    "INSERT IGNORE INTO ai_config (guild_id) VALUES (?)",
                    (guild_id,),
                )
            )

        for col, val in kwargs.items():
            ops.append(
                (
                    f"UPDATE ai_config SET {col} = ? WHERE guild_id = ?",
                    (val, guild_id),
                )
            )

        self._executemany(ops)

    # ── AI API Keys (multi-tenant pool) ───────────────────────────────────────
    #
    # Reglas de negocio:
    #   • Cada guild tiene asignada como máximo una key (PK en ai_guild_keys).
    #   • Cada key cubre como máximo 2 guilds (validado al asignar).
    #   • Soft-disable vía `active` en lugar de borrar (preserva auditoría).

    AI_KEY_MAX_GUILDS_PER_KEY = 2

    def list_ai_keys(self) -> List[Dict]:
        """
        Lista todas las API keys del pool con su uso (cuántos guilds tienen
        cada key asignada). No expone la api_key completa al caller — eso queda
        a cargo de la capa API; aquí devolvemos el campo crudo.
        """
        rows = self._fetchall(
            """
            SELECT k.id, k.label, k.api_key, k.active, k.notes, k.created_at,
                   COUNT(g.guild_id) AS guilds_assigned
              FROM ai_api_keys k
              LEFT JOIN ai_guild_keys g ON g.key_id = k.id
             GROUP BY k.id
             ORDER BY k.created_at DESC
            """,
            (),
        )
        return [dict(r) for r in rows]

    def get_ai_key(self, key_id: int) -> Optional[Dict]:
        row = self._fetchone(
            "SELECT * FROM ai_api_keys WHERE id = ?", (key_id,)
        )
        return dict(row) if row else None

    def add_ai_key(self, label: str, api_key: str, notes: Optional[str] = None) -> int:
        """Inserta una nueva key. Devuelve el id insertado."""
        now = datetime.now(timezone.utc).isoformat()
        if self.db_type == "postgresql":
            row = self._fetchone(
                "INSERT INTO ai_api_keys (label, api_key, notes, created_at) "
                "VALUES (?, ?, ?, ?) RETURNING id",
                (label, api_key, notes, now),
            )
            return int(row["id"]) if row else 0
        # sqlite + mariadb: insertar y leer last_insert_rowid / LAST_INSERT_ID()
        self._execute(
            "INSERT INTO ai_api_keys (label, api_key, notes, created_at) VALUES (?, ?, ?, ?)",
            (label, api_key, notes, now),
        )
        if self.db_type == "sqlite":
            row = self._fetchone("SELECT last_insert_rowid() AS id", ())
        else:  # mariadb
            row = self._fetchone("SELECT LAST_INSERT_ID() AS id", ())
        return int(row["id"]) if row else 0

    def update_ai_key(self, key_id: int, **kwargs) -> None:
        """Actualiza label, active, notes. api_key se actualiza por separado por seguridad."""
        allowed = {"label", "active", "notes", "api_key"}
        invalid = set(kwargs) - allowed
        if invalid:
            raise ValueError(f"Campos inválidos en update_ai_key: {invalid}")
        if not kwargs:
            return
        ops = [
            (f"UPDATE ai_api_keys SET {col} = ? WHERE id = ?", (val, key_id))
            for col, val in kwargs.items()
        ]
        self._executemany(ops)

    def delete_ai_key(self, key_id: int) -> None:
        """Borra una key. ON DELETE CASCADE limpia ai_guild_keys."""
        self._execute("DELETE FROM ai_api_keys WHERE id = ?", (key_id,))

    def get_ai_key_for_guild(self, guild_id: int) -> Optional[Dict]:
        """Devuelve la key asignada al guild (o None)."""
        row = self._fetchone(
            """
            SELECT k.id, k.label, k.api_key, k.active, g.assigned_at
              FROM ai_guild_keys g
              JOIN ai_api_keys k ON k.id = g.key_id
             WHERE g.guild_id = ?
            """,
            (guild_id,),
        )
        return dict(row) if row else None

    def count_guilds_for_key(self, key_id: int) -> int:
        row = self._fetchone(
            "SELECT COUNT(*) AS n FROM ai_guild_keys WHERE key_id = ?", (key_id,)
        )
        return int(row["n"]) if row else 0

    def assign_ai_key_to_guild(self, guild_id: int, key_id: int) -> None:
        """
        Asigna una key a un guild. Aplica las reglas:
          • Si el guild ya tiene una key, se reemplaza (PK garantiza unicidad).
          • Antes de asignar, valida que la key tenga capacidad (< 2 guilds).
        """
        key = self.get_ai_key(key_id)
        if not key:
            raise ValueError(f"Key {key_id} no existe")
        if not int(key.get("active", 0)):
            raise ValueError(f"Key {key_id} está inactiva")

        # Si el guild ya tenía la misma key, no contamos doble.
        existing = self.get_ai_key_for_guild(guild_id)
        if existing and int(existing["id"]) == int(key_id):
            return  # idempotente

        # Cuenta cuántos guilds usan esta key actualmente.
        current = self.count_guilds_for_key(key_id)
        if current >= self.AI_KEY_MAX_GUILDS_PER_KEY:
            raise ValueError(
                f"La key '{key['label']}' ya cubre el máximo de "
                f"{self.AI_KEY_MAX_GUILDS_PER_KEY} servidores"
            )

        now = datetime.now(timezone.utc).isoformat()
        if self.db_type == "sqlite":
            sql = (
                "INSERT OR REPLACE INTO ai_guild_keys (guild_id, key_id, assigned_at) "
                "VALUES (?, ?, ?)"
            )
        elif self.db_type == "postgresql":
            sql = (
                "INSERT INTO ai_guild_keys (guild_id, key_id, assigned_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT (guild_id) DO UPDATE SET "
                "key_id = EXCLUDED.key_id, assigned_at = EXCLUDED.assigned_at"
            )
        else:  # mariadb
            sql = (
                "INSERT INTO ai_guild_keys (guild_id, key_id, assigned_at) "
                "VALUES (?, ?, ?) "
                "ON DUPLICATE KEY UPDATE key_id = VALUES(key_id), "
                "assigned_at = VALUES(assigned_at)"
            )
        self._execute(sql, (guild_id, key_id, now))

    def unassign_ai_key_from_guild(self, guild_id: int) -> None:
        self._execute("DELETE FROM ai_guild_keys WHERE guild_id = ?", (guild_id,))

    def list_guilds_for_key(self, key_id: int) -> List[int]:
        rows = self._fetchall(
            "SELECT guild_id FROM ai_guild_keys WHERE key_id = ?", (key_id,)
        )
        return [int(r["guild_id"]) for r in rows]

    def ai_key_pool_health(self) -> Dict:
        """
        Reporte rápido del pool. Útil para alertar al admin si:
          • hay más guilds que (keys * 2) → faltan keys.
        """
        keys = self._fetchall("SELECT COUNT(*) AS n FROM ai_api_keys WHERE active = 1", ())
        active_keys = int(keys[0]["n"]) if keys else 0
        guilds = self._fetchall("SELECT COUNT(*) AS n FROM ai_guild_keys", ())
        assigned_guilds = int(guilds[0]["n"]) if guilds else 0
        capacity = active_keys * self.AI_KEY_MAX_GUILDS_PER_KEY
        return {
            "active_keys": active_keys,
            "assigned_guilds": assigned_guilds,
            "capacity": capacity,
            "deficit": max(0, assigned_guilds - capacity),
        }

    # ── Appeals ───────────────────────────────────────────────────────────────

    def create_appeal(
        self,
        guild_id: int,
        user_id: int,
        action_type: str,
        reason: str,
        appeal_text: str,
        case_id: Optional[int] = None,
    ) -> int:
        """Crea una apelación ligada a un caso de moderación exacto."""
        if case_id is None:
            return 0
        params = (
            guild_id,
            user_id,
            int(case_id),
            action_type,
            reason,
            appeal_text,
            datetime.now(timezone.utc).isoformat(),
        )
        try:
            with self._conn() as conn:
                if self.db_type == "postgresql":
                    from psycopg2.extras import RealDictCursor

                    cur = conn.cursor(cursor_factory=RealDictCursor)
                else:
                    cur = conn.cursor()
                cur.execute(
                    self._adapt(
                        "SELECT * FROM mod_actions WHERE guild_id = ? AND target_id = ? "
                        "AND id = ? AND UPPER(action_type) = ? LIMIT 1"
                    ),
                    (guild_id, user_id, int(case_id), str(action_type).upper()),
                )
                case_row = cur.fetchone()
                if not case_row:
                    return 0
                cur.execute(
                    self._adapt(
                        "SELECT id FROM appeals WHERE guild_id = ? AND case_id = ? LIMIT 1"
                    ),
                    (guild_id, int(case_id)),
                )
                if cur.fetchone():
                    return 0
                insert_sql = (
                    "INSERT INTO appeals "
                    "(guild_id, user_id, case_id, action_type, reason, appeal_text, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)"
                )
                if self.db_type == "postgresql":
                    insert_sql += " RETURNING id"
                cur.execute(self._adapt(insert_sql), params)
                if self.db_type == "postgresql":
                    row = cur.fetchone()
                    return int(row["id"]) if row else 0
                return int(cur.lastrowid or 0)
        except Exception:
            # Una apertura simultánea del mismo modal puede perder contra el
            # índice único; en ese caso la apelación ya quedó registrada.
            existing = self._fetchone(
                "SELECT id FROM appeals WHERE guild_id = ? AND case_id = ? LIMIT 1",
                (guild_id, int(case_id)),
            )
            if existing:
                return 0
            raise

    @staticmethod
    def _appeal_case_family(action_type: str) -> tuple[set[str], bool]:
        action = str(action_type or "").upper()
        if action in {"BAN", "TEMPBAN"}:
            return {"BAN", "TEMPBAN", "FORCEBAN", "AUTO_BAN"}, True
        if action == "MUTE":
            return {"MUTE", "AUTO_MUTE", "HARDMUTE"}, True
        if action == "WARN":
            return {"WARN"}, False
        return {action} if action else set(), False

    def get_appealable_active_case(
        self,
        guild_id: int,
        user_id: int,
        case_id: int,
        action_type: str,
    ) -> Optional[Dict]:
        """Devuelve solo el caso exacto si sigue siendo la sanción apelable actual."""
        action = str(action_type or "").upper()
        family, require_latest = self._appeal_case_family(action)
        if not family:
            return None
        case = self._fetchone(
            "SELECT * FROM mod_actions WHERE guild_id = ? AND target_id = ? AND id = ? "
            "AND UPPER(COALESCE(status, 'active')) = 'ACTIVE' LIMIT 1",
            (guild_id, user_id, case_id),
        )
        if not case or str(case.get("action_type") or "").upper() != action:
            return None
        if require_latest:
            placeholders = ", ".join("?" for _ in family)
            latest = self._fetchone(
                "SELECT id FROM mod_actions WHERE guild_id = ? AND target_id = ? "
                "AND UPPER(COALESCE(status, 'active')) = 'ACTIVE' "
                f"AND UPPER(action_type) IN ({placeholders}) ORDER BY id DESC LIMIT 1",
                (guild_id, user_id, *sorted(family)),
            )
            if not latest or int(latest["id"]) != int(case_id):
                return None
        return case

    def resolve_appealed_case(
        self,
        guild_id: int,
        user_id: int,
        case_id: int,
        action_type: str,
        appeal_id: int,
    ) -> bool:
        """Revoca el caso exacto y acepta su apelación en una transacción."""
        action = str(action_type or "").upper()
        family, require_latest = self._appeal_case_family(action)
        if not family:
            return False
        has_tempban_case = self._has_column("tempbans", "case_id")
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            if self.db_type == "postgresql":
                from psycopg2.extras import RealDictCursor

                cur = conn.cursor(cursor_factory=RealDictCursor)
            else:
                cur = conn.cursor()
            cur.execute(
                self._adapt(
                    "SELECT * FROM mod_actions WHERE guild_id = ? AND target_id = ? AND id = ? "
                    "AND UPPER(COALESCE(status, 'active')) = 'ACTIVE' LIMIT 1"
                ),
                (guild_id, user_id, case_id),
            )
            row = cur.fetchone()
            case = dict(row) if row else None
            if not case or str(case.get("action_type") or "").upper() != action:
                return False
            if require_latest:
                placeholders = ", ".join("?" for _ in family)
                cur.execute(
                    self._adapt(
                        "SELECT id FROM mod_actions WHERE guild_id = ? AND target_id = ? "
                        "AND UPPER(COALESCE(status, 'active')) = 'ACTIVE' "
                        f"AND UPPER(action_type) IN ({placeholders}) ORDER BY id DESC LIMIT 1"
                    ),
                    (guild_id, user_id, *sorted(family)),
                )
                latest = cur.fetchone()
                latest_id = (
                    int(latest["id"] if hasattr(latest, "keys") else latest[0])
                    if latest
                    else 0
                )
                if latest_id != int(case_id):
                    return False

            cur.execute(
                self._adapt(
                    "UPDATE mod_actions SET status = 'revoked', updated_at = ? "
                    "WHERE id = ? AND guild_id = ? "
                    "AND UPPER(COALESCE(status, 'active')) = 'ACTIVE'"
                ),
                (now, case_id, guild_id),
            )
            if int(cur.rowcount or 0) != 1:
                return False

            if action == "WARN":
                cur.execute(
                    self._adapt(
                        "UPDATE user_records SET warns = CASE "
                        "WHEN COALESCE(warns, 0) > 0 THEN warns - 1 ELSE 0 END "
                        "WHERE user_id = ? AND guild_id = ?"
                    ),
                    (user_id, guild_id),
                )
            elif action == "MUTE":
                cur.execute(
                    self._adapt(
                        "UPDATE user_records SET mute_start = NULL, mute_duration = NULL "
                        "WHERE user_id = ? AND guild_id = ?"
                    ),
                    (user_id, guild_id),
                )
            elif action in {"BAN", "TEMPBAN"} and has_tempban_case:
                cur.execute(
                    self._adapt(
                        "DELETE FROM tempbans WHERE guild_id = ? AND user_id = ? AND case_id = ?"
                    ),
                    (guild_id, user_id, case_id),
                )

            cur.execute(
                self._adapt(
                    "UPDATE pending_moderation_actions SET status = 'cancelled', "
                    "last_error = ?, updated_at = ? WHERE guild_id = ? AND user_id = ? "
                    "AND case_id = ? AND status = 'pending'"
                ),
                ("appeal_accepted", now, guild_id, user_id, case_id),
            )
            cur.execute(
                self._adapt(
                    "UPDATE appeals SET status = 'ACCEPTED' WHERE id = ? AND guild_id = ? "
                    "AND user_id = ? AND case_id = ? AND status = 'PROCESSING'"
                ),
                (appeal_id, guild_id, user_id, case_id),
            )
            if int(cur.rowcount or 0) != 1:
                raise RuntimeError("appeal_claim_changed_during_case_resolution")
            return True

    def get_appeal(self, appeal_id: int) -> Optional[Dict]:
        return self._fetchone("SELECT * FROM appeals WHERE id = ?", (appeal_id,))

    def update_appeal_status(self, appeal_id: int, status: str) -> None:
        self._execute("UPDATE appeals SET status = ? WHERE id = ?", (status, appeal_id))

    def claim_pending_appeal(self, appeal_id: int, guild_id: int) -> bool:
        """Reserva atómicamente una apelación pendiente para evitar dobles efectos."""
        return self._execute_rowcount(
            "UPDATE appeals SET status = 'PROCESSING' "
            "WHERE id = ? AND guild_id = ? AND UPPER(COALESCE(status, 'PENDING')) = 'PENDING'",
            (appeal_id, guild_id),
        ) == 1

    def finalize_claimed_appeal(self, appeal_id: int, guild_id: int, status: str) -> bool:
        return self._execute_rowcount(
            "UPDATE appeals SET status = ? "
            "WHERE id = ? AND guild_id = ? AND status = 'PROCESSING'",
            (status, appeal_id, guild_id),
        ) == 1

    def release_appeal_claim(self, appeal_id: int, guild_id: int) -> None:
        self._execute(
            "UPDATE appeals SET status = 'PENDING' "
            "WHERE id = ? AND guild_id = ? AND status = 'PROCESSING'",
            (appeal_id, guild_id),
        )

    def recover_processing_appeals(self) -> int:
        """Devuelve a PENDING reservas interrumpidas por un reinicio del proceso."""
        return self._execute_rowcount(
            "UPDATE appeals SET status = 'PENDING' WHERE status = 'PROCESSING'"
        )

    def get_appeals_by_guild(
        self, guild_id: int, status: Optional[str] = None
    ) -> List[Dict]:
        if status:
            return self._fetchall(
                "SELECT * FROM appeals WHERE guild_id = ? AND status = ? ORDER BY created_at DESC",
                (guild_id, status),
            )
        return self._fetchall(
            "SELECT * FROM appeals WHERE guild_id = ? ORDER BY created_at DESC",
            (guild_id,),
        )

    # ── Tempbans ──────────────────────────────────────────────────────────────

    def set_tempban(
        self, guild_id: int, user_id: int, moderator_id: int,
        reason: str, duration_secs: int, case_id: Optional[int] = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        if self._has_column("tempbans", "case_id"):
            self._execute(
                "INSERT INTO tempbans (guild_id, user_id, moderator_id, reason, ban_start, ban_duration, created_at, case_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, user_id, moderator_id, reason, now, duration_secs, now, case_id),
            )
        else:
            self._execute(
                "INSERT INTO tempbans (guild_id, user_id, moderator_id, reason, ban_start, ban_duration, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (guild_id, user_id, moderator_id, reason, now, duration_secs, now),
            )
        row = self._fetchone(
            "SELECT id FROM tempbans WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1",
            (guild_id, user_id),
        )
        return row["id"] if row else 0

    def get_active_tempbans(self) -> List[Dict]:
        return self._fetchall(
            "SELECT * FROM tempbans"
        )

    def clear_tempban(self, tempban_id: int) -> None:
        self._execute("DELETE FROM tempbans WHERE id = ?", (tempban_id,))

    def clear_tempbans_for_user(self, user_id: int, guild_id: int) -> None:
        self._execute(
            "DELETE FROM tempbans WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        )

    def clear_tempban_case(self, guild_id: int, user_id: int, case_id: int) -> None:
        """Elimina únicamente el registro temporal asociado al caso indicado."""
        if not self._has_column("tempbans", "case_id"):
            return
        self._execute(
            "DELETE FROM tempbans WHERE guild_id = ? AND user_id = ? AND case_id = ?",
            (guild_id, user_id, case_id),
        )

    # ── Auto-Responses ────────────────────────────────────────────────────────

    def add_autoresponse(
        self,
        guild_id: int,
        channel_id: Optional[int],
        trigger: str,
        response: str,
        match_type: str = "contains",
        case_sensitive: int = 0,
        response_data: Optional[str] = None,
        enabled: int = 1,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            "INSERT INTO autoresponses (guild_id, channel_id, trigger, response, match_type, "
            "case_sensitive, response_data, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                guild_id, channel_id, trigger, response, match_type,
                int(case_sensitive), response_data, int(enabled), now,
            ),
        )
        row = self._fetchone(
            "SELECT id FROM autoresponses WHERE guild_id = ? ORDER BY id DESC LIMIT 1",
            (guild_id,),
        )
        return row["id"] if row else 0

    def update_autoresponse(self, response_id: int, guild_id: int, **kwargs) -> None:
        allowed = {"channel_id", "trigger", "response", "match_type",
                   "case_sensitive", "response_data", "enabled"}
        invalid = set(kwargs) - allowed
        if invalid:
            raise ValueError(f"Columnas inválidas: {invalid}")
        if not kwargs:
            return
        ops = [
            (
                f"UPDATE autoresponses SET {col} = ? WHERE id = ? AND guild_id = ?",
                (val, response_id, guild_id),
            )
            for col, val in kwargs.items()
        ]
        self._executemany(ops)

    def remove_autoresponse(self, response_id: int, guild_id: int) -> None:
        self._execute(
            "DELETE FROM autoresponses WHERE id = ? AND guild_id = ?",
            (response_id, guild_id),
        )

    def get_autoresponses(self, guild_id: int) -> List[Dict]:
        return self._fetchall(
            "SELECT * FROM autoresponses WHERE guild_id = ? ORDER BY created_at DESC",
            (guild_id,),
        )

    def get_autoresponses_by_channel(self, guild_id: int, channel_id: int) -> List[Dict]:
        return self._fetchall(
            "SELECT * FROM autoresponses WHERE guild_id = ? AND (channel_id IS NULL OR channel_id = ?) ORDER BY created_at DESC",
            (guild_id, channel_id),
        )

    # ── Configuración Genérica ────────────────────────────────────────────────
    def _upsert_config(self, table: str, guild_id: int, **kwargs):
        allowed = VALID_GENERIC_CONFIG_COLUMNS.get(table)
        if allowed is None:
            raise ValueError(f"Invalid config table: {table}")
        invalid = set(kwargs) - allowed
        if invalid:
            raise ValueError(f"Invalid columns in {table}: {invalid}")

        ops = []
        if self.db_type == "sqlite":
            ops.append(
                (f"INSERT OR IGNORE INTO {table} (guild_id) VALUES (?)", (guild_id,))
            )
        elif self.db_type == "postgresql":
            ops.append(
                (
                    f"INSERT INTO {table} (guild_id) VALUES (?) ON CONFLICT (guild_id) DO NOTHING",
                    (guild_id,),
                )
            )
        else:
            ops.append(
                (f"INSERT IGNORE INTO {table} (guild_id) VALUES (?)", (guild_id,))
            )

        for col, val in kwargs.items():
            ops.append(
                (f"UPDATE {table} SET {col} = ? WHERE guild_id = ?", (val, guild_id))
            )
        self._executemany(ops)

    # ── Welcomes ──────────────────────────────────────────────────────────────
    def get_welcome_config(self, guild_id: int) -> Dict:
        row = self._fetchone(
            "SELECT * FROM welcome_config WHERE guild_id = ?", (guild_id,)
        )
        return row or {
            "guild_id": guild_id,
            "channel_id": None,
            "embed_data": None,
            "enabled": 0,
        }

    def set_welcome_config(self, guild_id: int, **kwargs) -> None:
        self._upsert_config("welcome_config", guild_id, **kwargs)

    # ── Boosts ────────────────────────────────────────────────────────────────
    def get_boost_config(self, guild_id: int) -> Dict:
        row = self._fetchone(
            "SELECT * FROM boost_config WHERE guild_id = ?", (guild_id,)
        )
        return row or {
            "guild_id": guild_id,
            "channel_id": None,
            "embed_data": None,
            "gif_url": None,
            "enabled": 0,
        }

    def set_boost_config(self, guild_id: int, **kwargs) -> None:
        self._upsert_config("boost_config", guild_id, **kwargs)

    # ── Suggestions ───────────────────────────────────────────────────────────
    def get_suggestions_config(self, guild_id: int) -> Dict:
        row = self._fetchone(
            "SELECT * FROM suggestions_config WHERE guild_id = ?", (guild_id,)
        )
        return row or {
            "guild_id": guild_id,
            "submit_channel_id": None,
            "review_channel_id": None,
            "public_channel_id": None,
        }

    def set_suggestions_config(self, guild_id: int, **kwargs) -> None:
        self._upsert_config("suggestions_config", guild_id, **kwargs)

    def create_suggestion(self, guild_id: int, user_id: int, content: str) -> int:
        ops = [
            (
                "INSERT INTO suggestions (guild_id, user_id, content, created_at) VALUES (?, ?, ?, ?)",
                (guild_id, user_id, content, datetime.now(timezone.utc).isoformat()),
            )
        ]
        self._executemany(ops)
        row = self._fetchone(
            "SELECT id FROM suggestions WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1",
            (guild_id, user_id),
        )
        return row["id"] if row else 0

    def get_suggestion(self, suggestion_id: int) -> Optional[Dict]:
        return self._fetchone(
            "SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)
        )

    def update_suggestion(self, suggestion_id: int, **kwargs) -> None:
        if not kwargs:
            return
        invalid = set(kwargs) - VALID_SUGGESTION_COLUMNS
        if invalid:
            raise ValueError(f"Columnas inválidas en suggestions: {invalid}")
        ops = [
            (f"UPDATE suggestions SET {col} = ? WHERE id = ?", (val, suggestion_id))
            for col, val in kwargs.items()
        ]
        self._executemany(ops)

    # ── Suggestion votes (tracking por usuario, idempotente) ──────────────────
    def get_user_vote(self, suggestion_id: int, user_id: int) -> int:
        """Retorna 1, -1 o 0 según el voto del usuario."""
        row = self._fetchone(
            "SELECT vote FROM suggestion_votes WHERE suggestion_id = ? AND user_id = ?",
            (suggestion_id, user_id),
        )
        return int(row["vote"]) if row else 0

    def cast_vote(self, suggestion_id: int, user_id: int, vote: int) -> Dict:
        """
        Aplica un voto (vote ∈ {-1, +1}). Si el usuario re-vota igual → quita.
        Devuelve los nuevos counts {upvotes, downvotes}.
        """
        if vote not in (-1, 1):
            raise ValueError("vote debe ser 1 o -1")

        prev = self.get_user_vote(suggestion_id, user_id)
        if prev == vote:
            self._execute(
                "DELETE FROM suggestion_votes WHERE suggestion_id = ? AND user_id = ?",
                (suggestion_id, user_id),
            )
        elif prev == 0:
            if self.db_type == "sqlite":
                sql = "INSERT OR REPLACE INTO suggestion_votes (suggestion_id, user_id, vote) VALUES (?, ?, ?)"
            elif self.db_type == "postgresql":
                sql = (
                    "INSERT INTO suggestion_votes (suggestion_id, user_id, vote) VALUES (?, ?, ?) "
                    "ON CONFLICT (suggestion_id, user_id) DO UPDATE SET vote = EXCLUDED.vote"
                )
            else:
                sql = (
                    "INSERT INTO suggestion_votes (suggestion_id, user_id, vote) VALUES (?, ?, ?) "
                    "ON DUPLICATE KEY UPDATE vote = VALUES(vote)"
                )
            self._execute(sql, (suggestion_id, user_id, vote))
        else:
            self._execute(
                "UPDATE suggestion_votes SET vote = ? WHERE suggestion_id = ? AND user_id = ?",
                (vote, suggestion_id, user_id),
            )

        ups = self._fetchone(
            "SELECT COUNT(*) AS c FROM suggestion_votes WHERE suggestion_id = ? AND vote = 1",
            (suggestion_id,),
        )
        downs = self._fetchone(
            "SELECT COUNT(*) AS c FROM suggestion_votes WHERE suggestion_id = ? AND vote = -1",
            (suggestion_id,),
        )
        upvotes = int(ups["c"]) if ups else 0
        downvotes = int(downs["c"]) if downs else 0
        self._executemany([
            ("UPDATE suggestions SET upvotes = ? WHERE id = ?", (upvotes, suggestion_id)),
            ("UPDATE suggestions SET downvotes = ? WHERE id = ?", (downvotes, suggestion_id)),
        ])
        return {"upvotes": upvotes, "downvotes": downvotes}

    def get_last_user_suggestion_ts(self, guild_id: int, user_id: int) -> Optional[str]:
        """Timestamp ISO de la última sugerencia del usuario (para cooldown)."""
        row = self._fetchone(
            "SELECT created_at FROM suggestions WHERE guild_id = ? AND user_id = ? "
            "AND status <> 'FAILED' "
            "ORDER BY id DESC LIMIT 1",
            (guild_id, user_id),
        )
        return row["created_at"] if row else None

    # ── Giveaways ─────────────────────────────────────────────────────────────
    def create_giveaway(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        prize: str,
        end_time: int,
        winners_count: int,
        req_roles: str,
        deny_roles: str,
    ) -> None:
        self._execute(
            "INSERT INTO giveaways (guild_id, channel_id, message_id, prize, end_time, winners_count, req_roles, deny_roles, participants) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                guild_id,
                channel_id,
                message_id,
                prize,
                end_time,
                winners_count,
                req_roles,
                deny_roles,
                "[]",
            ),
        )

    def get_giveaway(self, message_id: int) -> Optional[Dict]:
        return self._fetchone(
            "SELECT * FROM giveaways WHERE message_id = ?", (message_id,)
        )

    def get_active_giveaways(self) -> List[Dict]:
        return self._fetchall("SELECT * FROM giveaways WHERE ended = 0", ())

    def update_giveaway(self, message_id: int, **kwargs) -> None:
        if not kwargs:
            return
        invalid = set(kwargs) - VALID_GIVEAWAY_COLUMNS
        if invalid:
            raise ValueError(f"Columnas inválidas en giveaways: {invalid}")
        ops = [
            (f"UPDATE giveaways SET {col} = ? WHERE message_id = ?", (val, message_id))
            for col, val in kwargs.items()
        ]
        self._executemany(ops)

    def _giveaway_participants_in_transaction(self, cur, message_id: int) -> List[int]:
        cur.execute(
            self._adapt("SELECT user_id FROM giveaway_participants WHERE message_id = ? ORDER BY joined_at"),
            (message_id,),
        )
        participants = [int(row["user_id"] if hasattr(row, "keys") else row[0]) for row in cur.fetchall()]
        if participants:
            return participants
        cur.execute(self._adapt("SELECT participants FROM giveaways WHERE message_id = ?"), (message_id,))
        row = cur.fetchone()
        if not row:
            return []
        raw = row["participants"] if hasattr(row, "keys") else row[0]
        try:
            legacy = [int(user_id) for user_id in json.loads(raw or "[]")]
        except (TypeError, ValueError, json.JSONDecodeError):
            legacy = []
        now = datetime.now(timezone.utc).isoformat()
        for user_id in dict.fromkeys(legacy):
            cur.execute(
                self._adapt(
                    "INSERT INTO giveaway_participants (message_id, user_id, joined_at) VALUES (?, ?, ?)"
                ),
                (message_id, user_id, now),
            )
        return list(dict.fromkeys(legacy))

    def toggle_giveaway_participant(self, message_id: int, user_id: int) -> tuple[bool, List[int]]:
        """Alterna una participación y mantiene el JSON legacy en una sola transacción."""
        with self._conn() as conn:
            if self.db_type == "postgresql":
                from psycopg2.extras import RealDictCursor

                cur = conn.cursor(cursor_factory=RealDictCursor)
            else:
                cur = conn.cursor()
            participants = self._giveaway_participants_in_transaction(cur, message_id)
            if user_id in participants:
                cur.execute(
                    self._adapt(
                        "DELETE FROM giveaway_participants WHERE message_id = ? AND user_id = ?"
                    ),
                    (message_id, user_id),
                )
                participants.remove(user_id)
                joined = False
            else:
                cur.execute(
                    self._adapt(
                        "INSERT INTO giveaway_participants (message_id, user_id, joined_at) VALUES (?, ?, ?)"
                    ),
                    (message_id, user_id, datetime.now(timezone.utc).isoformat()),
                )
                participants.append(user_id)
                joined = True
            cur.execute(
                self._adapt("UPDATE giveaways SET participants = ? WHERE message_id = ?"),
                (json.dumps(participants), message_id),
            )
            return joined, participants

    def list_giveaway_participants(self, message_id: int) -> List[int]:
        with self._conn() as conn:
            if self.db_type == "postgresql":
                from psycopg2.extras import RealDictCursor

                cur = conn.cursor(cursor_factory=RealDictCursor)
            else:
                cur = conn.cursor()
            return self._giveaway_participants_in_transaction(cur, message_id)

    def claim_giveaway_finalization(self, message_id: int, winners: List[int]) -> bool:
        return self._execute_rowcount(
            "UPDATE giveaways SET ended = 2, winners = ? "
            "WHERE message_id = ? AND ended = 0 AND cancelled = 0",
            (json.dumps(winners), message_id),
        ) == 1

    def complete_giveaway_finalization(self, message_id: int) -> bool:
        return self._execute_rowcount(
            "UPDATE giveaways SET ended = 1 WHERE message_id = ? AND ended = 2",
            (message_id,),
        ) == 1

    def release_giveaway_finalization(self, message_id: int) -> bool:
        return self._execute_rowcount(
            "UPDATE giveaways SET ended = 0 WHERE message_id = ? AND ended = 2",
            (message_id,),
        ) == 1

    def recover_giveaway_finalizations(self) -> int:
        """Libera claims abandonados tras un reinicio de la única instancia soportada."""
        return self._execute_rowcount(
            "UPDATE giveaways SET ended = 0 WHERE ended = 2 AND cancelled = 0"
        )

    def cancel_active_giveaway(self, message_id: int) -> bool:
        return self._execute_rowcount(
            "UPDATE giveaways SET ended = 1, cancelled = 1 "
            "WHERE message_id = ? AND ended = 0 AND cancelled = 0",
            (message_id,),
        ) == 1

    # ── AutoRoles ─────────────────────────────────────────────────────────────
    def set_autorole(
        self, message_id: int, guild_id: int, channel_id: int, mapping_data: str
    ) -> None:
        ops = []
        if self.db_type == "sqlite":
            ops.append(
                (
                    "INSERT OR REPLACE INTO autoroles (message_id, guild_id, channel_id, mapping_data) VALUES (?, ?, ?, ?)",
                    (message_id, guild_id, channel_id, mapping_data),
                )
            )
        elif self.db_type == "postgresql":
            ops.append(
                (
                    "INSERT INTO autoroles (message_id, guild_id, channel_id, mapping_data) VALUES (?, ?, ?, ?) ON CONFLICT (message_id) DO UPDATE SET mapping_data = EXCLUDED.mapping_data",
                    (message_id, guild_id, channel_id, mapping_data),
                )
            )
        else:
            ops.append(
                (
                    "INSERT INTO autoroles (message_id, guild_id, channel_id, mapping_data) VALUES (?, ?, ?, ?) ON DUPLICATE KEY UPDATE mapping_data=VALUES(mapping_data)",
                    (message_id, guild_id, channel_id, mapping_data),
                )
            )
        self._executemany(ops)

    def get_autorole(self, message_id: int) -> Optional[Dict]:
        return self._fetchone(
            "SELECT * FROM autoroles WHERE message_id = ?", (message_id,)
        )

    def get_guild_autoroles(self, guild_id: int) -> List[Dict]:
        return self._fetchall("SELECT * FROM autoroles WHERE guild_id = ?", (guild_id,))

    def delete_autorole(self, message_id: int) -> None:
        self._execute("DELETE FROM autoroles WHERE message_id = ?", (message_id,))

    # ── Join Autoroles (rol al unirse al servidor) ────────────────────────────
    def get_join_autoroles(self, guild_id: int) -> List[Dict]:
        """Lista de roles que se asignan automáticamente cuando un usuario entra."""
        return self._fetchall(
            "SELECT role_id, created_at FROM join_autoroles WHERE guild_id = ? ORDER BY created_at",
            (guild_id,),
        )

    def add_join_autorole(self, guild_id: int, role_id: int) -> None:
        """Agrega un rol a la lista de auto-asignación al unirse. Idempotente."""
        if self.db_type == "sqlite":
            sql = "INSERT OR IGNORE INTO join_autoroles (guild_id, role_id) VALUES (?, ?)"
        elif self.db_type == "postgresql":
            sql = "INSERT INTO join_autoroles (guild_id, role_id) VALUES (?, ?) ON CONFLICT DO NOTHING"
        else:
            sql = "INSERT IGNORE INTO join_autoroles (guild_id, role_id) VALUES (?, ?)"
        self._execute(sql, (guild_id, role_id))

    def remove_join_autorole(self, guild_id: int, role_id: int) -> None:
        """Quita un rol de la lista de auto-asignación al unirse."""
        self._execute(
            "DELETE FROM join_autoroles WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id),
        )

    # ── Lofi Config ───────────────────────────────────────────────────────────
    def get_lofi_config(self, guild_id: int) -> Dict:
        row = self._fetchone(
            "SELECT * FROM lofi_config WHERE guild_id = ?", (guild_id,)
        )
        if row:
            # Defaults para columnas añadidas en migraciones (BBDDs viejas pueden no tenerlas).
            row.setdefault("auto_reconnect", 1)
            row.setdefault("pause_on_empty", 0)
            return row
        return {
            "guild_id": guild_id,
            "channel_id": None,
            "volume": 100,
            "enabled": 0,
            "stream_url": None,
            "station_name": "Lofi Radio 24/7",
            "auto_reconnect": 1,
            "pause_on_empty": 0,
        }

    def set_lofi_config(self, guild_id: int, **kwargs) -> None:
        self._upsert_config("lofi_config", guild_id, **kwargs)

    # ── Bot Stats (Web Panel IPC) ─────────────────────────────────────────────
    def update_bot_stats(
        self,
        members_online: int,
        total_members: int,
        open_tickets: int,
        uptime_seconds: int,
    ) -> None:
        ops = []
        now = datetime.now(timezone.utc).isoformat()
        if self.db_type == "sqlite":
            ops.append(("INSERT OR IGNORE INTO bot_stats (id) VALUES (1)", ()))
        elif self.db_type == "postgresql":
            ops.append(
                (
                    "INSERT INTO bot_stats (id) VALUES (1) ON CONFLICT (id) DO NOTHING",
                    (),
                )
            )
        else:
            ops.append(("INSERT IGNORE INTO bot_stats (id) VALUES (1)", ()))

        ops.append(
            (
                "UPDATE bot_stats SET members_online = ?, total_members = ?, open_tickets = ?, uptime_seconds = ?, last_updated = ? WHERE id = 1",
                (members_online, total_members, open_tickets, uptime_seconds, now),
            )
        )
        self._executemany(ops)

    def get_bot_stats(self) -> Dict:
        row = self._fetchone("SELECT * FROM bot_stats WHERE id = 1", ())
        return row or {
            "members_online": 0,
            "total_members": 0,
            "open_tickets": 0,
            "uptime_seconds": 0,
            "last_updated": "",
        }

    # ── Tickets ───────────────────────────────────────────────────────────────
    def get_ticket_config(self, guild_id: int) -> Dict:
        row = self._fetchone(
            "SELECT * FROM ticket_config WHERE guild_id = ?", (guild_id,)
        )
        return row or {
            "guild_id": guild_id,
            "panel_channel_id": None,
            "category_id": None,
            "log_channel_id": None,
            "allowed_roles": "[]",
            "immune_roles": "[]",
        }

    def set_ticket_config(self, guild_id: int, **kwargs) -> None:
        self._upsert_config("ticket_config", guild_id, **kwargs)

    def get_ticket_categories(self, guild_id: int) -> List[Dict]:
        return self._fetchall(
            "SELECT * FROM ticket_categories WHERE guild_id = ?", (guild_id,)
        )

    def add_ticket_category(
        self,
        guild_id: int,
        name: str,
        emoji: str,
        questions: str,
        close_reasons: str,
        welcome_embed_data: Optional[str] = None,
        description: Optional[str] = None,
        welcome_embed_template_key: Optional[str] = None,
        staff_role_id: Optional[int] = None,
        staff_role_ids: Optional[str] = None,
    ) -> None:
        self._execute(
            "INSERT INTO ticket_categories (guild_id, name, emoji, description, "
            "questions, close_reasons, welcome_embed_data, welcome_embed_template_key, "
            "staff_role_id, staff_role_ids) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                guild_id, name, emoji, description,
                questions, close_reasons, welcome_embed_data,
                welcome_embed_template_key, staff_role_id, staff_role_ids,
            ),
        )

    def update_ticket_category(
        self, category_id: int, guild_id: int, **kwargs
    ) -> None:
        """Actualiza campos de una categoría. Allowlist por seguridad."""
        allowed = {
            "name", "emoji", "description", "questions", "close_reasons",
            "welcome_embed_data", "welcome_embed_template_key", "staff_role_id", "staff_role_ids",
        }
        invalid = set(kwargs) - allowed
        if invalid:
            raise ValueError(f"Campos inválidos en update_ticket_category: {invalid}")
        if not kwargs:
            return
        ops = [
            (
                f"UPDATE ticket_categories SET {col} = ? WHERE id = ? AND guild_id = ?",
                (val, category_id, guild_id),
            )
            for col, val in kwargs.items()
        ]
        if "name" in kwargs:
            ops.insert(0, (
                "UPDATE tickets SET category_name = ? WHERE guild_id = ? AND category_name = "
                "(SELECT name FROM ticket_categories WHERE id = ? AND guild_id = ?)",
                (kwargs["name"], guild_id, category_id, guild_id),
            ))
        self._executemany(ops)

    def delete_ticket_category(self, category_id: int, guild_id: int) -> None:
        self._execute(
            "DELETE FROM ticket_categories WHERE id = ? AND guild_id = ?",
            (category_id, guild_id),
        )

    # ── Ticket Template Embeds (pool reutilizable) ────────────────────────────
    #
    # `template_key` agrupa plantillas por uso (panel_select, panel_inside,
    # msg_open, msg_close, custom_<x>). UNIQUE(guild_id, template_key) → upsert
    # por key. Usar `name` para distinguir plantillas custom.

    def list_ticket_templates(self, guild_id: int) -> List[Dict]:
        return self._fetchall(
            "SELECT * FROM ticket_template_embeds WHERE guild_id = ? ORDER BY created_at DESC",
            (guild_id,),
        )

    def get_ticket_template(self, guild_id: int, template_key: str) -> Optional[Dict]:
        row = self._fetchone(
            "SELECT * FROM ticket_template_embeds WHERE guild_id = ? AND template_key = ?",
            (guild_id, template_key),
        )
        return dict(row) if row else None

    def upsert_ticket_template(
        self,
        guild_id: int,
        template_key: str,
        embed_data: str,
        name: Optional[str] = None,
    ) -> None:
        """Crea o reemplaza la plantilla identificada por (guild_id, template_key)."""
        now = datetime.now(timezone.utc).isoformat()
        if self.db_type == "sqlite":
            self._execute(
                "INSERT INTO ticket_template_embeds "
                "(guild_id, template_key, name, embed_data, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, template_key) DO UPDATE SET "
                "embed_data = excluded.embed_data, name = excluded.name",
                (guild_id, template_key, name, embed_data, now),
            )
        elif self.db_type == "postgresql":
            self._execute(
                "INSERT INTO ticket_template_embeds "
                "(guild_id, template_key, name, embed_data, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (guild_id, template_key) DO UPDATE SET "
                "embed_data = EXCLUDED.embed_data, name = EXCLUDED.name",
                (guild_id, template_key, name, embed_data, now),
            )
        else:  # mariadb
            self._execute(
                "INSERT INTO ticket_template_embeds "
                "(guild_id, template_key, name, embed_data, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON DUPLICATE KEY UPDATE "
                "embed_data = VALUES(embed_data), name = VALUES(name)",
                (guild_id, template_key, name, embed_data, now),
            )

    def delete_ticket_template(self, guild_id: int, template_key: str) -> None:
        self._execute(
            "DELETE FROM ticket_template_embeds WHERE guild_id = ? AND template_key = ?",
            (guild_id, template_key),
        )

    def create_ticket(self, guild_id: int, user_id: int, category_name: str) -> Dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            if self.db_type == "postgresql":
                from psycopg2.extras import RealDictCursor

                cur = conn.cursor(cursor_factory=RealDictCursor)
            else:
                cur = conn.cursor()
            global_num = self._reserve_entity_number(
                cur, "ticket", guild_id, "tickets", "global_number"
            )
            cur.execute(
                self._adapt(
                    "INSERT INTO tickets (global_number, guild_id, user_id, category_name, status, created_at) "
                    "VALUES (?, ?, ?, ?, 'CREATING', ?)"
                ),
                (global_num, guild_id, user_id, category_name, now),
            )
            cur.execute(
                self._adapt(
                    "SELECT * FROM tickets WHERE guild_id = ? AND global_number = ? LIMIT 1"
                ),
                (guild_id, global_num),
            )
            row = cur.fetchone()
            return dict(row) if row else {}

    def get_ticket_by_channel(self, channel_id: int) -> Optional[Dict]:
        return self._fetchone(
            "SELECT * FROM tickets WHERE channel_id = ?", (channel_id,)
        )

    def get_ticket(self, ticket_id: int) -> Optional[Dict]:
        return self._fetchone("SELECT * FROM tickets WHERE id = ?", (ticket_id,))

    def update_ticket(self, ticket_id: int, **kwargs) -> None:
        if not kwargs:
            return
        invalid = set(kwargs) - VALID_TICKET_COLUMNS
        if invalid:
            raise ValueError(f"Columnas inválidas en tickets: {invalid}")
        ops = [
            (f"UPDATE tickets SET {col} = ? WHERE id = ?", (val, ticket_id))
            for col, val in kwargs.items()
        ]
        self._executemany(ops)

    def count_open_tickets_by_user(self, guild_id: int, user_id: int) -> int:
        row = self._fetchone(
            "SELECT COUNT(*) as cnt FROM tickets WHERE guild_id = ? AND user_id = ? AND status = 'OPEN'",
            (guild_id, user_id),
        )
        return int(row["cnt"]) if row else 0

    def get_last_ticket_time(self, guild_id: int, user_id: int) -> Optional[str]:
        row = self._fetchone(
            "SELECT MAX(created_at) as last FROM tickets WHERE guild_id = ? AND user_id = ? "
            "AND status IN ('OPEN', 'CLOSING', 'CLOSED')",
            (guild_id, user_id),
        )
        return row["last"] if row else None

    # ── Tags ──────────────────────────────────────────────────────────────────

    def get_tag(self, guild_id: int, name: str) -> Optional[Dict]:
        return self._fetchone(
            "SELECT * FROM tags WHERE guild_id = ? AND name = ?",
            (guild_id, name.lower()),
        )

    def get_all_tags(self, guild_id: int) -> List[Dict]:
        return self._fetchall(
            "SELECT * FROM tags WHERE guild_id = ? ORDER BY name ASC", (guild_id,)
        )

    def create_tag(
        self, guild_id: int, name: str, content: str, creator_id: int
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            "INSERT INTO tags (guild_id, name, content, creator_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, name.lower(), content, creator_id, now),
        )

    def update_tag(self, guild_id: int, name: str, content: str) -> None:
        self._execute(
            "UPDATE tags SET content = ? WHERE guild_id = ? AND name = ?",
            (content, guild_id, name.lower()),
        )

    def delete_tag(self, guild_id: int, name: str) -> None:
        self._execute(
            "DELETE FROM tags WHERE guild_id = ? AND name = ?", (guild_id, name.lower())
        )

    def increment_tag_uses(self, guild_id: int, name: str) -> None:
        self._execute(
            "UPDATE tags SET uses = uses + 1 WHERE guild_id = ? AND name = ?",
            (guild_id, name.lower()),
        )

    # ── Reports ───────────────────────────────────────────────────────────────

    def create_report(
        self, guild_id: int, reporter_id: int, reported_user_id: int, reason: str
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            "INSERT INTO reports (guild_id, reporter_id, reported_user_id, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, reporter_id, reported_user_id, reason, now),
        )
        row = self._fetchone(
            "SELECT id FROM reports WHERE guild_id = ? AND reporter_id = ? ORDER BY id DESC LIMIT 1",
            (guild_id, reporter_id),
        )
        return int(row["id"]) if row else 0

    def get_reports(self, guild_id: int, status: str = None) -> List[Dict]:
        if status:
            return self._fetchall(
                "SELECT * FROM reports WHERE guild_id = ? AND status = ? ORDER BY id DESC",
                (guild_id, status),
            )
        return self._fetchall(
            "SELECT * FROM reports WHERE guild_id = ? ORDER BY id DESC", (guild_id,)
        )

    def get_report(self, report_id: int) -> Optional[Dict]:
        """Obtiene un reporte por su ID."""
        return self._fetchone("SELECT * FROM reports WHERE id = ?", (report_id,))

    def update_report(self, report_id: int, **kwargs) -> None:
        valid = frozenset({"status", "ticket_id"})
        invalid = set(kwargs) - valid
        if invalid:
            raise ValueError(f"Columnas inválidas en reports: {invalid}")
        ops = [
            (f"UPDATE reports SET {col} = ? WHERE id = ?", (val, report_id))
            for col, val in kwargs.items()
        ]
        self._executemany(ops)

    def transition_ticket_status(
        self,
        ticket_id: int,
        expected_status: str,
        new_status: str,
        **fields,
    ) -> bool:
        invalid = set(fields) - VALID_TICKET_COLUMNS
        if invalid:
            raise ValueError(f"Columnas inválidas en tickets: {invalid}")
        assignments = ["status = ?", *[f"{column} = ?" for column in fields]]
        params = (new_status, *fields.values(), ticket_id, expected_status)
        return self._execute_rowcount(
            f"UPDATE tickets SET {', '.join(assignments)} WHERE id = ? AND status = ?",
            params,
        ) == 1

    def resolve_report(self, report_id: int, guild_id: int, status: str) -> bool:
        """Transiciona exactamente una vez un reporte PENDING del guild indicado."""
        if status not in {"RESOLVED", "DISMISSED"}:
            raise ValueError("Estado terminal de reporte inválido")
        return self._execute_rowcount(
            "UPDATE reports SET status = ? WHERE id = ? AND guild_id = ? AND status = 'PENDING'",
            (status, report_id, guild_id),
        ) == 1

    # ── Scheduled Messages ────────────────────────────────────────────────────

    def create_schedule(
        self,
        guild_id: int,
        name: str,
        channel_id: int,
        content: str,
        interval_seconds: int,
        created_by: int,
        schedule_mode: str = "interval",
        cron_hour: Optional[int] = None,
        cron_minute: Optional[int] = None,
        cron_weekdays: Optional[str] = None,
        timezone_name: Optional[str] = None,
        message_data: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            "INSERT INTO scheduled_messages "
            "(guild_id, name, channel_id, content, interval_seconds, created_by, created_at, "
            " schedule_mode, cron_hour, cron_minute, cron_weekdays, timezone, message_data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                guild_id, name, channel_id, content, interval_seconds, created_by, now,
                schedule_mode, cron_hour, cron_minute, cron_weekdays, timezone_name, message_data,
            ),
        )

    def get_schedules(self, guild_id: int) -> List[Dict]:
        return self._fetchall(
            "SELECT * FROM scheduled_messages WHERE guild_id = ? ORDER BY name ASC",
            (guild_id,),
        )

    def get_all_active_schedules(self) -> List[Dict]:
        return self._fetchall("SELECT * FROM scheduled_messages WHERE enabled = 1", ())

    def update_schedule(self, schedule_id: int, **kwargs) -> None:
        valid = frozenset(
            {
                "enabled", "channel_id", "content", "interval_seconds", "last_sent",
                "schedule_mode", "cron_hour", "cron_minute", "cron_weekdays",
                "timezone", "message_data",
            }
        )
        invalid = set(kwargs) - valid
        if invalid:
            raise ValueError(f"Columnas inválidas en scheduled_messages: {invalid}")
        ops = [
            (
                f"UPDATE scheduled_messages SET {col} = ? WHERE id = ?",
                (val, schedule_id),
            )
            for col, val in kwargs.items()
        ]
        self._executemany(ops)

    def delete_schedule(self, guild_id: int, name: str) -> None:
        self._execute(
            "DELETE FROM scheduled_messages WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )

    def get_schedule_by_name(self, guild_id: int, name: str) -> Optional[Dict]:
        return self._fetchone(
            "SELECT * FROM scheduled_messages WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )

    # ── Levels / XP ───────────────────────────────────────────────────────────

    def get_user_level(self, user_id: int, guild_id: int) -> Dict:
        row = self._fetchone(
            "SELECT * FROM user_levels WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        )
        return row or {
            "user_id": user_id,
            "guild_id": guild_id,
            "xp": 0,
            "level": 0,
            "message_count": 0,
        }

    @staticmethod
    def _xp_for_level(n: int) -> int:
        """XP total acumulado necesario para alcanzar el nivel n (fórmula MEE6)."""
        total = 0
        for k in range(1, n + 1):
            total += 5 * k * k + 50 * k + 100
        return total

    @staticmethod
    def _compute_level(total_xp: int) -> int:
        """Calcula el nivel para un XP total dado."""
        level = 0
        needed = 0
        while True:
            needed += 5 * (level + 1) ** 2 + 50 * (level + 1) + 100
            if total_xp < needed:
                break
            level += 1
        return level

    def add_xp(
        self,
        user_id: int,
        guild_id: int,
        amount: int,
        *,
        increment_message_count: bool = True,
    ) -> Dict:
        """Añade XP atómicamente y devuelve el estado anterior/nuevo.

        ``increment_message_count`` debe ser ``False`` para ajustes administrativos;
        regalar XP no equivale a que el usuario haya enviado un mensaje.
        """
        with self._conn() as conn:
            if self.db_type == "postgresql":
                from psycopg2.extras import RealDictCursor

                cur = conn.cursor(cursor_factory=RealDictCursor)
            else:
                cur = conn.cursor()

            if self.db_type == "sqlite":
                cur.execute(
                    "INSERT OR IGNORE INTO user_levels "
                    "(user_id, guild_id, xp, level, message_count) VALUES (?, ?, 0, 0, 0)",
                    (user_id, guild_id),
                )
            elif self.db_type == "postgresql":
                cur.execute(
                    self._adapt(
                        "INSERT INTO user_levels (user_id, guild_id, xp, level, message_count) "
                        "VALUES (?, ?, 0, 0, 0) ON CONFLICT (user_id, guild_id) DO NOTHING"
                    ),
                    (user_id, guild_id),
                )
            else:
                cur.execute(
                    self._adapt(
                        "INSERT IGNORE INTO user_levels "
                        "(user_id, guild_id, xp, level, message_count) VALUES (?, ?, 0, 0, 0)"
                    ),
                    (user_id, guild_id),
                )

            lock_suffix = "" if self.db_type == "sqlite" else " FOR UPDATE"
            cur.execute(
                self._adapt(
                    "SELECT xp, level, message_count FROM user_levels "
                    "WHERE user_id = ? AND guild_id = ?" + lock_suffix
                ),
                (user_id, guild_id),
            )
            raw = cur.fetchone()
            row = dict(raw) if raw else {"xp": 0, "level": 0, "message_count": 0}
            old_level = int(row.get("level") or 0)
            new_xp = max(0, int(row.get("xp") or 0) + int(amount))
            new_level = self._compute_level(new_xp)
            new_count = int(row.get("message_count") or 0) + int(increment_message_count)
            cur.execute(
                self._adapt(
                    "UPDATE user_levels SET xp = ?, level = ?, message_count = ? "
                    "WHERE user_id = ? AND guild_id = ?"
                ),
                (new_xp, new_level, new_count, user_id, guild_id),
            )

        leveled_up = new_level > old_level
        return {
            "xp": new_xp,
            "level": new_level,
            "old_level": old_level,
            "leveled_up": leveled_up,
            "message_count": new_count,
        }

    def reset_user_level(self, user_id: int, guild_id: int) -> None:
        self._execute(
            "UPDATE user_levels SET xp = 0, level = 0, message_count = 0 WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        )

    def get_leaderboard(self, guild_id: int, limit: int = 10) -> List[Dict]:
        return self._fetchall(
            "SELECT user_id, xp, level, message_count, "
            "ROW_NUMBER() OVER (ORDER BY xp DESC) as position "
            "FROM user_levels WHERE guild_id = ? ORDER BY xp DESC LIMIT ?",
            (guild_id, limit),
        )

    def get_xp_config(self, guild_id: int) -> Dict:
        row = self._fetchone("SELECT * FROM xp_config WHERE guild_id = ?", (guild_id,))
        return row or {
            "guild_id": guild_id,
            "enabled": 0,
            "xp_min": 15,
            "xp_max": 25,
            "cooldown_seconds": 60,
            "ignored_channels": "[]",
            "channel_multipliers": "{}",
            "announcement_channel_id": None,
            "announcement_message": None,
            "stack_rewards": 1,
        }

    def set_xp_config(self, guild_id: int, **kwargs) -> None:
        self._upsert_config("xp_config", guild_id, **kwargs)

    def get_level_rewards(self, guild_id: int) -> List[Dict]:
        return self._fetchall(
            "SELECT * FROM level_rewards WHERE guild_id = ? ORDER BY level ASC",
            (guild_id,),
        )

    def set_level_reward(self, guild_id: int, level: int, role_id: int) -> None:
        if self.db_type == "sqlite":
            self._execute(
                "INSERT INTO level_rewards (guild_id, level, role_id) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, level) DO UPDATE SET role_id = ?",
                (guild_id, level, role_id, role_id),
            )
        elif self.db_type == "postgresql":
            self._execute(
                "INSERT INTO level_rewards (guild_id, level, role_id) VALUES (?, ?, ?) "
                "ON CONFLICT (guild_id, level) DO UPDATE SET role_id = EXCLUDED.role_id",
                (guild_id, level, role_id),
            )
        else:
            self._execute(
                "INSERT INTO level_rewards (guild_id, level, role_id) VALUES (?, ?, ?) "
                "ON DUPLICATE KEY UPDATE role_id = VALUES(role_id)",
                (guild_id, level, role_id),
            )

    def delete_level_reward(self, guild_id: int, level: int) -> None:
        self._execute(
            "DELETE FROM level_rewards WHERE guild_id = ? AND level = ?",
            (guild_id, level),
        )

    def get_level_reward(self, guild_id: int, level: int) -> Optional[Dict]:
        return self._fetchone(
            "SELECT * FROM level_rewards WHERE guild_id = ? AND level = ?",
            (guild_id, level),
        )

    # ── Web Panel helpers ─────────────────────────────────────────────────────

    def get_user_rank(self, user_id: int, guild_id: int) -> int:
        """Retorna la posición del usuario en el leaderboard (1-indexed). 0 si no tiene XP."""
        row = self._fetchone(
            "SELECT COUNT(*) + 1 AS rank FROM user_levels "
            "WHERE guild_id = ? AND xp > (SELECT COALESCE(xp, 0) FROM user_levels WHERE user_id = ? AND guild_id = ?)",
            (guild_id, user_id, guild_id),
        )
        if row:
            return int(row["rank"])
        return 0

    def count_all_open_tickets(self) -> int:
        """Cuenta todos los tickets abiertos en todos los servidores."""
        row = self._fetchone(
            "SELECT COUNT(*) AS cnt FROM tickets WHERE status = 'OPEN'", ()
        )
        return int(row["cnt"]) if row else 0

    def count_open_tickets_by_guild(self, guild_id: int) -> int:
        """Cuenta tickets abiertos de un servidor específico."""
        row = self._fetchone(
            "SELECT COUNT(*) AS cnt FROM tickets WHERE guild_id = ? AND status = 'OPEN'",
            (guild_id,),
        )
        return int(row["cnt"]) if row else 0

    def get_all_tickets(
        self,
        guild_id: int,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict]:
        """Retorna tickets de un servidor con paginación y filtro opcional."""
        if status:
            return self._fetchall(
                "SELECT * FROM tickets WHERE guild_id = ? AND status = ? "
                "ORDER BY id DESC LIMIT ? OFFSET ?",
                (guild_id, status, limit, offset),
            )
        return self._fetchall(
            "SELECT * FROM tickets WHERE guild_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (guild_id, limit, offset),
        )

    def get_guild_giveaways(
        self, guild_id: int, active_only: bool = True
    ) -> List[Dict]:
        """Retorna sorteos de un servidor, opcionalmente solo activos."""
        if active_only:
            return self._fetchall(
                "SELECT * FROM giveaways WHERE guild_id = ? AND ended = 0 ORDER BY end_time ASC",
                (guild_id,),
            )
        return self._fetchall(
            "SELECT * FROM giveaways WHERE guild_id = ? ORDER BY id DESC",
            (guild_id,),
        )

    def get_mod_actions(
        self, guild_id: int, limit: int = 50, offset: int = 0
    ) -> List[Dict]:
        """Retorna acciones de moderación de un servidor con paginación."""
        return self._fetchall(
            "SELECT * FROM mod_actions WHERE guild_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (guild_id, limit, offset),
        )

    def get_users_with_warns(self, guild_id: int) -> List[Dict]:
        """Retorna usuarios con warns activos en un servidor."""
        return self._fetchall(
            "SELECT * FROM user_records WHERE guild_id = ? AND warns > 0 ORDER BY warns DESC",
            (guild_id,),
        )

    # ── Custom Commands ───────────────────────────────────────────────────────

    def get_custom_commands(self, guild_id: int) -> List[Dict]:
        """Retorna todos los custom commands de un servidor."""
        return self._fetchall(
            "SELECT * FROM custom_commands WHERE guild_id = ? ORDER BY name ASC",
            (guild_id,),
        )

    def get_custom_command(self, guild_id: int, name: str) -> Optional[Dict]:
        """Obtiene un custom command por nombre."""
        return self._fetchone(
            "SELECT * FROM custom_commands WHERE guild_id = ? AND name = ?",
            (guild_id, name.lower()),
        )

    def get_custom_command_by_id(self, cc_id: int) -> Optional[Dict]:
        """Obtiene un custom command por su ID."""
        return self._fetchone("SELECT * FROM custom_commands WHERE id = ?", (cc_id,))

    def get_enabled_custom_commands(
        self, guild_id: int, trigger_type: str = None
    ) -> List[Dict]:
        """Retorna CCs habilitados, opcionalmente filtrados por tipo de trigger."""
        if trigger_type:
            return self._fetchall(
                "SELECT * FROM custom_commands WHERE guild_id = ? AND enabled = 1 AND trigger_type = ?",
                (guild_id, trigger_type),
            )
        return self._fetchall(
            "SELECT * FROM custom_commands WHERE guild_id = ? AND enabled = 1",
            (guild_id,),
        )

    def create_custom_command(
        self,
        guild_id: int,
        name: str,
        trigger_type: str,
        trigger_value: str,
        conditions: str,
        actions: str,
        creator_id: int,
    ) -> Optional[Dict]:
        """Crea un nuevo custom command y retorna el registro."""
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            "INSERT INTO custom_commands (guild_id, name, trigger_type, trigger_value, "
            "conditions, actions, creator_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                guild_id,
                name.lower(),
                trigger_type,
                trigger_value,
                conditions,
                actions,
                creator_id,
                now,
            ),
        )
        return self.get_custom_command(guild_id, name)

    def update_custom_command(self, guild_id: int, name: str, **kwargs) -> None:
        """Actualiza campos de un custom command."""
        valid = frozenset(
            {
                "enabled",
                "trigger_type",
                "trigger_value",
                "conditions",
                "actions",
                "uses",
                "last_used",
                "permission_data",
                "delete_invocation",
                "response_data",
            }
        )
        invalid = set(kwargs) - valid
        if invalid:
            raise ValueError(f"Columnas inválidas en custom_commands: {invalid}")
        ops = [
            (
                f"UPDATE custom_commands SET {col} = ? WHERE guild_id = ? AND name = ?",
                (val, guild_id, name.lower()),
            )
            for col, val in kwargs.items()
        ]
        self._executemany(ops)

    def delete_custom_command(self, guild_id: int, name: str) -> None:
        """Elimina un custom command."""
        self._execute(
            "DELETE FROM custom_commands WHERE guild_id = ? AND name = ?",
            (guild_id, name.lower()),
        )

    def increment_cc_uses(self, guild_id: int, name: str) -> None:
        """Incrementa el contador de usos y actualiza last_used."""
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            "UPDATE custom_commands SET uses = uses + 1, last_used = ? WHERE guild_id = ? AND name = ?",
            (now, guild_id, name.lower()),
        )

    # ── CC Variables (persistentes) ───────────────────────────────────────────

    def get_cc_variable(
        self, guild_id: int, key: str, scope: str = "guild"
    ) -> Optional[str]:
        """Obtiene el valor de una variable. Retorna None si no existe."""
        row = self._fetchone(
            "SELECT value FROM cc_variables WHERE guild_id = ? AND key = ? AND scope = ?",
            (guild_id, key, scope),
        )
        return row["value"] if row else None

    def set_cc_variable(
        self, guild_id: int, key: str, value: str, scope: str = "guild"
    ) -> None:
        """Crea o actualiza una variable persistente."""
        if self.db_type == "sqlite":
            self._execute(
                "INSERT INTO cc_variables (guild_id, key, value, scope) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id, key, scope) DO UPDATE SET value = ?",
                (guild_id, key, value, scope, value),
            )
        elif self.db_type == "postgresql":
            self._execute(
                "INSERT INTO cc_variables (guild_id, key, value, scope) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (guild_id, key, scope) DO UPDATE SET value = EXCLUDED.value",
                (guild_id, key, value, scope),
            )
        else:
            self._execute(
                "INSERT INTO cc_variables (guild_id, `key`, value, scope) VALUES (?, ?, ?, ?) "
                "ON DUPLICATE KEY UPDATE value = VALUES(value)",
                (guild_id, key, value, scope),
            )

    def get_all_cc_variables(self, guild_id: int) -> List[Dict]:
        """Retorna todas las variables de un servidor."""
        return self._fetchall(
            "SELECT * FROM cc_variables WHERE guild_id = ? ORDER BY key ASC",
            (guild_id,),
        )

    def delete_cc_variable(self, guild_id: int, key: str, scope: str = "guild") -> None:
        """Elimina una variable persistente."""
        self._execute(
            "DELETE FROM cc_variables WHERE guild_id = ? AND key = ? AND scope = ?",
            (guild_id, key, scope),
        )

    def increment_cc_variable(
        self, guild_id: int, key: str, amount: int = 1, scope: str = "guild"
    ) -> str:
        """Incrementa una variable numérica y retorna el nuevo valor."""
        current = self.get_cc_variable(guild_id, key, scope)
        try:
            new_val = str(int(current or "0") + amount)
        except ValueError:
            new_val = str(amount)
        self.set_cc_variable(guild_id, key, new_val, scope)
        return new_val

    # ═══════════════════════════════════════════════════════════════════════════
    # VOICE GEN — Generador dinámico de canales de voz (Join To Create)
    # ═══════════════════════════════════════════════════════════════════════════

    def _ensure_voice_gen_tables(self) -> None:
        """
        Crea las tablas del Generador de VCs si no existen y añade columnas
        nuevas a tablas viejas vía ensure_column (idempotente).
        """
        self._execute("""
            CREATE TABLE IF NOT EXISTS voice_gen_config (
                guild_id             INTEGER PRIMARY KEY,
                generator_channel_id INTEGER,
                category_id          INTEGER,
                panel_channel_id     INTEGER,
                name_template        TEXT    DEFAULT '{username}''s VC',
                default_limit        INTEGER DEFAULT 0,
                enabled              INTEGER DEFAULT 0,
                panel_title          TEXT,
                panel_description    TEXT,
                panel_color          TEXT,
                auto_send_panel      INTEGER DEFAULT 1,
                panel_message_channel_id BIGINT,
                panel_message_id     BIGINT
            )
        """)
        # Migración no destructiva para BBDDs creadas antes de Fase 6.
        try:
            self.ensure_column("voice_gen_config", "panel_title", "TEXT")
            self.ensure_column("voice_gen_config", "panel_description", "TEXT")
            self.ensure_column("voice_gen_config", "panel_color", "TEXT")
            self.ensure_column("voice_gen_config", "auto_send_panel", "INTEGER DEFAULT 1")
            # JSON serializado con la forma de MessageEditor (content + embed).
            # Si está presente toma precedencia sobre las columnas sueltas.
            self.ensure_column("voice_gen_config", "panel_embed_data", "TEXT")
            # Referencia canónica del único panel permanente del servidor.
            self.ensure_column("voice_gen_config", "panel_message_channel_id", "BIGINT")
            self.ensure_column("voice_gen_config", "panel_message_id", "BIGINT")
        except Exception as e:
            logger.warning("ensure_column en voice_gen_config falló: %s", e)
        self._execute("""
            CREATE TABLE IF NOT EXISTS voice_gen_channels (
                channel_id   INTEGER PRIMARY KEY,
                guild_id     INTEGER NOT NULL,
                owner_id     INTEGER NOT NULL,
                panel_channel_id INTEGER,
                panel_message_id INTEGER,
                created_at   INTEGER DEFAULT (strftime('%s', 'now'))
            )
        """)
        try:
            self.ensure_column("voice_gen_channels", "panel_channel_id", "INTEGER")
            self.ensure_column("voice_gen_channels", "panel_message_id", "INTEGER")
        except Exception as e:
            logger.warning("ensure_column en voice_gen_channels falló: %s", e)

    def get_voice_gen_config(self, guild_id: int) -> Dict:
        """Retorna la configuración del generador de voz de un servidor."""
        self._ensure_voice_gen_tables()
        row = self._fetchone(
            "SELECT * FROM voice_gen_config WHERE guild_id = ?", (guild_id,)
        )
        return (
            dict(row)
            if row
            else {
                "guild_id": guild_id,
                "generator_channel_id": None,
                "category_id": None,
                "panel_channel_id": None,
                "name_template": "{username}'s VC",
                "default_limit": 0,
                "enabled": 0,
                "panel_message_channel_id": None,
                "panel_message_id": None,
            }
        )

    def set_voice_gen_config(self, guild_id: int, **kwargs) -> None:
        """Crea o actualiza la configuración del generador de voz."""
        self._ensure_voice_gen_tables()
        self._upsert_config("voice_gen_config", guild_id, **kwargs)

    def create_voice_gen_channel(
        self, channel_id: int, guild_id: int, owner_id: int
    ) -> None:
        """Registra un canal de voz generado."""
        self._ensure_voice_gen_tables()
        self._execute(
            "INSERT OR REPLACE INTO voice_gen_channels (channel_id, guild_id, owner_id) VALUES (?, ?, ?)",
            (channel_id, guild_id, owner_id),
        )

    def get_voice_gen_channel(self, channel_id: int) -> Optional[Dict]:
        """Retorna un canal generado por su ID, o None si no existe."""
        self._ensure_voice_gen_tables()
        row = self._fetchone(
            "SELECT * FROM voice_gen_channels WHERE channel_id = ?", (channel_id,)
        )
        return dict(row) if row else None

    def get_all_voice_gen_channels(self) -> List[Dict]:
        """Retorna todos los canales generados activos."""
        self._ensure_voice_gen_tables()
        return self._fetchall("SELECT * FROM voice_gen_channels")

    def get_voice_gen_channels_by_guild(self, guild_id: int) -> List[Dict]:
        """Retorna los canales generados de un servidor."""
        self._ensure_voice_gen_tables()
        return self._fetchall(
            "SELECT * FROM voice_gen_channels WHERE guild_id = ? ORDER BY created_at DESC",
            (guild_id,),
        )

    def update_voice_gen_channel_owner(
        self, channel_id: int, new_owner_id: int
    ) -> None:
        """Cambia el dueño de un canal generado."""
        self._ensure_voice_gen_tables()
        self._execute(
            "UPDATE voice_gen_channels SET owner_id = ? WHERE channel_id = ?",
            (new_owner_id, channel_id),
        )

    def update_voice_gen_channel_panel(
        self,
        channel_id: int,
        panel_channel_id: Optional[int],
        panel_message_id: Optional[int],
    ) -> bool:
        """Vincula el mensaje de control con su canal de voz generado."""
        self._ensure_voice_gen_tables()
        return self._execute_rowcount(
            "UPDATE voice_gen_channels SET panel_channel_id = ?, panel_message_id = ? "
            "WHERE channel_id = ?",
            (panel_channel_id, panel_message_id, channel_id),
        ) > 0

    def delete_voice_gen_channel(self, channel_id: int) -> None:
        """Elimina el registro de un canal generado."""
        self._ensure_voice_gen_tables()
        self._execute(
            "DELETE FROM voice_gen_channels WHERE channel_id = ?", (channel_id,)
        )

    # ── Invites ───────────────────────────────────────────────────────────────

    def get_invite_config(self, guild_id: int) -> Dict:
        """Obtiene la configuración de invitaciones de un servidor."""
        row = self._fetchone(
            "SELECT * FROM invite_config WHERE guild_id = ?", (guild_id,)
        )
        return row or {"guild_id": guild_id, "channel_id": None, "enabled": 1}

    def set_invite_config(self, guild_id: int, **kwargs) -> None:
        """Crea o actualiza la configuración de invitaciones."""
        self._upsert_config("invite_config", guild_id, **kwargs)

    def record_invite(
        self, guild_id: int, inviter_id: int, invited_id: int, invite_code: str
    ) -> None:
        """Registra un evento de invitación (quién invitó a quién)."""
        from datetime import datetime, timezone

        try:
            self._execute(
                "INSERT OR REPLACE INTO invite_stats "
                "(guild_id, inviter_id, invited_id, invite_code, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    guild_id,
                    inviter_id,
                    invited_id,
                    invite_code,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        except Exception:
            pass

    def get_user_invite_count(self, guild_id: int, user_id: int) -> int:
        """Devuelve cuántas personas ha invitado un usuario en este servidor."""
        row = self._fetchone(
            "SELECT COUNT(*) as c FROM invite_stats "
            "WHERE guild_id = ? AND inviter_id = ?",
            (guild_id, user_id),
        )
        return row["c"] if row else 0

    def get_invite_leaderboard(self, guild_id: int, limit: int = 10) -> List[Dict]:
        """Devuelve los top inviters de un servidor ordenados por total de invitaciones."""
        return self._fetchall(
            "SELECT inviter_id, COUNT(*) as total "
            "FROM invite_stats WHERE guild_id = ? "
            "GROUP BY inviter_id ORDER BY total DESC LIMIT ?",
            (guild_id, limit),
        )

    # ── AutoMod ───────────────────────────────────────────────────────────────

    DEFAULT_AUTOMOD_RULES: Dict = {
        "llm": DEFAULT_LLM_CONFIG.copy(),
        "spam": {
            "enabled": False,
            "warn_threshold": 5, "warn_timeframe": 10,
            "mute_threshold": 10, "mute_timeframe": 30, "mute_duration": 3600,
            "kick_threshold": 15, "kick_timeframe": 60,
            "ban_threshold": 20, "ban_timeframe": 60,
        },
        "mentions": {
            "enabled": False,
            "max_mentions": 5,
            "action": "warn",
        },
        "caps": {
            "enabled": False,
            "min_length": 15,
            "min_percent": 70,
            "action": "warn",
        },
        "links": {
            "enabled": False,
            "action": "warn",
            "whitelist": [],
            "block_invites": True,
        },
        "banned_words": {
            "enabled": False,
            "words": [],
            "action": "warn",
        },
        "duplicate": {
            "enabled": False,
            "threshold": 3,
            "window": 30,
            "action": "warn",
        },
        "emoji_spam": {
            "enabled": False,
            "max_emojis": 10,
            "action": "warn",
        },
        "ignored": {
            "channels": [],
            "roles": [],
            "exempt_admins": True,
            "exempt_bots": True,
        },
    }

    def _normalize_automod_rules(self, rules: Any) -> Dict:
        """Acepta reglas canónicas o payload legacy del dashboard y las normaliza."""
        if rules is None:
            rules = {}
        if isinstance(rules, str):
            try:
                rules = json.loads(rules or "{}")
            except json.JSONDecodeError:
                rules = {}
        if not isinstance(rules, dict):
            rules = {}

        normalized: Dict[str, Dict] = {}
        for key, defaults in self.DEFAULT_AUTOMOD_RULES.items():
            normalized[key] = defaults.copy()

        for key, value in rules.items():
            if key in {"words", "bannedWords", "banned_words"}:
                target = "banned_words"
            elif key in {"invites", "anti_invites", "antiInvites"}:
                target = "links"
            else:
                target = key
            if target not in normalized or not isinstance(value, dict):
                continue

            rule = value.copy()
            if target == "caps" and "max_caps_percent" in rule and "min_percent" not in rule:
                rule["min_percent"] = rule.pop("max_caps_percent")
            if target == "duplicate" and "duplicate_threshold" in rule and "threshold" not in rule:
                rule["threshold"] = rule.pop("duplicate_threshold")
            if key == "spam" and "duplicate_threshold" in rule:
                duplicate = normalized["duplicate"].copy()
                duplicate.update({
                    "enabled": bool(rule.get("enabled", True)),
                    "threshold": rule.pop("duplicate_threshold"),
                    "action": rule.get("action", duplicate.get("action", "warn")),
                })
                normalized["duplicate"] = duplicate
            if target == "links" and key in {"invites", "anti_invites", "antiInvites"}:
                rule.setdefault("block_invites", True)
                rule.setdefault("block_all_urls", False)

            merged = normalized[target].copy()
            merged.update(rule)
            normalized[target] = merged

        return normalized

    def get_automod_config(self, guild_id: int) -> Dict:
        row = self._fetchone(
            "SELECT * FROM automod_config WHERE guild_id = ?", (guild_id,)
        )
        if row:
            rules = self._normalize_automod_rules(row.get("rules") or {})
            return {
                "guild_id": guild_id,
                "enabled": int(row.get("enabled", 1) or 0),
                "rules": rules,
            }
        return {
            "guild_id": guild_id,
            "enabled": 1,
            "rules": {k: v.copy() for k, v in self.DEFAULT_AUTOMOD_RULES.items()},
        }

    def set_automod_config(self, guild_id: int, enabled: int = None, rules: Dict = None) -> None:
        current = self.get_automod_config(guild_id)
        if enabled is not None:
            current["enabled"] = int(enabled)
        if rules is not None:
            normalized = self._normalize_automod_rules(rules)
            for key, defaults in self.DEFAULT_AUTOMOD_RULES.items():
                if key == "llm":
                    if key in rules:
                        from llm_moderation_config import validate_llm_config
                        current["rules"][key] = validate_llm_config(rules[key], current["rules"].get(key))
                    continue
                if set(rules) == {"llm"}:
                    continue
                merged = defaults.copy()
                merged.update(normalized.get(key, {}))
                current["rules"][key] = merged
        self._upsert_config("automod_config", guild_id,
                            enabled=current["enabled"],
                            rules=json.dumps(current["rules"], ensure_ascii=False))

    def log_automod_action(self, guild_id: int, rule: str, user_id: int,
                           content: str, action_taken: str, *, extra=None) -> None:
        if action_taken not in {"delete", "warn", "mute", "kick", "ban"}:
            return
        from datetime import datetime, timezone
        self._execute(
            "INSERT INTO automod_log (guild_id, rule, user_id, content, action_taken, created_at, extra_data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, rule, user_id, content[:500] if content else None,
             action_taken, datetime.now(timezone.utc).isoformat(),
             json.dumps(extra, ensure_ascii=False) if extra is not None else None),
        )

    def get_ai_moderation_context(self, guild_id: int, channel_id: int, requester_id: int, limit: int = 8,
                                  *, guild_scope: bool = False, include_private: bool = False) -> List[Dict]:
        """Bounded target records; the caller must freshly authorize guild/private scope."""
        limit = max(1, min(int(limit), 20))
        rows = []
        for source, table, target, action in (
            ("automod", "automod_log", "user_id", "action_taken"),
            ("moderation", "mod_actions", "target_id", "action_type"),
        ):
            records = self._fetchall(
                f"SELECT * FROM {table} WHERE guild_id = ? AND {target} = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                (guild_id, requester_id, 100),
            )
            for record in records:
                try:
                    extra = json.loads(record.get("extra_data") or "{}")
                    if not isinstance(extra, dict):
                        continue
                    captured_channel = extra.get("channel_id")
                    captured_channel = str(captured_channel) if str(captured_channel or "").isdigit() else None
                    channel_verified = captured_channel == str(channel_id)
                    if captured_channel in (None, "") and source == "moderation":
                        evidence = urlsplit(record.get("evidence_url") or "")
                        parts = evidence.path.strip("/").split("/")
                        channel_verified = (evidence.scheme == "https" and evidence.netloc == "discord.com"
                            and len(parts) == 4 and parts[:2] == ["channels", str(guild_id)]
                            and parts[2].isdigit() and parts[3].isdigit() and not evidence.query and not evidence.fragment)
                        if channel_verified:
                            captured_channel = parts[2]
                        channel_verified = captured_channel == str(channel_id)
                    if not channel_verified and not guild_scope:
                        continue
                    if str(record[target]) != str(requester_id):
                        continue
                    if source == "automod" and record[action] not in {"delete", "warn", "mute", "kick", "ban"}:
                        continue
                    public_at_capture = extra.get("public_at_capture") if type(extra.get("public_at_capture")) is bool else None
                    reason = extra.get("reason") or record.get("reason") or ""
                    if not reason and include_private and source == "automod":
                        legacy = str(record.get("content") or "")
                        if re.match(r"(?:LLM )?message:\d+\s+Motivo: ", legacy):
                            reason = legacy
                    rows.append({
                        "id": record["id"], "source": source, "user_id": str(record[target]),
                        "channel_id": captured_channel,
                        "action": str(record[action])[:50], "rule": str(extra.get("rule") or extra.get("rule_key") or record.get("rule") or "")[:150] if public_at_capture is not None or include_private else "",
                        "reason": str(reason)[:1000] if public_at_capture is not None or include_private else "",
                        "message_id": str(extra.get("message_id") or ""),
                        "created_at": record["created_at"], "status": record.get("status", "executed"),
                        "public_at_capture": public_at_capture, "channel_verified": channel_verified,
                    })
                except (TypeError, ValueError, KeyError):
                    continue
        rows.sort(key=lambda row: (row["created_at"], row["id"]), reverse=True)
        return rows[:limit]

    def has_llm_message_action(self, guild_id: int, message_id: int) -> bool:
        """A message already sanctioned (or applying) must not be sanctioned again."""
        return bool(self._fetchone(
            "SELECT id FROM mod_actions WHERE guild_id = ? AND reason LIKE ? LIMIT 1",
            (guild_id, f"LLM message:{int(message_id)} %"),
        ))

    def get_automod_log(self, guild_id: int, limit: int = 50) -> List[Dict]:
        return self._fetchall(
            "SELECT * FROM automod_log WHERE guild_id = ? "
            "AND action_taken IN ('delete', 'warn', 'mute', 'kick', 'ban') "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (guild_id, limit),
        )

    # ── Suggestions ────────────────────────────────────────────────────────────

    def get_all_suggestions(self, guild_id: int, status: str = None) -> List[Dict]:
        """Devuelve sugerencias de un servidor, opcionalmente filtradas por estado."""
        if status:
            return self._fetchall(
                "SELECT * FROM suggestions WHERE guild_id = ? AND status = ? "
                "ORDER BY created_at DESC",
                (guild_id, status),
            )
        return self._fetchall(
            "SELECT * FROM suggestions WHERE guild_id = ? "
            "ORDER BY created_at DESC LIMIT 50",
            (guild_id,),
        )

    # ── Estado persistente de Starboard ──────────────────────────────────────

    def get_starboard_entry(self, guild_id: int, source_message_id: int) -> Optional[Dict]:
        return self._fetchone(
            "SELECT * FROM starboard_entries WHERE guild_id = ? AND source_message_id = ?",
            (guild_id, source_message_id),
        )

    def upsert_starboard_entry(
        self,
        guild_id: int,
        source_message_id: int,
        source_channel_id: int,
        starboard_message_id: int,
        star_count: int,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if self.get_starboard_entry(guild_id, source_message_id):
            self._execute(
                "UPDATE starboard_entries SET source_channel_id = ?, starboard_message_id = ?, "
                "star_count = ?, updated_at = ? WHERE guild_id = ? AND source_message_id = ?",
                (
                    source_channel_id,
                    starboard_message_id,
                    star_count,
                    now,
                    guild_id,
                    source_message_id,
                ),
            )
            return
        self._execute(
            "INSERT INTO starboard_entries (guild_id, source_message_id, source_channel_id, "
            "starboard_message_id, star_count, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                guild_id,
                source_message_id,
                source_channel_id,
                starboard_message_id,
                star_count,
                now,
            ),
        )

    def delete_starboard_entry(self, guild_id: int, source_message_id: int) -> None:
        self._execute(
            "DELETE FROM starboard_entries WHERE guild_id = ? AND source_message_id = ?",
            (guild_id, source_message_id),
        )

    # ── Menús de roles persistentes ─────────────────────────────────────────

    def save_role_menu(
        self,
        message_id: int,
        guild_id: int,
        channel_id: int,
        role_ids: List[int],
        placeholder: str,
        max_values: int,
        title: str,
        description: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        existing = self._fetchone("SELECT message_id FROM role_menus WHERE message_id = ?", (message_id,))
        payload = json.dumps([int(role_id) for role_id in role_ids])
        if existing:
            self._execute(
                "UPDATE role_menus SET guild_id = ?, channel_id = ?, role_ids = ?, placeholder = ?, "
                "max_values = ?, title = ?, description = ? WHERE message_id = ?",
                (guild_id, channel_id, payload, placeholder, max_values, title, description, message_id),
            )
            return
        self._execute(
            "INSERT INTO role_menus (message_id, guild_id, channel_id, role_ids, placeholder, "
            "max_values, title, description, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message_id, guild_id, channel_id, payload, placeholder, max_values, title, description, now),
        )

    def get_role_menus(self, guild_id: Optional[int] = None) -> List[Dict]:
        if guild_id is None:
            rows = self._fetchall("SELECT * FROM role_menus ORDER BY created_at")
        else:
            rows = self._fetchall(
                "SELECT * FROM role_menus WHERE guild_id = ? ORDER BY created_at", (guild_id,)
            )
        for row in rows:
            try:
                row["role_ids"] = [int(value) for value in json.loads(row.get("role_ids") or "[]")]
            except (TypeError, ValueError, json.JSONDecodeError):
                row["role_ids"] = []
        return rows

    def delete_role_menu(self, message_id: int, guild_id: Optional[int] = None) -> None:
        if guild_id is None:
            self._execute("DELETE FROM role_menus WHERE message_id = ?", (message_id,))
        else:
            self._execute(
                "DELETE FROM role_menus WHERE message_id = ? AND guild_id = ?",
                (message_id, guild_id),
            )

    # ── Recordatorios y lockdown anti-raid persistentes ──────────────────────

    def create_reminder(
        self,
        reminder_id: str,
        user_id: int,
        message: str,
        due_at: str,
        guild_id: Optional[int] = None,
    ) -> Optional[str]:
        """Crea un recordatorio o devuelve ``user``/``guild`` si se alcanzó la cuota."""
        with self._conn() as conn:
            if self.db_type == "postgresql":
                from psycopg2.extras import RealDictCursor

                cur = conn.cursor(cursor_factory=RealDictCursor)
            else:
                cur = conn.cursor()

            cur.execute(
                self._adapt(
                    "SELECT COUNT(*) AS total FROM persistent_reminders "
                    "WHERE user_id = ? AND status IN ('pending', 'processing')"
                ),
                (user_id,),
            )
            row = cur.fetchone()
            user_pending = int(row["total"] if hasattr(row, "keys") else row[0])
            if user_pending >= REMINDER_MAX_PENDING_PER_USER:
                return "user"

            if guild_id is not None:
                cur.execute(
                    self._adapt(
                        "SELECT COUNT(*) AS total FROM persistent_reminders "
                        "WHERE guild_id = ? AND status IN ('pending', 'processing')"
                    ),
                    (guild_id,),
                )
                row = cur.fetchone()
                guild_pending = int(row["total"] if hasattr(row, "keys") else row[0])
                if guild_pending >= REMINDER_MAX_PENDING_PER_GUILD:
                    return "guild"

            cur.execute(
                self._adapt(
                    "INSERT INTO persistent_reminders "
                    "(reminder_id, guild_id, user_id, message, due_at, status, attempts, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 'pending', 0, ?)"
                ),
                (
                    reminder_id,
                    guild_id,
                    user_id,
                    message,
                    due_at,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        return None

    def cleanup_terminal_reminders(self, cutoff_iso: str) -> int:
        return self._execute_rowcount(
            "DELETE FROM persistent_reminders WHERE status IN ('sent', 'failed') "
            "AND COALESCE(sent_at, due_at, created_at) < ?",
            (cutoff_iso,),
        )

    def recover_processing_reminders(self) -> int:
        return self._execute_rowcount(
            "UPDATE persistent_reminders SET status = 'pending' WHERE status = 'processing'"
        )

    def get_due_reminders(self, now_iso: str, limit: int = 50) -> List[Dict]:
        return self._fetchall(
            "SELECT * FROM persistent_reminders WHERE status = 'pending' AND due_at <= ? "
            "ORDER BY due_at LIMIT ?",
            (now_iso, limit),
        )

    def claim_reminder(self, reminder_id: str) -> bool:
        return self._execute_rowcount(
            "UPDATE persistent_reminders SET status = 'processing', attempts = attempts + 1 "
            "WHERE reminder_id = ? AND status = 'pending'",
            (reminder_id,),
        ) == 1

    def complete_reminder(self, reminder_id: str) -> None:
        self._execute(
            "UPDATE persistent_reminders SET status = 'sent', sent_at = ?, last_error = NULL "
            "WHERE reminder_id = ?",
            (datetime.now(timezone.utc).isoformat(), reminder_id),
        )

    def fail_reminder(self, reminder_id: str, error: str, *, retry: bool) -> None:
        self._execute(
            "UPDATE persistent_reminders SET status = ?, last_error = ? WHERE reminder_id = ?",
            ("pending" if retry else "failed", error[:1000], reminder_id),
        )

    def save_raid_lockdown(
        self,
        guild_id: int,
        original_verification_level: int,
        expires_at: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        existing = self.get_raid_lockdown(guild_id)
        if existing:
            return
        self._execute(
            "INSERT INTO raid_lockdowns (guild_id, original_verification_level, expires_at, "
            "status, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
            (guild_id, original_verification_level, expires_at, now, now),
        )

    def get_raid_lockdown(self, guild_id: int) -> Optional[Dict]:
        return self._fetchone(
            "SELECT * FROM raid_lockdowns WHERE guild_id = ? AND status = 'active'",
            (guild_id,),
        )

    def get_active_raid_lockdowns(self) -> List[Dict]:
        return self._fetchall("SELECT * FROM raid_lockdowns WHERE status = 'active'")

    def delete_raid_lockdown(self, guild_id: int) -> None:
        self._execute("DELETE FROM raid_lockdowns WHERE guild_id = ?", (guild_id,))
