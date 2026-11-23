import logging
import asyncio
import hashlib
import json
import re
import time
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit

import requests

import discord
from discord.ext import commands, tasks
from discord import app_commands

from radio_url_policy import (
    RadioURLPolicyError,
    redact_url_for_log,
    validate_public_http_url,
)

logger = logging.getLogger(__name__)

LOFI_STREAM_URL = "https://lofi.stream.laut.fm/lofi"
RADIO_API_URL = "https://de1.api.radio-browser.info/json/stations/search"

MAX_PLAYLIST_BYTES = 256 * 1024
MAX_PLAYLIST_URLS = 128
MAX_SEARCH_RESPONSE_BYTES = 256 * 1024
MAX_STREAM_FAILURES = 5
MAX_RECONNECT_DELAY_SECONDS = 300.0
HEALTHY_PLAYBACK_SECONDS = 30.0
MAX_STREAM_RESOLVER_WORKERS = 4
MAX_PENDING_STREAM_RESOLVES = 8
MAX_PENDING_STREAM_RESOLVES_PER_GUILD = 1
STREAM_RESOLVE_WAIT_SECONDS = 30.0


class _RadioBuffer(discord.AudioSource):
    """Reserva PCM de 300 ms; el reproductor nunca espera a la red."""

    FRAME_COUNT = 15
    SILENCE = bytes(discord.opus.Encoder.FRAME_SIZE)

    def __init__(self, original):
        self.original = original
        self._frames = queue.Queue(maxsize=self.FRAME_COUNT)
        self._stopped = threading.Event()
        self._finished = threading.Event()
        self._reader = None
        self._reader_error = None
        self._buffering = True
        self._played = False
        self._dry_since = None
        self._dry_peak = 0.0
        self._counts = {"startup_silence": 0, "underruns": 0, "silence_after_start": 0}

    def diagnostics(self, now):
        dry_since = self._dry_since
        dry = 0.0 if dry_since is None else now - dry_since
        return {**self._counts, "dry_peak_ms": round(max(self._dry_peak, dry) * 1000, 1)}

    def _silence(self, now):
        if self._played:
            if self._dry_since is None:
                self._counts["underruns"] += 1
                self._dry_since = now
            self._counts["silence_after_start"] += 1
        else:
            self._counts["startup_silence"] += 1
        return self.SILENCE

    @property
    def _current_error(self):
        return self._reader_error or getattr(self.original, "_current_error", None)

    def _read_frame(self):
        return self.original.read()

    def _fill(self):
        try:
            while not self._stopped.is_set():
                frame = self._read_frame()
                if not frame:
                    break
                while not self._stopped.is_set():
                    try:
                        self._frames.put(frame, timeout=0.1)
                        break
                    except queue.Full:
                        continue
        except Exception as exc:
            if not self._stopped.is_set():
                self._reader_error = exc
        finally:
            self._finished.set()

    def read(self) -> bytes:
        if self._stopped.is_set():
            return b""
        if self._reader is None:
            self._reader = threading.Thread(target=self._fill, name="radio-producer", daemon=True)
            self._reader.start()
        finished = self._finished.is_set()
        now = time.monotonic()
        if self._buffering:
            if self._frames.qsize() < self.FRAME_COUNT and not finished:
                return self._silence(now)
            self._buffering = False
        try:
            frame = self._frames.get_nowait()
            self._played = True
            if self._dry_since is not None:
                self._dry_peak = max(self._dry_peak, now - self._dry_since)
                self._dry_since = None
            return frame
        except queue.Empty:
            if finished:
                return b""
            # Tras arrancar, no ampliar un underrun aislado a otro prellenado.
            return self._silence(now)

    def cleanup(self):
        if self._stopped.is_set():
            return
        self._stopped.set()
        try:
            self.original.cleanup()
        finally:
            if self._reader is not None and self._reader is not threading.current_thread():
                self._reader.join(timeout=1.0)


