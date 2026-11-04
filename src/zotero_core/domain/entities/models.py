"""Zotero nouns, as read out of zotero.sqlite.

All joins or projections rather than raw rows. None has a `from_bridge` — nothing here
comes off the wire; the bridge DTOs live next door in `gui.py`.

⚠ TWELVE OF THESE WERE DEFINED INSIDE THE SQL LAYER. `ItemState`, `Library`, `ItemHit`
and the rest sat in `read/items.py`, `read/libraries.py`, `read/search.py` and
`read/collections.py` — so the package's actual Zotero nouns were declared by the module
that queries for them, while `domain/entities.py` held only the six bridge DTOs that
model an HTTP response. That is what made `domain/` a shared-utilities bucket rather than
a domain: the entities were somewhere else.

Moving them costs consumers nothing because tests and `omni-rag` couple to these types
STRUCTURALLY — by attribute and dict key, never by import path — and the four public ones
(`ItemState`, `ItemStates`, `ZoteroAttachment`, `ZoteroAttachments`) still resolve from
`zotero_core` unchanged.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from zotero_core.domain.read_mode import ReadMode


@dataclass(frozen=True)
class ZoteroSource:
    parent_key: str
    attachment_key: str
    title: str
    authors: str
    annotation_count: int
    citekey: str = ""
    #: What the annotated attachment IS. Added 2026-08-20 because the query that builds this
    #: used to filter to `application/pdf` and silently dropped every annotated HTML snapshot
    #: — carrying the type instead of filtering on it lets a caller narrow without the reader
    #: deciding for them.
    content_type: str = ""


@dataclass(frozen=True)
class Annotation:
    key: str
    attachment_key: str
    parent_key: str | None
    type: str
    page_label: str
    color: str
    text: str
    comment: str
    sort_index: str
    zotero_url: str


def to_jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {k: to_jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, list):
        return [to_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    return value


@dataclass(frozen=True)
class ItemState:
    """One item as the write gate needs to see it."""

    key: str
    exists: bool
    trashed: bool
    item_type: str = ""
    title: str = ""
    parent_key: str | None = None
    child_keys: tuple[str, ...] = ()
    # Trashing does not change collection membership, so this is recorded before a
    # write purely so a restore can be checked against it rather than assumed.
    collection_count: int = 0


@dataclass(frozen=True)
class ItemStates:
    """A batch of lookups, plus which read mode produced them."""

    states: dict[str, ItemState]
    read_mode: ReadMode

    def __getitem__(self, key: str) -> ItemState:
        return self.states[key]

    def get(self, key: str) -> ItemState | None:
        return self.states.get(key)

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(k for k, s in self.states.items() if not s.exists)

    @property
    def trashed(self) -> tuple[str, ...]:
        return tuple(k for k, s in self.states.items() if s.exists and s.trashed)

    @property
    def live(self) -> tuple[str, ...]:
        return tuple(k for k, s in self.states.items() if s.exists and not s.trashed)


@dataclass(frozen=True)
class ZoteroAttachment:
    """One stored PDF attachment, as a corpus builder needs to see it."""

    attachment_key: str  # == the storage folder name == the `zotero://open-pdf` deep-link key
    path: Path
    title: str  # the PARENT item's title, else the file's stem
    collection: str | None  # first collection the parent is filed under; None = filed nowhere


@dataclass(frozen=True)
class ZoteroAttachments:
    """An enumeration, plus which read mode produced it.

    `read_mode` travels for the same reason it does on `ItemStates`, and it matters MORE here: an
    `immutable=1` snapshot can be missing a paper added seconds ago, so a caller comparing this
    against its own index would see a phantom deletion. A short enumeration is a stale read, not an
    emptied library.
    """

    items: tuple[ZoteroAttachment, ...]
    read_mode: ReadMode

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self):
        return iter(self.items)


@dataclass(frozen=True)
class CollectionNode:
    """One collection. `path` is the breadcrumb, which is what makes a tree readable."""

    key: str
    name: str
    path: str
    depth: int
    parent_key: str | None
    item_count: int
    subcollections: tuple[CollectionNode, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CollectionTree:
    roots: tuple[CollectionNode, ...]
    read_mode: ReadMode
    truncated: bool = False

    def flat(self) -> tuple[CollectionNode, ...]:
        out: list[CollectionNode] = []

        def walk(nodes: tuple[CollectionNode, ...]) -> None:
            for node in nodes:
                out.append(node)
                walk(node.subcollections)

        walk(self.roots)
        return tuple(out)


@dataclass(frozen=True)
class CollectionMember:
    item_key: str
    title: str
    item_type: str
    trashed: bool


@dataclass(frozen=True)
class CollectionMembers:
    collection_key: str
    members: tuple[CollectionMember, ...]
    read_mode: ReadMode

    def __len__(self) -> int:
        return len(self.members)


@dataclass(frozen=True)
class ItemHit:
    item_key: str
    title: str
    item_type: str
    creators: str
    score: float
    matched_on: str


@dataclass(frozen=True)
class AnnotationHit:
    annotation_key: str
    attachment_key: str
    parent_key: str
    parent_title: str
    annotation_type: str
    text: str
    comment: str
    color: str
    page_label: str


@dataclass(frozen=True)
class FulltextHit:
    attachment_key: str
    parent_key: str
    title: str
    snippets: tuple[str, ...]
    match_count: int


@dataclass(frozen=True)
class Library:
    library_id: int
    library_type: str
    name: str
    editable: bool
    item_count: int
    collection_count: int


@dataclass(frozen=True)
class TrashedItem:
    """One item in the trash, with the date it was put there.

    ⚠ THE FIRST TIMESTAMP THIS PACKAGE READS. `grep -rn "dateAdded|dateModified|dateDeleted"`
    over `src/` returned zero hits before this: `deletedItems` was only ever COUNTED or used
    as a `NOT IN` filter, so "which of these did I trash in 2024" was not a question the
    package could answer — and 22 of the 25 trashed items could not even be named, because
    trashed rows are excluded from `search.items` and belong to no collection.

    `date_deleted` is Zotero's own string (`YYYY-MM-DD HH:MM:SS`, UTC), passed through rather
    than parsed. Parsing it here would invent a timezone the database does not state.
    """

    key: str
    title: str
    item_type: str
    date_deleted: str


@dataclass(frozen=True)
class TagUsage:
    """One tag name and how many items in this library carry it.

    Exists because "which tags does the library have" had no reader at all: `item_tags`
    answers per-item, so enumerating the vocabulary meant walking every item or writing
    SQL outside this package — the thing the suite rule forbids. Tag hygiene (case
    collisions, near-duplicates) is unanswerable without it.
    """

    name: str
    item_count: int
