import asyncio
import os
import socket
import unittest
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

from cogs._web_access import MAX_TEXT, WebAccess, _PublicProxy, _resolve, _url, _verified_url


class PublicURLTests(unittest.TestCase):
    def test_rejects_non_public_and_obfuscated_addresses(self):
        for url in (
            "file:///etc/passwd", "ftp://example.com/x", "https://user:pass@example.com",
            "https://localhost", "https://a.localhost", "http://metadata.internal",
            "http://127.0.0.1", "http://127.1", "http://2130706433", "http://0177.0.0.1",
            "http://0x7f000001", "http://0x7f.0.0.1", "http://10.0.0.1", "http://169.254.169.254",
            "http://100.100.100.200", "http://[::1]", "http://[::ffff:127.0.0.1]",
            "http://[64:ff9b::7f00:1]", "http://[2002:7f00:1::]", "http://224.0.0.1",
            "https://example.com:444", "http://example.com\\@127.0.0.1", "http://exam\nple.com",
            "https://%31%32%37.0.0.1", "http://[fe80::1%25eth0]", "https://" + "a" * 2050,
            "https://example.com/>)...[injected](https://other.example/)", "https://example.com/<tag>",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                _url(url)

    def test_normalizes_public_addresses_without_fragment(self):
        self.assertEqual(_url("https://Example.COM.:443/a?q=1#x"), ("https://example.com/a?q=1", "example.com", 443))
        self.assertEqual(_url("http://1.1.1.1")[1:], ("1.1.1.1", 80))
        self.assertEqual(_url("https://example.com/%3Ctag%3E")[0], "https://example.com/%3Ctag%3E")


class PublicDNSTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_mixed_public_private_dns_answers(self):
        resolver = AsyncMock(return_value=[(socket.AF_INET, 1, 6, "", ("1.1.1.1", 443)), (socket.AF_INET, 1, 6, "", ("10.0.0.1", 443))])
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", resolver), self.assertRaises(ValueError):
            await _resolve("example.com", 443)

    async def test_dns_timeout_has_safe_error(self):
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(side_effect=OSError("sensitive detail"))):
            with self.assertRaisesRegex(ValueError, "resolver el sitio público") as error:
                await _resolve("example.com", 443)
            self.assertNotIn("sensitive", str(error.exception))

    async def test_proxy_connects_validated_numeric_address(self):
        connection = AsyncMock(return_value=(Mock(), Mock()))
        with patch("cogs._web_access._resolve", AsyncMock(return_value=["1.1.1.1"])), patch("cogs._web_access.asyncio.open_connection", connection):
            await _PublicProxy()._connect("example.com", 443)
        connection.assert_awaited_once_with("1.1.1.1", 443, family=socket.AF_INET)

    async def test_rebinding_after_initial_validation_never_connects(self):
        answers = [(socket.AF_INET, 1, 6, "", ("1.1.1.1", 443))]
        rebound = [(socket.AF_INET, 1, 6, "", ("127.0.0.1", 443))]
        connection = AsyncMock()
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(side_effect=[answers, rebound])), patch("cogs._web_access.asyncio.open_connection", connection):
            await _verified_url("https://example.com")
            with self.assertRaises(ValueError):
                await _PublicProxy()._connect("example.com", 443)
        connection.assert_not_awaited()

    async def test_proxy_denies_internal_connect_and_closes_on_shutdown(self):
        proxy = _PublicProxy()
        address = await proxy.start()
        port = int(address.rsplit(":", 1)[1])
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        writer.close()
        await writer.wait_closed()
        await proxy.close()
        self.assertFalse(proxy.tasks)

    async def test_proxy_transfer_budget_closes_stream(self):
        async def serve(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1024\r\n\r\n" + b"x" * 1024)
            await writer.drain()
            writer.close()
        fixture = await asyncio.start_server(serve, "127.0.0.1", 0)
        fixture_port = fixture.sockets[0].getsockname()[1]
        proxy = _PublicProxy()
        try:
            port = int((await proxy.start()).rsplit(":", 1)[1])
            async def connect(host, target_port):
                return await asyncio.open_connection("127.0.0.1", fixture_port)
            with patch.object(proxy, "_connect", connect), patch("cogs._web_access.MAX_TRANSFER", 100):
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
                await writer.drain()
                self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
                self.assertTrue(proxy.exhausted)
                writer.close()
                await writer.wait_closed()
        finally:
            await proxy.close()
            fixture.close()
            await fixture.wait_closed()


class SearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_filters_and_bounds_results_and_cache_is_not_mutable(self):
        web = WebAccess()
        rows = [{"title": "t" * 400, "href": "https://example.com/x", "body": "s" * 2000}, {"href": "http://127.0.0.1"}]
        with patch.object(web, "_search", Mock(return_value=rows)) as search, patch("cogs._web_access._resolve", AsyncMock(return_value=["1.1.1.1"])):
            result = await web.search_web("test")
            self.assertEqual(len(result["results"]), 1)
            self.assertEqual(len(result["results"][0]["title"]), 300)
            self.assertEqual(len(result["results"][0]["snippet"]), 1000)
            result["results"][0]["url"] = "tampered"
            cached = await web.search_web("test")
            self.assertEqual(cached["results"][0]["url"], "https://example.com/x")
            self.assertEqual(cached["retrieved_at_utc"], result["retrieved_at_utc"])
            self.assertEqual(cached["source_kind"], "search_snippets")
            search.assert_called_once_with("test", "duckduckgo")
        await web.close()

    async def test_query_and_lifecycle_errors_are_safe_and_lazy(self):
        web = WebAccess()
        await web.start()
        self.assertIsNone(web._playwright)
        for query in ("", " ", "x" * 301, None):
            with self.assertRaises(ValueError):
                await web.search_web(query)
        with patch.object(web, "_search", Mock(side_effect=RuntimeError("secret"))):
            with self.assertRaisesRegex(ValueError, "buscadores públicos") as error:
                await web.search_web("test")
            self.assertNotIn("secret", str(error.exception))
        await web.close()
        await web.close()
        with self.assertRaises(ValueError):
            await web.read_webpage("https://example.com")

    async def test_primary_success_does_not_call_fallback_and_deduplicates_public_urls(self):
        web = WebAccess()
        rows = [{"href": "https://Example.com:443/x#one", "title": "First"},
                {"href": "https://example.com/x#two", "title": "Duplicate"}]
        with patch.object(web, "_search", Mock(return_value=rows)) as search, \
             patch("cogs._web_access._resolve", AsyncMock(return_value=["1.1.1.1"])):
            result = await web.search_web("public query")
        self.assertEqual([row["url"] for row in result["results"]], ["https://example.com/x"])
        search.assert_called_once_with("public query", "duckduckgo")
        await web.close()

    async def test_primary_error_empty_malformed_and_filtered_results_use_validated_fallback(self):
        cases = [RuntimeError("sensitive failure"), [], None, [None, "not a row", {}],
                 [{"href": "http://127.0.0.1"}, {"href": "https://private.example/x"}]]
        for rows in cases:
            with self.subTest(rows=type(rows).__name__):
                web = WebAccess()
                fallback = [None, {"href": "http://10.0.0.1"},
                            {"href": "https://example.com/source#fragment", "title": "Public", "body": "Snippet"}]
                async def resolve(host, port):
                    if host == "private.example":
                        raise ValueError("private DNS")
                    return ["1.1.1.1"]
                with patch.object(web, "_search", Mock(side_effect=[rows, fallback])) as search, \
                     patch("cogs._web_access._resolve", resolve):
                    result = await web.search_web("public query")
                    again = await web.search_web("public query")
                self.assertEqual(result["results"], again["results"])
                self.assertEqual(result["results"][0]["url"], "https://example.com/source")
                self.assertEqual(result["source_kind"], "search_snippets")
                self.assertEqual([call.args for call in search.call_args_list],
                                 [("public query", "duckduckgo"), ("public query", "yahoo")])
                await web.close()

    async def test_failed_or_empty_backends_are_not_cached_and_hide_raw_errors(self):
        for primary, fallback in (([], []), (RuntimeError("private detail"), RuntimeError("secret")),
                                   ([], [{"href": "http://127.0.0.1"}])):
            with self.subTest(primary=type(primary).__name__):
                web = WebAccess()
                with patch.object(web, "_search", Mock(side_effect=[primary, fallback, primary, fallback])) as search:
                    for _ in range(2):
                        with self.assertRaisesRegex(ValueError, "buscadores públicos") as error:
                            await web.search_web("public query")
                        self.assertNotIn("secret", str(error.exception))
                        self.assertNotIn("private", str(error.exception))
                self.assertEqual(search.call_count, 4)
                await web.close()

    async def test_search_diagnostics_hide_queries_urls_and_exception_messages(self):
        web = WebAccess()
        with patch.object(web, "_search", Mock(side_effect=RuntimeError("sensitive detail"))), \
             self.assertLogs("cogs._web_access", level="DEBUG") as logs:
            with self.assertRaises(ValueError):
                await web.search_web("public query")
        rendered = "\n".join(logs.output)
        self.assertIn("backend=duckduckgo outcome=RuntimeError", rendered)
        self.assertIn("backend=yahoo outcome=RuntimeError", rendered)
        self.assertNotIn("sensitive detail", rendered)
        self.assertNotIn("public query", rendered)
        await web.close()

    async def test_missing_dependency_retains_the_installation_hint(self):
        web = WebAccess()
        with patch.object(web, "_search", Mock(side_effect=ImportError("private install path"))) as search:
            with self.assertRaisesRegex(ValueError, "instalar la dependencia ddgs") as error:
                await web.search_web("public query")
        self.assertEqual(search.call_count, 2)
        self.assertNotIn("private", str(error.exception))
        await web.close()

    async def test_primary_timeout_reserves_time_for_fallback_without_waiting_for_worker(self):
        import threading
        release, started = threading.Event(), threading.Event()
        def search(query, backend):
            if backend == "duckduckgo":
                started.set()
                release.wait(2)
                return []
            return [{"href": "https://example.com"}]
        web = WebAccess()
        try:
            with patch("cogs._web_access.SEARCH_ATTEMPT_TIMEOUT", 0.02), \
                 patch.object(web, "_search", Mock(side_effect=search)) as search_mock, \
                 patch("cogs._web_access._resolve", AsyncMock(return_value=["1.1.1.1"])):
                result = await asyncio.wait_for(web.search_web("public query"), 0.5)
                self.assertTrue(started.is_set())
                self.assertEqual([call.args[1] for call in search_mock.call_args_list], ["duckduckgo", "yahoo"])
                self.assertTrue(result["results"])
        finally:
            release.set()
            await web.close()

    async def test_backend_timeout_and_external_cancellation_do_not_retry_or_continue(self):
        import threading
        release = threading.Event()
        def search(query, backend):
            if backend == "yahoo":
                release.wait(2)
            return []
        web = WebAccess()
        try:
            with patch("cogs._web_access.SEARCH_ATTEMPT_TIMEOUT", 0.01), \
                 patch.object(web, "_search", Mock(side_effect=search)) as search_mock:
                with self.assertRaisesRegex(ValueError, "buscadores públicos"):
                    await asyncio.wait_for(web.search_web("public query"), 0.5)
                self.assertEqual(search_mock.call_count, 2)
            with patch.object(web, "_search", Mock(side_effect=asyncio.CancelledError)) as search_mock:
                with self.assertRaises(asyncio.CancelledError):
                    await web.search_web("public query")
                search_mock.assert_called_once()
        finally:
            release.set()
            await web.close()

    def test_ddgs_uses_only_the_requested_backend_with_a_bounded_timeout(self):
        for backend in ("duckduckgo", "yahoo"):
            with self.subTest(backend=backend):
                client = Mock()
                constructor = Mock(return_value=client)
                engines = {"ddgs": Mock(DDGS=constructor),
                           "ddgs.engines.duckduckgo": Mock(Duckduckgo=SimpleNamespace(disabled=False)),
                           "ddgs.engines.yahoo": Mock(Yahoo=SimpleNamespace(disabled=False))}
                with patch.dict("sys.modules", engines):
                    WebAccess._search("short query", backend)
                constructor.assert_called_once_with(timeout=4)
                client.text.assert_called_once_with("short query", backend=backend, max_results=5)

    def test_disabled_backend_does_not_silently_use_auto(self):
        for backend in ("duckduckgo", "yahoo"):
            constructor = Mock()
            engines = {"ddgs": Mock(DDGS=constructor),
                       "ddgs.engines.duckduckgo": Mock(Duckduckgo=SimpleNamespace(disabled=True)),
                       "ddgs.engines.yahoo": Mock(Yahoo=SimpleNamespace(disabled=True))}
            with self.subTest(backend=backend), patch.dict("sys.modules", engines):
                with self.assertRaises(ValueError):
                    WebAccess._search("public query", backend)
            constructor.assert_not_called()

    async def test_timeout_during_driver_start_closes_context_manager(self):
        async def blocked_start():
            await asyncio.Event().wait()
        manager = Mock(start=blocked_start, __aexit__=AsyncMock())
        web = WebAccess()
        with patch("playwright.async_api.async_playwright", Mock(return_value=manager)), patch("cogs._web_access._verified_url", AsyncMock(return_value="https://example.com/")), patch("cogs._web_access.PAGE_TIMEOUT", 0.01):
            with self.assertRaisesRegex(ValueError, "agotó el tiempo"):
                await web.read_webpage("https://example.com/")
        manager.__aexit__.assert_awaited_once()
        self.assertIsNone(web._playwright)
        await web.close()


class BrowserHarnessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.web = WebAccess()
        self.document = {"title": "Public document", "text": "Verified public information.", "links": [],
            "document_ready": True, "extraction_truncated": False}
        self.page = Mock(url="https://example.com/", goto=AsyncMock(return_value=SimpleNamespace(
            status=200, headers={"content-type": "text/html"})), evaluate=AsyncMock(return_value=self.document),
            wait_for_timeout=AsyncMock())
        self.context = Mock(new_page=AsyncMock(return_value=self.page), route=AsyncMock(),
            route_web_socket=AsyncMock(), close=AsyncMock())
        self.browser = Mock(new_context=AsyncMock(return_value=self.context), close=AsyncMock())
        self.launch = AsyncMock(return_value=self.browser)
        self.manager = Mock(start=AsyncMock(return_value=SimpleNamespace(chromium=SimpleNamespace(launch=self.launch))),
            __aexit__=AsyncMock())
        self.manager_patch = patch("playwright.async_api.async_playwright", Mock(return_value=self.manager))
        self.url_patch = patch("cogs._web_access._verified_url", AsyncMock(return_value="https://example.com/"))
        self.manager_patch.start()
        self.url_patch.start()

    async def asyncTearDown(self):
        self.manager_patch.stop()
        self.url_patch.stop()
        await self.web.close()

    async def test_exact_user_agent_and_sandbox_are_applied(self):
        await self.web.read_webpage("https://example.com/")
        self.assertEqual(self.browser.new_context.call_args.kwargs["user_agent"],
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")
        self.assertTrue(self.launch.call_args.kwargs["chromium_sandbox"])
        self.context.close.assert_awaited_once()
        self.manager.__aexit__.assert_awaited_once()

    async def test_transient_http_retries_once_but_forbidden_never_retries(self):
        from cogs._web_access import WebReadError
        self.page.goto.side_effect = [SimpleNamespace(status=503, headers={}),
            SimpleNamespace(status=200, headers={"content-type": "text/html"})]
        result = await self.web.read_webpage("https://example.com/")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(self.page.goto.await_count, 2)
        self.page.goto.reset_mock(side_effect=True)
        self.page.goto.return_value = SimpleNamespace(status=403, headers={})
        with self.assertRaises(WebReadError) as caught:
            await self.web.read_webpage("https://example.com/")
        self.assertEqual(caught.exception.as_result()["http_status"], 403)
        self.assertFalse(caught.exception.as_result()["retryable"])
        self.page.goto.assert_awaited_once()

    async def test_loading_text_waits_and_challenge_is_not_a_page_read(self):
        from cogs._web_access import WebReadError
        self.page.evaluate.side_effect = [{**self.document, "text": "Loading..."}, self.document, self.document]
        result = await self.web.read_webpage("https://example.com/")
        self.assertIn("Verified public information", result["text"])
        self.page.evaluate.side_effect = None
        self.page.evaluate.return_value = {**self.document, "title": "Just a moment...", "text": "Verify you are human"}
        with self.assertRaises(WebReadError) as caught:
            await self.web.read_webpage("https://example.com/")
        self.assertEqual(caught.exception.as_result()["error"], "access_challenge")
        self.page.evaluate.return_value = {**self.document, "text": "An article explains how CAPTCHA and Cloudflare work."}
        self.page.evaluate.return_value["title"] = "CAPTCHA explained"
        self.assertIn("article", (await self.web.read_webpage("https://example.com/"))["text"])
        self.page.evaluate.return_value = {**self.document, "title": "Product Specs | TechPowerUp",
            "text": "Automated bot check in progress\nThis should only take a few seconds.\nDrag the handle to the target"}
        with self.assertRaises(WebReadError) as caught:
            await self.web.read_webpage("https://example.com/")
        self.assertEqual(caught.exception.code, "access_challenge")
        self.assertEqual(caught.exception.http_status, 200)
        self.page.evaluate.return_value = {**self.document, "document_url": "chrome-error://chromewebdata/", "text": "ERR_FAILED"}
        with self.assertRaises(WebReadError) as caught:
            await self.web.read_webpage("https://example.com/")
        self.assertEqual(caught.exception.code, "network_error")

    async def test_launch_network_and_extraction_errors_are_safe_and_distinct(self):
        from cogs._web_access import WebReadError
        for stage, expected in (("launch", "browser_launch_failed"), ("network", "network_error"),
                                ("extraction", "extraction_failed")):
            with self.subTest(stage=stage):
                self.launch.side_effect = RuntimeError("secret fixture") if stage == "launch" else None
                self.page.goto.side_effect = RuntimeError("net::ERR_NAME_NOT_RESOLVED secret") if stage == "network" else None
                self.page.evaluate.side_effect = RuntimeError("secret fixture") if stage == "extraction" else None
                with self.assertRaises(WebReadError) as caught:
                    await self.web.read_webpage("https://example.com/")
                result = caught.exception.as_result()
                self.assertEqual(result["error"], expected)
                self.assertNotIn("secret", str(result))
                self.assertIsNone(self.web._browser)
                self.assertIsNone(self.web._playwright)

    async def test_failed_read_logs_safe_failure_category_without_source_or_exception_text(self):
        from cogs._web_access import WebReadError
        self.launch.side_effect = RuntimeError("sensitive browser detail https://source.example/private")
        with self.assertLogs("cogs._web_access", level="WARNING") as logs:
            with self.assertRaises(WebReadError):
                await self.web.read_webpage("https://example.com/")
        rendered = "\n".join(logs.output)
        self.assertIn("code=browser_launch_failed stage=browser_launch", rendered)
        self.assertIn("http_status=None attempts=1", rendered)
        self.assertNotIn("sensitive", rendered)
        self.assertNotIn("https://", rendered)
        self.assertIsNone(self.web._browser)
        self.assertIsNone(self.web._playwright)

    async def test_global_timeout_does_not_restart_navigation_or_return_partial_text(self):
        from cogs._web_access import WebReadError
        async def blocked_navigation(*args, **kwargs):
            await asyncio.Event().wait()
        self.page.goto.side_effect = blocked_navigation
        with patch("cogs._web_access.PAGE_TIMEOUT", 0.2), self.assertRaises(WebReadError) as caught:
            await self.web.read_webpage("https://example.com/")
        self.assertEqual(caught.exception.code, "read_timeout")
        self.page.goto.assert_awaited_once()
        self.context.close.assert_awaited_once()


@unittest.skipUnless(os.environ.get("CATBOT_WEB_BROWSER_TEST") == "1", "Explicit Chrome smoke check; requires installed Google Chrome and Playwright")
class ChromeFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connections = []
        self.requests = []
        self.transient_calls = 0
        self.slow_started = asyncio.Event()
        self.web = WebAccess()
        async def serve(reader, writer):
            request = await reader.readuntil(b"\r\n\r\n")
            self.requests.append(request)
            if b" /slow " in request:
                self.slow_started.set()
                await reader.read()
                writer.close()
                return
            if b" /redirect " in request:
                response = b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/private\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            else:
                status = 200
                if b" /dynamic " in request:
                    body = b"<html><main id='result'>Loading...</main><script>setTimeout(()=>document.querySelector('#result').textContent='Delayed useful text',1400)</script></html>"
                elif b" /challenge " in request:
                    body = b"<title>Just a moment...</title><main>Verifying you are human</main>"
                elif b" /js-redirect " in request:
                    body = b"<main>Redirecting</main><script>location.href='/forbidden'</script>"
                elif b" /js-public " in request:
                    body = b"<main>Redirecting</main><script>location.href='/table'</script>"
                elif b" /forbidden " in request:
                    status, body = 403, b"<main>Request denied</main>"
                elif b" /table " in request:
                    body = b"<main><table><tr><td>Memoria</td><td>16 GB</td></tr><tr><td>Potencia</td><td>360 W</td></tr></table></main>"
                elif b" /transient " in request:
                    self.transient_calls += 1
                    status = 503 if self.transient_calls == 1 else 200
                    body = b"<main>Temporary error</main>" if status == 503 else b"<main>Recovered public document</main>"
                else:
                    body = ("<html><head><title>Fixture</title><meta property='article:published_time' content='2020-01-01'><meta itemprop='dateModified' content='2026-10-27'></head><body><p id='result'>Initial</p>"
                        "<script>document.querySelector('#result').textContent='Rendered with JavaScript';"
                        "document.querySelector('#result').textContent+='\\nUA='+navigator.userAgent;"
                        "fetch('http://127.0.0.1/private').catch(()=>{});"
                        "new WebSocket('ws://127.0.0.1/private');</script>"
                        "<a href='https://example.com/a'>Public link</a><a href='http://127.0.0.1/'>Internal link</a>"
                            + "<p>" + "x" * (MAX_TEXT + 1000) + "</p></body></html>").encode()
                response = b"HTTP/1.1 " + str(status).encode() + b" Fixture\r\nContent-Type: text/html\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
            writer.write(response)
            await writer.drain()
            writer.close()
        self.fixture = await asyncio.start_server(serve, "127.0.0.1", 0)
        fixture_port = self.fixture.sockets[0].getsockname()[1]
        original_connect = _PublicProxy._connect
        async def connect(proxy, host, port):
            self.connections.append((host, port))
            if host == "fixture.example":
                return await asyncio.open_connection("127.0.0.1", fixture_port)
            return await original_connect(proxy, host, port)
        self.connect_patch = patch.object(_PublicProxy, "_connect", connect)
        self.resolve_patch = patch("cogs._web_access._resolve", AsyncMock(return_value=["1.1.1.1"]))
        self.connect_patch.start()
        self.resolve_patch.start()

    async def asyncTearDown(self):
        await self.web.close()
        self.connect_patch.stop()
        self.resolve_patch.stop()
        self.fixture.close()
        await self.fixture.wait_closed()

    async def test_javascript_extracts_bounded_text_and_never_connects_private_subresources(self):
        result = await self.web.read_webpage("http://fixture.example")
        self.assertEqual(result["title"], "Fixture")
        self.assertEqual(result["source_kind"], "page_read")
        self.assertEqual(result["published_at_untrusted"], "2020-01-01")
        self.assertEqual(result["modified_at_untrusted"], "2026-10-27")
        self.assertIn("+00:00", result["retrieved_at_utc"])
        self.assertTrue(result["text"].startswith("Rendered with JavaScript"))
        from cogs._web_access import CHROME_USER_AGENT
        self.assertIn("UA=" + CHROME_USER_AGENT, result["text"])
        self.assertTrue(any(("User-Agent: " + CHROME_USER_AGENT).encode() in request for request in self.requests))
        self.assertLessEqual(len(result["text"].encode("utf-8")), MAX_TEXT)
        self.assertTrue(result["text_truncated"])
        self.assertEqual(result["links"], [{"title": "Public link", "url": "https://example.com/a"}])
        self.assertTrue(self.connections)
        self.assertTrue(all(host != "127.0.0.1" for host, _ in self.connections))
        self.assertIsNone(self.web._browser)

    async def test_redirect_to_private_url_is_blocked(self):
        with self.assertRaises(ValueError):
            await self.web.read_webpage("http://fixture.example/redirect")
        self.assertTrue(all(host != "127.0.0.1" for host, _ in self.connections))
        self.assertIsNone(self.web._browser)

    async def test_dynamic_table_and_transient_recovery(self):
        result = await self.web.read_webpage("http://fixture.example/dynamic")
        self.assertIn("Delayed useful text", result["text"])
        table = await self.web.read_webpage("http://fixture.example/table")
        self.assertIn("Memoria\t16 GB\t\nPotencia\t360 W", table["text"])
        recovered = await self.web.read_webpage("http://fixture.example/transient")
        self.assertEqual(recovered["attempts"], 2)
        self.assertIn("Recovered", recovered["text"])
        self.assertEqual(self.transient_calls, 2)

    async def test_challenge_and_final_javascript_navigation_status(self):
        from cogs._web_access import WebReadError
        with self.assertRaises(WebReadError) as challenge:
            await self.web.read_webpage("http://fixture.example/challenge")
        self.assertEqual(challenge.exception.code, "access_challenge")
        with self.assertRaises(WebReadError) as denied:
            await self.web.read_webpage("http://fixture.example/js-redirect")
        self.assertEqual(denied.exception.http_status, 403)
        result = await self.web.read_webpage("http://fixture.example/js-public")
        self.assertEqual(result["url"], "http://fixture.example/table")
        self.assertEqual(result["http_status"], 200)

    async def test_cancelled_read_reaps_browser_driver_and_proxy(self):
        task = asyncio.create_task(self.web.read_webpage("http://fixture.example/slow"))
        await asyncio.wait_for(self.slow_started.wait(), 10)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(self.web._browser)
        self.assertIsNone(self.web._playwright)


if __name__ == "__main__":
    unittest.main()
