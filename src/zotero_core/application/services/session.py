"""Everything one write operation needs, injected rather than constructed.

WHAT THIS REPLACES, AND WHY THE OLD SHAPE WAS THE PROBLEM
---------------------------------------------------------
There were two of these and neither declared a dependency. `verbs._Session` was a class
doing `self.linker = linker or LinkerClient()`, and `collections._session()` was a
function returning the same three defaults as a tuple. The `or Concrete()` is the whole
defect: it makes the parameter look injectable while guaranteeing the application layer
imports the adapter, so

  * `application` could never sit below `infrastructure`, because it named it, and
  * a test could only substitute a fake by rewriting the MODULE GLOBAL --
    `monkeypatch.setattr(f"{module}.CookjohnClient", ...)`, once per consuming module,
    with `raising=False` so a typo would fail silently.

Now the ports are required. There is no default, so there is nothing to import and
nothing to patch: a caller that wants the real thing asks `interfaces/factory.py` for it,
and a caller that wants a fake passes a fake.

`frozen=True` because a session is a description of one operation's collaborators, not a
place to stash state mid-verb.
"""

from __future__ import annotations

from dataclasses import dataclass

from zotero_core.domain.ports.catalogue import Catalogue
from zotero_core.domain.ports.collection_catalogue import CollectionCatalogue
from zotero_core.domain.ports.duplicates import DuplicateFinder
from zotero_core.domain.ports.journal import Journal
from zotero_core.domain.ports.paper_resolver import PaperResolver
from zotero_core.domain.ports.write_transport import Cookjohn, Linker
from zotero_core.domain.ports.zotero_probe import ZoteroProbe


@dataclass(frozen=True)
class WriteSession:
    """The transports, the catalogue, the journal and the probe for one operation."""

    linker: Linker
    cookjohn: Cookjohn
    store: Catalogue
    collections: CollectionCatalogue
    journal: Journal
    probe: ZoteroProbe
    duplicates: DuplicateFinder
    papers: PaperResolver

    def require(self, *needs: str) -> dict:
        # Imported here rather than at module scope: `liveness` imports nothing from this
        # module, but keeping the edge out of the import graph entirely is what lets the
        # spine contract order these two without a cycle.
        from zotero_core.application.services.liveness import require_zotero

        return require_zotero(
            needs=needs, linker=self.linker, cookjohn=self.cookjohn, probe=self.probe
        )
