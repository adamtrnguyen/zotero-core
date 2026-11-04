"""The read the write path needs before it is allowed to write.

Every precondition a verb checks is a read — does the key resolve, is it already trashed,
what tags are there to be replaced, which base field does this item type map to. This
port is exactly that set and nothing else: six methods and two attributes, taken from
every `store.*` reference in the write layer rather than from `ZoteroItemStore`'s full
surface, which is larger and serves the read side too.

Narrower than the class that implements it, on purpose. A port copied from an
implementation is just the implementation with extra steps; this one states what the
application actually depends on, so widening it is a visible decision.

`pdf_attachments`, `trashed_count` and `trashed_items` are read by the READ facade, not by
a write gate. They sit here rather than in a second item port because two ports over one
class, split only by which consumer calls them, is bookkeeping rather than design.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Catalogue(Protocol):
    """Read-only access to the Zotero catalogue, as the write gates use it."""

    #: Where the database is — `copy_database` needs it for the pre-write backup.
    #: `Path`, NOT `str | Path`: a Protocol ATTRIBUTE is invariant, so the wider union
    #: would not be satisfied by an implementation offering only `Path`.
    db_path: Path
    busy_timeout_ms: int

    def item_states(self, keys: list[str] | tuple[str, ...]) -> Any: ...

    def item_fields(self, key: str) -> dict[str, str]: ...

    def item_creators(self, key: str) -> tuple[dict[str, str], ...]: ...

    def item_tags(self, keys: list[str] | tuple[str, ...]) -> dict[str, tuple[str, ...]]: ...

    def base_field_map(self, item_type: str) -> dict[str, str]: ...

    def attachment_info(
        self, attachment_key: str, *, base_path: str | Path | None = None
    ) -> dict: ...

    def pdf_attachments(
        self, limit: int | None = None, *, storage_dir: Path | None = None
    ) -> Any: ...

    def trashed_count(self) -> tuple[int, str]: ...

    def trashed_items(self) -> tuple[tuple[Any, ...], str]: ...

    def all_tags(self) -> tuple[tuple[Any, ...], str]: ...

    def items_with_tag(self, name: str) -> tuple[tuple[str, ...], str]: ...
