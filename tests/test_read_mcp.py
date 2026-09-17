"""Tests for the read adapter -- the surface that had ZERO coverage before the merge.

`writes/`'s suite reached `read/items.py` only incidentally, through write verbs that
happened to call it for pre/post state. Nothing tested `cli.py`, `read_mcp.py`,
`service.py`, `bridge.py`, `bbt.py` or `annotations.py` at all: the entire agent-facing
read surface was untested, which is part of why five implemented methods stayed
unreachable long enough for a third-party plugin to fill the gap.

Most of these are PROPERTIES OVER `TOOLS` rather than per-tool assertions, mirroring
`test_mcp_server.py`. A property holds for the eleventh tool as well as the first, which
is the point of moving to a table.
"""

from __future__ import annotations

import ast
import email.message
import inspect
import pathlib
import sqlite3

import pytest

from zotero_core.domain.annotation_type import ANNOTATION_TYPE
from zotero_core.infrastructure.http.bridge import ZoteroBridgeError
from zotero_core.infrastructure.sqlite.connect import ZoteroReadError
from zotero_core.interfaces import read_mcp


@pytest.fixture()
def wired(zotero):
    """The throwaway database itself, for tests that add rows to it."""
    return zotero


# --------------------------------------------------------------------------
# properties over TOOLS
# --------------------------------------------------------------------------


def test_every_declared_property_is_a_real_parameter_of_the_verb():
    """The schema cannot promise an argument the verb would reject.

    This is the failure the table exists to prevent: the schema and the dispatch used to
    be two hand-maintained lists with nothing checking they agreed.
    """
    for spec in read_mcp.TOOLS:
        params = set(inspect.signature(spec.verb).parameters)
        undeclared = sorted(set(spec.properties) - params)
        assert not undeclared, f"{spec.name} declares {undeclared}, not accepted by {spec.verb}"


def test_every_required_argument_is_also_declared_as_a_property():
    for spec in read_mcp.TOOLS:
        missing = sorted(set(spec.required) - set(spec.properties))
        assert not missing, f"{spec.name} requires {missing} but does not declare them"


def test_every_required_argument_has_no_default_in_the_verb():
    """A required argument with a default is a contradiction -- one of them is a lie."""
    for spec in read_mcp.TOOLS:
        params = inspect.signature(spec.verb).parameters
        for name in spec.required:
            assert params[name].default is inspect.Parameter.empty, (
                f"{spec.name}.{name} is declared required but defaults to "
                f"{params[name].default!r}"
            )


def test_tool_names_are_unique_and_the_index_agrees_with_the_table():
    names = [spec.name for spec in read_mcp.TOOLS]
    assert len(names) == len(set(names)), "duplicate tool name"
    assert set(read_mcp._BY_NAME) == set(names)
    assert len(read_mcp._BY_NAME) == len(read_mcp.TOOLS)


def test_every_tool_has_a_description():
    for spec in read_mcp.TOOLS:
        assert spec.description.strip(), f"{spec.name} has no description"


def test_no_schema_exposes_an_injection_seam():
    """`store`, `ctx` and friends are for tests and composition, never for a caller."""
    forbidden = {"store", "ctx", "context", "conn", "db", "bridge", "bbt"}
    for spec in read_mcp.TOOLS:
        leaked = forbidden & set(spec.properties)
        assert not leaked, f"{spec.name} exposes {sorted(leaked)}"


def test_schema_shape_is_valid_json_schema_object():
    for spec in read_mcp.TOOLS:
        schema = spec.as_tool_schema()
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        assert isinstance(schema["required"], list)
        assert set(schema["required"]) <= set(schema["properties"])


def test_the_annotation_type_vocabulary_is_discoverable():
    """Types 3-6 used to be unguessable: the description advertised two by example.

    Pinned against `ANNOTATION_TYPE` so adding a type to the store fails here rather
    than silently leaving the schema behind.
    """
    assert set(read_mcp.ANNOTATION_TYPE_NAMES) == set(ANNOTATION_TYPE.values())
    for spec in read_mcp.TOOLS:
        prop = spec.properties.get("annotation_types")
        if prop is not None:
            assert set(prop["items"]["enum"]) == set(ANNOTATION_TYPE.values())


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------


