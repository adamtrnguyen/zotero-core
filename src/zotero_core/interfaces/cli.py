from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from zotero_core.application.services.context import ZoteroContext
from zotero_core.infrastructure.http.bbt import DEFAULT_BBT_RPC_URL
from zotero_core.infrastructure.http.bridge import DEFAULT_BRIDGE_URL
from zotero_core.infrastructure.sqlite.annotations import DEFAULT_ZOTERO_DB
from zotero_core.interfaces.arguments import parse_types
from zotero_core.interfaces.factory import build_context
from zotero_core.interfaces.rendering import render_json


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Through the composition root, not by constructing the facade: `ZoteroContext` takes
    # ports now, and deciding which real adapters those are is `factory`'s job. The three
    # arguments a caller actually varies -- `--db`, `--bridge-url`, `--bbt-url` -- are
    # unchanged, because they are the reason this CLI could always be pointed elsewhere and
    # the MCP adapter could not.
    ctx = build_context(
        bridge_url=args.bridge_url,
        zotero_db_path=args.db,
        bbt_rpc_url=args.bbt_url,
    )

    try:
        payload = dispatch(ctx, args)
    except Exception as exc:
        print_json({"ok": False, "error": str(exc)}, pretty=args.pretty)
        return 1

    print_json(payload, pretty=args.pretty)
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--bridge-url", default=DEFAULT_BRIDGE_URL)
    common.add_argument("--db", default=str(DEFAULT_ZOTERO_DB), type=Path)
    common.add_argument("--bbt-url", default=DEFAULT_BBT_RPC_URL)
    common.add_argument("--pretty", action="store_true")

    parser = argparse.ArgumentParser(
        prog="zotero-core",
        description="Read-only Zotero catalogue and window state",
        parents=[common],
    )

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ping", parents=[common])
    sub.add_parser("window-state", parents=[common])
    add_reader_parser(sub.add_parser("active-reader", parents=[common]))
    add_reader_parser(sub.add_parser("open-readers", parents=[common]))
    annotations = sub.add_parser("annotations", parents=[common])
    annotations.add_argument("attachment_key")
    add_annotation_filters(annotations)
    resolve = sub.add_parser("resolve-pdf", parents=[common])
    resolve.add_argument("identifier")
    resolve.add_argument("--pdf-key", action="store_true")
    sources = sub.add_parser("sources", parents=[common])
    sources.add_argument("--no-citekeys", action="store_true")
    sources.add_argument("--all", action="store_true", help="Include items without annotations")

    # Catalogue reads. Kept at PARITY with the MCP tool table on purpose: the read
    # surface split into "what the CLI can do" and "what an agent can do" once before,
    # and the half nobody could reach is the half that rotted.
    item = sub.add_parser("item", parents=[common], help="Everything about one item by key")
    item.add_argument("item_key")
    dup = sub.add_parser("duplicate", parents=[common], help="Is this already in the library?")
    dup.add_argument("--title")
    dup.add_argument("--doi")
    dup.add_argument("--isbn")
    dup.add_argument("--calibre-uuid")
    dup.add_argument(
        "--author",
        action="append",
        default=[],
        metavar="SURNAME",
        help="First author surname. The title tier needs one -- title alone never warns.",
    )
    pdfs = sub.add_parser("pdfs", parents=[common], help="Enumerate stored PDF attachments")
    pdfs.add_argument("--limit", type=int)
    sub.add_parser(
        "trash-count", parents=[common], help="How many items are in this library's trash"
    )
    sub.add_parser("tags", parents=[common], help="Every tag with its item count")
    p_iwt = sub.add_parser(
        "items-with-tag", parents=[common], help="Item keys carrying EXACTLY this tag"
    )
    p_iwt.add_argument("name")
    sub.add_parser(
        "trash-items",
        parents=[common],
        help="WHAT is in the trash: key, title, type and the date deleted, newest first",
    )

    sub.add_parser("libraries", parents=[common], help="Every library, with item counts")
    colls = sub.add_parser("collections", parents=[common], help="The whole collection tree")
    colls.add_argument("--library-id", type=int)
    citems = sub.add_parser("collection-items", parents=[common], help="What is in a collection")
    citems.add_argument("collection_key")
    citems.add_argument("--include-trashed", action="store_true")
    citems.add_argument("--library-id", type=int)
    icoll = sub.add_parser(
        "item-collections", parents=[common], help="Which collections an item is filed in"
    )
    icoll.add_argument("item_keys", nargs="+")
    icoll.add_argument("--library-id", type=int)
    fcoll = sub.add_parser("find-collections", parents=[common], help="Collections by name")
    fcoll.add_argument("name")
    fcoll.add_argument("--library-id", type=int)

    search = sub.add_parser("search", parents=[common], help="Fuzzy search title/creator/tag/DOI")
    search.add_argument("query")
    search.add_argument("--exact", action="store_true", help="Substring instead of fuzzy")
    search.add_argument("--limit", type=int, default=25)
    search.add_argument("--type", dest="item_type")
    search.add_argument("--library-id", type=int)
    sann = sub.add_parser("search-annotations", parents=[common], help="Search highlights")
    sann.add_argument("query", nargs="?", default="")
    sann.add_argument("--color")
    sann.add_argument("--annotation-types", default="")
    sann.add_argument("--limit", type=int, default=25)
    sft = sub.add_parser("search-fulltext", parents=[common], help="Search indexed PDF text")
    sft.add_argument("query")
    sft.add_argument("--limit", type=int, default=25)
    atext = sub.add_parser("attachment-text", parents=[common], help="Extracted text of one PDF")
    atext.add_argument("attachment_key")
    atext.add_argument("--max-chars", type=int, default=20000)
    return parser


