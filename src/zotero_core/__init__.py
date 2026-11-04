"""zotero-core: the ONE place that knows what a local Zotero library is.

Read and write, behind enforced internal layers:

    domain/       entities, values, policy          pure -- no driver imports
    read/         sqlite + bridge + BBT             "what is in the library"
    write/        gated verbs + two transports      "change it, recoverably"
    interfaces/   cli, read_mcp, write_mcp          the only layer that imports mcp

WHY ONE PACKAGE (merged 2026-08-19, was zotero-context + zotero-writes)
----------------------------------------------------------------------
The split was defensible and it was enforced -- `writes -> core, never the reverse`
had its own import-linter contract. What it could not survive was that the read half
was never finished. `core` shipped `items.py` and `duplicates.py` and exposed NEITHER
from its CLI or MCP surface, and never had collection or search queries at all. So an
agent asking "what is in collection X" had to go to a third-party plugin, and the
charter sentence -- "a second answer to what is in the library is the failure this
package exists to end" -- described a gap rather than a guarantee.

Two packages also meant two of everything that was really one thing: two pyprojects
agreeing on `mcp<2` and `ty==0.0.55` in duplicated comments, two lockfiles, and one
test suite that lived in `writes/` and reached `core` incidentally -- `core` had no
`tests/` directory and no `.importlinter` of its own.

The direction the split protected is now a LAYER, not a package boundary, and it is
still checked: see `.importlinter`, contract `layers`. Nothing was weakened by
merging; `domain` is a rule that could not previously be expressed at all.

WHAT THIS MODULE EXPORTS
------------------------
The READ surface only. `import zotero_core` must stay cheap and side-effect free:
the write verbs reach two HTTP transports and are an explicit opt-in --

    from zotero_core.application import create_item, trash_items

which also keeps the CRUD surface a deliberate act rather than something a caller
gets by accident.
"""

from zotero_core.application.services.context import ZoteroContext
from zotero_core.domain.entities.gui import (
    ReaderContext,
    ReaderPosition,
    ReaderState,
    TabState,
    WindowState,
    ZoteroCollection,
    ZoteroItem,
)
from zotero_core.domain.entities.models import Annotation, ZoteroSource
from zotero_core.domain.errors import ALL_REASONS, Reason, WriteBlocked
from zotero_core.domain.services.identifiers import clean_doi, clean_isbn
from zotero_core.infrastructure.http.bbt import DEFAULT_BBT_RPC_URL, BetterBibTeXClient
from zotero_core.infrastructure.http.bridge import ZoteroBridgeClient
from zotero_core.infrastructure.sqlite.annotations import DEFAULT_ZOTERO_DB, ZoteroAnnotationStore
from zotero_core.infrastructure.sqlite.duplicates import check_duplicate
from zotero_core.infrastructure.sqlite.items import (
    USER_LIBRARY_ID,
    ItemState,
    ItemStates,
    ZoteroAttachment,
    ZoteroAttachments,
    ZoteroItemStore,
)
from zotero_core.infrastructure.transports.cookjohn import CookjohnClient
from zotero_core.infrastructure.transports.linker import LinkerClient

__version__ = "0.4.0"

# The three WRITE VERBS are published LAZILY, and the reason is a real contract failure
# rather than a preference. Importing `zotero_core.anything` executes THIS file first
# (Python imports ancestor packages), so a module-level `from zotero_core.application.services.verbs
# import ...` here makes every layer -- including `domain` -- depend on `write`. That is
# precisely what `interfaces above write above infrastructure above domain` forbids, and
# import-linter caught it: `infrastructure.service -> zotero_core -> write.verbs`.
#
# PEP 562 defers the import to first ATTRIBUTE ACCESS, so `from zotero_core import
# write_note` still works, `hasattr` still works, and no import-time edge exists for the
# contract to trip on. The two transports above stay eager because they are
# `infrastructure`, which this file is already allowed to import.
#
# This is the cost of keeping re-exports at all -- omni-rag's convention is "never", and
# this package deviates on purpose because `__all__` IS its published API.
_LAZY_WRITE_VERBS = ("import_attachment", "update_metadata", "write_note")

#: The composition root, BOTH halves. Published because the verbs require a `WriteSession`
#: and `ZoteroContext` now requires eight ports, so a consumer cannot build either itself.
#: ⚠ `build_context` was NOT published at first, and that omission BROKE `arxiv-bulk`:
#: `ZoteroContext(zotero_db_path=...)` had been the documented way in, the read-side port
#: replaced that constructor, and the replacement was not on the public surface. Publishing
#: `ZoteroContext` as a NAME is not the same as publishing a way to construct one.
#: Lazy for the same reason as the verbs: it imports `interfaces`, which sits above
#: everything, and an eager import here would put that in every layer's import graph.
_LAZY_FACTORY = ("build_write_session", "build_context")


def __getattr__(name: str):
    source = None
    if name in _LAZY_WRITE_VERBS:
        source = "zotero_core.application.services.verbs"
    elif name in _LAZY_FACTORY:
        source = "zotero_core.interfaces.factory"
    if source:
        import importlib

        value = getattr(importlib.import_module(source), name)
        globals()[name] = value  # cached: __getattr__ is not consulted again
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # ⚠ THE WRITE SURFACE IS PUBLISHED TOO, as of 2026-08-19. This list was read-only for
    # as long as `core/` was a read-only package, and the merge made that a leftover rather
    # than a rule: `youtube2zotero/promote.py` needs writes, so it imported
    # `zotero_core.write.transports.cookjohn` and `zotero_core.write.verbs` directly -- the
    # paths as they were THEN, both of which have since moved (`transports/` to
    # `infrastructure/`, `write/` to `application/`). That is the same reach-past-the-surface
    # that broke `arxiv-bulk`, and publishing the names it actually uses made both moves
    # invisible to it -- which is exactly what happened, twice, with no edit on its side.
    # Publishing is the CHEAP half of the deal; the surface is frozen
    # by `tests/test_public_api.py`, so adding a name here is a deliberate commitment.
    # Defaults consumers actually need. Added 2026-08-19 because `arxiv-bulk` was importing
    # them from `read.annotations` / `read.bbt` directly -- reaching past the public surface
    # into modules that were about to move, which is how it broke earlier the same day.
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
    "CookjohnClient",
    "LinkerClient",
    "import_attachment",
    "update_metadata",
    "write_note",
    "build_write_session",
    "build_context",
    # The failure vocabulary. Published alongside the write verbs because a caller given a
    # published way to WRITE and no published way to CATCH has to import
    # `zotero_core.domain.errors` by path — the exact reach-past-the-surface that broke
    # `arxiv-bulk`. `Reason` and `ALL_REASONS` come too: `code` is the field a consumer
    # branches on, and it is worthless without the set of values it can take.
    "WriteBlocked",
    "Reason",
    "ALL_REASONS",
]
