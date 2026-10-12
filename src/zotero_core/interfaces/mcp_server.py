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

WHY THE SERVER SENDS INSTRUCTIONS
---------------------------------
Usage knowledge that spans tools -- which key a reader wants, that a snapshot read can lag,
how to build a deep link -- had nowhere to live in the server, so it lived in client-side
notes that a caller only sees if something loads them. MCP's `instructions` field is the
place a client shows its model once, beside the tool list. Per-tool facts stay in each
tool's description; this text holds only what crosses tools. `--read-only` gets a version
without the write lines, so it never advertises a tool it does not serve.
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

# Each line is one fact a caller needs before choosing a tool. The flag is True for a line
# that only makes sense when the write tools are served, so `--read-only` drops it.
_INSTRUCTION_LINES: tuple[tuple[str, bool], ...] = (
    ("Zotero library over MCP.", False),
    (
        "Reads: get_/search_/find_/list_zotero_*, plus check_zotero_duplicate, "
        "resolve_zotero_pdf and ping_zotero. Catalogue reads come from zotero.sqlite; "
        "window/reader state and citekeys need Zotero desktop running "
        "(ping_zotero checks the bridge plugin).",
        False,
    ),
    (
        "Changes: zotero_* (zotero_resolve_paper, zotero_list_undo and "
        "zotero_write_preflight write nothing). Every write is gated: existence -> "
        "duplicate refusal -> journal -> read-back. For items, trash is the strongest "
        "delete and zotero_restore_items reverses it; zotero_list_undo / zotero_undo "
        "replay the journalled inverse.",
        True,
    ),
    (
        "Writes need Zotero desktop running: zotero_write_preflight reports which "
        "plugin is down.",
        True,
    ),
    (
        "Keys are 8 uppercase alphanumerics. Annotations, reader positions and "
        "zotero://open-pdf take the ATTACHMENT key; resolve_zotero_pdf maps a citekey "
        "or parent key to it.",
        False,
    ),
    (
        "Before adding: check_zotero_duplicate, or zotero_resolve_paper for a URL. "
        "Save papers with zotero_save_paper.",
        True,
    ),
    (
        "read_mode 'immutable=1' means a point-in-time snapshot: items created since "
        "Zotero took the write lock can read as unknown or missing.",
        False,
    ),
    (
        "Links: zotero://select/library/items/<KEY>; "
        "zotero://open-pdf/library/items/<ATTACHMENT_KEY>?page=<N>, where N is the "
        "1-based page index, not the printed page label.",
        False,
    ),
    ("Read through these tools, not zotero.sqlite or the :23119 endpoints.", False),
)


def instructions(*, read_only: bool) -> str:
    """The server-level guidance sent at initialisation. See the module docstring."""
    return "\n".join(
        line for line, needs_writes in _INSTRUCTION_LINES if not (read_only and needs_writes)
    )


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
    await run_stdio(
        SERVER_NAME, tools, call_tool, instructions=instructions(read_only=read_only)
    )


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
