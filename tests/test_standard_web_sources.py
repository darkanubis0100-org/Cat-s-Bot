"""Per-guild standard retrieval configuration, without network or live bot state."""
import inspect
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from api.deps import require_guild_admin
from api.routes.guild import get_ia_config, patch_ia_config
from tests.test_conversation_memory import database


class StandardWebSourcesConfigTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = database()

    async def asyncTearDown(self):
        self.db._sqlite_conn.close()

    async def test_default_and_admin_roundtrip_are_guild_scoped_and_preserve_other_settings(self):
        self.assertEqual((await get_ia_config(10, self.db, {}))["ai_web_max_sources"], 3)
        self.assertEqual((await get_ia_config(10, self.db, {}))["ai_web_timeout_seconds"], 120)
        self.db.set_ai_config(10, ai_deep_think_daily_limit=7, ai_restricted_role_ids=["44"])
        for value in (1, 20):
            result = await patch_ia_config(10, {"ai_web_max_sources": value}, None, self.db, None, {})
            self.assertEqual(result["ai_web_max_sources"], value)
            self.assertEqual(result["ai_deep_think_daily_limit"], 7)
            self.assertEqual(result["ai_restricted_role_ids"], ["44"])
            self.assertEqual((await get_ia_config(10, self.db, {}))["ai_web_max_sources"], value)
        for seconds in (30, 600):
            result = await patch_ia_config(10, {"ai_web_timeout_seconds": seconds}, None, self.db, None, {})
            self.assertEqual(result["ai_web_timeout_seconds"], seconds)
            self.assertEqual((await get_ia_config(10, self.db, {}))["ai_web_timeout_seconds"], seconds)
            self.assertEqual(result["ai_deep_think_daily_limit"], 7)
            self.assertEqual(result["ai_web_max_sources"], 20)
        self.assertEqual(self.db.get_ai_config(11)["ai_web_max_sources"], 3)
        self.assertEqual(self.db.get_ai_config(11)["ai_web_timeout_seconds"], 120)
        self.assertIs(inspect.signature(get_ia_config).parameters["_user"].default.dependency, require_guild_admin)
        self.assertIs(inspect.signature(patch_ia_config).parameters["user"].default.dependency, require_guild_admin)

    async def test_db_and_api_reject_nonintegers_and_out_of_range_without_mutation(self):
        self.db.set_ai_config(10, ai_web_max_sources=5)
        before = self.db.get_ai_config(10)
        for value in (0, -1, 21, True, False, 3.0, "3", None, [], {}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.db.set_ai_config(10, ai_enabled=0, ai_web_max_sources=value)
                with self.assertRaises(HTTPException) as caught:
                    await patch_ia_config(10, {"ai_enabled": 0, "ai_web_max_sources": value}, None, self.db, None, {})
                self.assertEqual(caught.exception.status_code, 400)
                self.assertEqual(self.db.get_ai_config(10), before)

        for value in (29, 601, 0, -1, True, False, 120.0, "120", None, [], {}):
            with self.subTest(timeout=value):
                with self.assertRaises(ValueError):
                    self.db.set_ai_config(10, ai_enabled=0, ai_web_timeout_seconds=value)
                with self.assertRaises(HTTPException) as caught:
                    await patch_ia_config(10, {"ai_enabled": 0, "ai_web_timeout_seconds": value}, None, self.db, None, {})
                self.assertEqual(caught.exception.status_code, 400)
                self.assertEqual(self.db.get_ai_config(10), before)

    async def test_persistence_and_additive_legacy_migration_keep_existing_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.db"
            db = database(path)
            try:
                db.set_ai_config(10, ai_web_max_sources=8, ai_deep_think=1, ai_web_timeout_seconds=240)
            finally:
                db._sqlite_conn.close()
            reopened = database(path)
            try:
                self.assertEqual(reopened.get_ai_config(10)["ai_web_max_sources"], 8)
                self.assertEqual(reopened.get_ai_config(10)["ai_deep_think"], 1)
                self.assertEqual(reopened.get_ai_config(10)["ai_web_timeout_seconds"], 240)
            finally:
                reopened._sqlite_conn.close()
        legacy = database()
        try:
            legacy._sqlite_conn.execute("DROP TABLE ai_config")
            legacy._sqlite_conn.executescript("CREATE TABLE ai_config (guild_id INTEGER PRIMARY KEY, ai_enabled INTEGER); INSERT INTO ai_config VALUES (10, 0);")
            legacy._migrate_ai_config()
            legacy._migrate_ai_config()
            self.assertEqual(legacy.get_ai_config(10)["ai_web_max_sources"], 3)
            self.assertEqual(legacy.get_ai_config(10)["ai_enabled"], 0)
            self.assertEqual(legacy.get_ai_config(10)["ai_web_timeout_seconds"], 120)
            legacy._sqlite_conn.execute("UPDATE ai_config SET ai_web_timeout_seconds = NULL WHERE guild_id = 10")
            self.assertEqual(legacy.get_ai_config(10)["ai_web_timeout_seconds"], 120)
        finally:
            legacy._sqlite_conn.close()
