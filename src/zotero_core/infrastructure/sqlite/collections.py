"""Collection reads: the tree, membership, and the inverse.

WHY THIS DID NOT EXIST
----------------------
`zotero_core.infrastructure` owned every Zotero SQL query by charter and had NO collection queries
at all -- `grep parentCollectionID` over the package returned nothing, and the
`collections` table was joined in exactly one place, to turn an id into a display string
for a PDF's attachment row.

Collection WRITES were fully exposed the whole time (create, update, delete, file,
unfile). So an agent could create a collection and file items into it and then could not
read back what collections existed or what was in one. That asymmetry is why cookjohn's
third-party plugin stayed in the tool surface: it was the only answer to "what is in
collection X", which is precisely the "consumers duplicate Zotero SQL" outcome this
package exists to prevent.

TWO THINGS THAT BITE
--------------------
1. **Library scoping is not optional.** This machine has libraryIDs 1, 2, 4 and 5 --
   group libraries are real here, not hypothetical. An unscoped tree query silently
   mixes them, and the same collection NAME can exist in several. Every query below is
   scoped, matching `items.py`.
2. **A cycle makes collections VANISH; it does not hang.** This was worth getting
   right, because the intuitive fear is the wrong one. `parentCollectionID` is a
   SINGLE parent pointer, so a cycle (A's parent is B, B's parent is A) has no member
   with a NULL parent -- and the recursion is anchored on exactly that. The cycle is
   therefore unreachable from the anchor, the query terminates normally, and every
   collection inside the cycle is silently ABSENT from the tree.

   `MAX_DEPTH` is not a cycle guard, then; it bounds pathological DEPTH, and
   `CollectionTree.truncated` reports when it bit rather than quietly cutting the tree
   short. A test asserts the vanishing behaviour so it stays a known property instead
   of being rediscovered as "a collection disappeared".
"""

from __future__ import annotations

from pathlib import Path

from zotero_core.domain.entities.models import (
    CollectionMember,
    CollectionMembers,
    CollectionNode,
    CollectionTree,
)
from zotero_core.infrastructure.sqlite.annotations import DEFAULT_ZOTERO_DB
from zotero_core.infrastructure.sqlite.connect import (
    DEFAULT_BUSY_TIMEOUT_MS,
    USER_LIBRARY_ID,
    open_readonly,
)

# Deep enough that no real library reaches it (the deepest here is 3), shallow enough
# that a cycle terminates. Reported rather than silently truncated -- see `truncated`.
MAX_DEPTH = 32

PATH_SEPARATOR = " > "


_TREE_SQL = f"""
WITH RECURSIVE tree(collectionID, key, name, parentCollectionID, depth, path) AS (
    SELECT c.collectionID, c.key, c.collectionName, c.parentCollectionID, 0, c.collectionName
      FROM collections c
     WHERE c.parentCollectionID IS NULL AND c.libraryID = :library_id
    UNION ALL
    SELECT c.collectionID, c.key, c.collectionName, c.parentCollectionID,
           t.depth + 1, t.path || :sep || c.collectionName
      FROM collections c
      JOIN tree t ON c.parentCollectionID = t.collectionID
     WHERE c.libraryID = :library_id AND t.depth < {MAX_DEPTH}
)
SELECT t.collectionID, t.key, t.name, t.parentCollectionID, t.depth, t.path,
       (SELECT COUNT(*)
          FROM collectionItems ci
         WHERE ci.collectionID = t.collectionID
           AND ci.itemID NOT IN (SELECT itemID FROM deletedItems)) AS item_count
  FROM tree t
 ORDER BY t.path
"""

_MEMBERS_SQL = """
SELECT i.key,
       COALESCE(v.value, '') AS title,
       it.typeName,
       CASE WHEN d.itemID IS NULL THEN 0 ELSE 1 END AS trashed
  FROM collectionItems ci
  JOIN collections c ON c.collectionID = ci.collectionID
  JOIN items i ON i.itemID = ci.itemID
  JOIN itemTypes it ON it.itemTypeID = i.itemTypeID
  LEFT JOIN deletedItems d ON d.itemID = i.itemID
  LEFT JOIN itemData dat ON dat.itemID = i.itemID
       AND dat.fieldID = (SELECT fieldID FROM fields WHERE fieldName = 'title')
  LEFT JOIN itemDataValues v ON v.valueID = dat.valueID
 WHERE c.key = :collection_key AND c.libraryID = :library_id
 ORDER BY ci.orderIndex, title
"""

_INVERSE_SQL = """
SELECT i.key AS item_key, c.key AS collection_key, c.collectionName
  FROM items i
  JOIN collectionItems ci ON ci.itemID = i.itemID
  JOIN collections c ON c.collectionID = ci.collectionID
 WHERE i.libraryID = ? AND c.libraryID = ?
   AND i.key IN ({placeholders})
 ORDER BY c.collectionName
"""


