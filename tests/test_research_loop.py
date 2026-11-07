"""Model-directed research completion, checkpoints and real failure boundaries."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

from tests import test_conversation_memory as fixtures


report = fixtures.research_report


def fact(claim, url, quote):
    return {"claim": claim, "status": "supported", "support": [{"url": url, "quote": quote}]}


class ModelResearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.ConversationRuntimeTest()
        await self.fixture.asyncSetUp()
        self.engine = self.fixture.engine
        self.fixture.db.set_ai_config(10, ai_web_enabled=1)
        await self.engine.listen(self.fixture.message(ping=True))
        self.job = self.fixture.cog._queue.get_nowait()
        self.job["deep_think"] = True

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def call(self, name, argument, index=0):
        return {"role": "assistant", "tool_calls": [self.fixture.tool_call(name, argument, f"call{index}")]}

    async def test_failed_search_rejects_guessed_url_then_uses_remaining_search_and_reads_real_source(self):
        self.job["deep_think"] = False
        found = "https://official.example/statement"
        self.engine.web = SimpleNamespace(close=AsyncMock(), search_web=AsyncMock(side_effect=[ValueError("search unavailable"),
            {"results": [{"url": found, "snippet": "Official statement"}]}]),
            read_webpage=AsyncMock(return_value={"url": found, "text": "The company remains privately held."}))
        replies = [self.call("search_web", "Example listing latest", 1),
            self.call("read_webpage", "https://news.example/invented-article", 2),
            self.call("search_web", "Example official listing statement", 3), self.call("read_webpage", found, 4)]
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)) as provider:
            result = await self.engine.web_context(self.job)
        self.assertEqual(self.engine.web.search_web.await_count, 2)
        self.engine.web.read_webpage.assert_awaited_once_with(found, query=self.job["message"].content, max_text=ANY)
        self.assertIn("url_not_discovered", json.dumps(provider.await_args_list[2].args[2]))
        self.assertIn("The company remains privately held.", json.dumps(result["page_evidence_untrusted"]))
        self.assertEqual(result["diagnostics"]["search_attempts"], 2)
        self.assertEqual(result["diagnostics"]["read_attempts"], 1)
        self.assertEqual(result["diagnostics"]["stop_reason"], "round_limit")

    async def test_search_results_do_not_authorize_unrelated_urls_and_extracted_links_survive_redirects(self):
        self.job["deep_think"] = False
        initial, redirected, linked = "https://official.example/start", "https://official.example/redirected", "https://official.example/details"
        self.engine.web = SimpleNamespace(close=AsyncMock(), search_web=AsyncMock(return_value={"results": [{"url": initial}]}),
            read_webpage=AsyncMock(side_effect=[{"url": redirected, "text": "Read the public statement.", "links": [{"url": linked}]},
                {"url": linked, "text": "The company remains privately held."}]))
        replies = [self.call("search_web", "Example listing", 1), self.call("read_webpage", "https://news.example/guessed", 2),
            self.call("read_webpage", initial, 3), self.call("read_webpage", linked, 4)]
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)):
            result = await self.engine.web_context(self.job)
        self.assertEqual([call.args[0] for call in self.engine.web.read_webpage.await_args_list], [initial, linked])
        self.assertIn(redirected, result["verified_source_urls"])
        self.assertIn(linked, result["verified_source_urls"])

    async def test_explicit_user_url_remains_readable_after_search_failure(self):
        from cogs._llm_moderation import fingerprint
        for url in ("https://official.example/statement", "https://en.wikipedia.org/wiki/Alien_(film)"):
            with self.subTest(url=url):
                self.job["message"].content = "Read " + url
                self.job["fingerprint"] = fingerprint(self.job["message"])
                self.job["deep_think"] = False
                self.engine.web = SimpleNamespace(close=AsyncMock(), search_web=AsyncMock(side_effect=ValueError("unavailable")),
                    read_webpage=AsyncMock(return_value={"url": url, "text": "Public information from the requested source."}))
                replies = [self.call("search_web", "Example listing", 1), self.call("read_webpage", url, 2), {"content": "done"}]
                with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)):
                    result = await self.engine.web_context(self.job)
                self.assertEqual(self.engine.web.read_webpage.await_count, 1)
                self.assertIn(url, result["verified_source_urls"])

    async def test_compacted_ledger_keeps_a_read_source_authorized_after_link_cache_eviction(self):
        urls = [f"https://official.example/page{index}" for index in range(13)]
        documents = [{"url": url, "text": "Release 9 is current.", "links": [
            {"url": urls[index + 1] if index < 12 else "https://official.example/final"},
            *({"url": f"https://links.example/{index}/{n}"} for n in range(9))]} for index, url in enumerate(urls)]
        self.engine.web = SimpleNamespace(close=AsyncMock(), search_web=AsyncMock(return_value={"results": [{"url": urls[0]}]}),
            read_webpage=AsyncMock(side_effect=[*documents, documents[0]]))
        supported = fact("Current version", urls[0], "Release 9 is current.")
        replies = [self.call("search_web", "Example current", 0)]
        for index, url in enumerate(urls):
            replies.append(self.call("read_webpage", url, index + 1))
            if (index + 1) % 3 == 0:
                replies.append(report([supported, fact(f"Source {index}", url, "Release 9 is current.")], gaps=["Need independent verification"]))
        replies.extend([report([supported], gaps=["Need to verify the central source again"]),
            self.call("read_webpage", urls[0], 99), report([supported], sufficient=True)])
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)):
            result = await self.engine.web_context(self.job)
        self.assertEqual(self.engine.web.read_webpage.await_count, 14)
        self.assertTrue(result["assessment"]["sufficient"])

    async def test_normal_retrieval_caps_tools_and_passes_read_passages_without_claiming_completion(self):
        self.job["deep_think"] = False
        self.engine.web = SimpleNamespace(search_web=AsyncMock(return_value={"results": [
            {"url": f"https://official.example/{index}"} for index in range(4)]}), close=AsyncMock(),
            read_webpage=AsyncMock(side_effect=lambda url, **_: {"url": url, "text": "Memory: 16 GB.", "retrieved_at_utc": "2030-01-02"}))
        replies = [
            {"tool_calls": [self.fixture.tool_call("search_web", f"query {i}", f"search{i}") for i in range(3)]},
            {"tool_calls": [self.fixture.tool_call("read_webpage", f"https://official.example/{i}", f"read{i}") for i in range(4)]}]
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)) as provider:
            result = await self.engine.web_context(self.job)
        self.assertEqual(provider.await_count, 2)
        self.assertEqual(self.engine.web.search_web.await_count, 2)
        self.assertEqual(self.engine.web.read_webpage.await_count, 3)
        self.assertEqual(result["retrieval_mode"], "quick")
        self.assertFalse(result["complete"])
        self.assertFalse(result["assessment"]["sufficient"])
        self.assertEqual(len(result["page_evidence_untrusted"]), 3)
        self.assertIn("Memory: 16 GB.", json.dumps(result["page_evidence_untrusted"]))

    async def test_standard_deadline_is_configurable_and_covers_planning_search_and_reading(self):
        self.job["deep_think"] = False
        loop = asyncio.get_running_loop()
        url = "https://official.example/specifications"
        for seconds in (30, 120):
            with self.subTest(seconds=seconds):
                self.fixture.db.set_ai_config(10, ai_web_timeout_seconds=seconds)
                self.job["cfg"] = self.fixture.db.get_ai_config(10)
                clock = [loop.time()]
                async def advance(duration):
                    clock[0] += duration
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                async def search(query):
                    await advance(10)
                    return {"results": [{"url": url}]}
                async def read(target, **kwargs):
                    await advance(25)
                    return {"url": target, "text": "Memory: 16 GB."}
                replies = [self.call("search_web", "Example specifications", 1),
                           self.call("read_webpage", url, 2), {"content": "done"}]
                async def provider(*args, **kwargs):
                    async with asyncio.timeout(kwargs["timeout_seconds"]):
                        await advance(20)
                    return replies.pop(0)
                self.engine.web = SimpleNamespace(search_web=search, read_webpage=read, close=AsyncMock())
                with patch.object(loop, "time", lambda: clock[0]), patch.object(loop, "slow_callback_duration", 1000), \
                     patch("cogs._conversation.request_completion", new=provider):
                    result = await self.engine.web_context(self.job)
                if seconds == 30:
                    self.assertEqual(result["diagnostics"]["stop_reason"], "timeout")
                    self.assertEqual(result["verified_source_urls"], [])
                else:
                    self.assertEqual(result["verified_source_urls"], [url])
                    self.assertIn("Memory: 16 GB.", result["page_evidence_untrusted"][0]["text"])
                    self.assertEqual(result["diagnostics"]["stop_reason"], "planner_stop")

    async def test_normal_retrieval_has_four_planner_rounds_and_timeout_is_not_sufficiency(self):
        self.job["deep_think"] = False
        self.engine.web = SimpleNamespace(search_web=AsyncMock(return_value={"results": []}), close=AsyncMock())
        replies = [self.call("search_web", f"query {index}", index) for index in range(4)]
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)) as provider:
            result = await self.engine.web_context(self.job)
        self.assertEqual(provider.await_count, 4)
        self.assertEqual(self.engine.web.search_web.await_count, 2)
        self.assertFalse(result["complete"])
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=TimeoutError())):
            result = await self.engine.web_context(self.job)
        self.assertFalse(result["complete"])
        self.assertEqual(result["verified_source_urls"], [])

    async def test_standard_source_cap_below_and_above_three_supports_sequential_reads(self):
        for cap in (1, 5):
            with self.subTest(cap=cap):
                self.fixture.db.set_ai_config(10, ai_web_max_sources=cap)
                await self.engine.listen(self.fixture.message(200 + cap, ping=True))
                self.job = self.fixture.cog._queue.get_nowait()
                urls = [f"https://official.example/{index}" for index in range(cap + 1)]
                self.engine.web = SimpleNamespace(close=AsyncMock(),
                    search_web=AsyncMock(return_value={"results": [{"url": url} for url in urls]}),
                    read_webpage=AsyncMock(side_effect=lambda url, **_: {"url": url, "text": "Supported public fact."}))
                replies = [self.call("search_web", "Example official", 0)]
                replies.extend(self.call("read_webpage", url, index + 1) for index, url in enumerate(urls))
                replies.append({"role": "assistant", "content": "done"})
                with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)):
                    result = await self.engine.web_context(self.job)
                self.assertEqual(self.engine.web.read_webpage.await_count, cap)
                self.assertEqual(result["diagnostics"]["read_attempts"], cap)
                self.assertEqual(result["pages_read"], cap)

    async def test_standard_cap_counts_failed_access_but_not_rejected_or_duplicate_urls(self):
        self.fixture.db.set_ai_config(10, ai_web_max_sources=2)
        await self.engine.listen(self.fixture.message(202, ping=True))
        self.job = self.fixture.cog._queue.get_nowait()
        first, second = "https://official.example/a", "https://official.example/b"
        self.engine.web = SimpleNamespace(close=AsyncMock(),
            search_web=AsyncMock(return_value={"results": [{"url": first}, {"url": second}]}),
            read_webpage=AsyncMock(side_effect=[ValueError("read failed"), {"url": second, "text": "Supported public fact."}]))
        replies = [
            {"tool_calls": [self.fixture.tool_call("read_webpage", url, f"read{index}") for index, url in enumerate((
                "http://localhost/private", "https://cdn.discordapp.com/file"))]},
            self.call("search_web", "Example official", 0),
            {"tool_calls": [self.fixture.tool_call("read_webpage", url, f"read{index + 2}") for index, url in enumerate((
                "https://invented.example/a", first, first + "#fragment", second))]},
            self.call("read_webpage", "https://official.example/c", 20)]
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)):
            result = await self.engine.web_context(self.job)
        self.assertEqual([call.args[0] for call in self.engine.web.read_webpage.await_args_list], [first, second])
        self.assertEqual(result["diagnostics"]["read_attempts"], 2)
        self.assertEqual(result["pages_read"], 1)

    async def test_deep_research_ignores_standard_source_cap(self):
        self.fixture.db.set_ai_config(10, ai_web_max_sources=1)
        await self.engine.listen(self.fixture.message(201, ping=True))
        self.job = self.fixture.cog._queue.get_nowait()
        self.job["deep_think"] = True
        urls = [f"https://official.example/{index}" for index in range(5)]
        self.engine.web = SimpleNamespace(close=AsyncMock(), read_webpage=AsyncMock(side_effect=lambda url, **_: {
            "url": url, "text": "Supported public fact."}))
        replies = [self.call("read_webpage", url, index) for index, url in enumerate(urls)]
        replies.insert(3, report([fact("Fact", urls[2], "Supported public fact.")], gaps=["Independent confirmation needed"]))
        replies.append(report([fact("Fact", urls[-1], "Supported public fact.")], sufficient=True))
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)):
            result = await self.engine.web_context(self.job)
        self.assertEqual(self.engine.web.read_webpage.await_count, 5)
        self.assertTrue(result["complete"])

    async def test_quick_retrieval_small_context_keeps_a_useful_passage_with_long_urls(self):
        self.job.update(deep_think=False, web_byte_allowance=8192)
        urls = [f"https://official.example/{index}/" + "x" * 1990 for index in range(3)]
        self.engine.web = SimpleNamespace(close=AsyncMock(), read_webpage=AsyncMock(side_effect=lambda url, **_: {
            "url": url, "text": "Memory: 16 GB. " + "additional detail " * 500}))
        replies = [{"tool_calls": [self.fixture.tool_call("read_webpage", url, f"read{index}") for index, url in enumerate(urls)]},
                   {"role": "assistant", "content": "done"}]
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)):
            result = await self.engine.web_context(self.job)
        self.assertEqual(result["retrieval_mode"], "quick")
        self.assertTrue(result["page_evidence_untrusted"])
        self.assertIn("Memory: 16 GB.", result["page_evidence_untrusted"][0]["text"])
        self.assertTrue(result.get("page_evidence_truncated"))
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode("utf-8")), 8192)

    async def test_model_can_exceed_all_old_search_read_and_round_quotas(self):
        self.engine.web = SimpleNamespace(search_web=AsyncMock(side_effect=lambda query: {"results": [
            {"url": f"https://official.example/{int(query.rsplit(' ', 1)[-1])}"}]}),
            read_webpage=AsyncMock(side_effect=lambda url, **_: {"url": url, "text": "Release 9 is current."}), close=AsyncMock())
        replies = [self.call("search_web", f"Example query {index}", index) for index in range(22)]
        for index in range(14):
            url = f"https://official.example/{index}"
            replies.append(self.call("read_webpage", url, index + 22))
            if (index + 1) % 3 == 0:
                replies.append(report([fact(f"Source {index}", url, "Release 9 is current.")], gaps=["More comparison needed"]))
        replies.append(report([fact("Current version", "https://official.example/13", "Release 9 is current.")], sufficient=True))
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)):
            result = await self.engine.web_context(self.job)
        self.assertEqual(self.engine.web.search_web.await_count, 22)
        self.assertEqual(self.engine.web.read_webpage.await_count, 14)
        self.assertTrue(result["complete"])
        self.assertTrue(result["assessment"]["sufficient"])

    async def test_insufficient_checkpoint_continues_with_quotes_dates_and_material_gaps(self):
        first, second = "https://official.example/version", "https://official.example/performance"
        version = fact("Current version", first, "Release 9 is current.")
        performance = fact("Performance", second, "Performance improved 20 percent.")
        self.engine.web = SimpleNamespace(search_web=AsyncMock(return_value={"results": []}), close=AsyncMock(),
            read_webpage=AsyncMock(side_effect=[
                {"url": first, "text": "Release 9 is current.\n\n" + "RAW_CORPUS " * 500, "retrieved_at_utc": "2030-01-02", "published_at_untrusted": "2030-01-01"},
                {"url": second, "text": "Performance improved 20 percent."}]))
        replies = [self.call("read_webpage", first),
            report([version, {"claim": "Performance", "status": "unknown", "support": []}], gaps=["Performance unverified"]),
            self.call("read_webpage", second, 1), report([version, performance], sufficient=True)]
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)) as provider:
            result = await self.engine.web_context(self.job)
        self.assertEqual(provider.await_count, 4)
        resumed = json.dumps(provider.await_args_list[2].args[2])
        self.assertIn("Performance unverified", resumed)
        self.assertIn("2030-01-01", resumed)
        self.assertIn("Release 9 is current.", resumed)
        self.assertNotIn("RAW_CORPUS", resumed)
        self.assertTrue(result["assessment"]["sufficient"])
        self.assertEqual(result["assessment"]["facts"][0]["support"][0]["retrieved_at_utc"], "2030-01-02")

    async def test_missing_report_and_provider_timeout_are_errors_not_partial_completion(self):
        for response in ({"role": "assistant", "content": "done"}, TimeoutError()):
            with self.subTest(response=response):
                provider = AsyncMock(side_effect=response) if isinstance(response, Exception) else AsyncMock(return_value=response)
                with patch("cogs._conversation.request_completion", new=provider), self.assertRaises((ValueError, TimeoutError)):
                    await self.engine.web_context(self.job)

    async def test_model_can_request_essential_clarification_without_claiming_sufficiency(self):
        question = "¿Qué modelo y revisión de la GPU tienes?"
        with patch("cogs._conversation.request_completion", new=AsyncMock(return_value=report(
                gaps=["Unknown hardware revision"], clarification=question))):
            result = await self.engine.web_context(self.job)
        self.assertEqual(result["clarification_required"], question)
        self.assertFalse(result["assessment"]["sufficient"])
        self.assertTrue(result["complete"])

    async def test_identical_requests_and_checkpoint_state_return_explicitly_incomplete_evidence(self):
        self.engine.web = SimpleNamespace(search_web=AsyncMock(return_value={"results": []}), close=AsyncMock())
        replies = [self.call("search_web", "Example", 1), report(gaps=["Version unknown"]),
                   self.call("search_web", "Example", 2), report(gaps=["Version unknown"])]
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)):
            result = await self.engine.web_context(self.job)
        self.assertFalse(result["complete"])
        self.assertFalse(result["assessment"]["sufficient"])
        self.assertEqual(result["diagnostics"]["stop_reason"], "research_no_progress")

    async def test_cancellation_interrupts_research_without_continuing_tools(self):
        entered = asyncio.Event()
        async def pending(*_, **__):
            entered.set()
            await asyncio.Event().wait()
        self.engine.web = SimpleNamespace(search_web=AsyncMock(), close=AsyncMock())
        with patch("cogs._conversation.request_completion", new=pending):
            task = asyncio.create_task(self.engine.web_context(self.job))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.engine.web.search_web.assert_not_awaited()

    async def test_repeated_search_results_force_checkpoint_without_limiting_fresh_research(self):
        url = "https://official.example/version"
        self.engine.web = SimpleNamespace(close=AsyncMock(), search_web=AsyncMock(return_value={"results": [{"url": url}]}))
        replies = [self.call("search_web", f"rephrased query {i}", i) for i in range(4)]
        replies.append(report(gaps=["Version remains unverified"]))
        replies.append(report(gaps=["Version remains unverified"]))
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)) as provider:
            result = await self.engine.web_context(self.job)
        self.assertEqual([tool["function"]["name"] for tool in provider.await_args_list[4].kwargs["tools"]], ["submit_research"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["diagnostics"]["stop_reason"], "research_no_progress")

    async def test_validated_checkpoint_is_saved_before_a_later_provider_failure(self):
        url = "https://official.example/version"
        known = fact("Current version", url, "Release 9 is current.")
        self.engine.web = SimpleNamespace(close=AsyncMock(), read_webpage=AsyncMock(return_value={"url": url, "text": "Release 9 is current."}))
        replies = [self.call("read_webpage", url), report([known], gaps=["Performance unknown"]), TimeoutError()]
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)):
            result = await self.engine.web_context(self.job)
        self.assertFalse(result["complete"])
        self.assertEqual(result["assessment"]["facts"][0]["status"], "supported")
        saved = self.engine.research.load(self.engine.research_key(self.job), self.engine.research_token(self.job))
        self.assertEqual(saved["assessment"]["facts"][0]["claim"], "Current version")

    async def test_deep_length_recovery_checkpoints_once_without_replaying_truncated_output(self):
        from cogs._llm_moderation import EndpointIncompleteResponse
        url = "https://official.example/version"
        self.engine.web = SimpleNamespace(close=AsyncMock(), read_webpage=AsyncMock(return_value={"url": url, "text": "Release 9 is current."}))
        replies = [self.call("read_webpage", url), EndpointIncompleteResponse("length"),
                   report([fact("Current version", url, "Release 9 is current.")], sufficient=True)]
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)) as provider:
            result = await self.engine.web_context(self.job)
        self.assertTrue(result["complete"])
        recovery = provider.await_args_list[-1]
        self.assertEqual(recovery.kwargs["reasoning_effort"], "none")
        self.assertEqual(recovery.kwargs["max_output_tokens"], provider.await_args_list[-2].kwargs["max_output_tokens"])
        self.assertEqual([tool["function"]["name"] for tool in recovery.kwargs["tools"]], ["submit_research"])
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=EndpointIncompleteResponse("length"))) as provider:
            with self.assertRaises(EndpointIncompleteResponse):
                await self.engine.generate(self.job, [{"role": "system", "content": "Answer"}])
        self.assertEqual(provider.await_count, 2)

    async def test_quick_output_limit_preserves_read_evidence_after_one_recovery(self):
        from cogs._llm_moderation import EndpointIncompleteResponse
        self.job["deep_think"] = False
        url = "https://official.example/version"
        for reason in ("length", "max_output_tokens"):
            with self.subTest(reason=reason):
                self.engine.web = SimpleNamespace(close=AsyncMock(), read_webpage=AsyncMock(return_value={
                    "url": url, "text": "Release 9 is current.", "retrieved_at_utc": "2030-01-02"}))
                with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=[
                        self.call("read_webpage", url), EndpointIncompleteResponse(reason), EndpointIncompleteResponse(reason)])) as provider:
                    result = await self.engine.web_context(self.job)
                self.assertEqual(provider.await_count, 3)
                self.assertEqual(result["diagnostics"]["stop_reason"], "endpoint_output_limit")
                self.assertFalse(result["complete"])
                self.assertFalse(result["assessment"]["sufficient"])
                self.assertEqual(result["verified_source_urls"], [url])
                self.assertEqual(result["page_evidence_untrusted"][0]["text"], "Release 9 is current.")
                self.assertEqual(result["sources"][0]["retrieved_at_utc"], "2030-01-02")

    async def test_quick_output_limit_never_promotes_search_snippets_to_verified_evidence(self):
        from cogs._llm_moderation import EndpointIncompleteResponse
        self.job["deep_think"] = False
        url = "https://official.example/version"
        self.engine.web = SimpleNamespace(close=AsyncMock(), search_web=AsyncMock(return_value={"results": [
            {"url": url, "snippet": "Unverified release 99"}]}))
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=[
                self.call("search_web", "Example version"), EndpointIncompleteResponse("length"), EndpointIncompleteResponse("length")])) as provider:
            result = await self.engine.web_context(self.job)
        self.assertEqual(provider.await_count, 3)
        self.assertEqual(result["diagnostics"]["stop_reason"], "endpoint_output_limit")
        self.assertEqual(result["verified_source_urls"], [])
        self.assertEqual(result["page_evidence_untrusted"], [])
        self.assertEqual(result["assessment"]["facts"], [])
        self.assertFalse(result["complete"])

    async def test_quick_output_recovery_can_finish_a_tool_call_before_any_page_was_read(self):
        from cogs._llm_moderation import EndpointIncompleteResponse
        self.job["deep_think"] = False
        url = "https://official.example/version"
        self.engine.web = SimpleNamespace(close=AsyncMock(), read_webpage=AsyncMock(return_value={
            "url": url, "text": "Release 9 is current."}))
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=[EndpointIncompleteResponse("length"),
                self.call("read_webpage", url), {"content": "done"}])) as provider:
            result = await self.engine.web_context(self.job)
        self.assertEqual(provider.await_count, 3)
        for call in provider.await_args_list:
            self.assertEqual(call.kwargs["max_output_tokens"], 16384)
            self.assertEqual(call.kwargs["reasoning_effort"], "none")
        self.assertEqual(self.engine.budget_config(self.job, "web")["output_reserve"], 16384)
        self.assertIn("read_webpage", [tool["function"]["name"] for tool in provider.await_args_list[1].kwargs["tools"]])
        self.assertEqual(result["verified_source_urls"], [url])
        self.assertEqual(result["page_evidence_untrusted"][0]["text"], "Release 9 is current.")

    async def test_quick_output_recovery_does_not_accept_filtering_or_invalid_tool_payloads(self):
        from cogs._llm_moderation import EndpointIncompleteResponse
        self.job["deep_think"] = False
        invalid = {"tool_calls": [{"id": "invalid", "function": {
            "name": "read_webpage", "arguments": '{"url":"https://official.example/version","extra":"ignored?"}'}}]}
        for response in (EndpointIncompleteResponse("content_filter"), invalid):
            with self.subTest(response=type(response).__name__):
                provider = AsyncMock(side_effect=response) if isinstance(response, Exception) else AsyncMock(return_value=response)
                with patch("cogs._conversation.request_completion", new=provider):
                    with self.assertRaises(ValueError):
                        await self.engine.web_context(self.job)
                self.assertEqual(provider.await_count, 1)

    async def test_quick_16k_planner_uses_remaining_total_deadline_for_each_dispatch(self):
        from cogs._llm_moderation import EndpointIncompleteResponse
        self.job["deep_think"] = False
        loop, clock = asyncio.get_running_loop(), [asyncio.get_running_loop().time()]
        url = "https://official.example/version"
        async def advance(seconds):
            clock[0] += seconds
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        async def read(target, **kwargs):
            await advance(20)
            return {"url": target, "text": "Release 9 is current."}
        replies = [self.call("read_webpage", url), EndpointIncompleteResponse("length"), {"content": "done"}]
        durations, timeouts = iter((35, 10, 10)), []
        async def provider(*args, **kwargs):
            timeouts.append(kwargs["timeout_seconds"])
            async with asyncio.timeout(kwargs["timeout_seconds"]):
                await advance(next(durations))
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        self.engine.web = SimpleNamespace(close=AsyncMock(), read_webpage=read)
        with patch.object(loop, "time", lambda: clock[0]), patch.object(loop, "slow_callback_duration", 1000), \
                patch("cogs._conversation.request_completion", new=provider):
            result = await self.engine.web_context(self.job)
        self.assertEqual(timeouts, [120, 65, 55])
        self.assertEqual(result["verified_source_urls"], [url])
        self.assertEqual(result["diagnostics"]["stop_reason"], "planner_stop")

    async def test_quick_exhausted_output_reaches_answer_with_read_material_instead_of_generic_error(self):
        from cogs._llm_moderation import EndpointIncompleteResponse
        self.job["deep_think"] = False
        url = "https://official.example/version"
        intent = {"tool_calls": [{"id": "intent", "type": "function", "function": {
            "name": "request_public_web_research", "arguments": json.dumps({"public_brief": {
                "topic": "Example", "question": "Current Example version?"}})}}]}
        self.engine.web = SimpleNamespace(close=AsyncMock(), read_webpage=AsyncMock(return_value={
            "url": url, "text": "Release 9 is current."}))
        answer = "La documentación consultada indica la versión 9."
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=[intent,
                self.call("read_webpage", url), EndpointIncompleteResponse("length"),
                EndpointIncompleteResponse("length"), answer])) as provider:
            await self.engine.process(self.job)
        self.assertEqual(provider.await_count, 5)
        final_context = json.loads(provider.await_args.args[2][1]["content"])["web_research_untrusted"]
        self.assertEqual(final_context["diagnostics"]["stop_reason"], "endpoint_output_limit")
        self.assertIn("Release 9 is current.", final_context["page_evidence_untrusted"][0]["text"])
        self.assertEqual(self.job["message"].reply.await_args.args[0], answer)

    async def test_useful_pages_trigger_early_sufficiency_check_without_filling_context(self):
        urls = [f"https://official.example/source{i}" for i in range(3)]
        self.engine.web = SimpleNamespace(close=AsyncMock(), read_webpage=AsyncMock(side_effect=lambda url, **_: {
            "url": url, "text": "Release 9 is current."}))
        replies = [self.call("read_webpage", url, i) for i, url in enumerate(urls)]
        replies.append(report([fact("Current version", urls[0], "Release 9 is current.")], sufficient=True))
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)) as provider:
            result = await self.engine.web_context(self.job)
        self.assertTrue(result["complete"])
        self.assertEqual(provider.await_count, 4)
        self.assertEqual([tool["function"]["name"] for tool in provider.await_args.kwargs["tools"]], ["submit_research"])

    async def test_length_recovery_rechecks_revoked_authority_and_never_retries_filtering(self):
        from cogs._llm_moderation import EndpointIncompleteResponse
        messages = [{"role": "system", "content": "Answer"}]
        with patch.object(self.engine, "unchanged", new=AsyncMock(side_effect=[True, False])), patch(
                "cogs._conversation.request_completion", new=AsyncMock(side_effect=EndpointIncompleteResponse("length"))) as provider:
            self.assertIsNone(await self.engine.generate(self.job, messages))
        self.assertEqual(provider.await_count, 1)
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=EndpointIncompleteResponse("content_filter"))) as provider:
            with self.assertRaises(EndpointIncompleteResponse):
                await self.engine.generate(self.job, messages)
        self.assertEqual(provider.await_count, 1)

    async def test_rephrasing_gaps_or_rereading_identical_pages_cannot_fake_checkpoint_progress(self):
        url = "https://official.example/version"
        known = fact("Current version", url, "Release 9 is current.")
        for reread in (False, True):
            with self.subTest(reread=reread):
                self.engine.web = SimpleNamespace(close=AsyncMock(), read_webpage=AsyncMock(return_value={"url": url, "text": "Release 9 is current."}))
                replies = [self.call("read_webpage", url), report([known], gaps=["Gap A"])]
                if reread:
                    replies.append(self.call("read_webpage", url, 1))
                replies.append(report([known], gaps=["Same missing point, phrased as gap B"]))
                with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)) as provider:
                    result = await self.engine.web_context(self.job)
                self.assertFalse(result["complete"])
                self.assertEqual(result["diagnostics"]["stop_reason"], "research_no_progress")
                self.assertEqual(provider.await_count, 4 if reread else 3)
                self.assertEqual(result["assessment"]["facts"][0]["status"], "supported")

    async def test_rejected_no_progress_report_cannot_erase_the_accepted_checkpoint(self):
        url = "https://official.example/version"
        known = fact("Current version", url, "Release 9 is current.")
        self.engine.web = SimpleNamespace(close=AsyncMock(), read_webpage=AsyncMock(return_value={"url": url, "text": "Release 9 is current."}))
        replies = [self.call("read_webpage", url), report([known], gaps=["Performance unknown"]), report(gaps=["Different wording"])]
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)):
            result = await self.engine.web_context(self.job)
        self.assertFalse(result["complete"])
        self.assertEqual(result["assessment"]["facts"][0]["claim"], "Current version")
        saved = self.engine.research.load(self.engine.research_key(self.job), self.engine.research_token(self.job))
        self.assertEqual(saved["assessment"]["facts"][0]["claim"], "Current version")
