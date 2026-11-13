"""Fictitious end-to-end conversation state across the Responses adapter."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from cogs._conversation import FINAL_WEB_GROUNDING

from tests import test_conversation_memory as conversation_fixtures


def text_item(text, item_id="message"):
    return {"type": "message", "id": item_id, "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def reasoning(cipher, item_id="reasoning"):
    return {"type": "reasoning", "id": item_id, "summary": [], "encrypted_content": cipher}


def function_item(name, arguments, call_id, item_id):
    return {"type": "function_call", "id": item_id, "call_id": call_id, "name": name,
            "arguments": json.dumps(arguments), "status": "completed"}


def response(*items):
    return {"id": "response_fixture", "object": "response", "status": "completed", "output": list(items)}


class ResponsesConversationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = conversation_fixtures.ConversationRuntimeTest()
        await self.fixture.asyncSetUp()
        self.db, self.engine = self.fixture.db, self.fixture.engine
        self.db.set_automod_config(10, rules={"llm": {"endpoint_url": "https://provider.example/v1/responses",
            "api_type": "responses", "model": "fictitious", "api_key": "FAKE_CREDENTIAL", "enabled": False}})

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_direct_reply_uses_responses_and_does_not_persist_provider_sidecars(self):
        message = self.fixture.message(ping=True)
        message.content = "Hola"
        await self.engine.listen(message)
        with patch("cogs._llm_moderation.endpoint_json", new=AsyncMock(return_value=response(
                reasoning("PRIVATE_DIRECT_CIPHER"), text_item("Hola")))) as provider:
            await self.engine.process(self.fixture.cog._queue.get_nowait())
        provider.assert_awaited_once()
        payload = provider.await_args.args[2]
        self.assertFalse(payload["store"])
        self.assertIn("input", payload)
        self.assertNotIn("messages", payload)
        self.assertNotIn("previous_response_id", payload)
        self.assertNotIn("FAKE_CREDENTIAL", json.dumps(payload))
        self.assertEqual(message.reply.await_args.args[0], "Hola")
        stored = json.dumps(self.db.get_ai_chat_turns(10, 20))
        self.assertNotIn("CIPHER", stored)
        self.assertNotIn("_responses_output", stored)

    async def test_public_tool_rounds_replay_reasoning_and_call_ids_without_private_context(self):
        self.db.set_ai_config(10, ai_web_enabled=1)
        self.db.set_ai_memory(10, "user", 30, 20, "PRIVATE_PROFILE")
        self.db.append_ai_chat_turn(10, 20, 90, 31, "Other person", "PRIVATE_HISTORY")
        message = self.fixture.message(ping=True)
        message.content = "Busca la versión actual de Example en fuentes oficiales"
        self.engine.web = SimpleNamespace(
            search_web=AsyncMock(return_value={"results": [{"url": "https://official.example/releases", "snippet": "Current version"}]}),
            read_webpage=AsyncMock(return_value={"url": "https://official.example/releases", "title": "Official releases", "text": "Release 9"}),
            close=AsyncMock())
        await self.engine.listen(message)
        replies = [
            response(reasoning("PRIVATE_INTENT_CIPHER"), function_item("request_public_web_research", {}, "private_call", "private_item")),
            response(reasoning("PUBLIC_CIPHER_ONE", "reasoning_one"), function_item("search_web", {"query": "Example latest official version"}, "search_call", "function_item_search")),
            response(reasoning("PUBLIC_CIPHER_TWO", "reasoning_two"), function_item("read_webpage", {"url": "https://official.example/releases"}, "read_call", "function_item_read")),
            response(reasoning("PUBLIC_FINISH_CIPHER"), function_item("submit_research", {
                "public_topic": "Example", "summary": "Current release", "sufficient": True, "gaps": [],
                "facts": [{"claim": "Release 9", "status": "supported", "support": [
                    {"url": "https://official.example/releases", "quote": "Release 9"}]}]}, "report_call", "report_item")),
            response(reasoning("PRIVATE_FINAL_CIPHER"), text_item("Según la fuente oficial, la versión consultada es 9."))]
        with patch("cogs._llm_moderation.endpoint_json", new=AsyncMock(side_effect=replies)) as provider:
            await self.engine.process(self.fixture.cog._queue.get_nowait())
        self.assertEqual(provider.await_count, 5)
        payloads = [call.args[2] for call in provider.await_args_list]
        for payload in payloads:
            self.assertFalse(payload["store"])
            self.assertNotIn("previous_response_id", payload)
        for payload in payloads[1:4]:
            self.assertNotIn("PRIVATE_", json.dumps(payload))
        replay = payloads[2]["input"]
        self.assertIn(reasoning("PUBLIC_CIPHER_ONE", "reasoning_one"), replay)
        native_call = next(item for item in replay if item.get("type") == "function_call")
        self.assertEqual((native_call["id"], native_call["call_id"]), ("function_item_search", "search_call"))
        output = next(item for item in replay if item.get("type") == "function_call_output")
        self.assertEqual(output["call_id"], "search_call")
        self.assertIn("official.example", output["output"])
        replay_two = payloads[3]["input"]
        self.assertIn(reasoning("PUBLIC_CIPHER_TWO", "reasoning_two"), replay_two)
        self.assertEqual({item["call_id"] for item in replay_two if item.get("type") == "function_call_output"}, {"search_call", "read_call"})
        final = json.dumps(payloads[-1])
        self.assertIn("PRIVATE_PROFILE", final)
        self.assertIn("Release 9", final)
        self.assertNotIn("CIPHER", final)
        self.assertNotIn("_responses_output", final)
        self.assertEqual(message.reply.await_args.args[0], "Según la fuente oficial, la versión consultada es 9.")
        self.assertNotIn("CIPHER", json.dumps(self.db.get_ai_chat_turns(10, 20)))

    async def test_research_diagnostics_stay_internal_and_preserve_localized_factual_uncertainty(self):
        self.db.set_ai_config(10, ai_web_enabled=1)
        url = "https://official.example/gpu"
        for protocol_index, protocol in enumerate(("chat_completions", "responses")):
            self.db.set_automod_config(10, rules={"llm": {"endpoint_url": "https://provider.example/v1",
                "api_type": protocol, "model": "fictitious", "enabled": False}})
            for pages_read in (0, 1):
                with self.subTest(protocol=protocol, pages_read=pages_read):
                    message = self.fixture.message(200 + protocol_index * 2 + pages_read, ping=True)
                    message.content = "Busca la memoria de Example GPU y su precio en esta tienda"
                    await self.engine.listen(message)
                    answer = "No tengo un precio confirmado para esa tienda."
                    facts = [{"claim": "Precio local", "status": "unknown", "support": []}]
                    if pages_read:
                        answer = "La GPU tiene 16 GB de memoria. " + answer
                        facts.insert(0, {"claim": "16 GB de memoria", "status": "supported",
                            "support": [{"url": url, "quote": "Memory: 16 GB."}]})
                    web = {"results_untrusted": [{"result_untrusted": {"error": "web_research_context_limit"}}],
                        "sources": [{"url": url, "kind": "page_read"}] if pages_read else [],
                        "verified_source_urls": [url] if pages_read else [], "search_result_urls": [],
                        "complete": False, "limits_reached": True, "pages_read": pages_read,
                        "assessment": {"sufficient": False, "facts": facts, "gaps": ["Precio local sin confirmar"]}}
                    if protocol == "responses":
                        replies = [response(function_item("request_public_web_research", {}, "intent", "intent_item")),
                                   response(text_item(answer))]
                    else:
                        replies = [{"choices": [{"finish_reason": "tool_calls", "message": {"content": None,
                            "tool_calls": [{"id": "intent", "type": "function", "function": {
                                "name": "request_public_web_research", "arguments": "{}"}}]}}]},
                            {"choices": [{"finish_reason": "stop", "message": {"content": answer}}]}]
                    with patch.object(self.engine, "web_context", new=AsyncMock(return_value=web)), patch(
                            "cogs._llm_moderation.endpoint_json", new=AsyncMock(side_effect=replies)) as provider:
                        await self.engine.process(self.fixture.cog._queue.get_nowait())
                    self.assertEqual(provider.await_count, 2)
                    message.reply.assert_awaited_once()
                    self.assertEqual(message.reply.await_args.args[0], answer)
                    final = json.dumps(provider.await_args.args[2])
                    self.assertIn('"limits_reached": true', final.replace('\\"', '"'))
                    self.assertIn('"status": "unknown"', final.replace('\\"', '"'))
                    if pages_read:
                        self.assertIn('"status": "supported"', final.replace('\\"', '"'))

    async def test_invalid_web_intent_has_one_stateless_canonical_repair_and_valid_intent_needs_none(self):
        self.db.set_ai_config(10, ai_web_enabled=1)
        brief = {"topic": "Example listing", "question": "Is Example publicly listed?"}
        web = {"sources": [], "verified_source_urls": [], "complete": True, "assessment": {"sufficient": True}}
        for protocol_index, protocol in enumerate(("chat_completions", "responses")):
            self.db.set_automod_config(10, rules={"llm": {"endpoint_url": "https://provider.example/v1",
                "api_type": protocol, "model": "fictitious", "enabled": False}})
            for form_index, form in enumerate(("canonical", "repair", "flat", "repair_flat")):
                with self.subTest(protocol=protocol, form=form):
                    invalid = form.startswith("repair")
                    message = self.fixture.message(400 + protocol_index * 4 + form_index, ping=True)
                    message.content = brief["question"]
                    await self.engine.listen(message)
                    args = {"query": "Example listing"} if invalid else (brief if form == "flat" else {"public_brief": brief})
                    repaired = brief if form == "repair_flat" else {"public_brief": brief}
                    if protocol == "responses":
                        replies = [response(reasoning("INITIAL_OPAQUE_SIDECAR"), function_item("request_public_web_research", args, "first", "first_item"))]
                        if invalid:
                            replies.append(response(function_item("request_public_web_research", repaired, "repair", "repair_item")))
                        replies.append(response(text_item("Example remains privately held.")))
                    else:
                        arguments = [args, repaired] if invalid else [args]
                        replies = [{"choices": [{"finish_reason": "tool_calls", "message": {"content": None,
                            "tool_calls": [{"id": f"call{index}", "type": "function", "function": {
                                "name": "request_public_web_research", "arguments": json.dumps(value)}}]}}]}
                            for index, value in enumerate(arguments)]
                        replies.append({"choices": [{"finish_reason": "stop", "message": {"content": "Example remains privately held."}}]})
                    with patch.object(self.engine, "web_context", new=AsyncMock(return_value=web)) as research, patch(
                            "cogs._llm_moderation.endpoint_json", new=AsyncMock(side_effect=replies)) as provider:
                        await self.engine.process(self.fixture.cog._queue.get_nowait())
                    self.assertEqual(provider.await_count, 3 if invalid else 2)
                    research.assert_awaited_once()
                    self.assertEqual(research.await_args.args[0]["public_web_brief"], brief)
                    self.assertEqual(message.reply.await_args.args[0], "Example remains privately held.")
                    if invalid:
                        payload = provider.await_args_list[1].args[2]
                        names = [tool["name"] if protocol == "responses" else tool["function"]["name"] for tool in payload["tools"]]
                        self.assertEqual(names, ["request_public_web_research"])
                        self.assertEqual(payload["max_output_tokens" if protocol == "responses" else "max_tokens"], 512)
                        self.assertNotIn("INITIAL_OPAQUE_SIDECAR", json.dumps(payload))

    async def test_final_grounding_is_after_web_data_and_included_in_budget_for_both_protocols(self):
        self.db.set_ai_config(10, ai_web_enabled=1)
        brief = {"topic": "Example", "question": "Is Example publicly listed?"}
        web = {"sources": [], "verified_source_urls": [], "complete": False, "assessment": {"sufficient": False}}
        for index, protocol in enumerate(("chat_completions", "responses")):
            with self.subTest(protocol=protocol):
                self.db.set_automod_config(10, rules={"llm": {"endpoint_url": "https://provider.example/v1",
                    "api_type": protocol, "model": "fictitious", "enabled": False}})
                message = self.fixture.message(500 + index, ping=True)
                message.content = brief["question"]
                await self.engine.listen(message)
                if protocol == "responses":
                    replies = [response(function_item("request_public_web_research", {"public_brief": brief}, "intent", "intent_item")), response(text_item("Public answer."))]
                else:
                    replies = [{"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{"id": "intent", "type": "function",
                        "function": {"name": "request_public_web_research", "arguments": json.dumps({"public_brief": brief})}}]}}]},
                        {"choices": [{"finish_reason": "stop", "message": {"content": "Public answer."}}]}]
                with patch.object(self.engine, "web_context", new=AsyncMock(return_value=web)), patch.object(
                        self.engine.budget, "fit", new=AsyncMock(wraps=self.engine.budget.fit)) as fit, patch(
                        "cogs._llm_moderation.endpoint_json", new=AsyncMock(side_effect=replies)) as provider:
                    await self.engine.process(self.fixture.cog._queue.get_nowait())
                payload = provider.await_args.args[2]
                messages = payload["input" if protocol == "responses" else "messages"]
                self.assertEqual(messages[-1], {"role": "system", "content": FINAL_WEB_GROUNDING})
                self.assertIn("web_research_untrusted", json.dumps(messages[-2]))
                self.assertIn(messages[-1]["content"], fit.await_args.args[2])
                self.assertEqual(message.reply.await_args.args[0], "Public answer.")

    async def test_protocol_change_during_responses_call_discards_output(self):
        message = self.fixture.message(ping=True)
        await self.engine.listen(message)
        async def change_protocol(*args, **kwargs):
            self.db.set_automod_config(10, rules={"llm": {"endpoint_url": "https://provider.example/v1/chat/completions",
                "api_type": "chat_completions", "model": "fictitious", "api_key": "FAKE_CREDENTIAL", "enabled": False}})
            return response(text_item("Stale protocol reply"))
        with patch("cogs._llm_moderation.endpoint_json", new=AsyncMock(side_effect=change_protocol)) as provider:
            await self.engine.process(self.fixture.cog._queue.get_nowait())
        provider.assert_awaited_once()
        message.reply.assert_not_awaited()

    async def test_current_permission_change_during_responses_call_discards_output(self):
        role = self.fixture.grant_history_role(administrator=False, name="Helper")
        message = self.fixture.message(ping=True)
        await self.engine.listen(message)
        async def change_permission(*args, **kwargs):
            role._permissions = discord.Permissions(manage_messages=True).value
            return response(text_item("Stale permission reply"))
        with patch("cogs._llm_moderation.endpoint_json", new=AsyncMock(side_effect=change_permission)):
            await self.engine.process(self.fixture.cog._queue.get_nowait())
        message.reply.assert_not_awaited()

    async def test_full_context_checkpoints_and_continues_with_both_protocols(self):
        first, second = "https://official.example/releases", "https://official.example/performance"
        version = conversation_fixtures.research_fact(first, "Release 9 is current.")
        performance = conversation_fixtures.research_fact(second, "Performance improved 20 percent.")
        opaque = "OVERSIZED_PUBLIC_CIPHER" + "x" * 12000
        for index, protocol in enumerate(("chat_completions", "responses")):
            with self.subTest(protocol=protocol):
                self.db.set_automod_config(10, rules={"llm": {"endpoint_url": "https://provider.example/v1",
                    "api_type": protocol, "model": "fictitious", "enabled": False}})
                await self.engine.listen(self.fixture.message(300 + index, ping=True))
                job = self.fixture.cog._queue.get_nowait()
                job["deep_think"] = True
                self.engine.web = SimpleNamespace(read_webpage=AsyncMock(side_effect=[
                    {"url": first, "text": "Release 9 is current.", "retrieved_at_utc": "2030-01-02"},
                    {"url": second, "text": "Performance improved 20 percent."}]), close=AsyncMock())
                checkpoint = conversation_fixtures.research_report([version], gaps=["Performance unverified"])
                finished = conversation_fixtures.research_report([version, performance], sufficient=True)
                if protocol == "responses":
                    replies = [response(reasoning(opaque), function_item("read_webpage", {"url": first}, "read1", "item1")),
                        response(function_item("submit_research", json.loads(checkpoint["tool_calls"][0]["function"]["arguments"]), "report1", "report_item1")),
                        response(function_item("read_webpage", {"url": second}, "read2", "item2")),
                        response(function_item("submit_research", json.loads(finished["tool_calls"][0]["function"]["arguments"]), "report2", "report_item2"))]
                else:
                    replies = [{"choices": [{"finish_reason": "tool_calls", "message": message}]} for message in (
                        {"content": opaque, "tool_calls": [self.fixture.tool_call("read_webpage", first, "read1")]},
                        checkpoint, {"tool_calls": [self.fixture.tool_call("read_webpage", second, "read2")]}, finished)]
                with patch.object(self.engine.budget, "window", new=AsyncMock(return_value=32768)), patch(
                        "cogs._llm_moderation.endpoint_json", new=AsyncMock(side_effect=replies)) as provider:
                    result = await self.engine.web_context(job)
                self.assertEqual(provider.await_count, 4)
                payloads = [call.args[2] for call in provider.await_args_list]
                self.assertEqual([tool["name"] if protocol == "responses" else tool["function"]["name"]
                    for tool in payloads[1]["tools"]], ["submit_research"])
                self.assertNotIn("OVERSIZED_PUBLIC_CIPHER", json.dumps(payloads[1:]))
                if protocol == "responses":
                    self.assertFalse(any(item.get("type") in {"function_call", "function_call_output", "reasoning"} for item in payloads[1]["input"]))
                resumed = json.dumps(payloads[2])
                self.assertIn("Release 9 is current.", resumed)
                self.assertIn("2030-01-02", resumed)
                self.assertIn("Performance unverified", resumed)
                self.assertEqual([call.args[0] for call in self.engine.web.read_webpage.await_args_list], [first, second])
                self.assertTrue(result["complete"])
                self.assertTrue(result["assessment"]["sufficient"])
                self.assertNotIn("OVERSIZED_PUBLIC_CIPHER", json.dumps(result))
