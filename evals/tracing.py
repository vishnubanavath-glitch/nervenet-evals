"""Record everything the app's LLMs see and do, by wrapping functions at runtime.

Nothing in mcp-utilities is edited; wrappers are installed inside `Recorder`
and removed when it exits.

What is captured:
  llm_calls     - every chat LLM call (LLMManager.stream_response): model, system prompt,
                  full message history sent, tool names offered, thinking, text, tool_use
                  blocks, stop reason, token usage, duration
  tool_calls    - every MCP tool execution: arguments as sent (masked SQL), the raw
                  result (may hold real rows; LOCAL ONLY), and the result as the model
                  received it (filled in from the next LLM call's tool_result block)
  schema_calls  - every schema-context generator call (_ask): task, payload, output, usage
  api_requests  - every request made through the Anthropic SDK, from any thread
                  (chat, schema generator, background summariser): model + caller.
                  This is the ground truth for "which model was used".
"""
from __future__ import annotations

import copy
import threading
import time
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest import mock


def _jsonable(obj: Any) -> Any:
    """Turn SDK objects into plain data."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


class Trace:
    def __init__(self) -> None:
        self.llm_calls: list[dict[str, Any]] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.schema_calls: list[dict[str, Any]] = []
        self.api_requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def add(self, bucket: str, item: dict[str, Any]) -> None:
        with self._lock:
            getattr(self, bucket).append(item)

    def models_used(self) -> list[str]:
        return sorted({r["model"] for r in self.api_requests if r.get("model")})

    def to_dict(self) -> dict[str, Any]:
        return {
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "schema_calls": self.schema_calls,
            "api_requests": self.api_requests,
            "models_used": self.models_used(),
        }


def _link_tool_results(trace: Trace) -> None:
    """Attach, to each tool call, the tool_result block the model actually received."""
    seen: dict[str, Any] = {}
    for call in trace.llm_calls:
        for msg in call["request"]["messages"]:
            if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
                continue
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    seen[block.get("tool_use_id")] = block.get("content")
    for tc in trace.tool_calls:
        if tc["tool_use_id"] in seen:
            tc["result_seen_by_model"] = seen[tc["tool_use_id"]]


@contextmanager
def Recorder():
    """with Recorder() as trace: ...run the app...  -> trace holds everything."""
    import anthropic.resources.messages as sdk_messages
    import conversation.llm_manager as llm_manager
    import conversation.mcp.execution as execution
    import conversation.schema_context_generator as scg

    trace = Trace()

    # --- SDK level: every request, any caller, any thread -------------------------
    def sdk_wrap(name: str, original):
        def wrapper(self, *args, **kwargs):
            trace.add("api_requests", {
                "api": name,
                "model": kwargs.get("model"),
                "thread": threading.current_thread().name,
                "at": time.time(),
            })
            return original(self, *args, **kwargs)
        return wrapper

    # --- Chat LLM calls -------------------------------------------------------------
    original_stream = llm_manager.LLMManager.stream_response

    def stream_response(self, system_prompt, messages, usage_callback=None, tools=None,
                        result_container=None, tool_choice=None):
        container = result_container if result_container is not None else {}
        record = {
            "model": self.model,
            "request": {
                "system": copy.deepcopy(system_prompt),
                "messages": copy.deepcopy(messages),
                "tools_offered": [t.get("name") for t in (tools or [])],
                "tool_choice": tool_choice,
            },
            "started_at": time.time(),
        }
        try:
            yield from original_stream(self, system_prompt, messages, usage_callback=usage_callback,
                                       tools=tools, result_container=container, tool_choice=tool_choice)
        finally:
            final = container.get("final_message")
            record["duration_ms"] = int((time.time() - record["started_at"]) * 1000)
            record["ttft_ms"] = container.get("ttft_ms")
            if final is not None:
                record["response"] = {
                    "model": getattr(final, "model", None),
                    "stop_reason": final.stop_reason,
                    "content": _jsonable(final.content),
                    "usage": _jsonable(final.usage),
                }
            trace.add("llm_calls", record)

    # --- MCP tool calls -------------------------------------------------------------
    original_execute = execution.execute_tool

    def execute_tool(tool_name, arguments, tool_use_id, server_url, user=None):
        started = time.time()
        result = original_execute(tool_name=tool_name, arguments=arguments, tool_use_id=tool_use_id,
                                  server_url=server_url, user=user)
        trace.add("tool_calls", {
            "tool_use_id": tool_use_id,
            "name": tool_name,
            "arguments": copy.deepcopy(arguments),
            "user_id": getattr(user, "pk", None),
            "duration_ms": int((time.time() - started) * 1000),
            "is_error": bool((result or {}).get("is_error")),
            # Copied now: the chat loop rewrites this block before the model sees it.
            "raw_result": copy.deepcopy((result or {}).get("content")),
        })
        return result

    # --- Schema-context generator ---------------------------------------------------
    original_ask = scg._ask

    def ask(task, payload, schema, max_tokens):
        record = {"task": task, "payload": copy.deepcopy(payload), "model_setting": scg._model(),
                  "started_at": time.time()}
        try:
            data, usage = original_ask(task, payload, schema, max_tokens)
            record.update(output=copy.deepcopy(data), usage=usage)
            return data, usage
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record["duration_ms"] = int((time.time() - record["started_at"]) * 1000)
            trace.add("schema_calls", record)

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(
            sdk_messages.Messages, "create", sdk_wrap("messages.create", sdk_messages.Messages.create)))
        stack.enter_context(mock.patch.object(
            sdk_messages.Messages, "stream", sdk_wrap("messages.stream", sdk_messages.Messages.stream)))
        stack.enter_context(mock.patch.object(llm_manager.LLMManager, "stream_response", stream_response))
        stack.enter_context(mock.patch.object(execution, "execute_tool", execute_tool))
        stack.enter_context(mock.patch.object(scg, "_ask", ask))
        try:
            yield trace
        finally:
            _link_tool_results(trace)
