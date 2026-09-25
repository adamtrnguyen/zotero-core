"""The ONE registered Zotero MCP server: reads and gated writes.

WHY ONE SERVER
--------------
The package merged on 2026-08-19, but it kept two entry points (`zotero-core-read-mcp`,
`zotero-core-write-mcp`), and ZotLink stayed a third. An agent therefore saw three Zotero
servers -- `zotero-context`, `zotero-writes`, `zotlink` -- for one library. `pyproject.toml`
already named the end state: a single server with a `--read-only` switch, "once the read
surface is complete". It is complete; this is that server.

Nothing about the gates moves. A process boundary was never what protected a write: every
write verb runs its own preflight, duplicate refusal, journal and read-back, and agent
permissions are granted per TOOL, not per server. This module only concatenates the three
tool tables and routes each call to the adapter that owns it, so each keeps its own error
envelope -- the read side's `{ok, code, error}`, the write side's `WriteBlocked` detail.

`--read-only` serves the read table alone. It is the mode for a context that must not write,
which is the one job the separate read server used to do.

`read_mcp` and `write_mcp` stay as modules: they own their tables and envelopes. Their own
entry points are gone, so there is exactly one way to start a Zotero server.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from typing import Any

from zotero_core.interfaces import read_mcp, write_mcp
from zotero_core.interfaces.mcp_runtime import CallTool, run_stdio
from zotero_core.interfaces.tool_spec import ToolSpec

SERVER_NAME = "zotero"


def surfaces(*, read_only: bool) -> list[tuple[Sequence[ToolSpec], CallTool]]:
    """Each tool table with the adapter that renders its calls, in listing order."""
    tables: list[tuple[Sequence[ToolSpec], CallTool]] = [(read_mcp.TOOLS, read_mcp.render_call)]
    if not read_only:
        tables.append((write_mcp.TOOLS, write_mcp.render_call))
    return tables


def build(*, read_only: bool) -> tuple[list[ToolSpec], CallTool]:
    """The flat tool list and one router over it. Refuses a name served twice."""
    tools: list[ToolSpec] = []
    route: dict[str, CallTool] = {}
    for table, render in surfaces(read_only=read_only):
        for spec in table:
            if spec.name in route:
                raise ValueError(f"tool {spec.name!r} is declared by two adapters")
            route[spec.name] = render
            tools.append(spec)

    def call_tool(name: str, arguments: dict[str, Any]) -> str:
        render = route.get(name)
        if render is None:
            # Same wording the per-adapter dispatch uses, so a caller sees one message.
            return read_mcp.render_call(name, arguments)
        return render(name, arguments)

    return tools, call_tool


async def main(*, read_only: bool = False) -> None:
    tools, call_tool = build(read_only=read_only)
    await run_stdio(SERVER_NAME, tools, call_tool)


def run() -> None:
    parser = argparse.ArgumentParser(
        prog="zotero-core-mcp", description="The Zotero MCP server: reads and gated writes."
    )
    parser.add_argument(
        "--read-only", action="store_true", help="serve the read tools only (no writes)"
    )
    args = parser.parse_args()
    try:
        asyncio.run(main(read_only=args.read_only))
    except KeyboardInterrupt:
        pass