class _RadioAudioSource(_RadioBuffer):
    """Codifica antes de la reserva: el hilo de envío sólo recibe Opus listo."""

    SILENCE = b"\xf8\xff\xfe"  # Silencio Opus de 20 ms usado por discord.py.

    def __init__(self, original, voice_client, *, volume: float):
        self._error_source = original
        if volume != 1.0:
            original = discord.PCMVolumeTransformer(original, volume=volume)
        super().__init__(original)
        self._voice_client = voice_client
        self._encoder = None
        self._complexity = None
        self.bitrate = 128  # Valor del SDK si la conexión aún no conoce el canal.
        self.bitrate = self._channel_bitrate()
        self._last_read_at = None
        self._reader_gap_peak = 0.0
        self._read_counts = {"reader_frames": 0, "reader_late": 0, "reader_burst": 0}
        self._diagnostics_at = time.monotonic()
        self._diagnostics_last = {}
        self._diagnostics_logged_at = None

    def report_diagnostics(self, guild_id):
        """Resumen desde el loop del bot; contadores acumulativos sin reset/hilo."""
        now = time.monotonic()
        elapsed = now - self._diagnostics_at
        if elapsed < 60.0:
            return
        counts = {**self.diagnostics(now), **self._read_counts}
        report = {
            key: value if key.endswith("_ms") else value - self._diagnostics_last.get(key, 0)
            for key, value in counts.items()
        }
        # Los picos son de esta reproducción; los contadores son del intervalo.
        report["reader_gap_peak_ms"] = round(self._reader_gap_peak * 1000, 1)
        report["encoder_complexity"] = self._complexity if self._complexity is not None else "default"
        anomalous = any(report.get(key, 0) for key in ("underruns", "silence_after_start", "reader_late", "reader_burst"))
        # Salud al primer minuto y cada cinco; anomalías como máximo cada minuto.
        if anomalous or self._diagnostics_logged_at is None or now - self._diagnostics_logged_at >= 300:
            logger.info("[radio] Audio guild=%s window_s=%.1f stats=%s", guild_id, elapsed, report)
            self._diagnostics_logged_at = now
        self._diagnostics_at = now
        self._diagnostics_last = counts

    def _channel_bitrate(self) -> int:
        channel = getattr(self._voice_client, "channel", None)
        bits_per_second = getattr(channel, "bitrate", None)
        if bits_per_second is None:
            return self.bitrate
        # Discord usa bits/s; discord.py multiplica su parámetro por 1024.
        # Su encoder PCM admite 16..512, incluso si el canal permite sólo 8k.
        return max(16, min(512, bits_per_second // 1024))

    @property
    def _current_error(self):
        return self._reader_error or getattr(self._error_source, "_current_error", None)

    def is_opus(self) -> bool:
        return True

    @staticmethod
    def _configure_complexity(encoder) -> bool:
        """Ajuste opcional; el encoder sigue válido si el SDK no lo expone."""
        try:
            setter = getattr(encoder, "set_complexity", None)
            if callable(setter):
                return setter(5) is not False
            # discord.py 2.7.1 ya enlaza este CTL variádico de libopus.
            state_type = getattr(discord.opus, "EncoderStructPtr", None)
            state = getattr(encoder, "_state", None)
            ctl = getattr(getattr(discord.opus, "_lib", None), "opus_encoder_ctl", None)
            if not isinstance(state_type, type) or not isinstance(state, state_type) or not state or not callable(ctl):
                return False
            return ctl(state, 4010, 5) == 0  # OPUS_SET_COMPLEXITY_REQUEST
        except Exception:
            # Sólo falla esta optimización; no ocultar errores de encode/read.
            return False

    def _read_frame(self):
        frame = self.original.read()
        if not frame:
            return b""
        bitrate = self._channel_bitrate()
        if self._encoder is None:
            self._encoder = discord.opus.Encoder(
                application="audio", bitrate=bitrate, bandwidth="full",
                signal_type="music", fec=True, expected_packet_loss=.15,
            )
            if self._configure_complexity(self._encoder):
                self._complexity = 5
        elif bitrate != self.bitrate:
            self._encoder.set_bitrate(bitrate)
        self.bitrate = bitrate
        return self._encoder.encode(frame, discord.opus.Encoder.SAMPLES_PER_FRAME)

    def read(self) -> bytes:
        now = time.monotonic()
        # El SDK espera 40 ms después de su primera trama, luego usa 20 ms.
        # Pausas/reconexiones también pueden aparecer como huecos de lectura.
        if self._last_read_at is not None and self._read_counts["reader_frames"] > 1:
            gap = now - self._last_read_at
            self._reader_gap_peak = max(self._reader_gap_peak, gap)
            self._read_counts["reader_late"] += gap > .04
            self._read_counts["reader_burst"] += gap < .01
        self._last_read_at = now
        self._read_counts["reader_frames"] += 1
        return super().read()


class RadioStationSelectView(discord.ui.View):
    def __init__(self, bot: commands.Bot, author_id: int, guild_id: int):
        super().__init__(timeout=180)
        self.bot = bot
        self.author_id = author_id
        self.guild_id = guild_id

    async def _authorized(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id != self.guild_id or interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Solo quien realizó la búsqueda puede elegir la emisora.", ephemeral=True
            )
            return False
        guild = self.bot.get_guild(self.guild_id)
        if guild is None:
            await interaction.response.send_message(
                "❌ No se pudo verificar el servidor actual.", ephemeral=True
            )
            return False
        try:
            member, roles, live_guild = await asyncio.gather(
                guild.fetch_member(interaction.user.id),
                guild.fetch_roles(),
                self.bot.fetch_guild(self.guild_id),
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message(
                "❌ No se pudieron verificar tus permisos actuales.", ephemeral=True
            )
            return False
        if getattr(live_guild, "id", None) != self.guild_id:
            await interaction.response.send_message(
                "❌ La identidad actual del servidor no coincide.", ephemeral=True
            )
            return False
        roles_by_id = {int(role.id): role for role in roles}
        role_ids = {self.guild_id}
        role_ids.update(int(role_id) for role_id in getattr(member, "_roles", ()))
        if any(role_id not in roles_by_id for role_id in role_ids):
            await interaction.response.send_message(
                "❌ No se pudo verificar tu jerarquía completa.", ephemeral=True
            )
            return False
        permissions_value = 0
        for role_id in role_ids:
            permissions_value |= int(roles_by_id[role_id].permissions.value)
        if member.id != int(live_guild.owner_id) and not (
            permissions_value & 0x8
        ):
            await interaction.response.send_message(
                "❌ Debes seguir siendo administrador para cambiar la radio.", ephemeral=True
            )
            return False
        return True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self._authorized(interaction)


class Radio(commands.Cog):
    """Módulo de Radio Global 24/7 con búsqueda de emisoras."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db # type: ignore
        self._playback_wait = 1.0  # segundos a esperar tras parar una reproducción
        self._restart_in_progress: set[int] = set()
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._stream_sources: dict[int, str] = {}
        self._stream_failures: dict[int, int] = {}
        self._stream_blocked_until: dict[int, float] = {}
        self._resolver_executor = ThreadPoolExecutor(
            max_workers=MAX_STREAM_RESOLVER_WORKERS,
            thread_name_prefix="radio-resolver",
        )
        self._resolver_futures: set[asyncio.Future] = set()
        self._resolver_pending_by_guild: dict[int, int] = {}
        self.radio_manager.start()

    def cog_unload(self):
        self.radio_manager.cancel()
        self._resolver_executor.shutdown(wait=False, cancel_futures=True)

    async def _resolve_stream(self, guild_id: int, url: str) -> str:
        """Aísla resoluciones remotas y limita trabajos que un slow-drip retiene."""
        guild_id = int(guild_id)
        if (
            len(self._resolver_futures) >= MAX_PENDING_STREAM_RESOLVES
            or self._resolver_pending_by_guild.get(guild_id, 0)
            >= MAX_PENDING_STREAM_RESOLVES_PER_GUILD
        ):
            raise RadioURLPolicyError("Hay demasiadas resoluciones de radio pendientes.")

        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            self._resolver_executor,
            self._resolve_stream_sync,
            url,
        )
        self._resolver_futures.add(future)
        self._resolver_pending_by_guild[guild_id] = (
            self._resolver_pending_by_guild.get(guild_id, 0) + 1
        )

        def release_slot(done: asyncio.Future) -> None:
            self._resolver_futures.discard(done)
            remaining = self._resolver_pending_by_guild.get(guild_id, 1) - 1
            if remaining > 0:
                self._resolver_pending_by_guild[guild_id] = remaining
            else:
                self._resolver_pending_by_guild.pop(guild_id, None)

        future.add_done_callback(release_slot)
        try:
            return await asyncio.wait_for(
                asyncio.shield(future),
                timeout=STREAM_RESOLVE_WAIT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise RadioURLPolicyError(
                "La resolución de la radio excedió el tiempo permitido."
            ) from exc

    @tasks.loop(seconds=60)
    async def radio_manager(self):
        for guild in self.bot.guilds:
            try:
                await self._check_and_connect_guild(guild)
                vc = self._voice_client_for_guild(guild)
                source = getattr(vc, "source", None)
                if isinstance(source, _RadioAudioSource):
                    source.report_diagnostics(guild.id)
            except Exception:
                logger.exception("[radio] Fallo revisando la radio de guild %s", guild.id)

    @radio_manager.before_loop
    async def before_radio_manager(self):
        await self.bot.wait_until_ready()

    @staticmethod
    def _coerce_channel_id(value) -> int | None:
        if value in (None, "", 0, "0"):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _source_key(url: object) -> str:
        return hashlib.sha256(str(url or "").encode("utf-8", errors="replace")).hexdigest()

    def _sync_stream_source(self, guild_id: int, url: object) -> None:
        guild_id = int(guild_id)
        source_key = self._source_key(url)
        if self._stream_sources.get(guild_id) != source_key:
            self._stream_sources[guild_id] = source_key
            self._stream_failures.pop(guild_id, None)
            self._stream_blocked_until.pop(guild_id, None)

    def _reset_stream_state(self, guild_id: int, url: object) -> None:
        guild_id = int(guild_id)
        self._stream_sources[guild_id] = self._source_key(url)
        self._stream_failures.pop(guild_id, None)
        self._stream_blocked_until.pop(guild_id, None)

    def _stream_retry_delay(self, guild_id: int, url: object) -> float | None:
        self._sync_stream_source(guild_id, url)
        failures = self._stream_failures.get(int(guild_id), 0)
        if failures >= MAX_STREAM_FAILURES:
            return None
        remaining = self._stream_blocked_until.get(int(guild_id), 0.0) - time.monotonic()
        return 0.0 if remaining <= 0.1 else remaining

    def _record_stream_failure(self, guild_id: int, url: object) -> tuple[int, float]:
        self._sync_stream_source(guild_id, url)
        guild_id = int(guild_id)
        failures = self._stream_failures.get(guild_id, 0) + 1
        self._stream_failures[guild_id] = failures
        delay = min(2.0 * (2 ** (failures - 1)), MAX_RECONNECT_DELAY_SECONDS)
        self._stream_blocked_until[guild_id] = time.monotonic() + delay
        return failures, delay

    @staticmethod
    def _open_public_response(
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        timeout: tuple[float, float] = (5.0, 15.0),
        max_redirects: int = 4,
        deadline: float | None = None,
    ):
        """Abre una URL validando DNS y cada salto sin redirección automática."""
        current = validate_public_http_url(url)
        current_params = params
        for redirect_count in range(max_redirects + 1):
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RadioURLPolicyError("La conexión de radio excedió el tiempo permitido.")
                request_timeout = (
                    min(timeout[0], max(0.5, remaining / 2)),
                    min(timeout[1], max(0.5, remaining / 2)),
                )
            else:
                request_timeout = timeout
            response = requests.get(
                current,
                params=current_params,
                headers=headers,
                timeout=request_timeout,
                stream=True,
                allow_redirects=False,
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                redirect_base = response.url or current
                response.close()
                if not location:
                    raise RadioURLPolicyError("La redirección de radio no incluye destino.")
                if redirect_count >= max_redirects:
                    raise RadioURLPolicyError("La radio excede el límite de redirecciones.")
                current = validate_public_http_url(urljoin(redirect_base, location))
                current_params = None
                continue
            try:
                response.raise_for_status()
                validate_public_http_url(response.url or current)
            except Exception:
                response.close()
                raise
            return response
        raise RadioURLPolicyError("La radio excede el límite de redirecciones.")

    @staticmethod
    def _read_response_limited(
        response,
        max_bytes: int,
        *,
        deadline: float | None = None,
    ) -> bytes:
        try:
            declared = int(response.headers.get("content-length", "0") or 0)
        except (TypeError, ValueError):
            declared = 0
        if declared > max_bytes:
            raise RadioURLPolicyError("La respuesta remota excede el tamaño permitido.")

        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=16 * 1024):
            if deadline is not None and time.monotonic() > deadline:
                raise RadioURLPolicyError("La respuesta remota excedió el tiempo permitido.")
            if not chunk:
                continue
            size += len(chunk)
            if size > max_bytes:
                raise RadioURLPolicyError("La respuesta remota excede el tamaño permitido.")
            chunks.append(chunk)
        return b"".join(chunks)

    def _fetch_station_search_sync(self, url: str, params: dict) -> list[dict]:
        deadline = time.monotonic() + 15.0
        with self._open_public_response(
            url,
            params=params,
            headers={"User-Agent": "CatsBot/2.0 (radio-search)"},
            timeout=(5.0, 12.0),
            deadline=deadline,
        ) as response:
            raw = self._read_response_limited(
                response,
                MAX_SEARCH_RESPONSE_BYTES,
                deadline=deadline,
            )
        parsed = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(parsed, list):
            raise ValueError("La API de radio devolvió un formato inesperado.")
        return [item for item in parsed if isinstance(item, dict)]

    @staticmethod
    def _is_voice_channel(channel) -> bool:
        return isinstance(channel, (discord.VoiceChannel, discord.StageChannel))

    def _nearby_voice_channel(self, guild: discord.Guild, channel_id: int):
        candidates = [
            ch
            for ch in getattr(guild, "channels", [])
            if self._is_voice_channel(ch) and abs(int(ch.id) - int(channel_id)) <= 4096
        ]
        return candidates[0] if len(candidates) == 1 else None

    async def _resolve_voice_channel(self, guild: discord.Guild, channel_id: int):
        channel = guild.get_channel(channel_id)
        if self._is_voice_channel(channel):
            return channel

        try:
            fetched = await guild.fetch_channel(channel_id)
            if self._is_voice_channel(fetched):
                return fetched
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

        repaired = self._nearby_voice_channel(guild, channel_id)
        if repaired is not None:
            logger.warning(
                "[radio] %s: channel_id redondeado reparado: %s -> %s",
                guild.name,
                channel_id,
                repaired.id,
            )
            try:
                self.db.set_lofi_config(guild.id, channel_id=repaired.id)
            except Exception:
                logger.debug("[radio] No se pudo persistir channel_id reparado", exc_info=True)
            return repaired

        return None

    def _configured_radio_channel(self, guild: discord.Guild, cfg: dict):
        channel_id = self._coerce_channel_id(cfg.get("channel_id"))
        if not channel_id:
            return None
        channel = guild.get_channel(channel_id)
        return channel if self._is_voice_channel(channel) else None

    def _fallback_radio_channel(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return None

        vc = self._voice_client_for_guild(guild)
        if vc and vc.is_connected() and self._is_voice_channel(getattr(vc, "channel", None)):
            return vc.channel

        member_voice = getattr(getattr(interaction.user, "voice", None), "channel", None)
        if self._is_voice_channel(member_voice):
            return member_voice

        return None

    def _guild_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(int(guild_id))
        if lock is None:
            lock = asyncio.Lock()
            self._guild_locks[int(guild_id)] = lock
        return lock

    def _voice_client_for_guild(self, guild: discord.Guild):
        """Devuelve el voice client real aunque ``guild.voice_client`` venga desfasado.

        discord.py puede tener el cliente en ``bot.voice_clients`` mientras
        ``guild.voice_client`` todavía es ``None`` durante reconnects/resumes.
        Si llamamos ``channel.connect`` en ese estado, la librería lanza
        ``ClientException: Already connected to a voice channel`` cada tick.
        """
        vc = getattr(guild, "voice_client", None)
        if vc is not None:
            return vc

        for candidate in getattr(self.bot, "voice_clients", []) or []:
            if getattr(getattr(candidate, "guild", None), "id", None) == guild.id:
                return candidate
        return None

    async def _ensure_voice_client(self, guild: discord.Guild, channel):
        """Reutiliza/mueve/conecta el voice client sin duplicar conexiones."""
        vc = self._voice_client_for_guild(guild)
        if vc and vc.is_connected():
            current = getattr(vc, "channel", None)
            if getattr(current, "id", None) != getattr(channel, "id", None):
                logger.info(f"[radio] Moviendo al canal {channel.name} en {guild.name}")
                await vc.move_to(channel)
            return vc

        if vc is not None:
            try:
                await vc.disconnect(force=True)
            except Exception:
                logger.debug("[radio] No se pudo limpiar voice client viejo en %s", guild.name, exc_info=True)

        try:
            logger.info(f"[radio] Conectando a {channel.name} en {guild.name}")
            return await channel.connect(reconnect=True)
        except discord.ClientException as exc:
            existing = self._voice_client_for_guild(guild)
            if existing and existing.is_connected():
                logger.warning(
                    "[radio] %s ya tenía voice client activo; se reutiliza para cortar el loop de conexión (%s)",
                    guild.name,
                    exc,
                )
                current = getattr(existing, "channel", None)
                if getattr(current, "id", None) != getattr(channel, "id", None):
                    await existing.move_to(channel)
                return existing
            raise

    def _resolve_interaction_radio_channel(
        self,
        interaction: discord.Interaction,
        cfg: dict,
        *,
        persist_fallback: bool = False,
    ):
        if interaction.guild is None:
            return None

        channel = self._configured_radio_channel(interaction.guild, cfg)
        if channel:
            return channel

        channel = self._fallback_radio_channel(interaction)
        if channel and persist_fallback:
            self.db.set_lofi_config(interaction.guild.id, channel_id=channel.id)
            cfg["channel_id"] = channel.id
            logger.info(
                "[radio] guild=%s canal de radio actualizado por fallback: %s",
                interaction.guild.id,
                channel.id,
            )
        return channel

    async def _resolve_start_channel(
        self,
        interaction: discord.Interaction,
        cfg: dict,
        requested_channel=None,
    ):
        """Resuelve el destino de ``/radio start`` por orden de intención.

        Un canal indicado en el comando siempre gana. Si se omite, se usa el
        canal de voz actual del administrador y, por último, el canal guardado
        o aquel donde ya esté conectado el bot.
        """
        guild = interaction.guild
        if guild is None:
            return None

        if self._is_voice_channel(requested_channel):
            return requested_channel

        member_voice = getattr(getattr(interaction.user, "voice", None), "channel", None)
        if self._is_voice_channel(member_voice):
            return member_voice

        channel_id = self._coerce_channel_id(cfg.get("channel_id"))
        if channel_id:
            configured = await self._resolve_voice_channel(guild, channel_id)
            if self._is_voice_channel(configured):
                return configured

        vc = self._voice_client_for_guild(guild)
        if vc and vc.is_connected():
            current = getattr(vc, "channel", None)
            if self._is_voice_channel(current):
                return current

        return None

    @staticmethod
    def _voice_status_text(station_name: str | None) -> str:
        clean_name = " ".join(str(station_name or "Lofi Radio 24/7").split())
        status = f"🎶 {clean_name}"
        return status[:120]

    async def _set_voice_channel_status(self, channel, station_name: str | None) -> None:
        if not self._is_voice_channel(channel):
            return
        status = self._voice_status_text(station_name)
        try:
            await channel.edit(status=status, reason="Radio: actualizar estado del canal")
        except discord.Forbidden:
            logger.warning(
                "[radio] Sin permiso Set Voice Channel Status en canal %s (%s)",
                getattr(channel, "id", None),
                getattr(getattr(channel, "guild", None), "id", None),
            )
        except discord.HTTPException:
            logger.exception("[radio] Discord rechazó actualizar el status del canal %s", getattr(channel, "id", None))
        except TypeError:
            logger.debug("[radio] Esta versión de discord.py no soporta edit(status=...)")

    async def _clear_voice_channel_status(self, channel) -> None:
        if not self._is_voice_channel(channel):
            return
        try:
            await channel.edit(status=None, reason="Radio: limpiar estado del canal")
        except (discord.Forbidden, discord.HTTPException, TypeError):
            logger.debug(
                "[radio] No se pudo limpiar el status del canal %s",
                getattr(channel, "id", None),
                exc_info=True,
            )

    async def _check_and_connect_guild(
        self,
        guild: discord.Guild,
        *,
        restart_stream: bool = False,
    ):
        """Lógica de conexión/reproducción para un guild específico.

        Puede llamarse directamente desde el API (vía run_coroutine_threadsafe)
        para aplicar cambios inmediatamente sin esperar el tick de 60 s.
        """
        cfg = self.db.get_lofi_config(guild.id)
        logger.debug(
            f"[radio] guild={guild.name} enabled={cfg.get('enabled')} "
            f"channel_id={cfg.get('channel_id')}"
        )

        if not cfg.get("enabled"):
            vc = self._voice_client_for_guild(guild)
            if vc:
                try:
                    await self._clear_voice_channel_status(getattr(vc, "channel", None))
                    await vc.disconnect()
                except Exception:
                    pass
            return

        channel_id = self._coerce_channel_id(cfg.get("channel_id"))
        if not channel_id:
            logger.warning(f"[radio] {guild.name}: radio habilitada pero sin channel_id")
            return

        channel = await self._resolve_voice_channel(guild, channel_id)
        if not self._is_voice_channel(channel):
            logger.warning(
                f"[radio] {guild.name}: channel {channel_id} no encontrado o no es VoiceChannel"
            )
            return

        try:
            async with self._guild_lock(guild.id):
                vc = await self._ensure_voice_client(guild, channel)
        except Exception:
            logger.exception(f"[radio] No se pudo conectar/mover al canal en {guild.name}")
            return

        if restart_stream and vc.is_playing():
            logger.info(f"[radio] Reiniciando stream activo en {guild.name}")
            vc.stop()
            await asyncio.sleep(self._playback_wait)

        if not vc.is_playing():
            logger.info(f"[radio] Iniciando reproducción en {guild.name}")
            await self.start_playing(vc, channel, cfg)
        else:
            await self._set_voice_channel_status(channel, cfg.get("station_name"))

    async def connect_guild(self, guild_id: int, *, restart_stream: bool = False):
        """Punto de entrada para el API: conecta/actualiza radio en un guild concreto."""
        if restart_stream:
            await self.restart_guild(guild_id)
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            logger.warning(f"[radio] connect_guild: guild {guild_id} no encontrado en cache")
            return
        await self._check_and_connect_guild(guild, restart_stream=restart_stream)

    async def restart_guild(self, guild_id: int):
        """Reinicia de verdad la radio de un guild tras cambios desde dashboard.

        A diferencia de connect/update, esto fuerza un nuevo proceso FFmpeg:
        para la reproducción actual, desconecta el voice client y vuelve a
        conectar usando la configuración persistida más reciente.
        """
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            logger.warning(f"[radio] restart_guild: guild {guild_id} no encontrado en cache")
            return

        cfg = self.db.get_lofi_config(guild.id)
        self._reset_stream_state(
            guild.id,
            cfg.get("stream_url") or LOFI_STREAM_URL,
        )
        if cfg.get("enabled"):
            channel_id = self._coerce_channel_id(cfg.get("channel_id"))
            if not channel_id:
                logger.warning(f"[radio] {guild.name}: restart cancelado, radio habilitada sin channel_id")
                return
            channel = await self._resolve_voice_channel(guild, channel_id)
            if not self._is_voice_channel(channel):
                logger.warning(
                    f"[radio] {guild.name}: restart cancelado, channel {channel_id} no encontrado o no es VoiceChannel"
                )
                return

        self._restart_in_progress.add(guild_id)
        try:
            vc = self._voice_client_for_guild(guild)
            if vc and vc.is_connected():
                old_channel = getattr(vc, "channel", None)
                try:
                    if vc.is_playing() or vc.is_paused():
                        logger.info(f"[radio] Deteniendo stream activo en {guild.name} por cambio de dashboard")
                        vc.stop()
                except Exception:
                    logger.debug("[radio] No se pudo detener stream antes del restart", exc_info=True)

                await asyncio.sleep(self._playback_wait)
                try:
                    await self._clear_voice_channel_status(old_channel)
                    async with self._guild_lock(guild.id):
                        await vc.disconnect(force=True)
                    logger.info(f"[radio] Voice client desconectado en {guild.name} para reinicio limpio")
                except Exception:
                    logger.exception(f"[radio] No se pudo desconectar voice client en {guild.name}")
                    raise

                await asyncio.sleep(0.5)

            await self._check_and_connect_guild(guild, restart_stream=False)
        finally:
            self._restart_in_progress.discard(guild_id)

    async def start_playing(self, vc, channel, cfg) -> bool:
        stream_url = cfg.get("stream_url") or LOFI_STREAM_URL
        station_name = cfg.get("station_name") or "Lofi Radio 24/7"

        retry_delay = self._stream_retry_delay(channel.guild.id, stream_url)
        if retry_delay is None:
            logger.debug(
                "[radio] Circuito abierto para guild=%s source=%s",
                channel.guild.id,
                redact_url_for_log(stream_url),
            )
            return False
        if retry_delay > 0:
            return False

        try:
            resolved_url = await self._resolve_stream(channel.guild.id, stream_url)
            import shutil
            ffmpeg_path = shutil.which("ffmpeg") or "ffmpeg"
            ffmpeg_options = {
                'before_options': (
                    '-nostdin -rw_timeout 15000000 '
                    '-protocol_whitelist http,https,tcp,tls,crypto'
                ),
                'options': '-vn'
            }
            audio_source = discord.FFmpegPCMAudio(
                resolved_url,
                executable=ffmpeg_path,
                **ffmpeg_options,
            )

            audio_source = _RadioAudioSource(
                audio_source,
                vc,
                volume=cfg.get("volume", 100) / 100.0,
            )

            started_at = time.monotonic()

            def after_playback(error):
                if error:
                    logger.error(
                        "Error en reproducción de radio (%s)",
                        type(error).__name__,
                    )
                if channel.guild.id in self._restart_in_progress:
                    logger.debug("[radio] Ignorando auto-reconnect por restart manual en %s", channel.guild.id)
                    return
                # Re-leer config desde DB para tener el estado más actualizado
                current_cfg = self.db.get_lofi_config(channel.guild.id)
                current_url = current_cfg.get("stream_url") or LOFI_STREAM_URL
                if self._source_key(current_url) != self._source_key(stream_url):
                    logger.debug("[radio] Fuente cambiada; el flujo de cambio iniciará el nuevo stream")
                    return
                if not current_cfg.get("auto_reconnect", 1):
                    self._sync_stream_source(channel.guild.id, current_url)
                    self._stream_failures[channel.guild.id] = MAX_STREAM_FAILURES
                    logger.info("[radio] Auto-reconnect desactivado en guild=%s", channel.guild.id)
                    return
                if current_cfg.get("enabled"):
                    asyncio.run_coroutine_threadsafe(
                        self.reconnect_stream(
                            vc,
                            channel,
                            current_cfg,
                            playback_seconds=max(0.0, time.monotonic() - started_at),
                        ),
                        self.bot.loop,
                    )

            vc.play(
                audio_source,
                after=after_playback,
            )
            logger.info(
                "[radio] Perfil Opus aplicado guild=%s canal=%s bitrate=%s bit/s",
                channel.guild.id,
                getattr(getattr(vc, "channel", None), "id", None),
                audio_source.bitrate * 1024,
            )

            # Actualizar el status visible del canal de voz donde reproduce.
            # No cambiamos la presencia/perfil del bot: sólo el estado del voicechat.
            try:
                asyncio.create_task(self._set_voice_channel_status(channel, station_name))
            except RuntimeError:
                asyncio.run_coroutine_threadsafe(
                    self._set_voice_channel_status(channel, station_name),
                    self.bot.loop,
                )
            return True
        except Exception as exc:
            failures, delay = self._record_stream_failure(channel.guild.id, stream_url)
            logger.warning(
                "[radio] No se pudo iniciar guild=%s source=%s error=%s intento=%s/%s retry=%.0fs",
                channel.guild.id,
                redact_url_for_log(stream_url),
                type(exc).__name__,
                failures,
                MAX_STREAM_FAILURES,
                delay,
            )
            if failures >= MAX_STREAM_FAILURES:
                logger.error(
                    "[radio] Circuito abierto para guild=%s tras %s fallos; requiere restart o cambio de URL",
                    channel.guild.id,
                    failures,
                )
            return False

    def _resolve_stream_sync(
        self,
        url: str,
        depth: int = 0,
        deadline: float | None = None,
    ) -> str:
        """Resuelve playlists con límites y valida todos sus destinos HTTP(S).

        Método síncrono pensado para ejecutarse en executor y no bloquear el loop.
        """
        if depth > 3:
            raise RadioURLPolicyError("La playlist contiene demasiados niveles.")
        if deadline is None:
            deadline = time.monotonic() + 25.0
        if time.monotonic() > deadline:
            raise RadioURLPolicyError("La resolución de la radio excedió el tiempo permitido.")

        with self._open_public_response(
            url,
            timeout=(5.0, 10.0),
            deadline=deadline,
        ) as response:
            final_url = validate_public_http_url(response.url or url)
            content_type = response.headers.get("content-type", "").lower()
            path = urlsplit(final_url).path.lower()
            iterator = response.iter_content(chunk_size=16 * 1024)
            first_chunk = next(iterator, b"")
            if time.monotonic() > deadline:
                raise RadioURLPolicyError("La resolución de la radio excedió el tiempo permitido.")
            sniff = first_chunk[:4096].decode("utf-8-sig", errors="replace").lstrip()
            is_playlist = (
                path.endswith((".m3u", ".m3u8", ".pls"))
                or "scpls" in content_type
                or "mpegurl" in content_type
                or sniff.upper().startswith("#EXTM3U")
                or sniff.lower().startswith("[playlist]")
                or re.search(r"(?im)^file\d+\s*=", sniff) is not None
            )
            if not is_playlist:
                return final_url
            chunks = [first_chunk] if first_chunk else []
            size = len(first_chunk)
            for chunk in iterator:
                if time.monotonic() > deadline:
                    raise RadioURLPolicyError("La playlist excedió el tiempo permitido.")
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_PLAYLIST_BYTES:
                    raise RadioURLPolicyError("La playlist excede el tamaño permitido.")
                chunks.append(chunk)
            raw = b"".join(chunks)

        text = raw.decode("utf-8-sig", errors="replace")
        if "#EXT-X-DEFINE" in text.upper() or "{$" in text:
            raise RadioURLPolicyError("La playlist usa sustituciones dinámicas no permitidas.")
        candidates: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            pls_match = re.match(r"(?i)^file\d+\s*=\s*(.+)$", line)
            if pls_match:
                candidates.append(pls_match.group(1).strip())
                continue
            for uri_match in re.finditer(r'(?i)\bURI=(?:"([^"]+)"|([^,\s]+))', line):
                candidates.append((uri_match.group(1) or uri_match.group(2)).strip())
            if not line.startswith(("#", "[")) and "=" not in line:
                candidates.append(line)

        candidates = list(dict.fromkeys(candidates))
        resolved_candidates: list[str] = []
        if len(candidates) > MAX_PLAYLIST_URLS:
            raise RadioURLPolicyError("La playlist contiene demasiados destinos.")
        for candidate in candidates:
            if time.monotonic() > deadline:
                raise RadioURLPolicyError("La playlist excedió el tiempo permitido.")
            resolved = validate_public_http_url(urljoin(final_url, candidate))
            if resolved not in resolved_candidates:
                resolved_candidates.append(resolved)
        if not resolved_candidates:
            raise RadioURLPolicyError("La playlist no contiene destinos HTTP(S) válidos.")

        # FFmpeg necesita consumir el manifiesto HLS; todos los URI presentes ya
        # fueron validados. Para M3U/PLS clásicos resolvemos su primera entrada.
        if path.endswith(".m3u8") or "#EXT-X-" in text.upper():
            return final_url
        return self._resolve_stream_sync(
            resolved_candidates[0],
            depth + 1,
            deadline,
        )

    async def reconnect_stream(
        self,
        vc,
        channel,
        cfg,
        *,
        playback_seconds: float = 0.0,
    ):
        # Respetar la opción auto_reconnect del dashboard. Default 1 (activo).
        if not cfg.get("auto_reconnect", 1):
            return
        stream_url = cfg.get("stream_url") or LOFI_STREAM_URL
        if playback_seconds >= HEALTHY_PLAYBACK_SECONDS:
            self._reset_stream_state(channel.guild.id, stream_url)
        failures, delay = self._record_stream_failure(channel.guild.id, stream_url)
        if failures >= MAX_STREAM_FAILURES:
            logger.error(
                "[radio] Auto-reconnect detenido en guild=%s tras %s fallos consecutivos",
                channel.guild.id,
                failures,
            )
            return
        await asyncio.sleep(delay)
        cfg = self.db.get_lofi_config(channel.guild.id)
        if not cfg.get("auto_reconnect", 1):
            return
        if vc and vc.is_connected() and not vc.is_playing() and cfg.get("enabled"):
            # Pausar si el canal está vacío y el admin lo configuró así.
            if cfg.get("pause_on_empty"):
                non_bot_members = [m for m in channel.members if not m.bot]
                if not non_bot_members:
                    return
            await self.start_playing(vc, channel, cfg)

    radio_group = app_commands.Group(
        name="radio",
        description="Configuración de Radio 24/7",
        default_permissions=discord.Permissions(administrator=True),
    )

    @radio_group.command(
        name="start",
        description="Activa la radio sin necesidad de acceder al dashboard",
    )
    @app_commands.describe(
        canal="Canal donde reproducir; si se omite usa tu canal de voz o el guardado",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def radio_start(
        self,
        interaction: discord.Interaction,
        canal: discord.VoiceChannel | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            return await interaction.followup.send(
                "❌ Este comando solo se puede usar dentro de un servidor.",
                ephemeral=True,
            )

        try:
            cfg = self.db.get_lofi_config(guild.id)
        except Exception:
            logger.exception("[radio] No se pudo leer la configuración en guild %s", guild.id)
            return await interaction.followup.send(
                "❌ No pude leer la configuración de la radio. Inténtalo nuevamente.",
                ephemeral=True,
            )

        channel = await self._resolve_start_channel(interaction, cfg, canal)
        if channel is None:
            return await interaction.followup.send(
                "❌ Indica un canal con `canal:` o entra primero a un canal de voz.",
                ephemeral=True,
            )

        try:
            self.db.set_lofi_config(guild.id, channel_id=channel.id, enabled=1)
            self._reset_stream_state(
                guild.id,
                cfg.get("stream_url") or LOFI_STREAM_URL,
            )
        except Exception:
            logger.exception("[radio] No se pudo guardar la activación en guild %s", guild.id)
            return await interaction.followup.send(
                "❌ No pude guardar la configuración de la radio. Inténtalo nuevamente.",
                ephemeral=True,
            )

        try:
            await self._check_and_connect_guild(guild)
        except Exception:
            logger.exception(
                "[radio] Fallo inesperado aplicando /radio start en guild %s",
                guild.id,
            )

        vc = self._voice_client_for_guild(guild)
        connected_to_target = bool(
            vc
            and vc.is_connected()
            and getattr(getattr(vc, "channel", None), "id", None) == channel.id
        )
        if not connected_to_target:
            logger.warning(
                "[radio] guild=%s activada en channel=%s pero no se confirmó la conexión",
                guild.id,
                channel.id,
            )
            return await interaction.followup.send(
                f"⚠️ La radio quedó activada en {channel.mention}, pero no pude conectarme ahora. "
                "Comprueba mis permisos para **Conectar** y **Hablar**, y luego usa `/radio restart`.",
                ephemeral=True,
            )

        station = cfg.get("station_name") or "Lofi Radio 24/7"
        await interaction.followup.send(
            f"✅ **Radio activada** en {channel.mention} — **{station}**.",
            ephemeral=True,
        )

    @radio_group.command(name="status", description="Consulta el estado y configuración actual de la radio")
    async def radio_status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cfg = self.db.get_lofi_config(interaction.guild_id)
        if not cfg.get("enabled"):
            return await interaction.followup.send("❌ La radio está desactivada en este servidor.", ephemeral=True)

        canal = self._resolve_interaction_radio_channel(interaction, cfg)
        vc = self._voice_client_for_guild(interaction.guild)

        status_text = "🟢 Reproduciendo" if (vc and vc.is_playing()) else "🔴 Detenido / Conectando..."
        station = cfg.get("station_name", "Lofi Radio 24/7")

        embed = discord.Embed(title="Estado de la Radio", color=discord.Color.blue())
        embed.add_field(name="Estación", value=f"**{station}**", inline=False)
        embed.add_field(name="Canal", value=canal.mention if canal else "No encontrado", inline=True)
        embed.add_field(name="Estado", value=status_text, inline=True)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @radio_group.command(name="restart", description="Fuerza la reconexión y reinicio del stream de radio")
    @app_commands.checks.has_permissions(administrator=True)
    async def radio_restart(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cfg = self.db.get_lofi_config(interaction.guild_id)
        if not cfg.get("enabled"):
            return await interaction.followup.send(
                "❌ La radio no está activada. Usa `/radio start` primero.",
                ephemeral=True,
            )

        channel = self._resolve_interaction_radio_channel(
            interaction,
            cfg,
            persist_fallback=True,
        )
        if not channel:
            return await interaction.followup.send(
                "❌ Canal de voz no encontrado. Entra a uno o usa `/radio start canal:`.",
                ephemeral=True,
            )

        vc = self._voice_client_for_guild(interaction.guild)
        try:
            if vc and vc.is_connected():
                if vc.is_playing():
                    vc.stop()
                if vc.channel.id != channel.id:
                    await vc.move_to(channel)
            else:
                vc = await self._ensure_voice_client(interaction.guild, channel)

            await asyncio.sleep(self._playback_wait)
            self._reset_stream_state(
                interaction.guild_id,
                cfg.get("stream_url") or LOFI_STREAM_URL,
            )
            await self.start_playing(vc, channel, cfg)
            station = cfg.get("station_name", "Lofi Radio 24/7")
            await interaction.followup.send(f"🔄 Stream reiniciado — **{station}**", ephemeral=True)
        except Exception as e:
            logger.exception(f"Error en /radio restart en {interaction.guild.name}")
            await interaction.followup.send(f"❌ Error al reiniciar el stream: {e}", ephemeral=True)

    @radio_group.command(name="buscar", description="Busca estaciones de radio globales para reproducir")
    @app_commands.describe(nombre="Nombre, género o país a buscar")
    @app_commands.checks.has_permissions(administrator=True)
    async def radio_search(self, interaction: discord.Interaction, nombre: str):
        await interaction.response.defer(ephemeral=True)

        try:
            nombre = " ".join(nombre.split())[:100]
            if not nombre:
                return await interaction.followup.send(
                    "❌ Escribe un nombre, género o país para buscar.",
                    ephemeral=True,
                )
            params = {
                "name": nombre,
                "limit": 10,
                "hidebroken": "true",
                "order": "clickcount",
                "reverse": "true",
            }
            data = []
            # Lista de mirrors de radio-browser (rotación oficial). Si el primero
            # falla o devuelve vacío, probamos los siguientes — el bug reportado
            # era que con la radio activa la consulta a `de1` colgaba/timeout
            # silenciosamente y no se mostraban resultados.
            mirrors = [
                RADIO_API_URL,
                "https://de2.api.radio-browser.info/json/stations/search",
                "https://fi1.api.radio-browser.info/json/stations/search",
                "https://nl1.api.radio-browser.info/json/stations/search",
            ]
            for url in mirrors:
                try:
                    parsed = await asyncio.to_thread(
                        self._fetch_station_search_sync,
                        url,
                        params,
                    )
                    if isinstance(parsed, list) and parsed:
                        data = parsed
                        break
                except Exception as exc:
                    logger.debug(
                        "Mirror %s falló (%s)",
                        redact_url_for_log(url),
                        type(exc).__name__,
                    )
                    continue

            if not data:
                return await interaction.followup.send(
                    "❌ No se encontraron estaciones con ese nombre. "
                    "Si la radio está activa, espera unos segundos e inténtalo de nuevo.",
                    ephemeral=True,
                )

            options = []
            for idx, station in enumerate(data[:10]):
                name = " ".join(str(station.get("name") or "Desconocida").split())[:90]
                country = " ".join(str(station.get("country") or "").split())[:30]
                tags = " ".join(str(station.get("tags") or "").split())[:40]
                options.append(discord.SelectOption(
                    label=name,
                    description=f"{country} - {tags}"[:100],
                    value=f"{idx}"
                ))

            class RadioSelect(discord.ui.Select):
                def __init__(self, stations, db, bot, cog):
                    self.stations = stations
                    self.db = db
                    self.bot = bot
                    self.cog = cog
                    super().__init__(placeholder="Selecciona una emisora para reproducirla...", min_values=1, max_values=1, options=options)

                async def callback(self, inter: discord.Interaction):
                    view = self.view
                    if not isinstance(view, RadioStationSelectView) or not await view._authorized(inter):
                        return
                    try:
                        await inter.response.defer(ephemeral=True)
                    except Exception:
                        pass

                    idx = int(self.values[0])
                    station = self.stations[idx]
                    url = station.get("url_resolved") or station.get("url")
                    name = " ".join(str(station.get("name") or "Desconocida").split())[:90]

                    # Resolver playlists fuera del loop
                    try:
                        resolved = await self.cog._resolve_stream(inter.guild_id, url)
                    except Exception as exc:
                        logger.warning(
                            "[radio] Emisora rechazada source=%s error=%s",
                            redact_url_for_log(url),
                            type(exc).__name__,
                        )
                        await inter.followup.send(
                            "❌ La emisora devolvió una URL no permitida o no accesible.",
                            ephemeral=True,
                        )
                        try:
                            await inter.message.edit(view=None)
                        except Exception:
                            pass
                        return

                    # Asegurar columnas y actualizar configuración
                    try:
                        self.db.ensure_column("lofi_config", "stream_url", "TEXT")
                        self.db.ensure_column("lofi_config", "station_name", "TEXT")
                    except Exception:
                        pass

                    try:
                        self.db._upsert_config(
                            "lofi_config", inter.guild_id,
                            stream_url=resolved, station_name=name, enabled=1
                        )
                    except Exception:
                        logger.exception("Fallo guardando configuración de la emisora")

                    cfg = self.db.get_lofi_config(inter.guild_id)
                    channel = self.cog._resolve_interaction_radio_channel(
                        inter,
                        cfg,
                        persist_fallback=True,
                    )
                    vc = self.cog._voice_client_for_guild(inter.guild)

                    if not channel:
                        await inter.followup.send(
                            "❌ Canal de voz no encontrado. Entra a uno o usa `/radio start canal:`.",
                            ephemeral=True,
                        )
                        try:
                            await inter.message.edit(view=None)
                        except Exception:
                            pass
                        return

                    try:
                        if not vc or not vc.is_connected():
                            vc = await self.cog._ensure_voice_client(inter.guild, channel)
                            await asyncio.sleep(self.cog._playback_wait)
                            await self.cog.start_playing(vc, channel, cfg)
                        else:
                            # Cambio en caliente: detener el stream actual,
                            # mover de canal si hace falta y SIEMPRE relanzar
                            # con la nueva URL/estación.
                            if vc.is_playing():
                                vc.stop()

                            if vc.channel.id != channel.id:
                                try:
                                    await vc.move_to(channel)
                                except Exception:
                                    pass

                            await asyncio.sleep(self.cog._playback_wait)
                            await self.cog.start_playing(vc, channel, cfg)

                    except Exception as e:
                        logger.exception(f"Error aplicando emisora en {inter.guild.name}: {e}")
                        await inter.followup.send(f"❌ Error al aplicar la emisora: {e}", ephemeral=True)
                        try:
                            await inter.message.edit(view=None)
                        except Exception:
                            pass
                        return

                    try:
                        await inter.followup.send(f"📻 **Radio cambiada a:** {name}\nSe aplicará en unos segundos.", ephemeral=True)
                        try:
                            await inter.message.edit(view=None)
                        except Exception:
                            pass
                    except Exception:
                        pass

            view = RadioStationSelectView(
                self.bot,
                interaction.user.id,
                interaction.guild_id,
            )
            view.add_item(RadioSelect(data, self.db, self.bot, self))

            from cogs._managed_messages import send_managed
            await send_managed(self.bot, interaction.guild_id, "radio", interaction.followup.send,
                               "Selecciona la estación que deseas sintonizar:", view=view)

        except Exception as e:
            logger.error(f"Error en radio_search: {e}")
            await interaction.followup.send(f"❌ Error consultando la API de radio: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Radio(bot))
