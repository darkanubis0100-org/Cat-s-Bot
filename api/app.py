"""
api/app.py
──────────
Aplicación FastAPI principal — Bot ES.

Novedades v2:
  • Sirve el nuevo dashboard React desde new-dashboard/dist/
  • Registra /api/guilds (router nuevo del dashboard)
  • Fallback SPA: cualquier ruta bajo /panel/* → index.html
"""

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger("API")

BASE_DIR = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = BASE_DIR / "dashboard" / "dist"


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return url.rstrip("/")


def create_app(db=None, bot=None) -> FastAPI:
    app = FastAPI(
        title="Bot ES API",
        description="Backend REST para el panel web del Bot ES",
        version="2.0.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    # ── CORS ──────────────────────────────────────────────────────────────────
    dashboard_url = os.getenv("DASHBOARD_URL", "http://localhost:8080")
    api_base_url = os.getenv("API_BASE_URL", "http://localhost:8080")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(
            {
                _origin(dashboard_url),
                _origin(api_base_url),
                "http://localhost:3000",
                "http://localhost:5173",
                "http://localhost:8080",
            }
        ),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Inyectar DB y Bot ─────────────────────────────────────────────────────
    if db is not None:
        app.state.db = db
    if bot is not None:
        app.state.bot = bot
    app.state.started_at = datetime.now(timezone.utc)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        if exc.status_code == 403:
            logger.warning(
                "403 response path=%s method=%s detail=%s",
                request.url.path,
                request.method,
                exc.detail,
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None),
        )

    @app.middleware("http")
    async def log_api_mutations(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        if request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
            logger.info(
                "API mutation method=%s path=%s status=%s duration_ms=%.1f",
                request.method,
                request.url.path,
                response.status_code,
                (time.perf_counter() - started) * 1000,
            )
        return response

    # ── Registrar routers API ─────────────────────────────────────────────────
    from api.auth import router as auth_router
    from api.routes import (
        autoroles,
        autoresponses,
        channels,
        channel_settings,
        custom_commands,
        embeds,
        giveaways,
        moderation,
        radio,
        reports,
        schedules,
        tags,
        tickets,
        voice_gen,
    )
    from api.routes.embeds import _send_router as embeds_send_router
    from api.routes.guild import router as guilds_router
    from api.routes.guild import router_legacy

    app.include_router(auth_router)
    app.include_router(guilds_router)  # /api/guilds — nuevo dashboard
    app.include_router(router_legacy)  # /api/guild/{id} — compatibilidad
    app.include_router(embeds_send_router)  # /api/guilds/{id}/embeds/send
    app.include_router(moderation.router)
    app.include_router(moderation.cases_router)  # /api/moderation/{id}/cases
    app.include_router(tickets.router)
    app.include_router(tags.router)
    app.include_router(reports.router)
    app.include_router(schedules.router)
    app.include_router(giveaways.router)
    app.include_router(autoroles.router)
    app.include_router(radio.router)
    app.include_router(embeds.router)
    app.include_router(channels.router)
    app.include_router(channel_settings.router)
    app.include_router(voice_gen.router)
    app.include_router(autoresponses.router)
    app.include_router(custom_commands.router)

    from api.routes.emojis import router as emojis_router
    app.include_router(emojis_router)

    from api.routes.invites_route import router as invites_router
    from api.routes.suggestions_route import router as suggestions_router
    from api.routes.welcome import router as welcome_router
    from api.routes.ai_keys import (
        router_admin as ai_keys_admin_router,
        router_guild as ai_keys_guild_router,
    )
    from api.routes.public_stats import router as public_stats_router

    app.include_router(welcome_router)
    app.include_router(suggestions_router)
    app.include_router(invites_router)
    app.include_router(ai_keys_admin_router)  # /api/ai/keys/* (master admin)
    app.include_router(ai_keys_guild_router)  # /api/guilds/{id}/ia/key (guild admin)
    app.include_router(public_stats_router)  # /api/public/stats (sin auth)

    # ── Health-check ──────────────────────────────────────────────────────────
    @app.get("/api/health", tags=["health"])
    async def api_health():
        return {"status": "ok", "bot": "Cats Bots", "api": "v2.0.0"}

    # ── Servir SPA (Cats Bots) ─────────────────────────────────────────────────
    if DASHBOARD_DIR.is_dir():
        assets_dir = DASHBOARD_DIR / "assets"
        if assets_dir.is_dir():
            app.mount(
                "/assets",
                StaticFiles(directory=str(assets_dir)),
                name="spa-assets",
            )
            # Compat: builds previos referenciaban /panel/assets/*
            app.mount(
                "/panel/assets",
                StaticFiles(directory=str(assets_dir)),
                name="panel-assets-legacy",
            )

        icons_dir = DASHBOARD_DIR / "icons"
        if icons_dir.is_dir():
            app.mount(
                "/icons",
                StaticFiles(directory=str(icons_dir)),
                name="spa-icons",
            )
            app.mount(
                "/panel/icons",
                StaticFiles(directory=str(icons_dir)),
                name="panel-icons",
            )

        index_file = DASHBOARD_DIR / "index.html"

        # Servir favicon y otros archivos estáticos top-level si existen
        def _make_static_handler(path: Path):
            async def _serve_static():
                return FileResponse(str(path))
            return _serve_static

        for static_name in (
            "favicon.svg",
            "favicon.ico",
            "logo.png",
            "logo.svg",
            "robots.txt",
            "sitemap.xml",
            "site.webmanifest",
        ):
            candidate = DASHBOARD_DIR / static_name
            if candidate.is_file():
                for route_path in (f"/{static_name}", f"/panel/{static_name}"):
                    app.add_api_route(
                        route_path,
                        _make_static_handler(candidate),
                        methods=["GET"],
                        include_in_schema=False,
                    )

        async def _serve_index(_request: Request = None):
            if index_file.is_file():
                return FileResponse(str(index_file))
            return {"error": "SPA no compilada. Ejecuta: cd dashboard && npm run build"}

        @app.get("/", include_in_schema=False)
        async def serve_root():
            return await _serve_index()

        @app.get("/auth/callback", include_in_schema=False)
        async def serve_auth_callback():
            return await _serve_index()

        @app.get("/panel", include_in_schema=False)
        async def serve_panel_root():
            return await _serve_index()

        @app.get("/panel/{full_path:path}", include_in_schema=False)
        async def serve_panel(full_path: str, request: Request):
            return await _serve_index()

        # SPA fallback final — cualquier path no /api/* ni /assets/* sirve index.
        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_spa(full_path: str, request: Request):
            if full_path.startswith("api/") or full_path.startswith("assets/"):
                raise HTTPException(404, "Not found")
            return await _serve_index()

        logger.info("✅ SPA Cats Bots disponible en /")
    else:
        logger.warning(
            "⚠️  dashboard/dist/ no encontrado. Ejecuta: cd dashboard && npm run build"
        )

    return app


def iniciar_api(db=None, bot=None, host: str = "0.0.0.0", port: int = 8080) -> None:
    """Arranca el servidor FastAPI en un hilo daemon."""
    import uvicorn

    app = create_app(db=db, bot=bot)

    def _run():
        uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)

    t = Thread(target=_run, daemon=True)
    t.start()
    logger.info(f"API FastAPI iniciada en http://{host}:{port}")
    logger.info(f"Panel disponible en http://{host}:{port}/panel/")
