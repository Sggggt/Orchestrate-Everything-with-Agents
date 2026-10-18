"""Shared model transport and run-bound access; model credentials remain in the host."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

import httpx

from .errors import AdapterFailure, RuntimeFault, require
from .models import AdapterEvent, LLMRequest, LLMResponse


def model_required(config):
    return bool(
        config
        and (config.kind == "llm" or (config.kind == "process" and config.llm_required))
    )


def model_setup(config, *, has_api_key=False):
    required = model_required(config)
    missing = [name for name in ("base_url", "model") if not getattr(config, name, "")]
    state = (
        "not_required"
        if not required
        else "needs_configuration"
        if missing
        else "configured"
    )
    return {
        "required": required,
        "state": state,
        "missing": missing if required else [],
        "has_api_key": has_api_key,
        "verification": "not_recorded",
        "message": (
            "请前往 Agent 集成 → 配置 → 模型 API，填写 API 地址、模型和所需密钥，保存后测试连接。"
            if state == "needs_configuration"
            else "模型参数已保存；可在 Agent 配置页修改并测试，真实业务验收需另外运行。"
            if required
            else "此 Agent 无需平台模型配置。"
        ),
    }


async def sse_data(response, max_bytes=1024 * 1024):
    buffer, fields, event_bytes = b"", [], 0
    async for packet in response.aiter_bytes():
        for offset in range(0, len(packet), 65536):
            buffer += packet[offset : offset + 65536]
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.removesuffix(b"\r")
                require(
                    len(line) <= max_bytes,
                    "ADAPTER_PROTOCOL_ERROR",
                    "LLM stream line is too large",
                )
                if not line:
                    if fields:
                        try:
                            yield b"\n".join(fields).decode("utf-8")
                        except UnicodeError:
                            raise RuntimeFault(
                                "ADAPTER_PROTOCOL_ERROR",
                                "LLM stream is not valid UTF-8",
                            ) from None
                    fields, event_bytes = [], 0
                elif line.startswith(b"data:"):
                    value = line[5:].removeprefix(b" ")
                    event_bytes += len(value)
                    require(
                        event_bytes <= max_bytes,
                        "ADAPTER_PROTOCOL_ERROR",
                        "LLM stream event is too large",
                    )
                    fields.append(value)
            require(
                len(buffer) <= max_bytes,
                "ADAPTER_PROTOCOL_ERROR",
                "LLM stream line is too large",
            )


class ConfiguredLLM:
    def __init__(self, config, api_key=""):
        self.config = config.model_copy(deep=True)
        self.api_key = api_key

    async def stream_completion(self, messages, context, tools=None, tool_choice=None):
        require(
            self.config.base_url and self.config.model,
            "AGENT_NOT_CONFIGURED",
            "Configure an API endpoint and model first",
        )
        from .workspace import model_images
        messages = await model_images(messages, context)
        body = {"model": self.config.model, "messages": messages, "stream": True}
        if self.config.temperature is not None:
            body["temperature"] = self.config.temperature
        if self.config.max_output_tokens is not None:
            body[self.config.token_parameter] = self.config.max_output_tokens
        if tools:
            body["tools"] = tools
            choice = tool_choice if tool_choice is not None else getattr(context, "model_tool_choice", None)
            if choice is not None:
                body["tool_choice"] = choice
        headers = {"Accept": "text/event-stream"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        calls, text, completed, finish_reason = {}, "", False, None
        try:
            async with httpx.AsyncClient(
                timeout=self.config.timeout_seconds, follow_redirects=False,
                trust_env=urlsplit(self.config.base_url).hostname not in {"localhost", "127.0.0.1", "::1"},
            ) as client:
                async with client.stream(
                    "POST",
                    self.config.base_url + "/chat/completions",
                    json=body,
                    headers=headers,
                ) as response:
                    if response.status_code >= 400:
                        if response.status_code in {400, 415, 422} and any(
                            isinstance(message.get("content"), list) and any(block.get("type") == "image_url" for block in message["content"])
                            for message in messages):
                            raise RuntimeFault("MODEL_IMAGE_REJECTED", "The configured model rejected the image request")
                        raise AdapterFailure(
                            f"LLM API returned HTTP {response.status_code}",
                            retryable=response.status_code in {429, 502, 503, 504},
                            safe_to_retry=True,
                        )
                    require(
                        "text/event-stream"
                        in response.headers.get("content-type", "").lower(),
                        "ADAPTER_PROTOCOL_ERROR",
                        "LLM endpoint did not return an SSE stream",
                    )
                    async for data in sse_data(response):
                        if context.cancelled.is_set():
                            raise asyncio.CancelledError
                        data = data.strip()
                        if data == "[DONE]":
                            completed = True
                            break
                        if not data:
                            continue
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            raise RuntimeFault(
                                "ADAPTER_PROTOCOL_ERROR",
                                "LLM stream contains invalid JSON",
                            ) from None
                        if "error" in chunk:
                            raise AdapterFailure(
                                "LLM provider reported a streaming error"
                            )
                        if chunk.get("usage"):
                            yield AdapterEvent(
                                type="usage", payload={"usage": chunk["usage"]}
                            )
                        choices = chunk.get("choices", [])
                        if not choices:
                            continue
                        choice = choices[0]
                        delta = choice.get("delta", {})
                        content = delta.get("content")
                        if isinstance(content, str) and content:
                            text += content
                            require(
                                len(text.encode()) <= 4 * 1024 * 1024,
                                "ADAPTER_PROTOCOL_ERROR",
                                "LLM output is too large",
                            )
                            step = max(
                                1, context._runtime.config.max_event_payload_bytes // 8
                            )
                            for offset in range(0, len(content), step):
                                yield AdapterEvent(
                                    type="progress",
                                    payload={"delta": content[offset : offset + step]},
                                )
                        for item in delta.get("tool_calls", []):
                            index = item["index"]
                            require(
                                isinstance(index, int) and 0 <= index < 64,
                                "ADAPTER_PROTOCOL_ERROR",
                                "Too many LLM tool calls",
                            )
                            call = calls.setdefault(
                                index,
                                {
                                    "id": "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                },
                            )
                            if item.get("id"):
                                call["id"] += item["id"]
                            fields = item.get("function", {})
                            call["function"]["name"] += fields.get("name", "")
                            call["function"]["arguments"] += fields.get("arguments", "")
                            require(
                                len(call["id"]) <= 256
                                and len(call["function"]["name"]) <= 128,
                                "ADAPTER_PROTOCOL_ERROR",
                                "Tool identity is too large",
                            )
                            require(
                                len(call["function"]["arguments"].encode()) <= 65536,
                                "ADAPTER_PROTOCOL_ERROR",
                                "Tool arguments are too large",
                            )
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                    require(
                        completed and finish_reason is not None,
                        "ADAPTER_PROTOCOL_ERROR",
                        "LLM stream ended without a final boundary",
                    )
        except httpx.HTTPError:
            raise AdapterFailure(
                "LLM connection failed or timed out",
                retryable=True,
                safe_to_retry=not text and not calls,
            ) from None
        yield {
            "text": text,
            "calls": [calls[k] for k in sorted(calls)],
            "finish_reason": finish_reason,
        }

    async def complete(self, messages, context, *, tools=None, on_event=None, tool_choice=None):
        usage, result = {}, None
        async for event in self.stream_completion(messages, context, tools, tool_choice):
            if isinstance(event, dict):
                result = event
            else:
                if event.type == "usage":
                    usage = event.payload.get("usage", event.payload)
                if on_event:
                    await on_event(event)
        require(
            result is not None,
            "ADAPTER_PROTOCOL_ERROR",
            "Model did not return a completion",
        )
        return LLMResponse(
            model=self.config.model,
            text=result["text"],
            tool_calls=result["calls"],
            finish_reason=result["finish_reason"],
            usage=usage,
        )


class RunLLM:
    """Access one captured Agent model configuration, with no child-side credentials."""

    def __init__(self, context, client):
        self.context, self.client = context, client
        self.lock = asyncio.Lock()

    async def info(self):
        await self.context.check_current()
        if self.client is None:
            return {
                "required": False,
                "state": "not_required",
                "model": "",
                "base_url": "",
            }
        config = self.client.config
        return {
            **model_setup(config, has_api_key=bool(self.client.api_key)),
            **{
                key: getattr(config, key)
                for key in (
                    "base_url",
                    "model",
                    "temperature",
                    "max_output_tokens",
                    "token_parameter",
                    "timeout_seconds",
                )
            },
        }

    async def complete(
        self, messages, *, tools=None, include_instructions=True, emit_progress=False, tool_choice=None
    ):
        from pydantic import ValidationError

        try:
            request = LLMRequest(
                messages=messages,
                tools=tools,
                include_instructions=include_instructions,
                emit_progress=emit_progress,
                tool_choice=tool_choice,
            )
        except ValidationError:
            raise RuntimeFault(
                "INVALID_REQUEST", "Invalid or oversized model request"
            ) from None
        await self.context.check_current()
        require(
            self.client is not None and model_required(self.client.config),
            "MODEL_NOT_ENABLED",
            "Enable the platform model for this Agent in its configuration (llm_required=true for process Agents).",
        )
        require(
            self.client.config.base_url and self.client.config.model,
            "AGENT_NOT_CONFIGURED",
            "Configure this Agent's model API in Agent settings before running it.",
        )
        async with self.lock:
            await self.context.check_current()
            if self.context.cancelled.is_set():
                raise asyncio.CancelledError
            work = asyncio.create_task(self._complete(request))
            cancellation = asyncio.create_task(self.context.cancelled.wait())
            try:
                done, _ = await asyncio.wait(
                    {work, cancellation}, return_when=asyncio.FIRST_COMPLETED
                )
                if cancellation in done:
                    raise asyncio.CancelledError
                return await work
            finally:
                for task in (work, cancellation):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(work, cancellation, return_exceptions=True)

    async def _complete(self, request):
        from .presentation import OUTPUT_GUIDANCE, SummaryStream
        from .operations import OPERATIONS_GUIDANCE, COORDINATOR_GUIDANCE
        context, runtime = self.context, self.context._runtime
        messages = list(request.messages)
        if request.include_instructions:
            instructions = "\n\n".join(
                filter(
                    None, [self.client.config.system_prompt, context.request.agent_md, OUTPUT_GUIDANCE, OPERATIONS_GUIDANCE]
                )
            )
            if instructions:
                messages.insert(0, {"role": "system", "content": instructions})
            skills = await context.list_skills()
            if skills:
                messages.insert(0, {"role": "system", "content": "Enabled Skill catalog; use context.read_skill to load relevant instructions:\n" + json.dumps(skills)})
        if context.loaded_skills:
            messages.insert(0, {"role": "system", "content": "Required Skill instructions loaded by the platform:\n" + json.dumps(list(context.loaded_skills.values()))})
        if context.request.operations_consumer_id:
            messages.insert(0, {"role": "system", "content": COORDINATOR_GUIDANCE})
        # Retain the run's configuration snapshot; never accept caller-supplied
        # endpoint, model, Authorization, or another Agent's identity.
        messages = runtime.clean_json(messages, 524288)

        summary_stream = SummaryStream()
        async def progress(event):
            if event.type == "usage" or (
                request.emit_progress and event.type == "progress"
            ):
                req = context.request
                payload = event.payload
                if event.type == "progress":
                    delta = summary_stream.feed(payload.get("delta", ""))
                    if not delta:
                        return
                    payload = {"delta": delta, "format": "markdown", "channel": "summary"}
                runtime.events.progress(
                    req.session_id,
                    req.thread_id,
                    req.task_id,
                    req.run_id,
                    "run." + event.type,
                    payload,
                )

        tools, choice = request.tools, request.tool_choice
        if tools:
            from .execution_policy import model_tools
            tools, enforced = model_tools(context, tools)
            if enforced is not None:
                choice = enforced
        options = {"tools": tools, "on_event": progress}
        if choice is not None:
            options["tool_choice"] = choice
        response = await self.client.complete(messages, context, **options)
        if request.emit_progress:
            tail = summary_stream.finish()
            if tail:
                req = context.request
                runtime.events.progress(req.session_id, req.thread_id, req.task_id, req.run_id,
                                        "run.progress", {"delta": tail, "format": "markdown", "channel": "summary"})
        await context.check_current()
        safe = runtime.clean_json(response.model_dump(mode="json"), 524288)
        return LLMResponse.model_validate(safe)
