"""OpenAI-compatible streaming transport without platform resource admission."""

import asyncio
import json

import httpx

from oea.contracts.configuration import MODEL_SSE_EVENT_MAX_BYTES, MODEL_OUTPUT_MIN_BYTES, MODEL_OUTPUT_BYTES_PER_TOKEN
from oea.contracts.measurement import measurement, content_view
from oea.models.request import prepare_request
from oea.storage.persistence import now_ms
from oea.contracts.domain import Completion
from oea.contracts.errors import RuntimeFault, require


async def provider_error(response, redact):
    data = bytearray()
    try:
        async with asyncio.timeout(1):
            async for chunk in response.aiter_bytes(chunk_size=2048):
                data.extend(chunk[:max(0, 8192 - len(data))])
                if len(data) == 8192:
                    break
        value = json.loads(data)
    except (TimeoutError, httpx.HTTPError, ValueError, UnicodeDecodeError):
        return {"provider_body": "unavailable"}
    error = value.get("error", value) if isinstance(value, dict) else {}
    error = {"message": error} if isinstance(error, str) else error
    return {"provider_" + key: redact(str(error[key]))[:800] for key in ("code", "type", "param", "message")
            if isinstance(error, dict) and key in error and isinstance(error[key], (str, int))}


class ModelFailure(RuntimeFault):
    def __init__(self, code, message, partial, **options):
        super().__init__(code, message, **options)
        self.partial = partial


async def sse_data(response, max_bytes=MODEL_SSE_EVENT_MAX_BYTES):
    buffer, fields, event_bytes = b"", [], 0
    async for packet in response.aiter_bytes():
        for offset in range(0, len(packet), 65536):
            buffer += packet[offset:offset + 65536]
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.removesuffix(b"\r")
                require(len(line) <= max_bytes, "MODEL_PROTOCOL_ERROR", "Model stream line is too large", origin="local", stage="stream", measurement=measurement("sse_line_bytes", len(line), "byte", "measured", limit=max_bytes, limit_source="MODEL_SSE_EVENT_MAX_BYTES"))
                if not line:
                    if fields:
                        try:
                            yield b"\n".join(fields).decode("utf-8")
                        except UnicodeError:
                            raise RuntimeFault("MODEL_PROTOCOL_ERROR", "Model stream is not valid UTF-8") from None
                    fields, event_bytes = [], 0
                elif line.startswith(b"data:"):
                    value = line[5:].removeprefix(b" ")
                    event_bytes += len(value)
                    require(event_bytes <= max_bytes, "MODEL_PROTOCOL_ERROR", "Model stream event is too large", origin="local", stage="stream", measurement=measurement("sse_event_bytes", event_bytes, "byte", "measured", limit=max_bytes, limit_source="MODEL_SSE_EVENT_MAX_BYTES"))
                    fields.append(value)
            require(len(buffer) <= max_bytes, "MODEL_PROTOCOL_ERROR", "Model stream line is too large", origin="local", stage="stream", measurement=measurement("sse_line_bytes", len(buffer), "byte", "measured", limit=max_bytes, limit_source="MODEL_SSE_EVENT_MAX_BYTES"))


