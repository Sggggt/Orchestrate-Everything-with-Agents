"""Streaming boundaries, original provider errors, and retained partial output."""

import asyncio
import base64
import json
import unittest
from contextlib import asynccontextmanager

import httpx

from oea.contracts.configuration import Model
from oea.contracts.errors import RuntimeFault
from oea.models.api import ModelAPI, ModelFailure


def frame(value):
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode("utf-8")


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, open_tail=False):
        self.chunks, self.open_tail, self.closed = chunks, open_tail, False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.open_tail:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


class Transport:
    def __init__(self, handler):
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    @asynccontextmanager
    async def borrow(self, base_url):
        yield self.client


class ModelAPITests(unittest.IsolatedAsyncioTestCase):
    async def make(self, stream, *, status=200, redact=lambda value: value):
        self.requests = []

        def handle(request):
            self.requests.append(request)
            return httpx.Response(status, stream=stream, headers={"content-type": "text/event-stream" if status == 200 else "application/json"})

        transport = Transport(handle)
        self.addAsyncCleanup(transport.client.aclose)
        return ModelAPI(transport, secret=lambda reference: "unit-credential", redact=redact)

    def model(self):
        return Model(base_url="https://example.invalid/v1", model="test", credential_ref="vault:main")

    async def test_fragmented_unicode_and_usage_survive_actual_stream_parser(self):
        encoded = frame({"choices": [{"index": 0, "delta": {"content": "结果🙂"}, "finish_reason": None}]})
        tail = frame({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        usage = {"prompt_tokens": 12, "completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 4}}
        stream = Stream([bytes([part]) for part in encoded] + [tail, frame({"choices": [], "usage": usage}), b"data: [DONE]\n\n"])
        api = await self.make(stream)
        deltas = []

        async def emit(value):
            deltas.append(value["text"])

        result = await api.complete(self.model(), [{"role": "user", "content": "Read"}], emit=emit)
        self.assertEqual(result.text, "结果🙂")
        self.assertEqual(result.usage, usage)
        self.assertEqual(deltas, ["结果🙂"])
        self.assertEqual(self.requests[0].headers["authorization"], "Bearer unit-credential")

    async def test_terminal_boundary_returns_without_waiting_for_open_tail(self):
        stream = Stream([frame({"choices": [{"index": 0, "delta": {"content": "complete"}, "finish_reason": "stop"}]})], open_tail=True)
        api = await self.make(stream)
        result = await asyncio.wait_for(api.complete(self.model(), [{"role": "user", "content": "Read"}]), 1)
        self.assertEqual(result.text, "complete")
        self.assertTrue(stream.closed)

    async def test_disconnect_retains_the_complete_bounded_partial_result(self):
        partial = "evidence " * 3000
        stream = Stream([frame({"choices": [{"index": 0, "delta": {"content": partial}, "finish_reason": None}]})])
        api = await self.make(stream)
        with self.assertRaises(ModelFailure) as caught:
            await api.complete(self.model(), [{"role": "user", "content": "Read"}])
        self.assertEqual(caught.exception.code, "MODEL_STREAM_INTERRUPTED")
        self.assertEqual(caught.exception.partial.text, partial)

    async def test_http_error_keeps_original_safe_provider_detail(self):
        stream = Stream([json.dumps({"error": {"code": "invalid_parameter", "param": "tools", "message": "unit-credential was rejected"}}).encode()])
        api = await self.make(stream, status=400, redact=lambda text: text.replace("unit-credential", "[redacted]"))
        with self.assertRaises(RuntimeFault) as caught:
            await api.complete(self.model(), [{"role": "user", "content": "Read"}])
        self.assertEqual(caught.exception.details["provider_code"], "invalid_parameter")
        self.assertEqual(caught.exception.details["provider_param"], "tools")
        self.assertNotIn("unit-credential", json.dumps(caught.exception.as_dict()))

    async def test_tool_call_fragments_keep_the_provider_action_identity(self):
        stream = Stream([
            frame({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "original-call", "function": {"name": "read", "arguments": '{"path":'}}]}}]}),
            frame({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "_file", "arguments": '"source.txt"}'}}]}, "finish_reason": "tool_calls"}]}),
            b"data: [DONE]\n\n",
        ])
        api = await self.make(stream)
        result = await api.complete(self.model(), [{"role": "user", "content": "Read"}])
        self.assertEqual(result.tool_calls[0]["id"], "original-call")
        self.assertEqual(result.tool_calls[0]["function"]["name"], "read_file")
        self.assertEqual(json.loads(result.tool_calls[0]["function"]["arguments"]), {"path": "source.txt"})

    async def test_sse_envelope_bytes_do_not_limit_retained_output(self):
        envelope = frame({"id": "completion-" + "x" * 192, "object": "chat.completion.chunk", "created": 1,
                          "model": "test", "choices": [{"index": 0, "delta": {}, "finish_reason": None}]})
        self.assertGreater(len(envelope) * 8192, 2 * 1024 * 1024)
        usage = {"prompt_tokens": 12, "completion_tokens": 2}
        def chunks():
            for _ in range(8192):
                yield envelope
            yield frame({"choices": [{"delta": {"content": "Done"}, "finish_reason": "stop"}]})
            yield frame({"choices": [], "usage": usage})
            yield b"data: [DONE]\n\n"
        api = await self.make(Stream(chunks()))
        result = await api.complete(self.model(), [{"role": "user", "content": "Read"}])
        self.assertEqual(result.text, "Done")
        self.assertEqual(result.usage, usage)
        self.assertEqual(len(self.requests), 1)

    async def test_ignored_reasoning_does_not_consume_retained_output_bound(self):
        reasoning = frame({"choices": [{"delta": {"reasoning_content": "r" * (600 * 1024)}}]})
        self.assertGreater(len(reasoning) * 4, 2 * 1024 * 1024)
        stream = Stream([reasoning] * 4 + [frame({"choices": [{"delta": {"content": "Done"}, "finish_reason": "stop"}]}), b"data: [DONE]\n\n"])
        api = await self.make(stream)
        result = await api.complete(self.model(), [{"role": "user", "content": "Read"}])
        self.assertEqual(result.text, "Done")
        self.assertTrue(stream.closed)

    async def test_high_output_request_accepts_large_decoded_text_without_lowering_tokens(self):
        # Repeated whitespace can occupy many bytes while using few provider tokens.
        chunk = " " * (256 * 1024)
        def chunks():
            for _ in range(12):
                yield frame({"choices": [{"delta": {"content": chunk}}]})
            yield frame({"choices": [{"delta": {"content": "Done"}, "finish_reason": "stop"}]})
            yield frame({"choices": [], "usage": {"completion_tokens": 24580}})
            yield b"data: [DONE]\n\n"
        api = await self.make(Stream(chunks()))
        result = await api.complete(self.model(), [{"role": "user", "content": "Read"}], output_tokens=131072)
        self.assertEqual(result.text, chunk * 12 + "Done")
        self.assertEqual(result.usage["completion_tokens"], 24580)
        self.assertEqual(json.loads(self.requests[0].content)["max_completion_tokens"], 131072)
        self.assertEqual(len(self.requests), 1)

    async def test_high_output_request_accepts_large_fragmented_tool_arguments(self):
        padding = " " * (64 * 1024)
        def chunks():
            yield frame({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "original-call", "function": {"name": "inspect", "arguments": '{"value":1'}}]}}]})
            for _ in range(48):
                yield frame({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": padding}}]}}]})
            yield frame({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]}, "finish_reason": "tool_calls"}]})
            yield frame({"choices": [], "usage": {"completion_tokens": 24590}})
            yield b"data: [DONE]\n\n"
        api = await self.make(Stream(chunks()))
        result = await api.complete(self.model(), [{"role": "user", "content": "Read"}], output_tokens=131072)
        self.assertEqual(result.tool_calls[0]["id"], "original-call")
        self.assertEqual(result.tool_calls[0]["function"]["name"], "inspect")
        self.assertEqual(json.loads(result.tool_calls[0]["function"]["arguments"]), {"value": 1})
        self.assertGreater(len(result.tool_calls[0]["function"]["arguments"]), 2 * 1024 * 1024)
        self.assertEqual(result.usage["completion_tokens"], 24590)
        self.assertEqual(json.loads(self.requests[0].content)["max_completion_tokens"], 131072)

    async def test_tool_only_overflow_retains_bounded_unfinished_fragments_and_usage(self):
        chunk = "a" * (256 * 1024)
        usage = {"prompt_tokens": 12, "completion_tokens": 7}
        def chunks():
            yield frame({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "original-call", "function": {"name": "inspect"}}]}}]})
            yield frame({"choices": [], "usage": usage})
            for _ in range(9):
                yield frame({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": chunk}}]}}]})
        stream = Stream(chunks())
        api = await self.make(stream)
        with self.assertRaises(ModelFailure) as caught:
            await api.complete(self.model(), [{"role": "user", "content": "Read"}], output_tokens=1024)
        failure = caught.exception
        self.assertEqual(failure.code, "MODEL_RESPONSE_TOO_LARGE")
        self.assertEqual(failure.partial.text, "")
        self.assertEqual(failure.partial.finish_reason, "interrupted")
        self.assertEqual(failure.partial.usage, usage)
        self.assertEqual(failure.partial.tool_calls[0]["id"], "original-call")
        self.assertEqual(failure.partial.tool_calls[0]["function"]["name"], "inspect")
        self.assertEqual(failure.partial.tool_calls[0]["function"]["arguments"], chunk * 7)
        self.assertLessEqual(sum(len(value.encode("utf-8")) for value in (failure.partial.tool_calls[0]["id"], *failure.partial.tool_calls[0]["function"].values())), 2 * 1024 * 1024)
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(stream.closed)

    async def test_tool_only_disconnect_preserves_redacted_fragments_without_success(self):
        usage = {"prompt_tokens": 12, "completion_tokens": 3}
        stream = Stream([
            frame({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "original-call", "function": {"name": "inspect", "arguments": '{"path":"unit-credential'}}]}}]}),
            frame({"choices": [], "usage": usage}),
        ])
        api = await self.make(stream, redact=lambda value: value.replace("unit-credential", "[redacted]"))
        with self.assertRaises(ModelFailure) as caught:
            await api.complete(self.model(), [{"role": "user", "content": "Read"}])
        self.assertEqual(caught.exception.code, "MODEL_STREAM_INTERRUPTED")
        partial = caught.exception.partial
        self.assertEqual(partial.text, "")
        self.assertEqual(partial.finish_reason, "interrupted")
        self.assertEqual(partial.usage, usage)
        self.assertEqual(partial.tool_calls[0]["function"]["arguments"], '{"path":"[redacted]')
        self.assertNotIn("unit-credential", partial.model_dump_json())
        self.assertTrue(stream.closed)

    async def test_single_event_bound_still_rejects_oversized_frames_with_partial_evidence(self):
        stream = Stream([
            frame({"choices": [{"delta": {"content": "Retained evidence"}}]}),
            frame({"choices": [{"delta": {"content": "x" * (1024 * 1024)}}]}),
        ])
        api = await self.make(stream)
        with self.assertRaises(ModelFailure) as caught:
            await api.complete(self.model(), [{"role": "user", "content": "Read"}], output_tokens=131072)
        self.assertEqual(caught.exception.code, "MODEL_PROTOCOL_ERROR")
        self.assertEqual(caught.exception.partial.text, "Retained evidence")
        self.assertTrue(stream.closed)

    async def test_invalid_tool_index_retains_preceding_valid_fragments_as_failure(self):
        stream = Stream([
            frame({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "original-call", "function": {"name": "inspect", "arguments": "{"}}]}}]}),
            frame({"choices": [{"delta": {"tool_calls": [{"index": "invalid", "function": {"arguments": "}"}}]}}]}),
        ])
        api = await self.make(stream)
        with self.assertRaises(ModelFailure) as caught:
            await api.complete(self.model(), [{"role": "user", "content": "Read"}])
        self.assertEqual(caught.exception.code, "MODEL_PROTOCOL_ERROR")
        self.assertEqual(caught.exception.partial.tool_calls[0]["function"]["arguments"], "{")
        self.assertEqual(caught.exception.partial.finish_reason, "interrupted")

    async def test_full_image_allowance_with_unicode_and_tools_is_sent_once(self):
        image = "data:image/png;base64," + base64.b64encode(b"\0" * (5 * 1024 * 1024)).decode("ascii")
        prompt = "核对图片证据🙂"
        messages = [{"role": "user", "content": [{"type": "text", "text": prompt},
                    *[{"type": "image_url", "image_url": {"url": image, "detail": "auto"}} for _ in range(4)]]}]
        tools = [{"type": "function", "function": {"name": "inspect", "description": "读取证据",
                  "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]
        api = await self.make(Stream([frame({"choices": [{"delta": {"content": "Done"}, "finish_reason": "stop"}]}), b"data: [DONE]\n\n"]))
        result = await api.complete(self.model(), messages, tools=tools, output_tokens=131072)
        self.assertEqual(result.text, "Done")
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertGreater(len(request.content), 8 * 1024 * 1024)
        self.assertLess(len(request.content), 40 * 1024 * 1024)
        self.assertEqual(request.headers["authorization"], "Bearer unit-credential")
        self.assertEqual(request.headers["content-type"], "application/json")
        self.assertEqual(int(request.headers["content-length"]), len(request.content))
        self.assertIn(prompt.encode("utf-8"), request.content)
        body = json.loads(request.content)
        self.assertEqual(body["messages"], messages)
        self.assertEqual(body["tools"], tools)
        self.assertEqual(body["max_completion_tokens"], 131072)

    async def test_oversized_encoded_input_is_rejected_before_any_request(self):
        api = await self.make(Stream([]))
        messages = [{"role": "user", "content": "x" * (40 * 1024 * 1024)}]
        with self.assertRaises(RuntimeFault) as caught:
            await api.complete(self.model(), messages, output_tokens=131072)
        self.assertEqual(caught.exception.code, "MODEL_REQUEST_TOO_LARGE")
        self.assertFalse(caught.exception.details["request_sent"])
        self.assertEqual(self.requests, [])