def test_unknown_tool_raises(ctx):
    with pytest.raises(ValueError, match="Unknown tool"):
        read_mcp.call_read("get_zotero_nothing", {}, ctx=ctx)


def test_an_undeclared_argument_is_refused_not_dropped(ctx):
    """The old dispatch read `arguments.get(...)` per branch, so a misspelling vanished
    and the caller silently got the default."""
    with pytest.raises(ValueError, match="does not accept"):
        read_mcp.call_read(
            "get_zotero_item",
            {"item_key": "AAAA1111", "include_annotaions": True},
            ctx=ctx,
        )


def test_a_missing_required_argument_is_refused(ctx):
    with pytest.raises(ValueError, match="requires"):
        read_mcp.call_read("get_zotero_item", {}, ctx=ctx)


def test_an_explicit_null_on_an_optional_argument_falls_back_to_the_verb_default(wired, ctx):
    wired.add("AAAA1111", title="A Paper")
    result = read_mcp.call_read("list_zotero_pdfs", {"limit": None}, ctx=ctx)
    assert result["count"] == 0


# --------------------------------------------------------------------------
# the reads themselves
# --------------------------------------------------------------------------


def test_get_zotero_item_reports_a_non_empty_item_type(wired, ctx):
    """REGRESSION GUARD for the tool this one replaces.

    cookjohn's `get_item_details` returns `itemType: ""` for EVERY item -- verified
    2026-08-19 against a live zotero.sqlite that reported `journalArticle` for the same
    keys. An agent reading an item to decide what to do next got a blank type.
    """
    wired.add("AAAA1111", title="A Paper", item_type="book")
    result = read_mcp.call_read("get_zotero_item", {"item_key": "AAAA1111"}, ctx=ctx)
    assert result["ok"] is True
    assert result["item_type"] == "book"
    assert result["item_type"] != ""


def test_get_zotero_item_returns_fields_creators_and_tags(wired, ctx):
    wired.add(
        "AAAA1111",
        title="A Paper",
        fields={"DOI": "10.1/xyz"},
        tags=["read", "ml"],
        creators=[{"creatorType": "author", "firstName": "Max", "lastName": "Welling"}],
    )
    result = read_mcp.call_read("get_zotero_item", {"item_key": "AAAA1111"}, ctx=ctx)
    assert result["fields"]["DOI"] == "10.1/xyz"
    assert result["tags"] == ["ml", "read"]
    assert result["creators"][0]["lastName"] == "Welling"


def test_get_zotero_item_creators_keep_their_order(wired, ctx):
    """Author order is meaning, not presentation -- first author drives the dedupe tier."""
    wired.add(
        "AAAA1111",
        creators=[
            {"creatorType": "author", "lastName": "First"},
            {"creatorType": "author", "lastName": "Second"},
            {"creatorType": "author", "lastName": "Third"},
        ],
    )
    result = read_mcp.call_read("get_zotero_item", {"item_key": "AAAA1111"}, ctx=ctx)
    assert [c["lastName"] for c in result["creators"]] == ["First", "Second", "Third"]


def test_get_zotero_item_on_a_missing_key_says_so_rather_than_raising(ctx):
    result = read_mcp.call_read("get_zotero_item", {"item_key": "NOPE0000"}, ctx=ctx)
    assert result["ok"] is False
    assert result["error"] == "not_found"


def test_get_zotero_item_reports_trash_state(wired, ctx):
    wired.add("AAAA1111")
    wired.trash("AAAA1111")
    result = read_mcp.call_read("get_zotero_item", {"item_key": "AAAA1111"}, ctx=ctx)
    assert result["trashed"] is True


def test_every_read_carries_the_mode_that_served_it(wired, ctx):
    """The honesty mechanism `items.py` was built around, finally visible from outside.

    Every agent-facing read used to go through `annotations._connect`, which is
    `immutable=1`-only and reports nothing -- so a snapshot and a live read were
    indistinguishable to a caller.
    """
    wired.add("AAAA1111")
    assert read_mcp.call_read("get_zotero_item", {"item_key": "AAAA1111"}, ctx=ctx)["read_mode"]
    assert read_mcp.call_read("list_zotero_pdfs", {}, ctx=ctx)["read_mode"]


def test_check_zotero_duplicate_answers_without_writing(wired, ctx):
    """The verdict used to be reachable only by ATTEMPTING a create and reading the
    refusal, so "check before you add" cost a write attempt."""
    wired.add(
        "AAAA1111",
        title="Bayesian Learning",
        creators=[{"creatorType": "author", "lastName": "Welling"}],
    )
    before = wired.store().item_states(["AAAA1111"])

    ok = read_mcp.call_read("check_zotero_duplicate", {"title": "Something Absent"}, ctx=ctx)
    assert ok["verdict"] == "ok"

    warn = read_mcp.call_read(
        "check_zotero_duplicate",
        {
            "title": "bayesian   learning",  # normalisation: case and whitespace
            "creators": [{"creatorType": "author", "lastName": "Welling"}],
        },
        ctx=ctx,
    )
    assert warn["verdict"] == "warn"

    # nothing moved
    assert wired.store().item_states(["AAAA1111"]).live == before.live


def test_check_zotero_duplicate_blocks_on_doi(wired, ctx):
    wired.add("AAAA1111", fields={"DOI": "10.1/xyz"})
    result = read_mcp.call_read(
        "check_zotero_duplicate", {"doi": "https://doi.org/10.1/xyz"}, ctx=ctx
    )
    assert result["verdict"] == "block"


def test_get_zotero_trash_count(wired, ctx):
    wired.add("AAAA1111")
    wired.add("BBBB2222")
    wired.trash("BBBB2222")
    out = read_mcp.call_read("get_zotero_trash_count", {}, ctx=ctx)
    assert out["count"] == 1
    # ⚠ Was `["trashed"]`, with no `read_mode` in the envelope at all. This read is exactly
    # where the mode matters: under an `immutable=1` snapshot the number can predate a
    # purge that already happened, and a caller comparing it around a write would read that
    # as the write having done nothing.
    assert out["read_mode"] in {"mode=ro", "immutable=1"}


def test_get_zotero_sources_carries_a_read_mode(wired, ctx):
    """It returned a BARE LIST, which cannot carry one — the only read in the package that
    could not say whether it was served live or from a snapshot."""
    out = read_mcp.call_read("get_zotero_sources", {"include_citekeys": False}, ctx=ctx)
    assert out["count"] == len(out["sources"])
    assert out["read_mode"] in {"mode=ro", "immutable=1"}


# --------------------------------------------------------------------------
# error codes
# --------------------------------------------------------------------------


def test_annotation_reads_go_through_the_one_opener_and_record_the_mode(zotero):
    """REGRESSION. `ZoteroAnnotationStore` was the only sqlite reader in the package not
    using `connect.open_readonly` -- it hardcoded `immutable=1` and probed nothing, while
    `read/__init__.py` claimed every read reports the mode that served it. Two real
    consequences: an annotation read could never be a live read even with Zotero closed,
    and a caller could not tell it had been handed a snapshot."""
    from zotero_core.infrastructure.sqlite.annotations import ZoteroAnnotationStore

    store = ZoteroAnnotationStore(zotero.path)
    assert store.last_read_mode == ""
    store.get_sources_with_annotations()
    assert store.last_read_mode in {"mode=ro", "immutable=1"}


def test_the_annotation_error_alias_is_gone():
    """WAS `test_the_annotation_error_name_still_resolves`, asserting the alias held.

    The alias is removed. It was not a harmless name: `_CODES` matched on it and read as
    annotation-scoped while resolving to the class EVERY store raises, so a missing
    database via `collection_tree` came back as `annotation_read_failed`. The test below
    pins the corrected behaviour; this one stops the name coming back.
    """
    from zotero_core.infrastructure.sqlite import annotations

    assert not hasattr(annotations, "ZoteroAnnotationError")
    assert not hasattr(annotations, "ANNOTATION_TYPE")


