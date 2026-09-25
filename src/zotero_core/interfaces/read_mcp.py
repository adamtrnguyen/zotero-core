"""The read tools.

ONE TABLE, NOT TWO LISTS
------------------------
Tools are declared once, in `TOOLS`, and both the schema and the dispatch derive from
it. This adapter used to be a 3-place change per tool -- a `types.Tool(...)` literal
inside `list_tools()`, an `if name == ...` branch in `call_core()`, and hand-written
`arguments.get()` coercion inside that branch -- with nothing checking that the two
lists agreed about names, and no rejection of undeclared arguments: a misspelled
`include_annotaions` was silently dropped and the caller got the default.

The sibling write adapter solved this first and said why: "two copies of one fact
is the defect this package was built to end." The read adapter had 6 tools and never got
the refactor, which is also why it grew none.

WHAT IS NEW HERE (2026-08-19)
-----------------------------
Tools that were implemented and unreachable. `read/items.py` and
`read/duplicates.py` had SEVEN public methods between them and not one was exposed from
CLI or MCP, so an agent could not read an item's type, fields, creators or tags, and
could not ask "is this already in the library" without attempting a create and parsing
the refusal.

Collection reads landed next (`read/collections.py`), including the inverse -- which
collections an item is in -- that nothing answered before.

Search followed (`read/search.py`), including FUZZY metadata matching, which nothing in
the stack had: Zotero's own search is substring-only and returns zero for a query with
one typo.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from typing import Any

from zotero_core.application.services.context import ZoteroContext
from zotero_core.domain.annotation_type import ANNOTATION_TYPE_NAMES
from zotero_core.infrastructure.http.bridge import ZoteroBridgeError
from zotero_core.infrastructure.sqlite.connect import ZoteroReadError
from zotero_core.interfaces.arguments import parse_types
from zotero_core.interfaces.factory import build_context
from zotero_core.interfaces.mcp_runtime import run_stdio
from zotero_core.interfaces.rendering import render_json
from zotero_core.interfaces.tool_spec import ToolSpec as _ToolSpec
from zotero_core.interfaces.tool_spec import dispatch as _dispatch

SERVER_NAME = "zotero-context"

# Derived from the domain enum, not transcribed. This was a hand-maintained tuple whose
# own comment asked to be kept in step by hand -- a promise no comment can keep.

def _context_from_env() -> ZoteroContext:
    """The default context for a server nobody handed one to.

    ⚠ THIS REPLACES A MODULE-GLOBAL SINGLETON. `_CTX` was built lazily on first use and
    cached forever, which made it unreachable from a caller: the only way to point this
    adapter anywhere was an environment variable, and the only way for a TEST to substitute
    one was `monkeypatch.setattr(read_mcp, "_CTX", ...)` -- five sites did exactly that.

    Building per call is not the waste it looks: a context holds paths, URLs and ints and
    opens no connection, so there is nothing to cache. Every store opens its own connection
    per query and always did.

    The environment variables stay, because they are how the MCP registration points this at
    a copy of the database, and they are read HERE rather than inside the facade -- reading
    the environment is a deployment concern, which is what this layer is for.
    """
    kwargs: dict[str, Any] = {}
    if db := os.environ.get("ZOTERO_CORE_DB"):
        kwargs["zotero_db_path"] = db
    if url := os.environ.get("ZOTERO_CORE_BRIDGE_URL"):
        kwargs["bridge_url"] = url
    if url := os.environ.get("ZOTERO_CORE_BBT_URL"):
        kwargs["bbt_rpc_url"] = url
    return build_context(**kwargs)


# --------------------------------------------------------------------------
# verbs -- plain functions, keyword arguments only, no envelope
# --------------------------------------------------------------------------


def get_zotero_window_state(*, ctx: ZoteroContext) -> Any:
    return ctx.get_window_state()


def get_zotero_active_reader(
    include_annotations: bool = False,
    annotation_types: Any = None,
    include_citekeys: bool = True,
    *,
    ctx: ZoteroContext,
) -> Any:
    return ctx.get_active_reader_context(
        include_annotations=include_annotations,
        include_citekeys=include_citekeys,
        annotation_types=parse_types(annotation_types),
    )


def get_zotero_open_readers(
    include_annotations: bool = False,
    annotation_types: Any = None,
    include_citekeys: bool = True,
    *,
    ctx: ZoteroContext,
) -> Any:
    return ctx.get_open_reader_context(
        include_annotations=include_annotations,
        include_citekeys=include_citekeys,
        annotation_types=parse_types(annotation_types),
    )


def get_zotero_annotations(
    attachment_key: str,
    annotation_types: Any = None,
    include_text: bool = True,
    include_comments: bool = True,
    *,
    ctx: ZoteroContext,
) -> Any:
    return ctx.get_annotations(
        attachment_key,
        types=parse_types(annotation_types),
        include_text=include_text,
        include_comments=include_comments,
    )


def resolve_zotero_pdf(
    identifier: str,
    is_attachment_key: bool = False,
    *,
    ctx: ZoteroContext,
) -> dict:
    parent_key, attachment_key = ctx.resolve_pdf_attachment_key(
        identifier, is_attachment_key=is_attachment_key
    )
    return {"parent_key": parent_key, "attachment_key": attachment_key}


def get_zotero_sources(include_citekeys: bool = True, *, ctx: ZoteroContext) -> Any:
    # `include_citekeys` was never passed by the old dispatch, so MCP always paid the
    # Better BibTeX round trip even when the caller did not want citekeys -- and BBT
    # being down is indistinguishable from no result, because its client swallows every
    # network error and returns None.
    return ctx.get_sources_with_annotations(include_citekeys=include_citekeys)


def get_zotero_item(item_key: str, *, ctx: ZoteroContext) -> dict:
    return ctx.get_item(item_key)


def check_zotero_duplicate(
    title: str | None = None,
    doi: str | None = None,
    isbn: str | None = None,
    calibre_uuid: str | None = None,
    creators: Any = None,
    *,
    ctx: ZoteroContext,
) -> dict:
    return ctx.check_duplicate(
        title=title,
        doi=doi,
        isbn=isbn,
        calibre_uuid=calibre_uuid,
        creators=tuple(creators or ()),
    )


def list_zotero_pdfs(limit: int | None = None, *, ctx: ZoteroContext) -> dict:
    return ctx.list_pdfs(limit=limit)


def get_zotero_trash_count(*, ctx: ZoteroContext) -> dict:
    return ctx.trash_count()


def get_zotero_trash_items(*, ctx: ZoteroContext) -> dict:
    return ctx.trash_items()


def get_zotero_tags(*, ctx: ZoteroContext) -> dict:
    return ctx.tags()


def get_zotero_items_with_tag(name: str, *, ctx: ZoteroContext) -> dict:
    return ctx.items_with_tag(name)


def ping_zotero(*, ctx: ZoteroContext) -> dict:
    # CLI-only until now, so an agent could not ask "is the bridge up?" -- it could only
    # infer it from the shape of a failure.
    return ctx.ping()


def list_zotero_libraries(*, ctx: ZoteroContext) -> dict:
    return ctx.list_libraries()


def get_zotero_collections(library_id: int | None = None, *, ctx: ZoteroContext) -> dict:
    return ctx.collection_tree(library_id=library_id)


def get_zotero_collection_items(
    collection_key: str,
    include_trashed: bool = False,
    library_id: int | None = None,
    *,
    ctx: ZoteroContext,
) -> dict:
    return ctx.collection_items(
        collection_key, include_trashed=include_trashed, library_id=library_id
    )


def get_zotero_item_collections(
    item_keys: Any,
    library_id: int | None = None,
    *,
    ctx: ZoteroContext,
) -> dict:
    return ctx.item_collections(list(item_keys or ()), library_id=library_id)


def find_zotero_collections(
    name: str,
    library_id: int | None = None,
    *,
    ctx: ZoteroContext,
) -> dict:
    return ctx.find_collections(name, library_id=library_id)


def search_zotero_items(
    query: str,
    fuzzy: bool = True,
    limit: int = 25,
    item_type: str | None = None,
    library_id: int | None = None,
    *,
    ctx: ZoteroContext,
) -> dict:
    return ctx.search_items(
        query, fuzzy=fuzzy, limit=limit, item_type=item_type, library_id=library_id
    )


def search_zotero_annotations(
    query: str = "",
    color: str | None = None,
    annotation_types: Any = None,
    limit: int = 25,
    *,
    ctx: ZoteroContext,
) -> dict:
    return ctx.search_annotations(
        query, color=color, annotation_types=parse_types(annotation_types), limit=limit
    )


def search_zotero_fulltext(query: str, limit: int = 25, *, ctx: ZoteroContext) -> dict:
    return ctx.search_fulltext(query, limit=limit)


def get_zotero_attachment_text(
    attachment_key: str,
    max_chars: int = 20000,
    *,
    ctx: ZoteroContext,
) -> dict:
    return ctx.attachment_text(attachment_key, max_chars=max_chars)


_ANNOTATION_TYPES_PROP = {
    "type": "array",
    "items": {"type": "string", "enum": list(ANNOTATION_TYPE_NAMES)},
    "description": "Optional filter. Omit for all types.",
}
_LIBRARY_PROP = {
    "type": "integer",
    "description": "Defaults to the USER library. Group libraries have their own ids -- "
    "call list_zotero_libraries to see them.",
}
_CITEKEYS_PROP = {
    "type": "boolean",
    "default": True,
    "description": "Join Better BibTeX citekeys. Set false to skip the BBT round trip.",
}

TOOLS: tuple[_ToolSpec, ...] = (
    _ToolSpec(
        name="get_zotero_window_state",
        verb=get_zotero_window_state,
        description=(
            "Return what Zotero is currently SHOWING: selected collection, selected "
            "items, tabs, open readers and reader positions. Live GUI state, not the "
            "catalogue -- use get_zotero_item to look something up by key."
        ),
    ),
    _ToolSpec(
        name="get_zotero_active_reader",
        verb=get_zotero_active_reader,
        description="Return the active Zotero reader tab, optionally with annotations.",
        properties={
            "include_annotations": {"type": "boolean", "default": False},
            "annotation_types": _ANNOTATION_TYPES_PROP,
            "include_citekeys": _CITEKEYS_PROP,
        },
    ),
    _ToolSpec(
        name="get_zotero_open_readers",
        verb=get_zotero_open_readers,
        description="Return all open Zotero reader tabs, optionally with annotations.",
        properties={
            "include_annotations": {"type": "boolean", "default": False},
            "annotation_types": _ANNOTATION_TYPES_PROP,
            "include_citekeys": _CITEKEYS_PROP,
        },
    ),
    _ToolSpec(
        name="get_zotero_annotations",
        verb=get_zotero_annotations,
        description=(
            "Return highlights and comments for a Zotero ATTACHMENT key (not the parent "
            "item key -- use resolve_zotero_pdf to get one from a parent or citekey)."
        ),
        properties={
            "attachment_key": {"type": "string", "description": "8-char attachment key."},
            "annotation_types": _ANNOTATION_TYPES_PROP,
            "include_text": {"type": "boolean", "default": True},
            "include_comments": {"type": "boolean", "default": True},
        },
        required=("attachment_key",),
    ),
    _ToolSpec(
        name="resolve_zotero_pdf",
        verb=resolve_zotero_pdf,
        description=(
            "Resolve a Better BibTeX citekey, parent item key, or PDF attachment key to "
            "the {parent_key, attachment_key} pair. When an item has several PDFs, the "
            "one with the most annotations wins."
        ),
        properties={
            "identifier": {"type": "string"},
            "is_attachment_key": {"type": "boolean", "default": False},
        },
        required=("identifier",),
    ),
    _ToolSpec(
        name="get_zotero_sources",
        verb=get_zotero_sources,
        description=(
            "Every attachment carrying at least one annotation, with the item it belongs to "
            "and its `content_type`. NOT PDFs only — Zotero annotates HTML snapshots and "
            "EPUBs, and an attachment with no parent reports its own key as the source. "
            "Filter on `content_type` if you want just PDFs."
        ),
        properties={"include_citekeys": _CITEKEYS_PROP},
    ),
    _ToolSpec(
        name="get_zotero_item",
        verb=get_zotero_item,
        description=(
            "Everything about ONE item by key: type, title, fields, creators (in order), "
            "tags, trash state, parent and children. Carries `read_mode` -- `immutable=1` "
            "means the answer came from a point-in-time snapshot because Zotero held the "
            "write lock, not from the live database."
        ),
        properties={"item_key": {"type": "string", "description": "8-char item key."}},
        required=("item_key",),
    ),
    _ToolSpec(
        name="check_zotero_duplicate",
        verb=check_zotero_duplicate,
        description=(
            "Is this already in the library? Returns verdict block|warn|ok WITHOUT "
            "writing. DOI, ISBN or calibre_uuid match blocks; normalised title AND first "
            "author surname warns. Trashed matches are reported, never blocking. This is "
            "the same gate create_item runs -- ask it BEFORE adding."
        ),
        properties={
            "title": {"type": "string"},
            "doi": {"type": "string"},
            "isbn": {"type": "string"},
            "calibre_uuid": {"type": "string"},
            "creators": {
                "type": "array",
                "items": {"type": "object"},
                "description": "e.g. [{'creatorType':'author','lastName':'Welling'}]. "
                "Required for the title tier to fire -- title alone never warns.",
            },
        },
    ),
    _ToolSpec(
        name="list_zotero_pdfs",
        verb=list_zotero_pdfs,
        description=(
            "Enumerate stored PDF attachments with each one's parent title and "
            "collection, and the file path on disk."
        ),
        properties={"limit": {"type": "integer", "description": "Omit for all."}},
    ),
    _ToolSpec(
        name="get_zotero_trash_count",
        verb=get_zotero_trash_count,
        description=(
            "How many items are in the trash of ONE library (the user library by default). "
            "Scoped like every other read here — a machine with group libraries will see a "
            "smaller number than Zotero's global trash view."
        ),
    ),
    _ToolSpec(
        name="get_zotero_tags",
        verb=get_zotero_tags,
        description=(
            "Every tag in the library with how many items carry it, most-used first. The "
            "only way to see the tag VOCABULARY — every other read answers per-item — so "
            "it is what makes hygiene questions (case collisions, near-duplicates) askable."
        ),
    ),
    _ToolSpec(
        name="get_zotero_items_with_tag",
        verb=get_zotero_items_with_tag,
        description=(
            "Item keys carrying EXACTLY this tag, CASE-SENSITIVELY. `art` and `Art` are "
            "different tags and this distinguishes them, which is what a merge needs."
        ),
        properties={"name": {"type": "string"}},
        required=("name",),
    ),
    _ToolSpec(
        name="get_zotero_trash_items",
        verb=get_zotero_trash_items,
        description=(
            "WHAT is in the trash: key, title, item type and the date each was deleted, "
            "newest first. Trashed items are reachable through no other read — they are "
            "excluded from search and belong to no collection — so this is the only way to "
            "answer 'what did I delete, and when'."
        ),
    ),
    _ToolSpec(
        name="get_zotero_collections",
        verb=get_zotero_collections,
        description=(
            "The whole collection tree, nested, with breadcrumb `path` and a live "
            "`item_count` per collection. Counts EXCLUDE trashed items, so they match "
            "what the Zotero GUI shows. Scoped to the user library -- group libraries "
            "are not mixed in -- pass library_id for a group."
        ),
        properties={"library_id": _LIBRARY_PROP},
    ),
    _ToolSpec(
        name="get_zotero_collection_items",
        verb=get_zotero_collection_items,
        description=(
            "What is IN a collection: direct members with key, title and type. "
            "Subcollections are separate entries -- walk get_zotero_collections for the "
            "recursive view, which is also how the Zotero GUI behaves by default."
        ),
        properties={
            "collection_key": {"type": "string", "description": "8-char collection key."},
            "include_trashed": {"type": "boolean", "default": False},
            "library_id": _LIBRARY_PROP,
        },
        required=("collection_key",),
    ),
    _ToolSpec(
        name="get_zotero_item_collections",
        verb=get_zotero_item_collections,
        description=(
            "The INVERSE: which collections each item is filed in. An item can be in "
            "several, or none. Nothing else answers this -- Better BibTeX has only a "
            "citekey-keyed version, which cannot be reached from an item key."
        ),
        properties={
            "item_keys": {"type": "array", "items": {"type": "string"}},
            "library_id": _LIBRARY_PROP,
        },
        required=("item_keys",),
    ),
    _ToolSpec(
        name="find_zotero_collections",
        verb=find_zotero_collections,
        description=(
            "Collections whose name contains this string, case-insensitively. Returns "
            "each one's full breadcrumb path, which is what disambiguates the several "
            "collections that share a short name."
        ),
        properties={"name": {"type": "string"}, "library_id": _LIBRARY_PROP},
        required=("name",),
    ),
    _ToolSpec(
        name="search_zotero_items",
        verb=search_zotero_items,
        description=(
            "Search the library by title, creator, tag or DOI. FUZZY by default -- it "
            "tolerates typos, which Zotero's own substring search does not: 'Langevan "
            "Dynmaics' returns nothing there and scores 0.875 here. Each hit reports "
            "`matched_on` so a surprising result explains itself. Set fuzzy=false for "
            "exact substring."
        ),
        properties={
            "query": {"type": "string"},
            "fuzzy": {"type": "boolean", "default": True},
            "limit": {"type": "integer", "default": 25},
            "item_type": {
                "type": "string",
                "description": "Restrict to one Zotero type, e.g. 'book'.",
            },
            "library_id": _LIBRARY_PROP,
        },
        required=("query",),
    ),
    _ToolSpec(
        name="search_zotero_annotations",
        verb=search_zotero_annotations,
        description=(
            "Search your highlights and comments, optionally filtered by COLOUR and "
            "type. Colour is how people encode meaning in Zotero, so 'the yellow ones' "
            "is a real query. Substring, not fuzzy -- you highlighted the words "
            "yourself. Omit `query` to browse by colour or type alone."
        ),
        properties={
            "query": {"type": "string"},
            "color": {"type": "string", "description": "Zotero hex, e.g. '#ffd400'."},
            "annotation_types": _ANNOTATION_TYPES_PROP,
            "limit": {"type": "integer", "default": 25},
        },
    ),
    _ToolSpec(
        name="search_zotero_fulltext",
        verb=search_zotero_fulltext,
        description=(
            "Search the extracted TEXT of indexed PDFs, returning context snippets. "
            "Phrase-aware: matches across line breaks and punctuation. Narrowed by "
            "Zotero's word index first, so it reads only the handful of documents that "
            "could match rather than all 587 MB of cache."
        ),
        properties={
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 25},
        },
        required=("query",),
    ),
    _ToolSpec(
        name="get_zotero_attachment_text",
        verb=get_zotero_attachment_text,
        description=(
            "The full extracted text of one attachment. Returns "
            "`{ok: false, error: 'not_indexed'}` when Zotero never indexed it -- which "
            "is the honest answer for a scan with no text layer."
        ),
        properties={
            "attachment_key": {"type": "string"},
            "max_chars": {"type": "integer", "default": 20000},
        },
        required=("attachment_key",),
    ),
    _ToolSpec(
        name="list_zotero_libraries",
        verb=list_zotero_libraries,
        description=(
            "Every Zotero library with a live item count: the user's, plus any groups. "
            "Every other read here is scoped to ONE library and defaults to the user's, "
            "so this is how you tell 'you have nothing' from 'you are looking in the "
            "wrong library'."
        ),
    ),
    _ToolSpec(
        name="ping_zotero",
        verb=ping_zotero,
        description=(
            "Is the zotero-bridge plugin answering? Use this to tell 'Zotero is closed' "
            "apart from 'that key does not exist'."
        ),
    ),
)

_BY_NAME = {spec.name: spec for spec in TOOLS}

# A locked database, a missing key and a closed Zotero used to be indistinguishable:
# every failure flattened to {"ok": false, "error": str(exc)} with no code to branch on.
_CODES: tuple[tuple[type[BaseException], str], ...] = (
    (ZoteroBridgeError, "bridge_unreachable"),
    # ⚠ WAS `(ZoteroAnnotationError, "annotation_read_failed")`. That alias resolved to
    # `ZoteroReadError`, which every store raises, so a missing database reached through
    # `collection_tree` or `list_libraries` was reported to the client as an ANNOTATION
    # failure. Same class, honest name, and the code now says what actually happened --
    # which is the same thing `sqlite3.OperationalError` means, so they share it.
    (ZoteroReadError, "database_unavailable"),
    (sqlite3.OperationalError, "database_unavailable"),
    (ValueError, "bad_arguments"),
)


def error_code(exc: BaseException) -> str:
    for kind, code in _CODES:
        if isinstance(exc, kind):
            return code
    return "error"


def call_read(
    name: str, arguments: dict[str, Any], *, ctx: ZoteroContext | None = None
) -> Any:
    """Dispatch one tool call. Thin by design -- the logic is shared, see `tool_spec`.

    `ctx` is the injection point this adapter lacked. Defaulting it here is correct in a way
    the old module global was not: this is the composition root's own layer, the one place
    allowed to name concrete adapters.
    """
    return _dispatch(
        _BY_NAME, name, arguments,
        extra={"ctx": ctx if ctx is not None else _context_from_env()},
    )


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


def render_call(name: str, arguments: dict[str, Any]) -> str:
    """One tool call, rendered. The ONLY part of the server this adapter still owns.

    Every failure becomes data with a `code` a caller can branch on -- "a locked database, a
    missing key and a closed Zotero used to be indistinguishable", which is what `_CODES`
    exists for. The write adapter's version differs precisely here, which is why this is the
    callback and the rest is shared.
    """
    try:
        return render_json(call_read(name, arguments))
    except Exception as exc:  # noqa: BLE001 - the envelope is the contract
        return render_json({"ok": False, "code": error_code(exc), "error": str(exc)})


async def main() -> None:
    await run_stdio(SERVER_NAME, TOOLS, render_call)