def add_reader_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--include-annotations", action="store_true")
    parser.add_argument("--annotation-types", default="")
    parser.add_argument("--no-citekeys", action="store_true")


def add_annotation_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--annotation-types", default="")
    parser.add_argument("--no-text", action="store_true")
    parser.add_argument("--no-comments", action="store_true")


def _reader(ctx: ZoteroContext, args: argparse.Namespace, *, active_only: bool) -> Any:
    kwargs = {
        "include_annotations": args.include_annotations,
        "include_citekeys": not args.no_citekeys,
        "annotation_types": parse_types(args.annotation_types),
    }
    if active_only:
        # The `[0] if contexts else None` collapse lives on the facade now -- it was written
        # out here AND in `read_mcp`, identically.
        return ctx.get_active_reader_context(**kwargs)
    return ctx.get_open_reader_context(active_only=False, **kwargs)


def _resolve_pdf(ctx: ZoteroContext, args: argparse.Namespace) -> dict:
    parent_key, attachment_key = ctx.resolve_pdf_attachment_key(
        args.identifier, is_attachment_key=args.pdf_key
    )
    return {"parent_key": parent_key, "attachment_key": attachment_key}


def _duplicate(ctx: ZoteroContext, args: argparse.Namespace) -> dict:
    return ctx.check_duplicate(
        title=args.title,
        doi=args.doi,
        isbn=args.isbn,
        calibre_uuid=args.calibre_uuid,
        creators=tuple({"creatorType": "author", "lastName": s} for s in args.author),
    )


# A TABLE, not an if-chain. The chain this replaces tripped ruff's C901 complexity
# ceiling the moment four catalogue verbs were added -- which is the same pressure that
# made the MCP adapter unmaintainable, and the reason it grew no tools for months.
# Adding a verb is one entry here plus one parser above.
_HANDLERS: dict[str, Callable[[ZoteroContext, argparse.Namespace], Any]] = {
    "ping": lambda ctx, _a: {"ok": True, "bridge": ctx.ping()},
    "window-state": lambda ctx, _a: ctx.get_window_state(),
    "active-reader": lambda ctx, a: _reader(ctx, a, active_only=True),
    "open-readers": lambda ctx, a: _reader(ctx, a, active_only=False),
    "annotations": lambda ctx, a: ctx.get_annotations(
        a.attachment_key,
        types=parse_types(a.annotation_types),
        include_text=not a.no_text,
        include_comments=not a.no_comments,
    ),
    "resolve-pdf": _resolve_pdf,
    "sources": lambda ctx, a: ctx.get_sources_with_annotations(include_citekeys=not a.no_citekeys, include_all=getattr(a, "all", False)),
    "item": lambda ctx, a: ctx.get_item(a.item_key),
    "duplicate": _duplicate,
    "pdfs": lambda ctx, a: ctx.list_pdfs(limit=a.limit),
    "trash-count": lambda ctx, _a: ctx.trash_count(),
    "trash-items": lambda ctx, _a: ctx.trash_items(),
    "tags": lambda ctx, _a: ctx.tags(),
    "items-with-tag": lambda ctx, a: ctx.items_with_tag(a.name),
    "libraries": lambda ctx, _a: ctx.list_libraries(),
    "collections": lambda ctx, a: ctx.collection_tree(library_id=a.library_id),
    "collection-items": lambda ctx, a: ctx.collection_items(
        a.collection_key, include_trashed=a.include_trashed, library_id=a.library_id
    ),
    "item-collections": lambda ctx, a: ctx.item_collections(
        a.item_keys, library_id=a.library_id
    ),
    "find-collections": lambda ctx, a: ctx.find_collections(a.name, library_id=a.library_id),
    "search": lambda ctx, a: ctx.search_items(
        a.query, fuzzy=not a.exact, limit=a.limit, item_type=a.item_type,
        library_id=a.library_id,
    ),
    "search-annotations": lambda ctx, a: ctx.search_annotations(
        a.query,
        color=a.color,
        annotation_types=parse_types(a.annotation_types),
        limit=a.limit,
    ),
    "search-fulltext": lambda ctx, a: ctx.search_fulltext(a.query, limit=a.limit),
    "attachment-text": lambda ctx, a: ctx.attachment_text(
        a.attachment_key, max_chars=a.max_chars
    ),
}


def dispatch(ctx: ZoteroContext, args: argparse.Namespace) -> Any:
    handler = _HANDLERS.get(args.command)
    if handler is None:
        raise ValueError(f"Unknown command: {args.command}")
    return handler(ctx, args)


def print_json(payload: Any, *, pretty: bool = False) -> None:
    """Print a result. Serialisation is `rendering.render_json`, shared with both adapters.

    ⚠ This used to call `json.dumps` itself, WITHOUT `default=str` -- so a value the domain
    does not model (a `Path`, a `datetime` in a `WriteBlocked.detail`) raised here while the
    write adapter, which had the fallback, survived the identical payload.
    """
    print(render_json(payload, indent=2 if pretty else None))


if __name__ == "__main__":
    raise SystemExit(main())