def test_failures_carry_a_code_a_caller_can_branch_on():
    """A locked database, a missing key and a closed Zotero used to be one string."""
    assert read_mcp.error_code(ZoteroBridgeError("down")) == "bridge_unreachable"
    assert read_mcp.error_code(ZoteroReadError("no db")) == "database_unavailable"
    assert read_mcp.error_code(sqlite3.OperationalError("locked")) == "database_unavailable"
    assert read_mcp.error_code(ValueError("nope")) == "bad_arguments"
    assert read_mcp.error_code(RuntimeError("?")) == "error"


def test_a_non_annotation_read_failure_is_not_labelled_an_annotation_failure(tmp_path):
    """THE REGRESSION. Measured before the fix:

        collection_tree -> ZoteroReadError -> 'annotation_read_failed'
        list_libraries  -> ZoteroReadError -> 'annotation_read_failed'

    Every store calls `open_readonly`, so the one class covers all of them; the alias is
    what made an annotation-specific code look correct at the call site.
    """
    from zotero_core.infrastructure.sqlite.collections import ZoteroCollectionStore
    from zotero_core.infrastructure.sqlite.libraries import list_libraries

    absent = str(tmp_path / "absent.sqlite")
    for call in (lambda: ZoteroCollectionStore(absent).tree(), lambda: list_libraries(absent)):
        with pytest.raises(ZoteroReadError) as caught:
            call()
        assert read_mcp.error_code(caught.value) == "database_unavailable"


# --------------------------------------------------------------------------
# structure
# --------------------------------------------------------------------------


def test_mcp_is_imported_only_inside_a_function():
    """AST, not grep. A module-scope `import mcp` would force every consumer of a read
    verb to install an async runtime, and would break the `zotero-core` CLI on a machine
    that never opted into the extra."""
    source = pathlib.Path(read_mcp.__file__).read_text()
    tree = ast.parse(source)
    for node in tree.body:  # module scope only
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("mcp"), "module-scope `import mcp`"
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("mcp"), "module-scope `from mcp import`"


def test_the_adapter_imports_without_the_mcp_extra():
    """Importing the module must not require the extra; only `main()` does."""
    import importlib

    importlib.reload(read_mcp)
    assert read_mcp.TOOLS


def test_the_context_is_configurable_by_environment(monkeypatch, tmp_path):
    """MCP used to hard-wire ~/Zotero/zotero.sqlite -- the CLI had --db, MCP had nothing,
    so it could not be pointed at a copy or a fixture."""
    monkeypatch.setenv("ZOTERO_CORE_DB", str(tmp_path / "elsewhere.sqlite"))
    ctx = read_mcp._context_from_env()
    assert str(tmp_path / "elsewhere.sqlite") in str(ctx.items.db_path)


def test_both_adapters_share_one_tool_declaration_and_one_dispatch():
    """`_ToolSpec` and the dispatch existed TWICE, byte-identical apart from comments.

    Both adapters exist because of the `TOOLS` table -- each replaced a three-place-per-tool
    change with one entry. Declaring the table's row type twice, and its dispatch twice,
    reintroduced exactly that duplication one level up.
    """
    from zotero_core.interfaces import read_mcp, tool_spec, write_mcp

    # one row type, with the write side adding only `transport`
    assert issubclass(tool_spec.WriteToolSpec, tool_spec.ToolSpec)
    assert all(isinstance(s, tool_spec.ToolSpec) for s in read_mcp.TOOLS)
    assert all(isinstance(s, tool_spec.WriteToolSpec) for s in write_mcp.TOOLS)

    # a read tool must NOT claim a transport it does not have
    assert not any(hasattr(s, "transport") for s in read_mcp.TOOLS)

    # and one dispatch, reached from both
    import inspect

    for module in (read_mcp, write_mcp):
        src = inspect.getsource(module)
        assert "_dispatch(" in src, f"{module.__name__} hand-rolls its dispatch again"


# --------------------------------------------------------------------------
# sources with annotations -- the verb that used to hide 5 of 18
# --------------------------------------------------------------------------


