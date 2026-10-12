"""The stdio MCP server both adapters run. ONE implementation.

WHAT WAS DUPLICATED, AND WHY IT SURVIVED THE FIRST DEDUP
--------------------------------------------------------
`interfaces/tool_spec.py` exists because the per-tool declaration and dispatch were written
twice; its docstring makes exactly that argument. The BOOTSTRAP around them was left behind
and stayed duplicated: `read_mcp.main()` and `write_mcp.main()` were ~54 lines each, of which
roughly half was identical line for line — the `mcp` import guard, `Server(SERVER_NAME)`, the
entire `@server.list_tools()` handler, and the `stdio_server()` + `InitializationOptions`
block.

The genuinely different part is what happens to ONE tool call, which is why that is the
parameter: the read adapter turns any exception into `{ok, code, error}`, while the write
adapter has a `WriteBlocked` branch whose `detail` is the whole point of the adapter existing.
Passing a callback keeps that difference where it belongs and removes the rest.

WHY `mcp` IS IMPORTED IN THE FUNCTION BODY
------------------------------------------
`dependencies = []`. Both adapters must be IMPORTABLE without the `mcp` extra, so `TOOLS` can
be inspected and the entry point can reach its own error message. Two AST tests assert the
adapters have no module-scope `mcp` import; this module is subject to the same rule and keeps
the import inside `run_stdio` for the same reason.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from zotero_core.interfaces.tool_spec import ToolSpec

#: What an adapter does with one tool call: take the name and arguments, return the text to
#: send back. Returning TEXT rather than an `mcp` type is deliberate -- an adapter that built
#: `types.TextContent` itself would need `mcp` imported at ITS module scope, which is the one
#: thing the extra-free import has to preserve.
CallTool = Callable[[str, dict[str, Any]], str]


async def run_stdio(
    server_name: str,
    tools: Sequence[ToolSpec],
    call_tool: CallTool,
    *,
    instructions: str | None = None,
) -> None:
    """Serve `tools` over stdio until the client disconnects.

    `instructions` is the server-level guidance a client shows its model once, beside the
    tool list. It is passed TWICE on purpose: `Server(...)` stores it, but the
    `InitializationOptions` below are built by hand rather than by
    `server.create_initialization_options()`, and the hand-built ones are what the client
    actually receives. Passing it to `Server` alone would send nothing.
    """
    try:
        import mcp.types as types
        from mcp.server import NotificationOptions, Server
        from mcp.server.models import InitializationOptions
        from mcp.server.stdio import stdio_server
    except ImportError as exc:
        raise SystemExit(
            "The MCP adapter requires the optional dependency: uv sync --extra mcp"
        ) from exc

    from zotero_core import __version__

    server = Server(server_name, instructions=instructions)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=spec.name,
                description=spec.description,
                inputSchema=spec.as_tool_schema(),
            )
            for spec in tools
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        return [types.TextContent(type="text", text=call_tool(name, arguments or {}))]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=server_name,
                # Was hardcoded "0.1.0" in both adapters and would have stayed there through
                # every release; it is the package version now.
                server_version=__version__,
                instructions=instructions,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )
