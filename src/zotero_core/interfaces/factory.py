"""The composition root: the ONE place that names concrete adapters.

Free `build_*` functions rather than a container class, matching `omni-rag`'s
`interfaces/factory.py`. It lives in `interfaces` and not in `application` for the reason
the whole layering exists: something has to choose the real implementations, and whatever
does is the thing that cannot be reused with different ones.

WHAT MOVED HERE, AND WHY IT WAS A PROBLEM WHERE IT WAS
------------------------------------------------------
`verbs._Session.__init__` did `self.linker = linker or LinkerClient()`, and
`collections._session()` did the same three defaults again. That `or Concrete()` idiom
looks like injection and is the opposite of it: the parameter is optional, so the module
MUST import the adapter to have a default, and the application layer therefore named
infrastructure at three sites plus a re-export list.

It also left the test suite no way in. Substituting a fake meant rewriting the module
global — `monkeypatch.setattr(f"{module}.CookjohnClient", ...)` for each consuming module,
with `raising=False`, so a renamed module would silently stop being patched. The fixture's
own comment conceded it: "it is the only way in, because the MCP surface has no injection
parameter by design."

Now there is one. Every default lives in this file; everything above takes ports.
"""

from __future__ import annotations

from pathlib import Path

from zotero_core.application.services.context import ZoteroContext
from zotero_core.application.services.session import WriteSession
from zotero_core.domain.ports.annotation_catalogue import AnnotationCatalogue
from zotero_core.domain.ports.catalogue import Catalogue
from zotero_core.domain.ports.citation_keys import CitationKeys
from zotero_core.domain.ports.collection_catalogue import CollectionCatalogue
from zotero_core.domain.ports.duplicates import DuplicateFinder
from zotero_core.domain.ports.gui_bridge import GuiBridge
from zotero_core.domain.ports.journal import Journal
from zotero_core.domain.ports.library_catalogue import LibraryCatalogue
from zotero_core.domain.ports.paper_resolver import PaperResolver
from zotero_core.domain.ports.search_catalogue import SearchCatalogue
from zotero_core.domain.ports.write_transport import Cookjohn, Linker
from zotero_core.domain.ports.zotero_probe import ZoteroProbe
from zotero_core.infrastructure.http.bbt import DEFAULT_BBT_RPC_URL, BetterBibTeXClient
from zotero_core.infrastructure.http.bridge import DEFAULT_BRIDGE_URL, ZoteroBridgeClient
from zotero_core.infrastructure.http.papers import WebPaperResolver
from zotero_core.infrastructure.journal import FileJournal
from zotero_core.infrastructure.probe import HttpZoteroProbe
from zotero_core.infrastructure.sqlite.annotations import (
    DEFAULT_ZOTERO_DB,
    ZoteroAnnotationStore,
)
from zotero_core.infrastructure.sqlite.collections import ZoteroCollectionStore
from zotero_core.infrastructure.sqlite.duplicates import CatalogueDuplicateFinder
from zotero_core.infrastructure.sqlite.items import ZoteroItemStore
from zotero_core.infrastructure.sqlite.libraries import SqliteLibraryCatalogue
from zotero_core.infrastructure.sqlite.search import ZoteroSearchStore
from zotero_core.infrastructure.transports.cookjohn import CookjohnClient
from zotero_core.infrastructure.transports.linker import LinkerClient


def build_write_session(
    *,
    linker: Linker | None = None,
    cookjohn: Cookjohn | None = None,
    store: Catalogue | None = None,
    collections: CollectionCatalogue | None = None,
    journal: Journal | None = None,
    probe: ZoteroProbe | None = None,
    duplicates: DuplicateFinder | None = None,
    papers: PaperResolver | None = None,
) -> WriteSession:
    """Assemble one write session, defaulting each port to its real adapter.

    Every argument is an override, and they exist for callers that already hold a
    collaborator — a test with fakes, or a caller pointing at a different database. The
    DEFAULTS are the point: they are here, in one function, instead of spread across the
    verbs as `or Concrete()`.

    `duplicates` defaults to a finder over the SAME store, not a second one: a duplicate
    check that read a different catalogue than the existence gates would answer about a
    library the write is not going to touch.
    """
    store = store if store is not None else ZoteroItemStore()
    return WriteSession(
        linker=linker if linker is not None else LinkerClient(),
        cookjohn=cookjohn if cookjohn is not None else CookjohnClient(),
        store=store,
        collections=(
            collections
            if collections is not None
            else ZoteroCollectionStore(store.db_path, busy_timeout_ms=store.busy_timeout_ms)
        ),
        journal=journal if journal is not None else FileJournal(),
        probe=probe if probe is not None else HttpZoteroProbe(),
        duplicates=(
            duplicates if duplicates is not None else CatalogueDuplicateFinder(store)
        ),
        papers=papers if papers is not None else WebPaperResolver(),
    )


def build_context(
    *,
    bridge_url: str = DEFAULT_BRIDGE_URL,
    zotero_db_path: str | Path = DEFAULT_ZOTERO_DB,
    bbt_rpc_url: str = DEFAULT_BBT_RPC_URL,
    bridge: GuiBridge | None = None,
    annotations: AnnotationCatalogue | None = None,
    bbt: CitationKeys | None = None,
    items: Catalogue | None = None,
    collections: CollectionCatalogue | None = None,
    search: SearchCatalogue | None = None,
    libraries: LibraryCatalogue | None = None,
    duplicates: DuplicateFinder | None = None,
) -> ZoteroContext:
    """Assemble the read facade, defaulting every port to its real adapter.

    The three URL/path arguments are what a CALLER actually varies -- point it at a copy of
    the database, or a bridge on another port -- and they are kept because the CLI's `--db`
    and the MCP adapter's `ZOTERO_CORE_DB` both mean exactly this. The eight port arguments
    are overrides, for a test or a caller that already holds one.

    ⚠ TWO DEFECTS FIXED HERE, both found by an audit rather than by a test.

    1. This took `**overrides` and `.pop`ed names out of it, so a MISSPELLED port was
       silently ignored: `build_context(bbt_client=Fake())` returned a real
       `BetterBibTeXClient` and reported nothing. That is the same silent-no-op this
       module's own docstring indicts monkeypatching for -- "a renamed module would
       silently stop being patched" -- reintroduced as a typo'd keyword. Named parameters
       make it a `TypeError`.
    2. It used `x or Concrete()`, which discards a FALSY override. Measured: a stub whose
       `__bool__` returns False was thrown away and the real store used instead.
       `is not None` is the test that means "was one supplied", which is the question.
       `build_write_session` already did this correctly; the two now agree.

    Annotating each parameter with its PORT is what lets `ty` check the wiring at all. With
    the old `**overrides` every argument was laundered through `Unknown` before it reached
    the typed dataclass, so passing a non-port produced zero diagnostics here while
    constructing `ZoteroContext` directly produced eight.

    Every store reads the SAME database. Handing them different paths would let a duplicate
    check answer about one library while the existence gate read another -- which is also
    why `duplicates` defaults to a finder over the item store built here, not a second one.
    """
    db = zotero_db_path
    items = items if items is not None else ZoteroItemStore(db)
    return ZoteroContext(
        bridge=bridge if bridge is not None else ZoteroBridgeClient(bridge_url),
        annotations=(
            annotations if annotations is not None else ZoteroAnnotationStore(db)
        ),
        bbt=bbt if bbt is not None else BetterBibTeXClient(bbt_rpc_url),
        items=items,
        collections=(
            collections if collections is not None else ZoteroCollectionStore(db)
        ),
        search=search if search is not None else ZoteroSearchStore(db),
        libraries=libraries if libraries is not None else SqliteLibraryCatalogue(db),
        duplicates=(
            duplicates if duplicates is not None else CatalogueDuplicateFinder(items)
        ),
    )