def test_an_annotated_html_snapshot_is_a_source(zotero, ctx):
    """⚠ FOUR annotated snapshots were invisible in the live library, carrying 77
    annotations between them — one of them the second half of a duplicated paper.

    The query filtered `att.contentType = 'application/pdf'`. Zotero annotates snapshots and
    EPUBs too, so the filter did not narrow the answer, it falsified it: the verb an agent
    reaches for to audit annotation coverage reported a clean library.
    """
    zotero.add("PARENT01", "A Paper With A Snapshot")
    zotero.add("SNAP0001", "snapshot.html", "attachment",
               parent="PARENT01", content_type="text/html")
    zotero.add("ANNO0001", "", "annotation", parent="SNAP0001")

    out = ctx.get_sources_with_annotations(include_citekeys=False)
    keys = {s.attachment_key for s in out["sources"]}
    assert "SNAP0001" in keys, "an annotated HTML snapshot is still being dropped"
    snap = next(s for s in out["sources"] if s.attachment_key == "SNAP0001")
    assert snap.content_type == "text/html"      # carried, not filtered on
    assert snap.parent_key == "PARENT01"


def test_a_standalone_annotated_attachment_is_its_own_source(zotero, ctx):
    """The other dropped shape: the query joined THROUGH a parent, so an attachment filed
    directly in the library had no row at all.

    A standalone attachment is its own source rather than no source, so it reports its own
    key — stated here because `ZoteroSource.parent_key` is a `str` and the alternative
    (empty) would read as "parent unknown" rather than "there is no parent".
    """
    zotero.add("LOOSE001", "loose.pdf", "attachment")     # no parent=
    zotero.add("ANNO0002", "", "annotation", parent="LOOSE001")

    out = ctx.get_sources_with_annotations(include_citekeys=False)
    loose = next(s for s in out["sources"] if s.attachment_key == "LOOSE001")
    assert loose.parent_key == "LOOSE001"
    assert loose.annotation_count == 1


def test_the_sources_verb_agrees_with_the_annotation_search(zotero, ctx):
    """THE PROPERTY, rather than the two shapes. Both read the same table, so any attachment
    the search can find must be reachable as a source — that equality is what broke, and it
    broke quietly because each verb was self-consistent."""
    zotero.add("PARENT02", "Another Paper")
    zotero.add("PDF00002", "paper.pdf", "attachment", parent="PARENT02")
    zotero.add("ANNO0003", "", "annotation", parent="PDF00002")
    zotero.add("SNAP0002", "snap.html", "attachment",
               parent="PARENT02", content_type="text/html")
    zotero.add("ANNO0004", "", "annotation", parent="SNAP0002")

    hits, _ = ctx.search.annotations("", limit=10**9)
    searchable = {h.attachment_key for h in hits}
    reported = {s.attachment_key for s in ctx.get_sources_with_annotations(
        include_citekeys=False)["sources"]}
    assert searchable == reported


# --------------------------------------------------------------------------
# trash -- the one unscoped read, and the first timestamp the package reads
# --------------------------------------------------------------------------


def test_the_trash_count_is_scoped_to_one_library(zotero, ctx):
    """⚠ THE ONE UNSCOPED READ IN THE PACKAGE. `SELECT COUNT(*) FROM deletedItems` carried no
    `libraryID`, while `items.py`'s own docstring says "Scoped to `library_id` like every
    other read here".

    On the live library this was not academic: the unscoped count answered 25 while the user
    library's trash is EMPTY — all 25 sit in three group libraries. Any comparison against a
    sibling read was comparing two different populations.
    """
    zotero.add("MINE0001", "mine", trashed=True, library_id=1)
    zotero.add("THEIR001", "theirs", trashed=True, library_id=2)

    from zotero_core.infrastructure.sqlite.items import ZoteroItemStore

    mine, _ = ZoteroItemStore(zotero.path, library_id=1).trashed_count()
    theirs, _ = ZoteroItemStore(zotero.path, library_id=2).trashed_count()
    assert (mine, theirs) == (1, 1), "the count is leaking across libraries"


