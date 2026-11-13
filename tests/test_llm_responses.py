import asyncio
import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
from fastapi import HTTPException

from api.routes.guild import patch_automod_config
from cogs._llm_moderation import auxiliary_url, completion_url, endpoint_json, request_completion, validate_decision
from cogs._llm_responses import responses_input
from cogs._managed_messages import manage_payload
from llm_moderation_config import validate_llm_config
from tests.test_managed_messages import make_db

TOOLS = [{"type": "function", "function": {"name": "search_web", "description": "Public search",
          "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}}}]


def output_text(text):
    return {"type": "message", "id": "msg_test", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def response(*items, **kwargs):
    return {"status": "completed", "output": list(items), "error": None, "incomplete_details": None, **kwargs}


class ResponsesProtocolTests(unittest.IsolatedAsyncioTestCase):
    cfg = {"endpoint_url": "https://llm.example/v1", "api_key": "", "model": "selected-model", "api_type": "responses"}

    async def test_text_vision_flat_tools_and_stateless_wire_contract(self):
        messages = [{"role": "system", "content": "Instructions"}, {"role": "user", "content": [
            {"type": "text", "text": "Question"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,aW1hZ2U=", "detail": "low"}}]}]
        with patch("cogs._llm_moderation.endpoint_json", AsyncMock(return_value=response(output_text("Answer")))) as endpoint:
            result = await request_completion(None, self.cfg, messages, tools=TOOLS, structured=True)
        payload = endpoint.await_args.args[2]
        self.assertEqual(result["content"], "Answer")
        self.assertIs(payload["store"], False)
        self.assertEqual(payload["max_output_tokens"], 1000)
        self.assertEqual(payload["input"][1]["content"], [{"type": "input_text", "text": "Question"},
            {"type": "input_image", "image_url": "data:image/png;base64,aW1hZ2U=", "detail": "low"}])
        self.assertEqual(payload["tools"][0]["name"], "search_web")
        self.assertNotIn("function", payload["tools"][0])
        self.assertIs(payload["tools"][0]["strict"], False)
        self.assertEqual(payload["include"], ["reasoning.encrypted_content"])
        for incompatible in ("messages", "max_tokens", "response_format", "temperature", "reasoning_effort", "previous_response_id", "conversation"):
            self.assertNotIn(incompatible, payload)

    async def test_tool_call_ids_and_encrypted_items_are_replayed_without_server_state(self):
        encrypted = {"id": "rs_test", "type": "reasoning", "summary": [], "encrypted_content": "opaque-cipher"}
        call = {"type": "function_call", "id": "fc_not_call_id", "call_id": "call_1", "name": "search_web", "arguments": '{"query":"public"}'}
        with patch("cogs._llm_moderation.endpoint_json", AsyncMock(return_value=response(encrypted, call))):
            first = await request_completion(None, self.cfg, [{"role": "user", "content": "Question"}], tools=TOOLS, structured=True)
        self.assertEqual(first["tool_calls"][0]["id"], "call_1")
        self.assertEqual(first["_responses_output"], [encrypted, call])
        replay = responses_input([{"role": "user", "content": "Question"}, first,
            {"role": "tool", "tool_call_id": "call_1", "content": '{"result":"data"}'}])
        self.assertEqual(replay[1:3], [encrypted, call])
        self.assertEqual(replay[3], {"type": "function_call_output", "call_id": "call_1", "output": '{"result":"data"}'})
        for tool_id in ("fc_not_call_id", "other_call"):
            with self.subTest(tool_id=tool_id), self.assertRaises(ValueError):
                responses_input([first, {"role": "tool", "tool_call_id": tool_id, "content": "data"}])
        with self.assertRaises(ValueError):
            responses_input([first])
        with patch("cogs._llm_moderation.endpoint_json", AsyncMock(return_value=response({**encrypted, "encrypted_content": None}, call))):
            nullable = await request_completion(None, self.cfg, [], tools=TOOLS, structured=True)
        replay = responses_input([nullable, {"role": "tool", "tool_call_id": "call_1", "content": "data"}])
        self.assertIsNone(replay[0]["encrypted_content"])
        with patch("cogs._llm_moderation.endpoint_json", AsyncMock(return_value=response({**encrypted, "encrypted_content": 123}, call))), self.assertRaises(ValueError):
            await request_completion(None, self.cfg, [], tools=TOOLS, structured=True)

    async def test_incomplete_failed_refusal_and_bad_or_unoffered_tools_fail_closed(self):
        call = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "search_web", "arguments": '{"query":"public"}'}
        invalid = [response(output_text("Partial"), status="incomplete"), response(output_text("Text"), status="failed"),
            response(output_text("Text"), error={"message": "private-provider-body"}),
            response(output_text("Text"), incomplete_details={"reason": "max_output_tokens"}),
            response({"type": "message", "role": "assistant", "content": [{"type": "refusal", "refusal": "private-provider-body"}]}),
            response({"type": "web_search_call", "status": "completed"}), response({**call, "name": "unoffered"}),
            response({**call, "call_id": "bad/id"}), response(call, deepcopy(call)), response({**call, "arguments": "not JSON"}),
            response({**call, "arguments": "null"}), response({**call, "arguments": "{}" + " " * 4097}),
            response({**output_text("Text"), "status": "incomplete"}), {"output_text": "SDK helper is not the JSON wire format"}]
        for item in invalid:
            with self.subTest(item=item), patch("cogs._llm_moderation.endpoint_json", AsyncMock(return_value=item)):
                with self.assertRaises(ValueError) as error:
                    await request_completion(None, self.cfg, [], tools=TOOLS, structured=True)
                self.assertNotIn("private-provider-body", str(error.exception))
        with patch("cogs._llm_moderation.endpoint_json", AsyncMock(return_value=response(call))), self.assertRaises(ValueError):
            await request_completion(None, self.cfg, [], moderation=True)

    async def test_five_tools_preserve_every_call_and_reject_an_invalid_last_call(self):
        from cogs._conversation import WEB_TOOLS
        for protocol in ("chat_completions", "responses"):
            for name, key in (("search_web", "query"), ("read_webpage", "url")):
                for invalid in (None, "unoffered", "duplicate_id", "oversized_arguments"):
                    with self.subTest(protocol=protocol, name=name, invalid=invalid):
                        calls = [{"id": f"call{i}", "type": "function", "function": {
                            "name": name, "arguments": json.dumps({key: f"https://official.example/{i}"})}} for i in range(5)]
                        if invalid == "unoffered":
                            calls[-1]["function"]["name"] = "private_unoffered_tool"
                        elif invalid == "duplicate_id":
                            calls[-1]["id"] = calls[0]["id"]
                        elif invalid == "oversized_arguments":
                            calls[-1]["function"]["arguments"] = "private-payload" * 400
                        raw = (response(*[{"type": "function_call", "call_id": call["id"], **call["function"]} for call in calls])
                            if protocol == "responses" else {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": calls}}]})
                        with patch("cogs._llm_moderation.endpoint_json", AsyncMock(return_value=raw)):
                            if invalid:
                                with self.assertRaises(ValueError) as error:
                                    await request_completion(None, {**self.cfg, "api_type": protocol}, [], tools=WEB_TOOLS, structured=True)
                                self.assertNotIn("private-", str(error.exception))
                            else:
                                result = await request_completion(None, {**self.cfg, "api_type": protocol}, [], tools=WEB_TOOLS, structured=True)
                                self.assertEqual(result["tool_calls"], calls)

    async def test_moderation_json_contract_still_uses_strict_decision_validation(self):
        message = SimpleNamespace(id=10, author=SimpleNamespace(id=20), content="insulto literal")
        decision = {"message_id": "10", "author_id": "20", "action": "warn", "duration_seconds": None,
            "reason": "Incumple la regla", "rule": "No insultar", "evidence": [{"source": "text", "id": "10", "quote": "insulto"}]}
        with patch("cogs._llm_moderation.endpoint_json", AsyncMock(return_value=response(output_text(json.dumps(decision))))) as endpoint:
            raw = await request_completion(None, self.cfg, [{"role": "user", "content": "Return JSON"}], moderation=True)
        self.assertEqual(validate_decision(raw, message, "No insultar", set()), decision)
        self.assertEqual(endpoint.await_args.args[2]["text"], {"format": {"type": "json_object"}})
        self.assertEqual(endpoint.await_args.args[2]["max_output_tokens"], 512)
        with self.assertRaises(ValueError):
            validate_decision(raw.replace('"author_id": "20"', '"author_id": "21"'), message, "No insultar", set())

    async def test_chat_completions_default_payload_is_unchanged(self):
        with patch("cogs._llm_moderation.endpoint_json", AsyncMock(return_value={"choices": [{"finish_reason": "stop", "message": {"content": "Legacy reply"}}]})) as endpoint:
            self.assertEqual(await request_completion(None, {key: value for key, value in self.cfg.items() if key != "api_type"}, [], moderation=True), "Legacy reply")
        payload = endpoint.await_args.args[2]
        self.assertEqual(payload["max_tokens"], 512)
        self.assertEqual(payload["reasoning_effort"], "none")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertNotIn("input", payload)

    def test_bases_full_routes_and_auxiliary_paths_preserve_origin_and_custom_urls(self):
        for api_type, suffix in (("chat_completions", "/chat/completions"), ("responses", "/responses")):
            for base in ("https://llm.example", "https://llm.example/v1", "https://llm.example/v1/"):
                self.assertEqual(completion_url(base, api_type), "https://llm.example/v1" + suffix)
            full = "https://llm.example/v1" + suffix
            self.assertEqual(completion_url(full + "/", api_type), full)
            self.assertEqual(auxiliary_url(full, "models", api_type), "https://llm.example/v1/models")
            self.assertEqual(completion_url("https://llm.example/custom/path", api_type), "https://llm.example/custom/path")
            self.assertIsNone(auxiliary_url("https://llm.example/custom/path", "models", api_type))
        self.assertEqual(auxiliary_url("https://llm.example/v1/responses", "responses/input_tokens", "responses"), "https://llm.example/v1/responses/input_tokens")

    async def test_api_protocol_switch_preserves_same_endpoint_key_and_url_consents(self):
        db = make_db()
        self.addCleanup(db._sqlite_conn.close)
        url = "http://127.0.0.1:8080/v1"
        with patch.dict("os.environ", {"CATBOT_LLM_ALLOWED_HOSTS": ""}):
            await patch_automod_config(1, {"rules": {"llm": {"endpoint_url": url, "api_key": "private-key",
                "insecure_http_confirmed_url": url, "private_endpoint_confirmed_url": url}}}, db, {})
            result = await patch_automod_config(1, {"rules": {"llm": {"api_type": "responses"}}}, db, {})
            cfg = db.get_automod_config(1)["rules"]["llm"]
            self.assertEqual(cfg["api_key"], "private-key")
            self.assertEqual(cfg["private_endpoint_confirmed_url"], url)
            self.assertEqual(cfg["insecure_http_confirmed_url"], url)
            self.assertEqual(result["config"]["rules"]["llm"]["api_type"], "responses")
            self.assertNotIn("private-key", json.dumps(result))
            for value in ({"api_type": "unknown"}, {"endpoint_url": url + "/chat/completions"}):
                with self.assertRaises(HTTPException) as error:
                    await patch_automod_config(1, {"rules": {"llm": value}}, db, {})
                self.assertEqual(error.exception.status_code, 400)
            fresh = db.get_automod_config(2)["rules"]["llm"]
            self.assertEqual(fresh["api_type"], "chat_completions")
            self.assertEqual(fresh, validate_llm_config(fresh))

    async def test_actual_loopback_http_supports_both_protocols_and_responses_counter(self):
        received = []
        chosen = {"message_id": "10", "author_id": "20", "action": "warn", "duration_seconds": None,
            "reason": "Incumple la regla", "rule": "No insultar", "evidence": [{"source": "text", "id": "10", "quote": "insulto"}]}
        reasoning = {"type": "reasoning", "id": "rs_fixture", "summary": [], "encrypted_content": "fixture-cipher"}
        tool_call = {"type": "function_call", "id": "fc_fixture", "call_id": "call_fixture", "name": "search_web", "arguments": '{"query":"public"}'}
        async def serve(reader, writer):
            try:
                head = (await reader.readuntil(b"\r\n\r\n")).decode("ascii").split("\r\n")
                headers = dict(line.split(": ", 1) for line in head[1:] if ": " in line)
                payload = json.loads(await reader.readexactly(int(headers["Content-Length"])))
                path = head[0].split()[1]
                received.append((path, payload))
                if path == "/v1/chat/completions":
                    result = {"choices": [{"finish_reason": "stop", "message": {"content": "legacy"}}]}
                elif path == "/v1/responses/input_tokens":
                    result = {"object": "response.input_tokens", "input_tokens": 7}
                elif payload.get("text", {}).get("format", {}).get("type") == "json_object":
                    result = response(output_text(json.dumps(chosen)))
                elif payload.get("tools") and not any(item.get("type") == "function_call_output" for item in payload["input"]):
                    result = response(reasoning, tool_call)
                elif payload["input"][-1].get("content") == '["Hola"]':
                    result = response(output_text('["Buenas"]'))
                else:
                    result = response(output_text("responses"))
                body = json.dumps(result).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        origin = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        try:
            with patch.dict("os.environ", {"CATBOT_LLM_ALLOWED_HOSTS": ""}):
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as session:
                    for api_type, endpoint, expected in (("chat_completions", origin + "/v1", "legacy"),
                                                          ("responses", origin + "/v1", "responses"),
                                                          ("responses", origin + "/v1/responses", "responses")):
                        cfg = validate_llm_config({"model": "fixture", "endpoint_url": endpoint, "api_type": api_type,
                            "insecure_http_confirmed_url": endpoint, "private_endpoint_confirmed_url": endpoint})
                        self.assertEqual(await request_completion(session, cfg, [{"role": "user", "content": "Question"}]), expected)
                    vision = [{"role": "user", "content": [{"type": "text", "text": "Question"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aW1hZ2U="}}]}]
                    first = await request_completion(session, cfg, vision, tools=TOOLS, structured=True)
                    self.assertEqual(first["tool_calls"][0]["id"], "call_fixture")
                    self.assertEqual(await request_completion(session, cfg, [*vision, first,
                        {"role": "tool", "tool_call_id": "call_fixture", "content": "Fixture result"}], tools=TOOLS), "responses")
                    self.assertEqual(received[-1][1]["input"][1:3], [reasoning, tool_call])
                    self.assertEqual(received[-2][1]["input"][0]["content"][1]["type"], "input_image")
                    raw = await request_completion(session, cfg, [{"role": "user", "content": "Return JSON"}], moderation=True)
                    self.assertEqual(validate_decision(raw, SimpleNamespace(id=10, author=SimpleNamespace(id=20), content="insulto literal"), "No insultar", set()), chosen)
                    self.assertEqual(received[-1][1]["max_output_tokens"], 512)
                    db = make_db()
                    self.addCleanup(db._sqlite_conn.close)
                    db.set_automod_config(1, rules={"llm": cfg})
                    managed = await manage_payload(SimpleNamespace(db=db), 1, {"content": "Hola"}, enabled=True)
                    self.assertEqual(managed["content"], "Buenas")
                    self.assertEqual(received[-1][1]["input"][-1]["content"], '["Hola"]')
                    count = await endpoint_json(session, cfg, {"model": "fixture", "input": responses_input([
                        {"role": "user", "content": "Question"}])}, auxiliary="responses/input_tokens")
                    self.assertEqual(count["input_tokens"], 7)
            self.assertEqual([path for path, payload in received], ["/v1/chat/completions", *["/v1/responses"] * 6, "/v1/responses/input_tokens"])
            self.assertIn("messages", received[0][1])
            self.assertNotIn("messages", received[1][1])
            self.assertIs(received[1][1]["store"], False)
            self.assertEqual(received[-1][1]["input"], [{"role": "user", "content": "Question"}])
            self.assertTrue(all(payload["store"] is False for path, payload in received if path == "/v1/responses"))
        finally:
            server.close()
            await server.wait_closed()
