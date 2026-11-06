"""Bounded public-web search and ephemeral, sandboxed Google Chrome reading.

The proxy connects to the address it validated, rather than letting Chrome
resolve again. Every redirect and subresource crosses that same boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import re
import socket
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
from cogs._research_context import select_passages


logger = logging.getLogger(__name__)


MAX_TEXT = 128_000
MAX_RESULTS = 5
SEARCH_ATTEMPT_TIMEOUT = 6
PAGE_TIMEOUT = 20
MAX_TRANSFER = 8 * 1024 * 1024
MAX_REQUESTS = 64
CHROME_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
_WEB_SLOT = asyncio.Semaphore(1)
# A timed-out primary must not prevent the fallback from starting.
_SEARCH_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="catsbot-search")


class WebReadError(ValueError):
    """Safe observations for the planner; never forward browser command lines or stacks."""
    def __init__(self, code, detail, *, http_status=None, transient=False, stage="navigation"):
        super().__init__(detail)
        self.code, self.http_status, self.transient, self.stage = code, http_status, transient, stage
        self.url, self.attempts = None, 1

    def as_result(self):
        return {"status": "error", "error": self.code, "detail": str(self), "url": self.url,
                "http_status": self.http_status, "stage": self.stage, "attempts": self.attempts,
                "retryable": False,
                "next_action": "Do not repeat this URL; the reader already applied any safe retry. Try another relevant public source. Stop on access challenges; never bypass login, CAPTCHA or site restrictions."}


_EXTRACT_DOCUMENT = """() => {
    const main = document.querySelector('main,article,[role="main"]') || document.body;
    const copy = main?.cloneNode(true);
    for (const el of copy?.querySelectorAll('script,style,noscript,nav,footer') || []) el.remove();
    for (const el of copy?.querySelectorAll('td,th') || []) el.append('\\t');
    for (const el of copy?.querySelectorAll('p,div,section,article,main,li,h1,h2,h3,h4,tr,table') || []) el.append('\\n');
    for (const el of copy?.querySelectorAll('br') || []) el.replaceWith(document.createTextNode('\\n'));
    const raw = copy?.textContent || '';
    return {title: document.title.slice(0,300), text: raw.slice(0,500000),
        document_url: document.URL,
        document_ready: document.readyState !== 'loading', extraction_truncated: raw.length > 500000,
        published_at_untrusted: document.querySelector('meta[property="article:published_time"],meta[name="date"],meta[itemprop="datePublished"]')?.content?.slice(0,100) || null,
        modified_at_untrusted: document.querySelector('meta[property="article:modified_time"],meta[name="last-modified"],meta[itemprop="dateModified"]')?.content?.slice(0,100) || null,
        dates_untrusted: Array.from(document.querySelectorAll('time[datetime]')).slice(0,5).map(el=>el.dateTime.slice(0,100)),
        links: Array.from(document.querySelectorAll('a[href]')).slice(0,50)
            .map(a=>({title:(a.innerText || '').slice(0,150),url:a.href}))};
}"""


async def _rendered_document(page):
    previous = None
    await page.wait_for_timeout(750)
    for _ in range(12):
        document = await page.evaluate(_EXTRACT_DOCUMENT)
        text = document["text"].strip()
        title = document["title"].strip().casefold().rstrip(".!… ")
        # Short interstitials only: articles mentioning CAPTCHA/Cloudflare remain ordinary sources.
        titled_challenge = (title in {"just a moment", "attention required | cloudflare", "verify you are human", "access denied"}
            and any(marker in text.casefold() for marker in ("verify you are human", "verifying you are human", "checking your browser", "enable javascript and cookies", "access denied")))
        slider_challenge = (text.casefold().startswith("automated bot check in progress")
                            and "drag the handle to the target" in text.casefold())
        if len(text) < 3000 and (titled_challenge or slider_challenge):
            raise WebReadError("access_challenge", "El sitio mostró una comprobación de acceso; no se intentará eludirla.", stage="render")
        useful = bool(text) and text.casefold().rstrip(".!… ") not in {"loading", "cargando", "please wait", "espere"}
        if useful and text == previous and document.get("document_ready", True):
            return {**document, "render_state": "stable_dom"}
        previous = text
        await page.wait_for_timeout(250)
    if not useful:
        raise WebReadError("empty_content", "La página no produjo texto útil dentro de la ventana de renderizado.", stage="render")
    return {**document, "render_state": "partial_dom"}


async def _read_document(page, url, proxy, deadline):
    latest_response = [None]
    def navigation_response(response):
        if response.request.is_navigation_request() and response.frame == page.main_frame:
            latest_response[0] = response
    page.on("response", navigation_response)
    for attempt in (1, 2):
        latest_response[0] = None
        stage = "navigation"
        try:
            # Commit yields the HTTP result even if a subresource never finishes loading.
            response = await page.goto(url, wait_until="commit", timeout=10_000)
            latest_response[0] = latest_response[0] or response
            if response is None:
                raise WebReadError("navigation_failed", "La navegación no devolvió una respuesta HTTP.")
            if response.status >= 400:
                raise WebReadError("http_error", f"El sitio devolvió HTTP {response.status}.",
                    http_status=response.status, transient=response.status in {502, 503, 504})
            content_type = response.headers.get("content-type", "").lower()
            if not any(kind in content_type for kind in ("text/html", "text/plain", "application/xhtml+xml")):
                raise WebReadError("unsupported_content", "La URL no contiene una página de texto compatible.")
            stage = "render"
            document = await _rendered_document(page)
            # JavaScript can replace the main document after the initial HTTP 200.
            final_response = latest_response[0]
            document_url = document.get("document_url", page.url)
            if document_url.startswith("chrome-error://"):
                raise WebReadError("network_error", "Chrome mostró una página interna de error de navegación.")
            if _url(getattr(final_response, "url", document_url))[0] != _url(document_url)[0]:
                raise WebReadError("navigation_changed", "El documento cambió sin una respuesta HTTP verificable.", stage=stage)
            if final_response.status >= 400:
                raise WebReadError("http_error", f"El sitio devolvió HTTP {final_response.status}.", http_status=final_response.status,
                    transient=final_response.status in {502, 503, 504}, stage=stage)
            if not any(kind in final_response.headers.get("content-type", "").lower()
                       for kind in ("text/html", "text/plain", "application/xhtml+xml")):
                raise WebReadError("unsupported_content", "El documento final no contiene una página de texto compatible.", stage=stage)
            document["document_url"] = document_url
            if proxy.exhausted:
                raise WebReadError("page_budget_exceeded", "La página supera el presupuesto de lectura.", stage=stage)
            return document, final_response.status, attempt
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if proxy.exhausted:
                error = WebReadError("page_budget_exceeded", "La página supera el presupuesto de lectura.", stage=stage)
            elif isinstance(exc, WebReadError):
                error = exc
                if error.stage == "render" and latest_response[0] is not None:
                    error.http_status = error.http_status or latest_response[0].status
            elif type(exc).__name__ == "TimeoutError":
                error = WebReadError("navigation_timeout", "La navegación agotó su tiempo de espera.", transient=True, stage=stage)
            elif stage == "navigation":
                network = re.search(r"net::(ERR_[A-Z_]+)", str(exc))
                code = network.group(1) if network else None
                transient = code in {"ERR_CONNECTION_RESET", "ERR_CONNECTION_CLOSED", "ERR_EMPTY_RESPONSE", "ERR_TIMED_OUT",
                                     "ERR_NAME_NOT_RESOLVED", "ERR_NETWORK_CHANGED", "ERR_HTTP2_PROTOCOL_ERROR"}
                error = WebReadError("network_error" if code else "navigation_failed",
                    f"La navegación falló ({code})." if code else "La navegación falló antes de obtener una página.", transient=transient)
            else:
                error = WebReadError("extraction_failed", "No se pudo extraer el texto de la página.", stage=stage)
            error.attempts = attempt
            if error.transient and attempt == 1 and deadline - time.monotonic() >= 4:
                await page.wait_for_timeout(250)
                continue
            raise error from None


def _url(value: str) -> tuple[str, str, int]:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("URL web no válida.")
    if any(ord(char) <= 32 or ord(char) == 127 for char in value) or any(char in value for char in "\\<>"):
        raise ValueError("URL web no válida.")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (ValueError, UnicodeError):
        raise ValueError("URL web no válida.") from None
    if parsed.scheme not in {"http", "https"} or not host or parsed.username is not None or parsed.password is not None:
        raise ValueError("Solo se permiten páginas públicas HTTP o HTTPS sin credenciales.")
    if port != (443 if parsed.scheme == "https" else 80):
        raise ValueError("Solo se permiten los puertos web estándar.")
    if len(host) > 253 or "%" in host or host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".home", ".lan")):
        raise ValueError("La dirección no pertenece a la web pública.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Reject legacy numeric forms such as 127.1, 0177.0.0.1 and 0x7f000001.
        if all(part.isdigit() or part.lower().startswith("0x") for part in host.split(".")):
            raise ValueError("La dirección IP no es válida.") from None
        if any(not label or len(label) > 63 or label[0] == "-" or label[-1] == "-" or not all(c.isalnum() or c == "-" for c in label) for label in host.split(".")):
            raise ValueError("El nombre web no es válido.")
    else:
        if not _public_ip(address):
            raise ValueError("La dirección no pertenece a la web pública.")
    authority = f"[{host}]" if ":" in host else host
    return urlunsplit((parsed.scheme, authority, parsed.path or "/", parsed.query, "")), host, port


def _public_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return _public_ip(address.ipv4_mapped)
        # Do not tunnel IPv4 through transition ranges or NAT64.
        if address.sixtofour is not None or address.teredo is not None or address in ipaddress.ip_network("64:ff9b::/96") or address in ipaddress.ip_network("64:ff9b:1::/48"):
            return False
    return True


async def _resolve(host: str, port: int) -> list[str]:
    try:
        answers = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), 4)
    except (OSError, asyncio.TimeoutError):
        raise ValueError("No se pudo resolver el sitio público.") from None
    addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
    if not addresses or any(not _public_ip(ipaddress.ip_address(address)) for address in addresses):
        raise ValueError("La dirección no pertenece a la web pública.")
    return addresses


async def _verified_url(value: str) -> str:
    normalized, host, port = _url(value)
    await _resolve(host, port)
    return normalized


class _PublicProxy:
    """One page's short-lived HTTP forward proxy; HTTPS remains end-to-end TLS."""

    def __init__(self):
        self.server = None
        self.tasks: set[asyncio.Task] = set()
        self.writers: set[asyncio.StreamWriter] = set()
        self.transferred = 0
        self.requests = 0
        self.exhausted = False

    async def start(self) -> str:
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0, limit=16_384)
        return f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for writer in list(self.writers):
            writer.close()
        pending = list(self.tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _connect(self, host: str, port: int):
        addresses = await _resolve(host, port)
        for address in addresses:
            try:
                # Numeric address + numeric family prevents a second DNS lookup.
                family = socket.AF_INET6 if ":" in address else socket.AF_INET
                return await asyncio.wait_for(asyncio.open_connection(address, port, family=family), 4)
            except (OSError, asyncio.TimeoutError):
                continue
        raise ValueError("No se pudo conectar al sitio público.")

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.writers.add(writer)
        upstream = None
        try:
            self.requests += 1
            if self.requests > MAX_REQUESTS or len(self.tasks) > 16 or self.exhausted:
                self.exhausted = True
                return
            async with asyncio.timeout(PAGE_TIMEOUT):
                headers = await reader.readuntil(b"\r\n\r\n")
                if len(headers) > 16_384:
                    return
                lines = headers.decode("iso-8859-1").split("\r\n")
                method, target, version = lines[0].split(" ")
                if version not in {"HTTP/1.0", "HTTP/1.1"}:
                    return
                if method == "CONNECT":
                    _, host, port = _url(f"https://{target}/")
                    if port != 443:
                        return
                    remote, upstream = await self._connect(host, port)
                    self.writers.add(upstream)
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await writer.drain()
                elif method in {"GET", "HEAD"}:
                    normalized, host, port = _url(target)
                    parsed = urlsplit(normalized)
                    if parsed.scheme != "http":
                        return
                    remote, upstream = await self._connect(host, port)
                    self.writers.add(upstream)
                    # One request per socket. Drop proxy/framing/host headers.
                    outgoing = [f"{method} {parsed.path or '/'}{'?' + parsed.query if parsed.query else ''} HTTP/1.1", f"Host: {parsed.netloc}", "Connection: close"]
                    for line in lines[1:]:
                        name, separator, value = line.partition(":")
                        if separator and name.lower() not in {"host", "connection", "proxy-connection", "proxy-authorization", "content-length", "transfer-encoding", "upgrade"}:
                            outgoing.append(f"{name}:{value}")
                    upstream.write(("\r\n".join(outgoing) + "\r\n\r\n").encode("iso-8859-1"))
                    await upstream.drain()
                else:
                    return
                async def copy(source, destination):
                    while chunk := await source.read(32_768):
                        self.transferred += len(chunk)
                        if self.transferred > MAX_TRANSFER:
                            self.exhausted = True
                            raise ValueError("La página supera el límite de descarga.")
                        destination.write(chunk)
                        await destination.drain()
                pumps = [asyncio.create_task(copy(remote, writer)), asyncio.create_task(copy(reader, upstream))]
                try:
                    done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
                    for completed in done:
                        completed.result()
                finally:
                    for pump in pumps:
                        pump.cancel()
                    await asyncio.gather(*pumps, return_exceptions=True)
        except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            for connection in (writer, upstream):
                if connection:
                    self.writers.discard(connection)
                    connection.close()
            self.tasks.discard(task)


class WebAccess:
    def __init__(self):
        self._playwright = None
        self._browser = None
        self._cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._closed = False

    async def start(self):
        """Intentionally lazy: disabled guilds need no Chrome installation."""

    async def close(self):
        self._closed = True
        async with _WEB_SLOT:
            if self._browser:
                await self._browser.close()
                self._browser = None
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
            self._cache.clear()

    async def search_web(self, query: str) -> dict:
        if self._closed:
            raise ValueError("El acceso web está cerrado.")
        if not isinstance(query, str) or not query.strip() or len(query) > 300:
            raise ValueError("La búsqueda debe tener entre 1 y 300 caracteres.")
        query = query.strip()
        async with _WEB_SLOT:
            if self._closed:
                raise ValueError("El acceso web está cerrado.")
            cached = self._cache.get(query)
            if cached and time.monotonic() - cached[0] < 300:
                self._cache.move_to_end(query)
                return {**cached[1], "results": [dict(row) for row in cached[1]["results"]]}
            results, missing_dependencies = [], 0
            try:
                async with asyncio.timeout(15):
                    for backend in ("duckduckgo", "yahoo"):
                        outcome = "empty"
                        try:
                            # Reserve time for the fallback instead of letting one backend consume all 15 seconds.
                            async with asyncio.timeout(SEARCH_ATTEMPT_TIMEOUT):
                                rows = await asyncio.get_running_loop().run_in_executor(_SEARCH_EXECUTOR, self._search, query, backend)
                                if not isinstance(rows, list):
                                    outcome, rows = "malformed", []
                                seen = set()
                                for row in rows[:MAX_RESULTS]:
                                    if not isinstance(row, dict):
                                        continue
                                    try:
                                        url = await _verified_url(row.get("href") or row.get("url") or "")
                                    except ValueError:
                                        continue
                                    if url in seen:
                                        continue
                                    seen.add(url)
                                    results.append({"title": str(row.get("title") or "")[:300], "url": url,
                                        "snippet": str(row.get("body") or "")[:1000],
                                        "published_at_untrusted": str(row.get("date") or "")[:100] or None})
                                if outcome != "malformed":
                                    outcome = "ok" if results else "unusable" if rows else "empty"
                        except ImportError:
                            missing_dependencies += 1
                            outcome = "dependency_missing"
                        except TimeoutError:
                            outcome = "timeout"
                        except Exception as exc:
                            outcome = type(exc).__name__
                        logger.debug("Public search backend=%s outcome=%s results=%d", backend, outcome, len(results))
                        if results:
                            break
            except TimeoutError:
                logger.debug("Public search outcome=total_timeout")
            if not results:
                if missing_dependencies == 2:
                    raise ValueError("La búsqueda web requiere instalar la dependencia ddgs.") from None
                raise ValueError("Los buscadores públicos no están disponibles o agotaron el tiempo de búsqueda.") from None
            result = {"results": results, "source_kind": "search_snippets", "query": query,
                      "retrieved_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self._cache[query] = (time.monotonic(), result)
            self._cache.move_to_end(query)
            while len(self._cache) > 32:
                self._cache.popitem(last=False)
            return {**result, "results": [dict(row) for row in results]}

    @staticmethod
    def _search(query: str, backend: str):
        from ddgs import DDGS
        from ddgs.engines.yahoo import Yahoo
        from ddgs.engines.duckduckgo import Duckduckgo
        engine = {"duckduckgo": Duckduckgo, "yahoo": Yahoo}[backend]
        if engine.disabled:
            raise ValueError("El buscador no está disponible.")
        return DDGS(timeout=4).text(query, backend=backend, max_results=MAX_RESULTS)

    async def read_webpage(self, url: str, *, query: str = "", max_text: int = MAX_TEXT) -> dict:
        if self._closed:
            raise ValueError("El acceso web está cerrado.")
        normalized, _, _ = _url(url)
        async with _WEB_SLOT:
            if self._closed:
                raise ValueError("El acceso web está cerrado.")
            proxy = _PublicProxy()
            context = None
            manager = None
            stage = "validation"
            try:
                deadline = time.monotonic() + PAGE_TIMEOUT
                async with asyncio.timeout(PAGE_TIMEOUT):
                    await _verified_url(normalized)
                    stage = "browser_launch"
                    from playwright.async_api import async_playwright
                    manager = async_playwright()
                    self._playwright = await manager.start()
                    server = await proxy.start()
                    self._browser = await self._playwright.chromium.launch(
                        channel="chrome", headless=True, chromium_sandbox=True,
                        executable_path=os.environ.get("CATBOT_CHROME_EXECUTABLE") or None,
                        proxy={"server": server, "bypass": "<-loopback>"},
                        args=["--disable-quic", "--disable-background-networking", "--disable-extensions",
                              "--renderer-process-limit=2", "--js-flags=--max-old-space-size=128",
                              "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                              "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"],
                    )
                    context = await self._browser.new_context(accept_downloads=False, service_workers="block", permissions=[],
                                                              user_agent=CHROME_USER_AGENT)
                    count = 0
                    async def guard(route):
                        nonlocal count
                        count += 1
                        try:
                            _url(route.request.url)
                            allowed = route.request.method in {"GET", "HEAD"} and route.request.resource_type in {"document", "script", "stylesheet", "xhr", "fetch"} and count <= MAX_REQUESTS
                        except ValueError:
                            allowed = False
                        await (route.continue_() if allowed else route.abort())
                    await context.route("**/*", guard)
                    await context.route_web_socket("**/*", lambda websocket: websocket.close())
                    page = await context.new_page()
                    context.on("page", lambda popup: asyncio.create_task(popup.close()) if popup != page else None)
                    page.on("download", lambda download: asyncio.create_task(download.cancel()))
                    stage = "navigation"
                    extracted, http_status, attempts = await _read_document(page, normalized, proxy, deadline)
                    stage = "links"
                    final_url = await _verified_url(extracted["document_url"])
                    if proxy.exhausted:
                        raise ValueError("La página supera el presupuesto de lectura.")
                    links = []
                    for link in extracted["links"]:
                        if len(links) >= 10:
                            break
                        try:
                            target = await _verified_url(link["url"])
                        except ValueError:
                            continue
                        links.append({"title": link["title"], "url": target})
                    passages = select_passages(extracted["text"], query, min(MAX_TEXT, max(0, max_text)))
                    return {"status": "success", "title": extracted["title"], "url": final_url, **passages, "links": links,
                            "http_status": http_status, "attempts": attempts, "render_state": extracted["render_state"],
                            "warnings": ["Text captured while the DOM was still changing/loading; this is a partial reading."] if extracted["render_state"] == "partial_dom" else [],
                            "extraction_truncated": extracted.get("extraction_truncated", False),
                            "source_kind": "page_read", "retrieved_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "published_at_untrusted": extracted.get("published_at_untrusted"),
                            "modified_at_untrusted": extracted.get("modified_at_untrusted"),
                            "dates_untrusted": extracted.get("dates_untrusted", [])}
            except ImportError:
                error = WebReadError("browser_dependency_missing", "La lectura web requiere instalar Playwright y Google Chrome.", stage=stage)
            except WebReadError as exc:
                error = exc
            except asyncio.TimeoutError:
                error = WebReadError("read_timeout", "La página agotó el tiempo de lectura.", stage=stage)
            except ValueError as exc:
                error = WebReadError("url_validation_failed", str(exc)[:240], stage=stage)
            except Exception:
                error = WebReadError("browser_launch_failed" if stage == "browser_launch" else "extraction_failed",
                    "No se pudo iniciar Google Chrome con su sandbox." if stage == "browser_launch" else "No se pudo completar la lectura de la página.", stage=stage)
            finally:
                if context:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(context.close(), 3)
                if self._browser:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(self._browser.close(), 3)
                    self._browser = None
                # Stop the driver too: a launch cancelled before returning its
                # Browser handle must not leave an unmanaged Chrome process.
                if manager:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(manager.__aexit__(None, None, None), 3)
                    self._playwright = None
                await proxy.close()
            error.url = normalized
            raise error from None
