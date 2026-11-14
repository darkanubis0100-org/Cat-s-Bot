"""Public-intent provenance and bounded source discovery regressions."""
import json
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests import test_conversation_memory as fixtures


class PublicSelectionTests(unittest.TestCase):
    def setUp(self):
        from cogs._conversation import web_selection_context, web_selection_arguments
        self.select = web_selection_arguments
        self.original = {"trigger": {"text": "¿La versión de Cafe\u0301‑Δ?"},
                         "referenced_message_untrusted": {"text": "Tema: Cafe\u0301‑Δ.", "author_id": "30"},
                         "conversation_untrusted": [{"text": "Otro producto"}],
                         "personal_memories_untrusted": [{"text": "Private product"}],
                         "research_notes_untrusted": {"summary": "Invented product", "public_brief": None}}
        self.context, self.sources = web_selection_context(self.original)
        self.arguments = {"topic": {"source": "r", "first": 2, "last": 5}, "question": {"first": 0, "last": 8}}

    def test_reconstruction_preserves_original_unicode_punctuation_and_attribution(self):
        self.assertEqual(self.select(self.arguments, self.context, self.sources),
                         {"topic": "Cafe\u0301‑Δ", "question": self.original["trigger"]["text"]})
        self.assertEqual(self.context["referenced_message_untrusted"]["author_id"], "30")
        self.assertEqual(self.original["referenced_message_untrusted"]["text"], "Tema: Cafe\u0301‑Δ.")
        self.assertNotIn("Private product", repr(self.sources))
        self.assertNotIn("Invented product", repr(self.sources))

    def test_pruned_modified_or_unlisted_sources_cannot_be_resolved_from_the_hidden_map(self):
        for source in ("missing", "c0"):
            with self.subTest(source=source):
                fitted = {**self.context, "conversation_untrusted": []}
                arguments = {**self.arguments, "topic": {"source": source, "first": 0, "last": 1}}
                with self.assertRaisesRegex(ValueError, "unavailable_web_source"):
                    self.select(arguments, fitted, self.sources)
        changed = copy.deepcopy(self.context)
        changed["referenced_message_untrusted"]["text"]["tokens"][2][1] = "Different"
        with self.assertRaisesRegex(ValueError, "modified_web_source"):
            self.select(self.arguments, changed, self.sources)

    def test_ranges_are_strict_contiguous_in_bounds_and_question_cannot_change_source(self):
        for first, last in ((True, 5), (2, False), (2.0, 5), ("2", 5), (-1, 5), (5, 2), (2, 99)):
            with self.subTest(first=first, last=last), self.assertRaisesRegex(ValueError, "invalid_web_span"):
                self.select({**self.arguments, "topic": {"source": "r", "first": first, "last": last}}, self.context, self.sources)
        for arguments in ({"public_brief": {"topic": "Cafe", "question": "Question"}},
                          {**self.arguments, "topic": {"source": "r", "first": 2, "last": 5, "join": [7]}}):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, "invalid_web_selection"):
                self.select(arguments, self.context, self.sources)

    def test_question_source_requires_visible_unchanged_text_identical_to_current_trigger(self):
        from cogs._conversation import web_selection_context
        self.original["conversation_untrusted"].append({"text": self.original["trigger"]["text"]})
        context, sources = web_selection_context(self.original)
        selected = {**self.arguments, "question": {"source": "c1", "first": 0, "last": 8}}
        expected = {"topic": "Cafe\u0301‑Δ", "question": self.original["trigger"]["text"]}
        self.assertEqual(self.select(selected, context, sources), expected)
        self.assertEqual(self.select(self.arguments, context, sources), expected)
        for source in ("c0", "r"):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "invalid_question_source"):
                self.select({**selected, "question": {"source": source, "first": 0, "last": 0}}, context, sources)
        with self.assertRaisesRegex(ValueError, "unavailable_web_source"):
            self.select(selected, {**context, "conversation_untrusted": context["conversation_untrusted"][:1]}, sources)
        for field in ("trigger", "duplicate"):
            with self.subTest(altered=field):
                changed = copy.deepcopy(context)
                row = changed["trigger"] if field == "trigger" else changed["conversation_untrusted"][1]
                row["text"]["tokens"][0][1] = "Altered"
                with self.assertRaisesRegex(ValueError, "modified_web_source"):
                    self.select(selected, changed, sources)

    def test_tool_source_enums_use_only_intact_visible_annotations_without_mutating_global_schema(self):
        from cogs._conversation import WEB_SELECTION_TOOL, web_selection_context, web_selection_tool
        original_tool = copy.deepcopy(WEB_SELECTION_TOOL)
        self.original["conversation_untrusted"].append({"text": self.original["trigger"]["text"]})
        context, sources = web_selection_context(self.original)
        fitted = {**context, "conversation_untrusted": context["conversation_untrusted"][1:]}
        properties = web_selection_tool(fitted, sources)["function"]["parameters"]["properties"]
        self.assertEqual(properties["topic"]["properties"]["source"]["enum"], ["c1", "r", "t"])
        self.assertEqual(properties["question"]["properties"]["source"]["enum"], ["c1", "t"])
        self.assertEqual(properties["question"]["required"], ["source", "first", "last"])
        fitted["conversation_untrusted"][0]["text"]["tokens"][0][1] = "Altered"
        properties = web_selection_tool(fitted, sources)["function"]["parameters"]["properties"]
        self.assertEqual(properties["question"]["properties"]["source"]["enum"], ["t"])
        self.assertEqual(WEB_SELECTION_TOOL, original_tool)

    def test_reconstructed_spans_keep_existing_privacy_and_length_limits(self):
        from cogs._conversation import web_selection_context
        for text, reason in (("password: private-value", "private_public_brief"), ("X" * 201, "invalid_public_brief")):
            with self.subTest(reason=reason):
                context, sources = web_selection_context({"trigger": {"text": "Busca Example"},
                    "referenced_message_untrusted": {"text": text}})
                last = len(context["referenced_message_untrusted"]["text"]["tokens"]) - 1
                arguments = {"topic": {"source": "r", "first": 0, "last": last}, "question": {"first": 0, "last": 1}}
                with self.assertRaisesRegex(ValueError, reason):
                    self.select(arguments, context, sources)


class PublicSelectionBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_history_can_be_pruned_with_its_enum_cost_and_keep_required_context(self):
        from cogs._conversation import web_selection_context, web_selection_tool, WEB_CLARIFICATION_TOOL
        from cogs._conversation_budget import ConversationBudget
        current = "Busca Oriole"
        context = {"trigger": {"text": current}, "conversation_untrusted": [{"text": current} for _ in range(5000)]}
        annotated, sources = web_selection_context(context)
        cfg = {"endpoint_url": "https://fixture.invalid/v1", "model": "fixture", "context_window_tokens": 50000, "output_reserve": 16384}
        prompt = "Select the public request."
        fitted = await ConversationBudget().fit(None, cfg, prompt, annotated, [])
        self.assertGreater(len(fitted["conversation_untrusted"]), 0)
        self.assertLess(len(fitted["conversation_untrusted"]), 5000)
        for row in fitted["conversation_untrusted"]:
            row.pop("_web_source_enum_budget", None)
        tool = web_selection_tool(fitted, sources)
        actual_input_bound = len((prompt + json.dumps(fitted, ensure_ascii=False) + json.dumps([tool, WEB_CLARIFICATION_TOOL], ensure_ascii=False)).encode("utf-8"))
        self.assertLessEqual(actual_input_bound + 16384, cfg["context_window_tokens"])
        self.assertEqual(context["trigger"]["text"], current)
        self.assertNotIn("_web_source_enum_budget", json.dumps(context))
        self.assertNotIn("c0", tool["function"]["parameters"]["properties"]["topic"]["properties"]["source"]["enum"])

    async def test_required_context_that_really_exceeds_the_window_is_rejected(self):
        from cogs._conversation import web_selection_context
        from cogs._conversation_budget import ConversationBudget, ContextBudgetError
        context, _ = web_selection_context({"trigger": {"text": "X" * 10000}})
        cfg = {"endpoint_url": "https://fixture.invalid/v1", "model": "fixture", "context_window_tokens": 22000, "output_reserve": 16384}
        with self.assertRaises(ContextBudgetError):
            await ConversationBudget().fit(None, cfg, "Select the public request.", context, [])


class PublicHarnessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.ConversationRuntimeTest()
        await self.fixture.asyncSetUp()
        self.engine = self.fixture.engine
        self.fixture.db.set_ai_config(10, ai_web_enabled=1)

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def job(self, text):
        message = self.fixture.message(ping=True)
        message.content = text
        await self.engine.listen(message)
        return self.fixture.cog._queue.get_nowait()

    def intent(self, brief):
        return {"tool_calls": [{"id": "intent", "type": "function", "function": {
            "name": "request_public_web_research", "arguments": json.dumps({"public_brief": brief})}}]}

    def call(self, name, argument, index):
        return {"tool_calls": [self.fixture.tool_call(name, argument, str(index))]}

    async def test_invented_premise_is_repaired_before_isolated_planner(self):
        question = "dame specs de la tarjeta gráfica mas potente de bolt.graphics"
        job = await self.job("Holiwis, " + question + ". Mi horario privado es de noche.")
        invented = {"topic": "Bolt Graphics — GPU cloud provider", "question": "Which cloud GPU does Bolt offer?"}
        brief = {"topic": "bolt.graphics", "question": question}
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=[
                self.intent(invented), fixtures.web_selection(brief["topic"], question, current=job["message"].content), "Respuesta pública."])) as provider, \
             patch.object(self.engine, "web_context", new=AsyncMock(return_value={"sources": []})) as research:
            await self.engine.process(job)
        self.assertEqual(research.await_args.args[0]["public_web_brief"], brief)
        self.assertEqual(provider.await_count, 3)
        self.assertNotIn("Mi horario", json.dumps(job["public_web_brief"]))

    async def test_public_intent_repair_preserves_web_profile_and_authorized_context(self):
        self.fixture.db.set_ai_config(10, ai_web_timeout_seconds=150)
        self.fixture.db.set_automod_config(10, rules={"llm": {"endpoint_url": "https://provider.example/v1",
            "model": "synthetic", "chat_reasoning_effort": "max", "web_reasoning_effort": "high"}})
        self.fixture.db.append_ai_chat_turn(10, 20, 90, 30, "User", "Estoy mirando Example. PRIVATE_CONTEXT")
        job = await self.job("Busca la última versión")
        invalid = self.intent({"topic": "Example", "question": "Find the latest version"})
        brief = {"topic": "Example", "question": job["message"].content}
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=[invalid,
                fixtures.web_selection("Example", brief["question"], source="c0", topic_text="Estoy mirando Example. PRIVATE_CONTEXT")])) as provider, \
                patch.object(self.engine, "web_context", new=AsyncMock(return_value=None)) as research:
            await self.engine.process(job)
        self.assertEqual(provider.await_count, 2)
        repair = provider.await_args_list[1]
        self.assertEqual(repair.kwargs["max_output_tokens"], 16384)
        self.assertEqual(repair.kwargs["reasoning_effort"], "high")
        self.assertEqual(repair.kwargs["timeout_seconds"], 150)
        self.assertEqual(repair.kwargs["tool_choice"], "required")
        self.assertIn("PRIVATE_CONTEXT", json.dumps(repair.args[2]))
        self.assertNotIn("_web_source_enum_budget", json.dumps(repair.args[2]))
        self.assertEqual(research.await_args.args[0]["public_web_brief"], brief)
        self.assertNotIn("PRIVATE_CONTEXT", json.dumps(job["public_web_brief"]))

    async def test_followup_resolves_the_public_entity_even_when_generic_topic_is_grounded_twice(self):
        for index, repeated_attribute in enumerate((False, True)):
            with self.subTest(repeated_attribute=repeated_attribute):
                previous = self.fixture.message(90 + index)
                previous.content = "Estoy mirando Oriole en oriole.example. PRIVATE_CONTEXT"
                if repeated_attribute:
                    previous.content += " Me interesa la última versión."
                self.fixture.db.append_ai_chat_turn(10, 20, previous.id, 30, "User", previous.content)
                message = self.fixture.message(700 + index, ping=True)
                message.content = "¿Y cuál es la última versión?"
                await self.engine.listen(message)
                job = self.fixture.cog._queue.get_nowait()
                generic = {"topic": "la última versión", "question": message.content}
                resolved = {"topic": "Oriole", "question": message.content}
                with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=[
                        self.intent(generic), fixtures.web_selection("Oriole", message.content, source="c0", topic_text=previous.content)])) as provider, \
                        patch.object(self.engine, "web_context", new=AsyncMock(return_value=None)) as research:
                    await self.engine.process(job)
                self.assertEqual(provider.await_count, 2)
                self.assertEqual(research.await_args.args[0]["public_web_brief"], resolved)
                self.assertEqual(provider.await_args.kwargs["max_output_tokens"], 16384)
                self.assertEqual(provider.await_args.kwargs["tool_choice"], "required")
                self.assertNotIn("PRIVATE_CONTEXT", json.dumps(job["public_web_brief"]))

    async def test_resolver_selects_literal_spans_instead_of_writing_a_public_label(self):
        previous = self.fixture.message(90)
        previous.content = "Estoy mirando Python en python.org. PRIVATE_CONTEXT"
        self.fixture.db.append_ai_chat_turn(10, 20, previous.id, 30, "User", previous.content)
        job = await self.job("¿Y cuál es la última versión?")
        routing = self.intent({"topic": "Python", "question": job["message"].content})
        selection = {"tool_calls": [{"id": "select", "type": "function", "function": {
            "name": "select_public_web_research", "arguments": json.dumps({
                "topic": {"source": "c0", "first": 2, "last": 6}, "question": {"source": "c1", "first": 0, "last": 7}})}}]}
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=[routing, selection])) as provider, \
                patch.object(self.engine, "web_context", new=AsyncMock(return_value=None)) as research:
            await self.engine.process(job)
        research.assert_awaited_once()
        self.assertEqual(job["public_web_brief"], {"topic": "Python en python.org", "question": job["message"].content})
        self.assertEqual(provider.await_count, 2)
        self.assertNotIn("PRIVATE_CONTEXT", json.dumps(job["public_web_brief"]))

    async def test_selection_cannot_recover_a_history_source_pruned_by_budget(self):
        self.fixture.db.append_ai_chat_turn(10, 20, 90, 30, "User", "Estoy mirando Oriole.")
        job = await self.job("¿Cuál es la última versión?")
        fit = self.engine.budget.fit
        async def prune(*args, **kwargs):
            result = await fit(*args, **kwargs)
            if isinstance(result["trigger"]["text"], dict):
                result["conversation_untrusted"] = []
            return result
        selected = fixtures.web_selection("Oriole", job["message"].content, topic_text="Estoy mirando Oriole.", source="c0")
        with patch.object(self.engine.budget, "fit", side_effect=prune), \
                patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=[
                    self.intent({"topic": "Oriole", "question": job["message"].content}), selected])) as provider, \
                patch.object(self.engine, "web_context", new=AsyncMock()) as research:
            await self.engine.process(job)
        self.assertEqual(provider.await_count, 2)
        self.assertNotIn("Oriole", provider.await_args.args[2][1]["content"])
        research.assert_not_awaited()
        self.assertNotIn("public_web_brief", job)

    async def test_scope_cancelled_during_subject_selection_stops_before_public_effects(self):
        job = await self.job("Busca Oriole")
        self.engine.web = SimpleNamespace(close=AsyncMock(), search_web=AsyncMock(), read_webpage=AsyncMock())
        async def provider(*args, **kwargs):
            names = {tool["function"]["name"] for tool in kwargs["tools"]}
            if "select_public_web_research" in names:
                self.fixture.db.set_ai_config(10, ai_enabled=0)
                return fixtures.web_selection("Oriole", job["message"].content)
            return self.intent({"topic": "Oriole", "question": job["message"].content})
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=provider)) as request:
            await self.engine.process(job)
        self.assertEqual(request.await_count, 2)
        self.engine.web.search_web.assert_not_awaited()
        self.engine.web.read_webpage.assert_not_awaited()
        job["message"].reply.assert_not_awaited()

    async def test_invalid_intent_diagnostics_distinguish_shape_from_provenance_without_values(self):
        job = await self.job("Dame las specs de Example")
        invalid = {"tool_calls": [{"id": "intent", "type": "function", "function": {
            "name": "request_public_web_research", "arguments": json.dumps({"PRIVATE_ARGUMENT_KEY": "PRIVATE_ARGUMENT_VALUE"})}}]}
        repair = fixtures.web_selection("Example", job["message"].content, source="MISSING_PRIVATE_SOURCE")
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=[invalid, repair])) as provider, \
                patch.object(self.engine, "web_context", new=AsyncMock()) as research, \
                self.assertLogs("cogs._conversation", level="WARNING") as logged:
            await self.engine.process(job)
        text = "\n".join(logged.output)
        self.assertIn("stage=initial", text)
        self.assertIn("reason=invalid_web_brief_arguments", text)
        self.assertIn("stage=repair", text)
        self.assertIn("reason=unavailable_web_source", text)
        self.assertIn("tool_count=1", text)
        self.assertIn("argument_shape=", text)
        self.assertNotIn("PRIVATE_ARGUMENT", text)
        self.assertNotIn("MISSING_PRIVATE_SOURCE", text)
        self.assertEqual(provider.await_count, 2)
        research.assert_not_awaited()

    async def test_failed_repair_reports_stage_without_private_fallback_or_extra_calls(self):
        for index, (repair, reason) in enumerate((({"content": "PRIVATE_MODEL_TEXT"}, "missing_tool"),
                (TimeoutError(), "provider_timeout"), (ValueError("endpoint_http_502"), "endpoint_http_502"))):
            with self.subTest(reason=reason):
                message = self.fixture.message(400 + index, ping=True)
                message.content = "Busca Example. Mi horario privado es de noche."
                await self.engine.listen(message)
                job = self.fixture.cog._queue.get_nowait()
                invalid = self.intent({"topic": "Example", "question": "Find Example"})
                with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=[invalid, repair])) as provider, \
                        patch.object(self.engine, "web_context", new=AsyncMock()) as research, \
                        self.assertLogs("cogs._conversation", level="WARNING") as logged:
                    await self.engine.process(job)
                text = "\n".join(logged.output)
                self.assertIn("stage=repair", text)
                self.assertIn("reason=" + reason, text)
                self.assertNotIn("PRIVATE_MODEL_TEXT", text)
                self.assertNotIn("horario privado", text)
                self.assertEqual(provider.await_count, 2)
                research.assert_not_awaited()
                self.assertNotIn("public_web_brief", job)

    async def test_repair_clarification_requires_the_explicit_empty_tool(self):
        for index, arguments in enumerate(({}, {"query": "PRIVATE_MODEL_VALUE"})):
            with self.subTest(arguments_valid=not arguments):
                message = self.fixture.message(500 + index, ping=True)
                message.content = "Busca eso"
                await self.engine.listen(message)
                job = self.fixture.cog._queue.get_nowait()
                invalid = self.intent({"topic": "Invented", "question": "Find invented information"})
                clarification = {"tool_calls": [{"id": "clarify", "type": "function", "function": {
                    "name": "request_public_web_clarification", "arguments": json.dumps(arguments)}}]}
                with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=[invalid, clarification])) as provider, \
                        patch.object(self.engine, "web_context", new=AsyncMock()) as research, \
                        self.assertLogs("cogs._conversation", level="INFO") as logged:
                    await self.engine.process(job)
                text = "\n".join(logged.output)
                self.assertIn("reason=" + ("invalid_clarification_arguments" if arguments else "clarification_requested"), text)
                self.assertNotIn("PRIVATE_MODEL_VALUE", text)
                self.assertEqual(provider.await_count, 2)
                research.assert_not_awaited()
                self.assertNotIn("public_web_brief", job)
                self.assertNotIn("Inténtalo más tarde", message.reply.await_args.args[0])

    async def test_repair_output_limit_recovery_stays_in_the_offered_tools_and_is_bounded(self):
        from cogs._llm_moderation import EndpointIncompleteResponse
        for index, (deep, recovered) in enumerate(((False, True), (True, True), (False, False))):
            with self.subTest(deep=deep, recovered=recovered):
                message = self.fixture.message(600 + index, ping=True)
                message.content = "Dame las specs de Example"
                await self.engine.listen(message)
                job = self.fixture.cog._queue.get_nowait()
                job["deep_think"] = deep
                brief = {"topic": "Example", "question": message.content}
                invalid = self.intent({"topic": "Example", "question": "Find Example specifications"})
                final = fixtures.web_selection("Example", message.content) if recovered else EndpointIncompleteResponse("length")
                with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=[
                        invalid, EndpointIncompleteResponse("length"), final])) as provider, \
                        patch.object(self.engine, "web_context", new=AsyncMock(return_value=None)) as research:
                    await self.engine.process(job)
                self.assertEqual(provider.await_count, 3)
                self.assertEqual(research.await_count, int(recovered))
                recovery = provider.await_args_list[-1]
                self.assertEqual(recovery.kwargs["tool_choice"], "required")
                self.assertEqual({tool["function"]["name"] for tool in recovery.kwargs["tools"]},
                                 {"select_public_web_research", "request_public_web_clarification"})
                self.assertNotIn("search/read", recovery.args[2][0]["content"])
                self.assertNotIn("submit_research", recovery.args[2][0]["content"])

    async def test_missing_public_projection_never_exports_current_text(self):
        job = await self.job("Busca Example; mi horario privado es de noche")
        with patch("cogs._conversation.request_completion", new=AsyncMock(return_value={"content": "done"})) as provider:
            result = await self.engine.web_context(job)
        provider.assert_not_awaited()
        self.assertIn("clarification_required", result)

    async def test_legacy_label_is_not_silently_accepted_as_a_selection(self):
        job = await self.job("Dame las specs de Example")
        paraphrase = {"topic": "Example", "question": "What are the Example specifications?"}
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=[
                self.intent(paraphrase), self.intent(paraphrase)])) as provider, \
             patch.object(self.engine, "web_context", new=AsyncMock()) as research:
            await self.engine.process(job)
        research.assert_not_awaited()
        self.assertEqual(provider.await_count, 2)
        reply = job["message"].reply.await_args.args[0]
        self.assertNotIn("Inténtalo más tarde", reply)
        self.assertIn("pregunta", reply)

    async def test_public_domain_seed_and_search_recovery_share_existing_limits(self):
        question = "dame specs de la tarjeta gráfica mas potente de bolt.graphics"
        job = await self.job(question + " y mi servidor privado es private.example")
        job["public_web_brief"] = {"topic": "bolt.graphics", "question": question}
        root = "https://bolt.graphics/"
        docs = "https://docs.example/wiki/x/public"
        self.engine.web = SimpleNamespace(close=AsyncMock(), search_web=AsyncMock(side_effect=[
            {"results": [{"url": "https://other.example/unrelated", "title": "Other supplier"}]},
            {"results": [{"url": root, "title": "Public manufacturer"}]}]),
            read_webpage=AsyncMock(side_effect=[{"url": root, "text": "Manufacturer documentation.", "links": [{"url": docs}]},
                {"url": docs, "text": "The largest PCIe card has 384 GB."}]))
        replies = [self.call("search_web", "unrelated cloud GPUs", 1),
            {"tool_calls": [self.fixture.tool_call("read_webpage", url, str(i)) for i, url in enumerate(
                ("https://bolt.graphics/guessed", root, docs), 2)]},
            self.call("search_web", "third query", 5), {"content": "done"}]
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)) as provider:
            result = await self.engine.web_context(job)
        self.assertEqual([call.args[0] for call in self.engine.web.search_web.await_args_list],
                         ["unrelated cloud GPUs", "site:bolt.graphics"])
        self.assertEqual([call.args[0] for call in self.engine.web.read_webpage.await_args_list], [root, docs])
        self.assertEqual(result["diagnostics"]["search_attempts"], 2)
        self.assertEqual(result["diagnostics"]["read_attempts"], 2)
        planner = json.loads(provider.await_args_list[0].args[2][1]["content"])
        self.assertEqual(planner["explicit_public_urls"], [root])
        self.assertNotIn("private.example", json.dumps(planner))
        self.assertIn("url_not_discovered", json.dumps(result["diagnostics"]))
        self.assertIn("quick_retrieval_limit", json.dumps(result["diagnostics"]))

    async def test_empty_planner_or_report_reads_public_seed_with_valid_replay_for_both_protocols(self):
        from tests.test_conversation_responses import response, text_item, reasoning, function_item
        url = "https://hardware.example/"
        for protocol in ("chat_completions", "responses"):
            for terminal in ("text", "report"):
                with self.subTest(protocol=protocol, terminal=terminal):
                    self.fixture.db.set_automod_config(10, rules={"llm": {"endpoint_url": "https://provider.example/v1",
                        "api_type": protocol, "model": "fixture", "enabled": False}})
                    job = await self.job("Dame las specs de hardware.example")
                    job["public_web_brief"] = {"topic": "hardware.example", "question": job["message"].content}
                    self.engine.web = SimpleNamespace(close=AsyncMock(), search_web=AsyncMock(),
                        read_webpage=AsyncMock(return_value={"url": url, "text": "The public card has 384 GB of memory."}))
                    first = {"role": "assistant", "content": "No evidence"} if terminal == "text" else fixtures.research_report(clarification="   ")
                    final = fixtures.research_report([fixtures.research_fact(url, "The public card has 384 GB of memory.")], sufficient=True)
                    final["tool_calls"][0]["id"] = "complete"
                    if protocol == "responses":
                        item = text_item("No evidence", "initial") if terminal == "text" else function_item(
                            "submit_research", json.loads(first["tool_calls"][0]["function"]["arguments"]), "report", "report_item")
                        replies = [response(reasoning("PUBLIC_REASONING", "first_reasoning"), item), response(function_item(
                            "submit_research", json.loads(final["tool_calls"][0]["function"]["arguments"]), "complete", "last"))]
                    else:
                        replies = [{"choices": [{"finish_reason": "tool_calls" if first.get("tool_calls") else "stop", "message": first}]},
                                   {"choices": [{"finish_reason": "tool_calls", "message": final}]}]
                    with patch("cogs._llm_moderation.endpoint_json", new=AsyncMock(side_effect=replies)) as provider:
                        result = await self.engine.web_context(job)
                    self.assertEqual(self.engine.web.read_webpage.await_count, 1)
                    self.assertEqual(self.engine.web.read_webpage.await_args.args[0], url)
                    self.engine.web.search_web.assert_not_awaited()
                    self.assertEqual(provider.await_count, 2)
                    self.assertEqual(result["verified_source_urls"], [url])
                    self.assertIn("384 GB", result["page_evidence_untrusted"][0]["text"])
                    replay = provider.await_args.args[2]
                    self.assertIn("read_webpage", json.dumps(replay))
                    if protocol == "responses":
                        self.assertIn("PUBLIC_REASONING", json.dumps(replay))

    async def test_empty_planner_failures_remain_bounded_without_repeating_seed_or_query(self):
        job = await self.job("Dame las specs de hardware.example")
        job["public_web_brief"] = {"topic": "hardware.example", "question": job["message"].content}
        self.engine.web = SimpleNamespace(close=AsyncMock(), search_web=AsyncMock(return_value={"results": []}),
            read_webpage=AsyncMock(side_effect=ValueError("unavailable")))
        with patch("cogs._conversation.request_completion", new=AsyncMock(return_value={"role": "assistant", "content": "No evidence"})) as provider:
            result = await self.engine.web_context(job)
        self.engine.web.read_webpage.assert_awaited_once()
        queries = [call.args[0] for call in self.engine.web.search_web.await_args_list]
        self.assertTrue(queries)
        self.assertEqual(len(queries), len(set(queries)))
        self.assertLessEqual(len(queries), 2)
        self.assertLessEqual(provider.await_count, 4)
        self.assertEqual(result["pages_read"], 0)
        self.assertFalse(result["assessment"]["sufficient"])

    async def test_rejected_discovery_does_not_starve_later_public_fallback_source(self):
        job = await self.job("Dame las specs de Example")
        job["public_web_brief"] = {"topic": "Example", "question": job["message"].content}
        url = "https://official.example/specs"
        self.engine.web = SimpleNamespace(close=AsyncMock(), search_web=AsyncMock(return_value={"results": [
            {"url": "https://discord.com/channels/1/2/3"}, {"url": url}]}),
            read_webpage=AsyncMock(return_value={"url": url, "text": "The card has 384 GB of memory."}))
        with patch("cogs._conversation.request_completion", new=AsyncMock(return_value={"role": "assistant", "content": "No evidence"})):
            result = await self.engine.web_context(job)
        self.engine.web.read_webpage.assert_awaited_once()
        self.assertEqual(self.engine.web.read_webpage.await_args.args[0], url)
        self.assertEqual(result["diagnostics"]["read_attempts"], 1)
        self.assertEqual(result["verified_source_urls"], [url])

    async def test_plain_stop_and_unsupported_report_do_not_close_with_only_a_homepage(self):
        for terminal in ("text", "unsupported_report"):
            with self.subTest(terminal=terminal):
                job = await self.job("Dame las specs de hardware.example")
                job["public_web_brief"] = {"topic": "hardware.example", "question": job["message"].content}
                root, detail = "https://hardware.example/", "https://hardware.example/product"
                self.engine.web = SimpleNamespace(close=AsyncMock(), search_web=AsyncMock(), read_webpage=AsyncMock(side_effect=[
                    {"url": root, "text": "Manufacturer homepage.", "links": [{"url": detail}]},
                    {"url": detail, "text": "Card X has 384 GB of memory."}]))
                unsupported = fixtures.research_report([fixtures.research_fact(root, "An invented specification.")], sufficient=True)
                unsupported["tool_calls"][0]["id"] = "unsupported"
                stop = {"role": "assistant", "content": "No specifications found"} if terminal == "text" else unsupported
                supported = fixtures.research_report([fixtures.research_fact(detail, "Card X has 384 GB of memory.")], sufficient=True)
                replies = [self.call("read_webpage", root, 1), stop, self.call("read_webpage", detail, 3), supported]
                with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)) as provider:
                    result = await self.engine.web_context(job)
                self.assertEqual(self.engine.web.read_webpage.await_count, 2)
                self.assertTrue(result["assessment"]["sufficient"])
                self.assertEqual(result["diagnostics"]["stop_reason"], "report")
                self.assertTrue(all(call.kwargs.get("tool_choice") == "required" for call in provider.await_args_list))

    async def test_repeated_plain_stops_are_bounded_and_keep_partial_evidence(self):
        job = await self.job("Dame las specs de hardware.example")
        job["public_web_brief"] = {"topic": "hardware.example", "question": job["message"].content}
        url = "https://hardware.example/"
        self.engine.web = SimpleNamespace(close=AsyncMock(), read_webpage=AsyncMock(return_value={"url": url, "text": "Public manufacturer homepage."}))
        with patch("cogs._conversation.request_completion", new=AsyncMock(return_value={"role": "assistant", "content": "No more information"})) as provider:
            result = await self.engine.web_context(job)
        self.engine.web.read_webpage.assert_awaited_once()
        self.assertEqual(provider.await_count, 4)
        self.assertEqual(result["diagnostics"]["stop_reason"], "round_limit")
        self.assertFalse(result["complete"])
        self.assertEqual(result["verified_source_urls"], [url])

    async def test_essential_clarification_is_bounded_before_return_without_web_actions(self):
        for allowance in (512, 48000):
            with self.subTest(allowance=allowance):
                job = await self.job("Consulta pública de Example " * 18)
                job["public_web_brief"] = {"topic": "Example " * 24, "question": job["message"].content}
                job["web_byte_allowance"] = allowance
                question = ("¿Qué revisión de Example quieres consultar? " * 7)[:300]
                self.engine.web = SimpleNamespace(close=AsyncMock(), search_web=AsyncMock(), read_webpage=AsyncMock())
                with patch("cogs._conversation.request_completion", new=AsyncMock(return_value=fixtures.research_report(clarification=question))):
                    result = await self.engine.web_context(job)
                self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), allowance)
                self.engine.web.search_web.assert_not_awaited()
                self.engine.web.read_webpage.assert_not_awaited()
                if allowance == 48000:
                    self.assertEqual(result["clarification_required"], question.strip())

    async def test_one_targeted_recovery_search_requires_failed_known_source_and_remaining_reads(self):
        from cogs._web_access import WebReadError
        failed, found = "https://docs.example/short", "https://docs.example/specifications"
        for scenario in ("recover", "duplicate", "no_failure", "no_reads", "access_challenge", "401", "403"):
            with self.subTest(scenario=scenario):
                self.fixture.db.set_ai_config(10, ai_web_max_sources=1 if scenario == "no_reads" else 3)
                job = await self.job("Dame las specs de Example")
                job["public_web_brief"] = {"topic": "Example", "question": job["message"].content}
                error = WebReadError("access_challenge" if scenario == "access_challenge" else "empty_content", "No public document",
                                     http_status=int(scenario) if scenario in {"401", "403"} else None)
                first = {"url": failed, "text": "Public manufacturer overview."} if scenario == "no_failure" else error
                self.engine.web = SimpleNamespace(close=AsyncMock(),
                    search_web=AsyncMock(side_effect=lambda query: {"results": [{"url": found if "recovered" in query else failed}]}),
                    read_webpage=AsyncMock(side_effect=[first, {"url": found, "text": "Card X has 384 GB of memory."}]))
                recovery = "(site: other.example) -site:docs.example +site: ignored.example recovered " + "x" * 200
                extra = ([self.fixture.tool_call("search_web", "site:other.example repeat", "duplicate")] if scenario == "duplicate" else [])
                extra += [self.fixture.tool_call("search_web", recovery, "third"), self.fixture.tool_call("search_web", "fourth", "fourth")]
                replies = [{"tool_calls": [self.fixture.tool_call("search_web", "site:docs.example repeat", "one"),
                                            self.fixture.tool_call("search_web", "Example specs", "two")]},
                    self.call("read_webpage", failed, "failed"), {"tool_calls": extra}, self.call("read_webpage", found, "found")]
                with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)) as provider:
                    result = await self.engine.web_context(job)
                queries = [call.args[0] for call in self.engine.web.search_web.await_args_list]
                allowed = scenario in {"recover", "duplicate"}
                self.assertEqual(len(queries), 3 if allowed else 2)
                if allowed:
                    self.assertTrue(queries[-1].startswith("site:docs.example recovered "))
                    self.assertEqual(queries[-1].count("site:"), 1)
                    self.assertNotIn("other.example", queries[-1])
                    self.assertNotIn("ignored.example", queries[-1])
                    self.assertLessEqual(len(queries[-1]), 300)
                    self.assertIn(found, result["verified_source_urls"])
                    self.assertEqual(result["diagnostics"]["recovery_searches"], 1)
                    if scenario == "duplicate":
                        self.assertIn("duplicate_web_request", json.dumps(result["diagnostics"]))
                for dispatch in provider.await_args_list:
                    for message in dispatch.args[2]:
                        if message.get("role") == "tool":
                            budget = json.loads(message["content"]).get("retrieval_budget", {})
                            self.assertTrue(all(value >= 0 for key, value in budget.items() if key.endswith("remaining")))

    async def test_failed_source_requires_targeted_search_before_other_navigation_or_report(self):
        from cogs._llm_responses import responses_input
        from cogs._web_access import WebReadError
        self.fixture.db.set_ai_config(10, ai_web_max_sources=5)
        job = await self.job("Dame las specs de Example")
        job["public_web_brief"] = {"topic": "Example", "question": job["message"].content}
        home, failed, other, canonical = ("https://maker.example/", "https://docs.example/short",
                                          "https://news.example/general", "https://docs.example/card-x")
        outside, deceptive, subdomain = "https://unrelated.example/topic", "https://docs.example.attacker.test/page", "https://api.docs.example/guide"
        overview, specification = "Example produces graphics cards.", "Card X has 384 GB of memory."
        async def read(url, **kwargs):
            if url == failed:
                raise WebReadError("empty_content", "Public document is empty")
            return {"url": url, "text": specification if url == canonical else overview}
        self.engine.web = SimpleNamespace(close=AsyncMock(), read_webpage=AsyncMock(side_effect=read),
            search_web=AsyncMock(side_effect=lambda query: {"results": [{"url": url} for url in
                ([outside, deceptive, canonical, subdomain] if query.startswith("site:docs.example ") else [home, failed, other])]}))
        premature = fixtures.research_report([fixtures.research_fact(home, overview)], sufficient=True)
        final = fixtures.research_report([fixtures.research_fact(canonical, specification)], sufficient=True)
        final["tool_calls"][0]["id"] = "complete"
        replies = [{"tool_calls": [self.fixture.tool_call("search_web", query, str(i))
                                   for i, query in enumerate(("Example cards", "Example specifications"))]},
                   {"tool_calls": [self.fixture.tool_call("read_webpage", url, "read" + str(i))
                                   for i, url in enumerate((home, failed, other))]}, premature,
                   {"tool_calls": [self.fixture.tool_call("search_web", "docs.example [DOCS.EXAMPLE.] Card X specifications", "recover"),
                                   self.fixture.tool_call("search_web", "unrelated fourth search", "fourth")]},
                   {"tool_calls": [self.fixture.tool_call("read_webpage", url, "after" + str(i))
                                   for i, url in enumerate((outside, deceptive, canonical))]}, final]
        for response in replies:
            response["role"] = "assistant"
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)) as provider:
            result = await self.engine.web_context(job)
        self.assertEqual([call.args[0] for call in self.engine.web.read_webpage.await_args_list], [home, failed, canonical])
        self.assertEqual([call.args[0] for call in self.engine.web.search_web.await_args_list],
                         ["Example cards", "Example specifications", "site:docs.example Card X specifications"])
        self.assertEqual(result["diagnostics"]["recovery_searches"], 1)
        self.assertEqual(result["diagnostics"]["read_attempts"], 3)
        self.assertNotIn(outside, result["search_result_urls"])
        self.assertNotIn(deceptive, result["search_result_urls"])
        self.assertIn(subdomain, result["search_result_urls"])
        self.assertTrue(result["assessment"]["sufficient"])
        for dispatch in provider.await_args_list[2:4]:
            self.assertEqual([tool["function"]["name"] for tool in dispatch.kwargs["tools"]], ["search_web"])
            self.assertEqual(dispatch.kwargs["tool_choice"], "required")
        replay = provider.await_args.args[2]
        self.assertIn("targeted_recovery_required", json.dumps(replay))
        responses_input(replay)  # Every native call, including deferred ones, still has its output.

    async def test_recovery_results_outside_host_are_empty_and_never_authorize_reads(self):
        from cogs._web_access import WebReadError
        job = await self.job("Dame las specs de Example")
        job["public_web_brief"] = {"topic": "Example", "question": job["message"].content}
        failed, outside = "https://docs.example/short", "https://unrelated.example/topic"
        self.engine.web = SimpleNamespace(close=AsyncMock(),
            search_web=AsyncMock(side_effect=[{"results": [{"url": failed}]}, {"results": []},
                                              {"status": "ok", "backend_degraded": True, "results": [{"url": outside}]}]),
            read_webpage=AsyncMock(side_effect=WebReadError("empty_content", "No document")))
        replies = [{"tool_calls": [self.fixture.tool_call("search_web", query, str(i)) for i, query in enumerate(("Example", "Example card"))]},
                   self.call("read_webpage", failed, "failed"), self.call("search_web", "docs.example Card X", "recovery"),
                   self.call("read_webpage", outside, "outside")]
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)) as provider:
            result = await self.engine.web_context(job)
        self.engine.web.read_webpage.assert_awaited_once()
        self.assertEqual(self.engine.web.search_web.await_count, 3)
        self.assertNotIn(outside, result["search_result_urls"])
        recovered = next(json.loads(message["content"]) for message in provider.await_args.args[2]
                         if message.get("tool_call_id") == "recovery")
        self.assertEqual(recovered["status"], "no_results")
        self.assertTrue(recovered["backend_degraded"])
        self.assertEqual(recovered["results"], [])
        self.assertEqual(recovered["discarded_outside_site"], 1)
        self.assertIn("url_not_discovered", json.dumps(result["diagnostics"]))

    async def test_documentation_and_recovered_page_precede_marketing_with_native_replay(self):
        from cogs._llm_responses import responses_input
        from cogs._web_access import WebReadError
        from tests.test_conversation_responses import response, function_item, text_item
        root, document, canonical, store = ("https://maker.example/", "https://docs.example/wiki/x/public",
                                            "https://docs.example/specifications", "https://maker.example/store")
        product = "https://maker.example/how-it-works"
        for protocol in ("chat_completions", "responses"):
            for initial_searches in (0, 2):
                with self.subTest(protocol=protocol, initial_searches=initial_searches):
                    self.fixture.db.set_ai_config(10, ai_web_max_sources=5)
                    self.fixture.db.set_automod_config(10, rules={"llm": {"endpoint_url": "https://provider.example/v1",
                        "api_type": protocol, "model": "fixture", "enabled": False}})
                    job = await self.job("Dame docs de maker.example" if initial_searches else "Dame las specs de maker.example")
                    job["public_web_brief"] = {"topic": "maker.example", "question": job["message"].content}
                    empty_recovery = not initial_searches or protocol == "chat_completions"
                    reads, dispatches = [], []
                    async def read(url, **kwargs):
                        reads.append(url)
                        if url == document:
                            raise WebReadError("empty_content", "The linked document is empty")
                        links = ([{"url": store, "title": "Store"}, {"url": product, "title": "Product details"}] if url == root
                                 else [{"url": store, "title": "Store"}, {"url": document, "title": ""}] if url == product else [])
                        return {"url": url, "text": "Card X has 384 GB of memory." if url == canonical else "Manufacturer overview.", "links": links}
                    self.engine.web = SimpleNamespace(close=AsyncMock(), read_webpage=AsyncMock(side_effect=read),
                        search_web=AsyncMock(side_effect=lambda query: {"results": [{"url": canonical if query.startswith("site:docs.example") else root}]}))
                    async def endpoint(session, config, payload, **kwargs):
                        dispatches.append(payload)
                        offered = [tool.get("name") or tool["function"]["name"] for tool in payload["tools"]]
                        if initial_searches and len(dispatches) == 1:
                            requests = [("search_web", job["public_web_brief"]["question"]), ("search_web", "Maker specifications")]
                        elif root not in reads:
                            requests = [("read_webpage", root), ("read_webpage", product), ("read_webpage", store)]
                        elif offered == ["search_web"]:
                            if empty_recovery:
                                if protocol == "responses":
                                    return response(text_item("No further evidence", "premature_stop"))
                                return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "No further evidence"}}]}
                            requests = [("search_web", "docs.example Card X specifications")]
                        else:
                            requests = [("read_webpage", store), ("search_web", "another comparison"), ("search_web", "one too many")]
                        calls = [self.fixture.tool_call(name, argument, f"call{len(dispatches)}_{i}") for i, (name, argument) in enumerate(requests)]
                        if protocol == "responses":
                            return response(*[function_item(call["function"]["name"], json.loads(call["function"]["arguments"]),
                                                            call["id"], "item_" + call["id"]) for call in calls])
                        return {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "tool_calls": calls}}]}
                    with patch("cogs._llm_moderation.endpoint_json", new=AsyncMock(side_effect=endpoint)):
                        result = await self.engine.web_context(job)
                    self.assertEqual(reads[:4], [root, product, document, canonical])
                    self.assertEqual(reads.count(document), 1)
                    self.assertEqual(reads.count(canonical), 1)
                    self.assertEqual(self.engine.web.search_web.await_count, 3 if initial_searches else 2)
                    self.assertEqual(self.engine.web.search_web.await_args_list[initial_searches].args[0],
                        "site:docs.example " + job["public_web_brief"]["question"] if empty_recovery else "site:docs.example Card X specifications")
                    self.assertLessEqual(result["diagnostics"]["read_attempts"], 5)
                    self.assertEqual(result["diagnostics"]["recovery_searches"], 1)
                    self.assertIn(canonical, result["verified_source_urls"])
                    last = dispatches[-1]
                    replay = last["input"] if protocol == "responses" else responses_input(last["messages"])
                    self.assertTrue(any(item.get("type") == "function_call_output" and canonical in item["output"] for item in replay))

    async def test_document_priority_respects_news_read_budget_and_access_restrictions(self):
        from cogs._web_access import WebReadError
        root, document, store = "https://maker.example/", "https://docs.example/wiki/x/public", "https://maker.example/store"
        for scenario in ("news", "read_limit", "401", "403", "access_challenge"):
            with self.subTest(scenario=scenario):
                self.fixture.db.set_ai_config(10, ai_web_max_sources=1 if scenario == "read_limit" else 3)
                job = await self.job(("Últimas noticias de " if scenario == "news" else "Dame las specs de ") + "maker.example")
                job["public_web_brief"] = {"topic": "maker.example", "question": job["message"].content}
                reads, calls = [], []
                async def read(url, **kwargs):
                    reads.append(url)
                    if url == document:
                        raise WebReadError("access_challenge" if scenario == "access_challenge" else "http_error", "Access restricted",
                                           http_status=int(scenario) if scenario in {"401", "403"} else None)
                    return {"url": url, "text": "Public overview", "links": [{"url": document, "title": "Documentation"},
                            {"url": store, "title": "Store"}] if url == root else []}
                self.engine.web = SimpleNamespace(close=AsyncMock(), read_webpage=AsyncMock(side_effect=read), search_web=AsyncMock())
                async def planner(*args, **kwargs):
                    calls.append(1)
                    urls = (root, store) if len(calls) == 1 else (store,)
                    return {"role": "assistant", "tool_calls": [self.fixture.tool_call("read_webpage", url, f"call{len(calls)}_{i}") for i, url in enumerate(urls)]}
                with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=planner)):
                    result = await self.engine.web_context(job)
                self.engine.web.search_web.assert_not_awaited()
                self.assertEqual(reads.count(document), 0 if scenario in {"news", "read_limit"} else 1)
                self.assertLessEqual(result["diagnostics"]["read_attempts"], 1 if scenario == "read_limit" else 3)
                self.assertEqual(result["diagnostics"]["recovery_searches"], 0)

    async def test_priority_document_survives_read_and_final_fit_with_its_late_variant_table(self):
        from cogs._research_context import select_passages
        self.fixture.db.set_ai_config(10, ai_web_max_sources=6)
        root, a, b, product, document = ("https://maker.example/", "https://maker.example/a", "https://maker.example/b",
                                       "https://maker.example/product", "https://docs.example/specifications")
        question = "Dame specs de la tarjeta más potente de maker.example"
        job = await self.job(question)
        brief = {"topic": "maker.example", "question": question}
        table = "Model\tSoldered memory\tExpandable memory\tTotal capacity\tForm factor\nCard Pro-X\t128 GB\t256 GB\t384 GB\tPCIe card\nSystem Max-Y\t256 GB\t2,048 GB\t2,304 GB\tWhole server"
        document_text = "\n\n".join(f"Technical background section {i}: " + "public engineering detail " * 12 for i in range(80)) + "\n\nVariant specifications\n" + table
        self.assertGreater(len(document_text), 20000)
        self.assertLess(len(document_text.encode()), 32000)
        reads, final_context = [], []
        async def read(url, *, query, max_text):
            reads.append((url, max_text))
            body = document_text if url == document else "\n\n".join(f"Public section {i}: " + "manufacturer overview " * 18 for i in range(75))
            links = [{"url": url} for url in (a, b, product)] if url == root else [{"url": document, "title": "Documentation"}] if url == product else []
            return {"url": url, **select_passages(body, query, max_text), "links": links}
        self.engine.web = SimpleNamespace(close=AsyncMock(), read_webpage=AsyncMock(side_effect=read), search_web=AsyncMock())
        async def provider(session, cfg, messages, **kwargs):
            offered = [tool["function"]["name"] for tool in kwargs.get("tools") or []]
            if "request_public_web_research" in offered:
                return self.intent(brief)
            if "select_public_web_research" in offered:
                return fixtures.web_selection(**brief)
            if offered:
                if not reads:
                    return {"role": "assistant", "tool_calls": [self.fixture.tool_call("read_webpage", url, str(i))
                                                                 for i, url in enumerate((root, a, b, product))]}
                return {"role": "assistant", "content": "No consolidated report"}
            final_context.append(json.loads(messages[1]["content"])["web_research_untrusted"])
            return "La tarjeta Card Pro-X tiene 384 GB; System Max-Y corresponde a un servidor completo."
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=provider)):
            await self.engine.process(job)
        self.assertTrue(final_context)
        pages = final_context[0]["page_evidence_untrusted"]
        self.assertEqual(pages[0]["url"], document)
        self.assertEqual(pages[0]["text"], document_text)
        self.assertGreaterEqual(dict(reads)[document], len(document_text.encode()))
        self.assertIn(table, pages[0]["text"])
        self.engine.web.search_web.assert_not_awaited()
        self.assertLessEqual(len(json.dumps(final_context[0], ensure_ascii=False).encode()), job["web_byte_allowance"])
        self.assertNotIn("No pude completar", job["message"].reply.await_args.args[0])

    async def test_final_handoff_keeps_read_evidence_without_researcher_conclusions(self):
        from cogs._research_context import assess_research
        job = await self.job("Dame specs de la tarjeta de Example")
        brief = {"topic": "Example", "question": job["message"].content}
        url = "https://docs.example/specifications"
        quote = "Card X has two ports. Server Y has eight ports."
        page = {"url": url, "text": "Variant\tScope\tPorts\nCard X\tPCIe card\t2\nServer Y\tWhole server\t8\n" + quote,
                "retrieved_at_utc": "2030-01-02", "published_at_untrusted": "2029-12-01"}
        report = {"summary": "UNSUPPORTED_SUMMARY: four ports per card", "sufficient": False,
                  "gaps": ["UNSUPPORTED_GAP: no price has been published"], "facts": [
                      {"claim": "UNSUPPORTED_CLAIM: four ports per card", "status": "supported", "support": [{"url": url, "quote": quote}]},
                      {"claim": "UNSUPPORTED_UNKNOWN: memory is undocumented", "status": "unknown", "support": []}]}
        web = {"retrieval_mode": "quick", "public_brief": brief, "assessment": assess_research(report, {url: page}),
               "page_evidence_untrusted": [page], "verified_source_urls": [url], "sources": [{"url": url, "kind": "page_read"}],
               "clarification_required": "¿Qué revisión concreta de la tarjeta necesitas?"}
        original = json.dumps(web, sort_keys=True)
        with patch.object(self.engine, "web_context", new=AsyncMock(return_value=web)), patch(
                "cogs._conversation.request_completion", new=AsyncMock(side_effect=[self.intent(brief), fixtures.web_selection(**brief), "Consulta pública."])) as provider:
            await self.engine.process(job)
        context = json.loads(provider.await_args.args[2][1]["content"])["web_research_untrusted"]
        self.assertNotIn("UNSUPPORTED_", json.dumps(context))
        self.assertEqual(context["page_evidence_untrusted"], [page])
        self.assertEqual(context["verified_source_urls"], [url])
        self.assertEqual(context["assessment"]["facts"][0]["support"][0]["quote"], quote)
        self.assertEqual(context["clarification_required"], web["clarification_required"])
        self.assertEqual(json.dumps(web, sort_keys=True), original)
        saved = self.engine.research.load(self.engine.research_key(job), self.engine.research_token(job))
        self.assertEqual(saved["assessment"], web["assessment"])
        self.assertEqual(provider.await_count, 3)

    async def test_model_read_of_discovered_document_keeps_full_evidence_after_redirect(self):
        from cogs._research_context import select_passages
        job = await self.job("Dame specs de la tarjeta de Example")
        brief = {"topic": "Example", "question": job["message"].content}
        discovered, final = "https://knowledge.example/public/sheet-1", "https://knowledge.example/public/current-1"
        quote = "Card Pro-X has two ports; Server Max-Y has eight ports."
        document = "\n\n".join(f"Technical background section {i}: " + "public engineering detail " * 12 for i in range(80))
        document += "\n\nVariant\tScope\tPorts\nCard Pro-X\tPCIe card\t2\nServer Max-Y\tWhole server\t8\n" + quote
        self.assertGreater(len(document), 20000)
        async def read(url, *, query, max_text):
            return {"url": final, **select_passages(document, query, max_text), "links": []}
        self.engine.web = SimpleNamespace(close=AsyncMock(),
            search_web=AsyncMock(return_value={"results": [{"url": discovered, "title": "Technical documentation"}]}),
            read_webpage=AsyncMock(side_effect=read))
        replies = [self.intent(brief), fixtures.web_selection(**brief), self.call("search_web", "Example card specifications", "search"),
                   self.call("read_webpage", discovered, "read"),
                   fixtures.research_report([fixtures.research_fact(final, quote)], sufficient=True), "La tarjeta tiene dos puertos."]
        with patch("cogs._conversation.request_completion", new=AsyncMock(side_effect=replies)) as provider:
            await self.engine.process(job)
        context = json.loads(provider.await_args.args[2][1]["content"])["web_research_untrusted"]
        self.assertEqual(context["page_evidence_untrusted"][0]["text"], document)
        self.assertEqual(context["page_evidence_untrusted"][0]["url"], final)
        self.assertEqual(context["verified_source_urls"], [final])
        self.assertEqual(self.engine.web.read_webpage.await_args.args[0], discovered)
        self.engine.web.read_webpage.assert_awaited_once()
        self.engine.web.search_web.assert_awaited_once()
        self.assertEqual(provider.await_count, 6)

    async def test_followup_without_pages_keeps_validated_quotes_dates_and_internal_ledger(self):
        from cogs._research_context import assess_research
        job = await self.job("¿Y las specs de Example?")
        job["requester_permissions_snapshot"] = fixtures.requester_permissions(job["message"])
        url, quote = "https://docs.example/card", "Card X has two ports."
        assessment = assess_research({"summary": "UNSUPPORTED_SUMMARY", "sufficient": True, "gaps": [], "facts": [
            {"claim": "UNSUPPORTED_CLAIM: four ports", "status": "supported", "support": [{"url": url, "quote": quote}]}]},
            {url: {"text": quote, "retrieved_at_utc": "2030-01-02", "published_at_untrusted": "2029-12-01"}})
        web = {"assessment": assessment, "public_brief": {"topic": "Example", "question": "Card specifications"},
               "sources": [{"url": url, "kind": "page_read"}]}
        await self.engine.save_research(job, web)
        with patch("cogs._conversation.request_completion", new=AsyncMock(return_value="La tarjeta tiene dos puertos.")) as provider:
            await self.engine.process(job)
        context = json.loads(provider.await_args.args[2][1]["content"])["research_notes_untrusted"]
        self.assertNotIn("UNSUPPORTED_", json.dumps(context))
        self.assertEqual(context["assessment"]["facts"][0]["support"], assessment["facts"][0]["support"])
        saved = self.engine.research.load(self.engine.research_key(job), self.engine.research_token(job))
        self.assertEqual(saved["assessment"], assessment)
        self.assertEqual(provider.await_count, 1)


