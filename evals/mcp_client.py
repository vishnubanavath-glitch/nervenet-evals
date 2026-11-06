"""Call the app's MCP tools directly as a test user (no LLM), via the same path the chat uses.

    from evals.mcp_client import ToolCaller
    tools = ToolCaller(ds, "full_access")
    result = tools.call("query", {"queries": [{"source_id": "tgspdcl", "sql": "SELECT COUNT(*) FROM tbl_1"}]})

The call goes through conversation.mcp.execution.execute_tool: the user's OAuth token,
the MCP server's auth + RBAC, anonymization and the real database.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from evals.config import Dataset


@dataclass
class ToolResult:
    tool: str
    arguments: dict[str, Any]
    raw: str                 # text exactly as the MCP server returned it
    data: Any                # parsed JSON (or None if not JSON)
    is_error: bool           # MCP-level error flag
    duration_ms: int

    @property
    def ok(self) -> bool:
        """False on transport errors and on tool payloads saying ok=false."""
        if self.is_error:
            return False
        if isinstance(self.data, dict) and self.data.get("ok") is False:
            return False
        return True


class ToolCaller:
    def __init__(self, ds: Dataset, user_label: str):
        from accounts.models import User
        from conversation.mcp.discovery import get_cached_mcp_tools

        self.ds = ds
        self.user_label = user_label
        self.user = User.objects.get(email=ds.user(user_label).email)
        tools = get_cached_mcp_tools()
        if tools.get("discovery_failed"):
            raise RuntimeError(f"MCP tool discovery failed: {tools.get('error')}")
        self.routing: dict[str, str] = tools["routing"]
        self.schemas = {t["name"]: t for t in tools["tools"]}

    def call(self, tool: str, arguments: dict[str, Any]) -> ToolResult:
        from conversation.mcp.execution import execute_tool

        if tool not in self.routing:
            raise KeyError(f"Tool '{tool}' not offered by the MCP server (have {sorted(self.routing)})")
        started = time.time()
        block = execute_tool(
            tool_name=tool, arguments=arguments, tool_use_id=f"eval_{uuid.uuid4().hex[:12]}",
            server_url=self.routing[tool], user=self.user,
        )
        raw = block.get("content") if isinstance(block.get("content"), str) else json.dumps(block.get("content"))
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            data = None
        return ToolResult(tool=tool, arguments=arguments, raw=raw or "", data=data,
                          is_error=bool(block.get("is_error")), duration_ms=int((time.time() - started) * 1000))

    def sql(self, sql: str, limit: int | None = None, source_id: str | None = None) -> ToolResult:
        item: dict[str, Any] = {"source_id": source_id or self.ds.source_id, "sql": sql}
        if limit is not None:
            item["limit"] = limit
        return self.call("query", {"queries": [item]})