class ModelAPI:
    def __init__(self, transport, *, secret, redact=lambda value: value):
        self.transport, self.secret, self.redact = transport, secret, redact

    async def complete(self, model, messages, *, tools=(), output_tokens=None, emit=None, on_fact=None):
        require(model.base_url and model.model, "MODEL_NOT_CONFIGURED", "Save a model endpoint and model name before running")
        credential = self.secret(model.credential_ref)
        require(not model.credential_ref or credential, "MODEL_NOT_CONFIGURED", "The configured model credential is unavailable", details={"request_sent": False})
        prepared = prepare_request(model, messages, tools=tools, output_tokens=output_tokens)
        facts = {"prepared_at": now_ms(), "send_started_at": None, "response_received_at": None,
                 "terminal_at": None, "send_started": False, "response_received": False, "provider_accepted": None}
        async def fact(stage):
            if on_fact:
                await on_fact(stage, dict(facts), prepared.measurement)
        await fact("prepared")
        headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
        if credential:
            headers["Authorization"] = "Bearer " + credential
        text, calls, usage, finished, finish_reason, response_bytes = [], {}, {}, False, "stop", 0
        # Bound retained output, not repeated SSE envelopes or ignored provider
        # fields. The byte allowance is a local policy, not a tokenizer guarantee.
        response_limit = max(MODEL_OUTPUT_MIN_BYTES, prepared.output_tokens * MODEL_OUTPUT_BYTES_PER_TOKEN)

        def append_output(fragment, fragments):
            nonlocal response_bytes
            require(isinstance(fragment, str), "MODEL_PROTOCOL_ERROR", "Model output fragments must be text")
            size = len(fragment.encode("utf-8"))
            require(response_bytes + size <= response_limit, "MODEL_RESPONSE_TOO_LARGE", "Decoded model output exceeds its transport bound",
                    origin="local", stage="capture", measurement=measurement("observed_output_bytes", response_bytes + size, "byte", "measured", limit=response_limit, limit_source="local_output_retention"),
                    details={"output_bytes": response_bytes + size, "saved_output_bytes": response_bytes, "max_output_bytes": response_limit})
            response_bytes += size
            if fragment:
                fragments.append(fragment)

        def ordered_calls(transform=lambda value: value):
            return tuple({"id": transform("".join(calls[index]["id"])), "type": "function",
                          "function": {key: transform("".join(calls[index][key])) for key in ("name", "arguments")}}
                         for index in sorted(calls))

        def output_measurement():
            return measurement("captured_output_bytes", response_bytes, "byte", "measured", limit=response_limit,
                               limit_source="max(MODEL_OUTPUT_MIN_BYTES,output_tokens*MODEL_OUTPUT_BYTES_PER_TOKEN)")

        def partial():
            return Completion(text=self.redact("".join(text)), tool_calls=ordered_calls(self.redact),
                              usage=usage, finish_reason="interrupted", measurements=(prepared.measurement, output_measurement()), attempt_facts=dict(facts),
                              content_view=content_view("full", integrity="partial", source_complete=False))

        try:
            async with self.transport.borrow(model.base_url) as client:
                facts.update(send_started=True, send_started_at=now_ms())
                await fact("send_started")
                async with client.stream("POST", model.base_url + "/chat/completions", content=prepared.content, headers=headers,
                                         timeout=model.timeout_seconds) as response:
                    facts.update(response_received=True, response_received_at=now_ms())
                    await fact("response_received")
                    if response.status_code >= 400:
                        provider = await provider_error(response, self.redact)
                        raise RuntimeFault("MODEL_HTTP_ERROR", "Model provider returned an error",
                                           retryable=response.status_code in {429, 502, 503, 504},
                                           origin="provider", stage="response", reason="quota" if provider.get("provider_code") in {"insufficient_quota", "quota_exceeded"} else "unknown",
                                           details={"http_status": response.status_code, **provider})
                    require("text/event-stream" in response.headers.get("content-type", "").lower(),
                            "MODEL_PROTOCOL_ERROR", "Model provider did not return an SSE stream")
                    stream = sse_data(response)
                    async for data in stream:
                        if data.strip() == "[DONE]":
                            finished = True
                            break
                        value = json.loads(data)
                        if value.get("error"):
                            raise RuntimeFault("MODEL_PROVIDER_ERROR", "Model provider reported a stream error", details={"provider_error": self.redact(value["error"])})
                        if value.get("usage"):
                            usage = value["usage"] if isinstance(value["usage"], dict) else {"invalid_usage_type": type(value["usage"]).__name__}
                        for choice in value.get("choices", []):
                            if choice.get("index", 0) != 0:
                                continue
                            delta = choice.get("delta", {})
                            if delta.get("refusal"):
                                finish_reason = "refusal"
                                append_output(delta["refusal"], text)
                            if delta.get("content"):
                                append_output(delta["content"], text)
                                if emit:
                                    await emit({"kind": "model.delta", "text": delta["content"]})
                            for part in delta.get("tool_calls", []):
                                index = part["index"]
                                require(type(index) is int and index >= 0, "MODEL_PROTOCOL_ERROR", "Model tool call indexes must be nonnegative integers")
                                call = calls.setdefault(index, {"id": [], "name": [], "arguments": []})
                                append_output(part.get("id", ""), call["id"])
                                function = part.get("function", {})
                                append_output(function.get("name", ""), call["name"])
                                append_output(function.get("arguments", ""), call["arguments"])
                            if choice.get("finish_reason"):
                                if finish_reason != "refusal":
                                    finish_reason = choice["finish_reason"]
                                finished = True
                        if finished:
                            # A completion boundary is authoritative even when
                            # a provider keeps its optional usage tail open.
                            try:
                                async with asyncio.timeout(0.1):
                                    async for tail in stream:
                                        if tail.strip() == "[DONE]":
                                            break
                                        final = json.loads(tail)
                                        if final.get("usage"):
                                            usage = final["usage"] if isinstance(final["usage"], dict) else {"invalid_usage_type": type(final["usage"]).__name__}
                            except (TimeoutError, httpx.HTTPError, ValueError, RuntimeFault):
                                pass
                            break
                    require(finished, "MODEL_STREAM_INTERRUPTED", "Model stream ended before a completion boundary",
                            details={"partial_text_preview": self.redact("".join(text))[-1600:]})
        except (httpx.TimeoutException, TimeoutError):
            raise ModelFailure("MODEL_TIMEOUT", "Model request timed out", partial(), origin="transport", stage="stream", details={"attempt_facts": dict(facts)}) from None
        except httpx.HTTPError:
            raise ModelFailure("MODEL_CONNECTION_FAILED", "Model connection failed", partial(), origin="transport", stage="stream", details={"attempt_facts": dict(facts)}) from None
        except (ValueError, TypeError, KeyError):
            raise ModelFailure("MODEL_PROTOCOL_ERROR", "Model stream contains invalid structured data", partial(), origin="transport", stage="stream", details={"attempt_facts": dict(facts)}) from None
        except RuntimeFault as error:
            error.details["attempt_facts"] = dict(facts)
            error.identity.setdefault("stage", "stream")
            if text or calls or usage:
                value = partial()
                if error.code == "MODEL_RESPONSE_TOO_LARGE":
                    value.content_view = content_view("full", integrity="truncated", source_complete=False)
                raise ModelFailure(error.code, error.safe_message, value,
                                   retryable=error.retryable, details=error.details, **error.identity) from error
            raise
        ordered = ordered_calls()
        if finish_reason in {"stop", "tool_calls"} and not (all(call["id"] and call["function"]["name"] for call in ordered) and len({call["id"] for call in ordered}) == len(ordered)):
            raise ModelFailure("MODEL_PROTOCOL_ERROR", "Model tool calls require unique identities and names", partial())
        facts["terminal_at"] = now_ms()
        integrity = "incomplete" if finish_reason in {"length", "max_tokens"} else "complete"
        return Completion(text="".join(text), tool_calls=ordered, usage=usage, finish_reason=finish_reason,
                          measurements=(prepared.measurement, output_measurement()), attempt_facts=facts,
                          content_view=content_view("full", integrity=integrity, source_complete=integrity == "complete"))
