"""PARITY WITH COOKJOHN -- the gate for unregistering its MCP server.

cookjohn's `zotero-plugin` MCP exposes 24 tools to an agent. Nine of them are WRITES that
duplicate the gated ones in `zotero_core.application`, with no precondition, no journal and no
read-back; those are the ones this suite exists to replace. The other fifteen are READS,
and for a long time they were the ONLY answer to "what is in collection X", "what type is
this item" and "search my library" -- which is why the server stayed registered long after
its write half was superseded.

This file asserts that every one of those fifteen now has a home here, or is on a short
explicit list of things deliberately not carried over. It exists so that unregistering
cookjohn is a checked step rather than a hopeful one: without it, the read gap reappears
silently and nobody notices until an agent cannot answer a question it used to answer.

⚠ The plugin is NOT uninstalled by any of this. `zotero_core.application` uses it as a write
TRANSPORT over :23121, and `importers/calibre2zotero` and `calibre-zotero-jump` open
their own connections to the same port. Unregistering removes 24 tools from an agent's
tool list; it changes nothing about the transport.
"""

from __future__ import annotations

from zotero_core.interfaces import read_mcp

# Every read cookjohn exposes -> the tool here that answers the same question.
# Several map to the same tool because cookjohn splits by call shape where this package
# splits by question: `get_subcollections` and `get_collection_details` are both answered
# by the one nested tree, and `get_item_abstract` is one field of a full item read.
COOKJOHN_READS = {
    "get_annotations": "get_zotero_annotations",
    "search_annotations": "search_zotero_annotations",
    "search_fulltext": "search_zotero_fulltext",
    "get_content": "get_zotero_attachment_text",
    "get_item_details": "get_zotero_item",
    "get_item_abstract": "get_zotero_item",
    "get_collections": "get_zotero_collections",
    "get_subcollections": "get_zotero_collections",
    "get_collection_details": "get_zotero_collections",
    "get_collection_items": "get_zotero_collection_items",
    "search_collections": "find_zotero_collections",
    "search_library": "search_zotero_items",
    "get_libraries": "list_zotero_libraries",
    "search_libraries": "list_zotero_libraries",
}

# Deliberately NOT carried over. One entry, and it needs a reason, not a shrug.
DELIBERATELY_DROPPED = {
    "fulltext_database": (
        "Index statistics (how many pages/chars Zotero has indexed). Not a question about "
        "the library, and the useful version of it -- 'is this paper searchable' -- is "
        "answered by get_zotero_attachment_text returning not_indexed."
    ),
}

# cookjohn's writes. All nine are superseded by GATED equivalents in zotero_core.application,
# which additionally has trash/restore and linked attachments -- cookjohn exposes no
# equivalent for those at all.
COOKJOHN_WRITES = {
    "write_item": "zotero_create_item",
    "write_metadata": "zotero_update_metadata",
    "write_note": "zotero_write_note",
    "write_tag": "zotero_add_tags",
    "create_collection": "zotero_create_collection",
    "update_collection": "zotero_update_collection",
    "delete_collection": "zotero_delete_collection",
    "add_items_to_collection": "zotero_add_items_to_collection",
    "remove_items_from_collection": "zotero_remove_items_from_collection",
}


def _read_tool_names() -> set[str]:
    return {spec.name for spec in read_mcp.TOOLS}


def test_every_cookjohn_read_has_an_equivalent_here():
    """THE GATE. If this fails, do not unregister zotero-plugin."""
    available = _read_tool_names()
    missing = {cookjohn: mine for cookjohn, mine in COOKJOHN_READS.items() if mine not in available}
    assert not missing, f"no equivalent for cookjohn reads: {missing}"


def test_the_two_sets_account_for_every_cookjohn_read():
    """No read may be quietly forgotten -- it is either mapped or explicitly dropped."""
    overlap = set(COOKJOHN_READS) & set(DELIBERATELY_DROPPED)
    assert not overlap, f"listed as both mapped and dropped: {sorted(overlap)}"
    assert len(COOKJOHN_READS) + len(DELIBERATELY_DROPPED) == 15