def test_trashed_items_can_be_named_and_dated(zotero, ctx):
    """Before this, 22 of 25 trashed items could not be NAMED at all -- they are excluded
    from `search.items` by SQL and belong to no collection, so no public read reached them --
    and none could be dated, because nothing in the package read a timestamp."""
    zotero.add("OLD00001", "An Old Mistake", trashed=True, deleted_at="2024-03-12 23:10:11")
    zotero.add("NEW00001", "A Recent One", trashed=True, deleted_at="2026-08-20 09:00:00")
    zotero.add("ALIVE001", "Still Here")

    out = ctx.trash_items()
    by_key = {i["key"]: i for i in out["items"]}
    assert set(by_key) == {"OLD00001", "NEW00001"}, "a live item leaked into the trash listing"
    assert by_key["OLD00001"]["date_deleted"].startswith("2024")
    assert by_key["OLD00001"]["title"] == "An Old Mistake"
    assert out["count"] == 2
    assert out["read_mode"] in {"mode=ro", "immutable=1"}
    # newest first, so "what did I just delete" is the first row rather than the last
    assert [i["key"] for i in out["items"]] == ["NEW00001", "OLD00001"]


# --------------------------------------------------------------------------
# tags -- the vocabulary had no reader at all
# --------------------------------------------------------------------------


def test_the_tag_vocabulary_is_readable(zotero, ctx):
    """`item_tags` answers PER ITEM, so "which tags exist" meant walking every item or
    writing SQL outside the package. That gap is why 35 case-colliding tag groups sat
    unnoticed in the live library."""
    zotero.add("AAAA1111", "One", tags=["art", "shared"])
    zotero.add("BBBB2222", "Two", tags=["shared"])

    out = ctx.tags()
    counts = {t["name"]: t["item_count"] for t in out["tags"]}
    assert counts["shared"] == 2
    assert counts["art"] == 1
    assert out["count"] == len(out["tags"])
    assert out["read_mode"] in {"mode=ro", "immutable=1"}


def test_tag_lookup_is_case_sensitive(zotero, ctx):
    """THE PROPERTY A MERGE DEPENDS ON. `art` and `Art` are separate rows in `tags`; a
    case-insensitive lookup would make them indistinguishable and the merge untargetable."""
    zotero.add("AAAA1111", "One", tags=["art"])
    zotero.add("BBBB2222", "Two", tags=["Art"])

    assert ctx.items_with_tag("art")["item_keys"] == ["AAAA1111"]
    assert ctx.items_with_tag("Art")["item_keys"] == ["BBBB2222"]
    assert ctx.items_with_tag("ART")["count"] == 0


def test_tags_are_scoped_to_one_library(zotero, ctx):
    """Scoped like every other read. An unscoped version would mix a group library's
    vocabulary into the personal one -- the exact shape of error tag hygiene looks for."""
    zotero.add("MINE0001", "mine", tags=["local"], library_id=1)
    zotero.add("THEIR001", "theirs", tags=["remote"], library_id=2)

    names = {t["name"] for t in ctx.tags()["tags"]}
    assert "local" in names
    assert "remote" not in names, "tag vocabulary is leaking across libraries"


def test_a_bridge_that_ERRORS_is_not_reported_as_unreachable(monkeypatch):
    """⚠ An HTTPError means the bridge ANSWERED. It was reported as "not reachable at
    {url}", which sends you to check whether Zotero is running when the plugin has already
    said what went wrong — and the plugin's `{"ok": false, "error": ...}` body was read by
    nobody. `transports/linker.py` gets this right and its docstring explains why."""
    import io
    import urllib.error

    from zotero_core.infrastructure.http.bridge import ZoteroBridgeClient, ZoteroBridgeError

    body = io.BytesIO(b'{"ok": false, "error": "no browser window is open"}')

    def _raise(*_a, **_k):
        raise urllib.error.HTTPError(
            "http://x/window-state", 500, "Server Error", email.message.Message(), body
        )

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    with pytest.raises(ZoteroBridgeError) as e:
        ZoteroBridgeClient().get_window_state_raw()

    assert "no browser window is open" in str(e.value), "the plugin's reason was discarded"
    assert "not reachable" not in str(e.value), "an answering bridge called unreachable"
