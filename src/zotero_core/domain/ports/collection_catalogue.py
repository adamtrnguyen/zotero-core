"""Reading the collection side of the catalogue, as the collection verbs need it.

Separate from `Catalogue` because the two are read by different things and the split is
already in the code: `Catalogue` is what every write gate uses (does this key exist, what
tags does it carry), while this is used only by the six collection verbs, to answer
"where does this collection sit" and "what is already in it".

A port, not a deferred import of the store: a function-level import is still a
dependency, just one that does not show at the module's head.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CollectionCatalogue(Protocol):
    """Read-only access to collections and their membership."""

    #: Which library this adapter reads. The read facade offers every collection call
    #: per-library by asking the current adapter for a sibling (`for_library`), which keeps
    #: construction with the code that knows how.
    library_id: int

    def for_library(self, library_id: int) -> CollectionCatalogue: ...

    def tree(self) -> Any: ...

    def items(self, collection_key: str, *, include_trashed: bool = False) -> Any: ...

    def find(self, name: str) -> tuple[tuple[Any, ...], str]: ...

    def collections_of(
        self, item_keys: list[str] | tuple[str, ...]
    ) -> dict[str, tuple[dict[str, str], ...]]: ...
