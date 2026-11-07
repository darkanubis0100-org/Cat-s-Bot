import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from api.app import create_app
from main import BotES


class RAGRetirementTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_ignores_enabled_legacy_archive(self):
        with tempfile.TemporaryDirectory() as tmp, chdir(tmp):
            workspace = Path("workspaces/123")
            workspace.mkdir(parents=True)
            config = workspace / "config.yaml"
            config.write_text("enabled: true\nbackfill_enabled: true\n", encoding="utf-8")
            archive = workspace / "raw.sqlite"
            archive.write_bytes(b"existing private archive")
            before = {path: path.read_bytes() for path in workspace.iterdir()}
            bot = SimpleNamespace(
                loop=Mock(),
                tree=SimpleNamespace(sync=AsyncMock(return_value=[])),
                on_app_command_error=Mock(),
                load_extension=AsyncMock(),
            )

            with patch("discord.ext.tasks.Loop.start"):
                await BotES.setup_hook(bot)

            extensions = {call.args[0] for call in bot.load_extension.await_args_list}
            self.assertNotIn("cogs.rag_scraper", extensions)
            self.assertTrue({"cogs.ia", "cogs.moderation", "cogs.logging"} <= extensions)
            self.assertFalse(hasattr(bot, "rag_workspace"))
            self.assertEqual(before, {path: path.read_bytes() for path in workspace.iterdir()})

    async def test_archive_api_is_unavailable_and_core_routes_remain(self):
        app = create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for method, suffix, body in (
                ("GET", "/status", None),
                ("PATCH", "/config", {"enabled": True, "backfill_enabled": True}),
                ("POST", "/backfill", {}),
                ("POST", "/export", {"format": "jsonl", "export_all": True}),
            ):
                with self.subTest(method=method, suffix=suffix):
                    response = await client.request(method, f"/api/guilds/123/rag{suffix}", json=body)
                    self.assertIn(response.status_code, (404, 405))
            self.assertEqual((await client.get("/api/health")).status_code, 200)
        paths = app.openapi()["paths"]
        self.assertIn("/api/guilds/{guild_id}/ia/memory", paths)
        self.assertIn("/api/moderation/{guild_id}/cases", paths)
        self.assertFalse(hasattr(app.state, "rag_workspace"))


if __name__ == "__main__":
    unittest.main()
