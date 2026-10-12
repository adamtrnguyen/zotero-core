"""The server-level `instructions` and the cross-tool facts the descriptions carry.

WHY THESE EXIST
---------------
Usage knowledge that spans tools used to live in client-side notes, which a caller sees only
if something loads them. It now ships with the server: `instructions` at initialisation, and
the per-tool facts in each description. Both are prose, and prose rots without a gate, so:

  * the instructions are set, and say the things a caller gets wrong without them
  * they reach the client -- `run_stdio` builds `InitializationOptions` by hand, so setting
    them on `Server` alone would send nothing
  * every tool they name is a tool the server serves, in that mode
  * the attachment-key footer is on every tool whose answer is keyed by an attachment
"""

from __future__ import annotations

import asyncio
import contextlib
import re

import pytest

from zotero_core.interfaces import mcp_server, read_mcp

KEY_PHRASES = (
    "8 uppercase alphanumerics",
    "ATTACHMENT key",
    "resolve_zotero_pdf",
    "immutable=1",
    "zotero://select/library/items/",
    "zotero://open-pdf/library/items/",
    "not the printed page label",
    "not zotero.sqlite",
)
WRITE_PHRASES = ("check_zotero_duplicate", "zotero_save_paper", "zotero_restore_items")

_TOOL_NAME = re.compile(
    r"\b(?:zotero_\w+|(?:get|search|find|list)_zotero_\w+|\w+_zotero(?:_\w+)?)\b"
)


def _named_tools(text: str) -> set[str]:
    # `get_/search_/find_/list_zotero_*` is a prefix pattern, not a tool name.
    return {name for name in _TOOL_NAME.findall(text) if not name.endswith("_")}


@pytest.mark.parametrize("read_only", [False, True])
def test_instructions_are_set_and_carry_the_key_facts(read_only):
    text = mcp_server.instructions(read_only=read_only)
    assert text.strip()
    for phrase in KEY_PHRASES:
        assert phrase in text, f"instructions lost {phrase!r}"


def test_the_write_lines_appear_only_when_writes_are_served():
    full = mcp_server.instructions(read_only=False)
    read_only = mcp_server.instructions(read_only=True)
    for phrase in WRITE_PHRASES:
        assert phrase in full
    assert "zotero_save_paper" not in read_only
    assert "zotero_write_preflight" not in read_only


@pytest.mark.parametrize("read_only", [False, True])
def test_every_tool_the_instructions_name_is_served(read_only):
    """A renamed tool would otherwise leave the instructions pointing at nothing."""
    served = {spec.name for spec in mcp_server.build(read_only=read_only)[0]}
    named = _named_tools(mcp_server.instructions(read_only=read_only))
    assert named, "the tool-name pattern matched nothing; the check would be vacuous"
    assert named <= served, f"instructions name unserved tools: {sorted(named - served)}"


def test_run_stdio_hands_the_instructions_to_the_client(monkeypatch):
    """The hand-built `InitializationOptions` are what the client receives."""
    pytest.importorskip("mcp")
    import mcp.server.stdio as stdio
    from mcp.server import Server

    captured: dict[str, object] = {}

    @contextlib.asynccontextmanager
    async def fake_stdio_server():
        yield (None, None)

    async def fake_run(self, read_stream, write_stream, initialization_options, *a, **k):
        captured["server"] = self.instructions
        captured["client"] = initialization_options.instructions

    monkeypatch.setattr(stdio, "stdio_server", fake_stdio_server)
    monkeypatch.setattr(Server, "run", fake_run)

    asyncio.run(mcp_server.main(read_only=False))

    expected = mcp_server.instructions(read_only=False)
    assert captured == {"server": expected, "client": expected}


ATTACHMENT_KEYED = (
    "get_zotero_active_reader",
    "get_zotero_open_readers",
    "get_zotero_annotations",
    "search_zotero_annotations",
)


@pytest.mark.parametrize("name", ATTACHMENT_KEYED)
def test_attachment_keyed_tools_carry_the_footer(name):
    spec = next(s for s in read_mcp.TOOLS if s.name == name)
    assert "ATTACHMENT key" in spec.description
    assert "resolve_zotero_pdf" in spec.description


@pytest.mark.parametrize("name", ["get_zotero_annotations", "search_zotero_annotations"])
def test_annotation_tools_forbid_inventing_a_colour_meaning(name):
    spec = next(s for s in read_mcp.TOOLS if s.name == name)
    assert "never invent" in spec.description
    assert "verbatim" in spec.description
