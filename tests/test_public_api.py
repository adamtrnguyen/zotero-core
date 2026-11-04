"""`zotero_core.__all__` is the PUBLISHED API. This is the gate that makes moving cheap.

`zotero-core` is a distributed library, not an application, so it deviates from the
convention the rest of the estate follows: omni-rag's fifteen layer `__init__.py` files are
all zero bytes and nothing is ever re-exported. Here the top-level `__all__` is what
`omni-rag` and `youtube2zotero` import, and keeping it stable is precisely what turns "every
internal move is a breaking change" into "internal moves are free".

That trade only holds if something checks it, which nothing did.
"""

from __future__ import annotations

import pytest

import zotero_core

# Frozen 2026-08-19, before the DDD restructure. A name may be ADDED; removing or failing to
# resolve one is a breaking change for a consumer that pins this package by tag.
PUBLISHED = (
    "DEFAULT_BBT_RPC_URL",
    "DEFAULT_ZOTERO_DB",
    "USER_LIBRARY_ID",
    "Annotation",
    "BetterBibTeXClient",
    "ItemState",
    "ItemStates",
    "ReaderContext",
    "ReaderPosition",
    "ReaderState",
    "TabState",
    "WindowState",
    "ZoteroAnnotationStore",
    "ZoteroAttachment",
    "ZoteroAttachments",
    "ZoteroBridgeClient",
    "ZoteroCollection",
    "ZoteroContext",
    "ZoteroItem",
    "ZoteroItemStore",
    "ZoteroSource",
    "__version__",
    "check_duplicate",
    "clean_doi",
    "clean_isbn",
    # The write surface, published 2026-08-19 -- see the note in `__init__.py`. Read-only
    # `__all__` was a leftover from the pre-merge package, and it forced the one consumer
    # that writes to import `zotero_core.application.*` by path.
    "CookjohnClient",
    "LinkerClient",
    "import_attachment",
    "update_metadata",
    "write_note",
    # The composition root, published because the verbs now REQUIRE a session.
    "build_write_session",
    "build_context",
    "WriteBlocked",
    "Reason",
    "ALL_REASONS",
)


@pytest.mark.parametrize("name", PUBLISHED)
def test_every_published_name_still_resolves_from_the_top(name):
    """Wherever the type moves internally, `from zotero_core import X` must keep working."""
    assert hasattr(zotero_core, name), f"{name} no longer resolves from zotero_core"


def test_all_lists_exactly_the_published_names():
    """A name added to `__all__` without being added here is an undeclared API expansion."""
    missing = sorted(set(PUBLISHED) - set(zotero_core.__all__))
    assert not missing, f"dropped from __all__: {missing}"
    added = sorted(set(zotero_core.__all__) - set(PUBLISHED))
    assert not added, f"added to __all__ without updating this test: {added}"


def test_the_public_surface_is_enough_for_the_known_consumers():
    """No consumer should need a submodule path.

    `arxiv-bulk` used to import from `read.annotations`, `read.bbt` and `domain.entities`
    directly, and broke on 2026-08-19 when those moved. The fix was not to pin the module
    layout -- it was to put what it needed on the public surface, so the layout is free to
    change. These four names are what it uses.
    """
    for name in (
        "ZoteroContext",
        "Annotation",
        "DEFAULT_ZOTERO_DB",
        "DEFAULT_BBT_RPC_URL",
        # ⚠ `build_context` was missing here and `arxiv-bulk` BROKE. It constructed the
        # facade directly -- `ZoteroContext(zotero_db_path=...)` -- which worked until the
        # read-side port replaced that constructor with eight required ports. The name
        # `ZoteroContext` never left the surface, so a name-resolution test saw nothing
        # wrong. A type you cannot construct is not a usable export.
        "build_context",
        "WriteBlocked",
        "Reason",
        "ALL_REASONS",
    ):
        assert hasattr(zotero_core, name), f"{name} left the public surface"

    # And the surface must be enough to CONSTRUCT it, not merely to name it.
    ctx = zotero_core.build_context()
    assert isinstance(ctx, zotero_core.ZoteroContext)


def test_the_public_surface_is_enough_to_write():
    """`youtube2zotero/promote.py` is the consumer that WRITES.

    It imported `zotero_core.write.transports.cookjohn` and `zotero_core.write.verbs` by
    path -- the paths as they were then -- because `__all__` published only reads, a
    leftover from when `core/` was a read-only package rather than a decision that survived
    the merge. BOTH of those paths have since moved (`transports/` to `infrastructure/`,
    `write/` to `application/`) and this consumer needed no edit for either. These are the
    names it actually uses.
    """
    for name in (
        # a published way to write needs a published way to catch
        "WriteBlocked",
        "Reason",
        "build_write_session",
        "build_context",
        "WriteBlocked",
        "Reason",
        "ALL_REASONS",
        "CookjohnClient",
        "import_attachment",
        "update_metadata",
        "write_note",
    ):
        assert hasattr(zotero_core, name), f"{name} left the public surface"


def test_omni_rag_only_needs_the_top_level_import():
    """omni-rag's `zotero_catalogue.py` does `from zotero_core import ZoteroItemStore` and
    then reads `.attachment_key`, `.path`, `.title`, `.collection`, `.read_mode` off what
    `pdf_attachments()` returns. Structural, so the type may move; the name may not."""
    store_cls = zotero_core.ZoteroItemStore
    assert hasattr(store_cls, "pdf_attachments")
    fields = {"attachment_key", "path", "title", "collection"}
    assert fields <= set(zotero_core.ZoteroAttachment.__dataclass_fields__)
    assert "read_mode" in zotero_core.ZoteroAttachments.__dataclass_fields__