def test_every_deliberate_drop_carries_a_reason():
    for tool, reason in DELIBERATELY_DROPPED.items():
        assert len(reason) > 40, f"{tool} is dropped without a real reason"


def test_every_cookjohn_write_has_a_gated_equivalent():
    """Imported lazily: the write adapter needs the `mcp` extra, the read one does not."""
    from zotero_core.interfaces import write_mcp

    gated = {spec.name for spec in write_mcp.TOOLS}
    missing = {c: mine for c, mine in COOKJOHN_WRITES.items() if mine not in gated}
    assert not missing, f"ungated cookjohn writes with no gated equivalent: {missing}"


def test_the_gated_surface_does_things_cookjohn_cannot():
    """The replacement is not merely equal -- trash/restore and linked attachments have
    no cookjohn equivalent at all, which is why 'just use cookjohn' was never an option."""
    from zotero_core.interfaces import write_mcp

    gated = {spec.name for spec in write_mcp.TOOLS}
    for extra in ("zotero_trash_items", "zotero_restore_items", "zotero_link_attachment"):
        assert extra in gated
        assert extra not in COOKJOHN_WRITES.values()


def test_the_replacement_for_get_item_details_reports_a_real_item_type(zotero):
    """Parity here means BETTER, not merely equal.

    cookjohn's `get_item_details` returns `itemType: ""` for every item -- verified
    2026-08-19 against a live zotero.sqlite that reported `journalArticle` for the same
    keys. An agent reading an item to decide what to do next got a blank type.
    """
    from zotero_core.interfaces.factory import build_context

    ctx = build_context(zotero_db_path=zotero.path)
    zotero.add("AAAA1111", title="A Paper", item_type="conferencePaper")
    result = read_mcp.call_read("get_zotero_item", {"item_key": "AAAA1111"}, ctx=ctx)
    assert result["item_type"] == "conferencePaper"


def test_search_parity_is_an_improvement_not_a_regression(zotero):
    """`search_library` is cookjohn's substring search. The replacement is fuzzy, so it
    answers a query cookjohn returns nothing for."""
    from zotero_core.interfaces.factory import build_context

    ctx = build_context(zotero_db_path=zotero.path)
    zotero.add("AAAA1111", title="Bayesian Learning via Stochastic Gradient Langevin Dynamics")
    result = read_mcp.call_read("search_zotero_items", {"query": "Langevan Dynmaics"}, ctx=ctx)
    assert result["count"] == 1


def test_group_libraries_are_reachable_rather_than_scoped_away(zotero):
    """`get_libraries`/`search_libraries` are cookjohn's group-library reads, and this
    package scopes to the user library by default. That default would be a capability
    LOSS if there were no way past it -- there are 109 items in four non-empty groups on
    this machine."""
    import sqlite3

    from zotero_core.interfaces.factory import build_context

    ctx = build_context(zotero_db_path=zotero.path)
    con = sqlite3.connect(zotero.path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO libraries (libraryID, type, editable, filesEditable)"
            " VALUES (9, 'group', 1, 1)"
        )
        con.execute("INSERT INTO groups (groupID, libraryID, name) VALUES (99, 9, 'D7 resources')")
        con.execute(
            "INSERT INTO collections (collectionID, collectionName, libraryID, key)"
            " VALUES (9500, 'Group Work', 9, 'GRPCOLL1')"
        )
        con.commit()
    finally:
        con.close()

    result = read_mcp.call_read("list_zotero_libraries", {}, ctx=ctx)
    listed = {lib["library_id"] for lib in result["libraries"]}
    assert 9 in listed

    tree = read_mcp.call_read("get_zotero_collections", {"library_id": 9}, ctx=ctx)
    assert [c["name"] for c in tree["collections"]] == ["Group Work"]