class ZoteroCollectionStore:
    """Read-only collection access. Mirrors `ZoteroItemStore`'s shape deliberately."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        library_id: int = USER_LIBRARY_ID,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ):
        self.db_path = Path(db_path or DEFAULT_ZOTERO_DB).expanduser()
        self.library_id = library_id
        self.busy_timeout_ms = busy_timeout_ms

    def for_library(self, library_id: int) -> ZoteroCollectionStore:
        """A sibling reading the SAME database, scoped to another library.

        Satisfies the matching port. The read facade used to do this itself, constructing
        a concrete store from a stashed db path -- an application-layer facade building an
        adapter. Asking the current adapter for a sibling keeps "how do I make another one
        of me" with the class that knows, and returns `self` for the library it already reads.

        Cheap on purpose: constructing one holds paths and an int, and opens nothing.
        """
        if library_id == self.library_id:
            return self
        return ZoteroCollectionStore(
            self.db_path, library_id=library_id, busy_timeout_ms=self.busy_timeout_ms
        )

    def tree(self) -> CollectionTree:
        """The whole nested tree in ONE query, with breadcrumb paths and item counts.

        `item_count` excludes trashed items, because a collection showing 12 when the
        GUI shows 9 is a bug report waiting to happen -- trashing an item does not
        remove it from `collectionItems`.
        """
        conn, read_mode = open_readonly(self.db_path, busy_timeout_ms=self.busy_timeout_ms)
        try:
            rows = conn.execute(
                _TREE_SQL, {"library_id": self.library_id, "sep": PATH_SEPARATOR}
            ).fetchall()
        finally:
            conn.close()

        by_parent: dict[int | None, list[tuple]] = {}
        seen_depth = 0
        for row in rows:
            by_parent.setdefault(row[3], []).append(row)
            seen_depth = max(seen_depth, row[4])
        id_to_key = {row[0]: row[1] for row in rows}

        def build(parent_id: int | None) -> tuple[CollectionNode, ...]:
            return tuple(
                CollectionNode(
                    key=row[1],
                    name=row[2],
                    path=row[5],
                    depth=row[4],
                    parent_key=id_to_key.get(row[3]),
                    item_count=row[6],
                    subcollections=build(row[0]),
                )
                for row in by_parent.get(parent_id, [])
            )

        return CollectionTree(
            roots=build(None),
            read_mode=read_mode,
            truncated=seen_depth >= MAX_DEPTH,
        )

    def find(self, name: str) -> tuple[tuple[CollectionNode, ...], str]:
        """Collections whose name contains `name`, case-insensitively, AND the read mode.

        Substring, not fuzzy -- the fuzzy matcher belongs with the rest of search and is
        not worth a second implementation here.

        ⚠ RETURNS THE READ MODE, which it used to throw away. It walks `self.tree()`, which
        already carries one, so the information was in hand and dropped: `find_collections`
        was the only collection read whose envelope had no `read_mode`, while
        `collection_tree` returned one under the SAME `collections` key. A caller could not
        tell whether a `find` had been served live or from an `immutable=1` snapshot -- and
        under a snapshot, "no collection by that name" and "not visible yet" are the same
        answer. The `(rows, read_mode)` shape matches what `search`'s methods already return.
        """
        tree = self.tree()
        needle = name.strip().casefold()
        if not needle:
            return (), tree.read_mode
        found = tuple(node for node in tree.flat() if needle in node.name.casefold())
        return found, tree.read_mode

    def items(self, collection_key: str, *, include_trashed: bool = False) -> CollectionMembers:
        """What is IN a collection. Direct members only -- subcollections are separate.

        Zotero's GUI shows direct members too (unless "Show Items from Subcollections"
        is on), so this matches what a caller sees on screen. Walk `tree()` for the
        recursive version.
        """
        conn, read_mode = open_readonly(self.db_path, busy_timeout_ms=self.busy_timeout_ms)
        try:
            rows = conn.execute(
                _MEMBERS_SQL,
                {"collection_key": collection_key, "library_id": self.library_id},
            ).fetchall()
        finally:
            conn.close()

        members = tuple(
            CollectionMember(item_key=row[0], title=row[1], item_type=row[2], trashed=bool(row[3]))
            for row in rows
            if include_trashed or not row[3]
        )
        return CollectionMembers(
            collection_key=collection_key, members=members, read_mode=read_mode
        )

    def collections_of(
        self, item_keys: list[str] | tuple[str, ...]
    ) -> dict[str, tuple[dict[str, str], ...]]:
        """The INVERSE: which collections is each item filed in?

        New capability -- nothing answered this before, in this package or in cookjohn.
        Better BibTeX has a citekey-keyed version, which is not usable from an item key.
        An item can be in several collections, or none.
        """
        keys = [str(key) for key in item_keys if str(key)]
        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        sql = _INVERSE_SQL.format(placeholders=placeholders)
        conn, _mode = open_readonly(self.db_path, busy_timeout_ms=self.busy_timeout_ms)
        try:
            rows = conn.execute(sql, (self.library_id, self.library_id, *keys)).fetchall()
        finally:
            conn.close()

        out: dict[str, tuple[dict[str, str], ...]] = dict.fromkeys(keys, ())
        for item_key, collection_key, name in rows:
            out[item_key] = (*out[item_key], {"key": collection_key, "name": name})
        return out