class PublicSeedTests(unittest.TestCase):
    def test_accent_equivalence_returns_the_original_authorized_fragment(self):
        from cogs._conversation import web_brief_arguments
        text = "Holiwis, dame specs de la tarjeta gráfica mas potente de bolt.graphics"
        extracted = {"topic": "bolt.graphics", "question": "dame specs de la tarjeta gráfica más potente de bolt.graphics"}
        result = web_brief_arguments({"public_brief": extracted}, {"trigger": {"text": text}})
        self.assertEqual(result["question"], text.removeprefix("Holiwis, "))
        self.assertEqual(result["topic"], extracted["topic"])

    def test_accent_matching_still_rejects_new_premises_private_data_and_ambiguity(self):
        from cogs._conversation import web_brief_arguments
        context = {"trigger": {"text": "Dame specs de Example; password: private"}}
        for question in ("Dame specs de Example cloud", "password: private"):
            with self.subTest(question=question), self.assertRaises(ValueError):
                web_brief_arguments({"public_brief": {"topic": "Example", "question": question}}, context)
        # No exact form exists; two differently accented source fragments would match.
        with self.assertRaises(ValueError):
            web_brief_arguments({"public_brief": {"topic": "Example", "question": "càfe"}},
                {"trigger": {"text": "Example cafe café"}})

    def test_bare_domains_are_complete_tokens_not_emails_files_or_private_hosts(self):
        from cogs._conversation import public_source_urls
        brief = {"topic": "Examples", "question": "bolt.graphics and https://official.example/specs"}
        self.assertEqual(public_source_urls(brief), {"https://bolt.graphics/", "https://official.example/specs"})
        for text in ("name@bolt.graphics", "user:pass@bolt.graphics", "README.md", "report.pdf", "config.json",
                     "app.py", "localhost", "127.0.0.1", "office.local", "cdn.discordapp.com", "discord.com/channels/1/2"):
            with self.subTest(text=text):
                self.assertEqual(public_source_urls({"topic": "Example", "question": text}), set())


if __name__ == "__main__":
    unittest.main()
