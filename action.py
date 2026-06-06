"""
action.py — the Action cognitive layer.

This is the only module that talks to the MCP server. It launches
mcp_server.py over stdio transport, lists the advertised tools, and dispatches
the decision layer's PlannedToolCalls. Tool dispatch is NOT reimplemented here —
we use the MCP ClientSession's own call_tool machinery.

The MCP server is async and lives in its own subprocess. To expose a simple
synchronous API to the agent loop while keeping one server process alive for the
whole run, the connection runs on a dedicated event loop in a background thread;
sync methods submit coroutines to it.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from schemas import ActionResult, PlannedToolCall, ToolSpec

HERE = Path(__file__).parent
SERVER_SCRIPT = HERE / "mcp_server.py"


def _parse_call_result(result: Any) -> Any:
    """Turn an MCP CallToolResult into a plain Python value."""
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        # FastMCP wraps non-dict returns under {"result": ...}
        if isinstance(structured, dict) and set(structured.keys()) == {"result"}:
            return structured["result"]
        return structured
    chunks = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            chunks.append(text)
    joined = "\n".join(chunks)
    try:
        return json.loads(joined)
    except (json.JSONDecodeError, ValueError):
        return joined


class MCPToolHost:
    """Keeps one MCP server subprocess alive for the duration of a run."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._tools: list[ToolSpec] = []
        self._error: BaseException | None = None
        fut = asyncio.run_coroutine_threadsafe(self._serve(), self._loop)
        self._serve_future = fut
        self._ready.wait(timeout=60)
        if self._error:
            raise self._error

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _serve(self) -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(SERVER_SCRIPT)],
            cwd=str(HERE),
        )
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    self._session = session
                    await session.initialize()
                    listing = await session.list_tools()
                    self._tools = [
                        ToolSpec(
                            name=t.name,
                            description=t.description or "",
                            input_schema=t.inputSchema or {},
                        )
                        for t in listing.tools
                    ]
                    self._ready.set()
                    # Hold the session open until shutdown is requested.
                    while not self._stop.is_set():
                        await asyncio.sleep(0.1)
        except BaseException as e:  # noqa: BLE001 — surface startup failures to caller
            self._error = e
            self._ready.set()

    # ---- sync API used by the agent loop ----
    def list_tools(self) -> list[ToolSpec]:
        return self._tools

    def execute(self, call: PlannedToolCall) -> ActionResult:
        try:
            coro = self._session.call_tool(call.name, call.arguments)
            result = asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=120)
            if getattr(result, "isError", False):
                return ActionResult(
                    call_id=call.id, tool_name=call.name, ok=False,
                    error=str(_parse_call_result(result)),
                )
            return ActionResult(
                call_id=call.id, tool_name=call.name, ok=True,
                result=_parse_call_result(result),
            )
        except Exception as e:  # noqa: BLE001 — a tool error must not crash the loop
            return ActionResult(
                call_id=call.id, tool_name=call.name, ok=False, error=f"{type(e).__name__}: {e}",
            )

    def close(self) -> None:
        self._stop.set()
        try:
            self._serve_future.result(timeout=10)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
