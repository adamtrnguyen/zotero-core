"""zotero-core: the ONE place that knows what a local Zotero library is.

Read and write, behind the layers `.importlinter` enforces (contract `layers`):

    domain/          entities, values, ports, policy     pure -- no driver imports
    application/     read facade, gated verbs, undo      depends on domain ports only
    infrastructure/  sqlite, bridge, BBT, transports     the adapters
    interfaces/      cli, MCP server, factory            the only layer that imports mcp

One package, because every write gate is a read: writes depend on reads, reads never on
writes, and that direction is a layer contract. Why the read and write packages merged:
docs/history/package-structure.md.

WHAT THIS MODULE EXPORTS
------------------------
`__all__` is the published API, frozen by `tests/test_public_api.py`. `import zotero_core`
stays cheap and side-effect free: the read surface and the two transport clients import
eagerly; the write verbs and the composition root (`build_context`, `build_write_session`)
resolve lazily on first attribute access.
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

# Published LAZILY (PEP 562). Importing any `zotero_core.*` module runs this file first, so
# an eager import of `application.services.verbs` here would make every layer, `domain`
# included, depend on the write verbs, which the `layers` contract forbids.
# `from zotero_core import write_note` and `hasattr` still work. The transports stay eager:
# they are infrastructure, which this file may import. A root `__all__` deviates from
# omni-rag's "never re-export" on purpose, because it IS the published API.
_LAZY_WRITE_VERBS = ("import_attachment", "update_metadata", "write_note")

#: The composition root, BOTH halves. Published because the verbs require a `WriteSession`
#: and `ZoteroContext` requires eight ports, so a consumer cannot build either itself:
#: publishing a type is not publishing a way to construct one.
#: Lazy for the same reason as the verbs: it imports `interfaces`, which sits above
#: everything. Why this is published: docs/history/public-api.md.
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
    # Every name a known consumer imports is published here, writes included, so an
    # internal move never reaches a consumer. Adding a name is a deliberate commitment:
    # `tests/test_public_api.py` freezes the list. History: docs/history/public-api.md.
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
