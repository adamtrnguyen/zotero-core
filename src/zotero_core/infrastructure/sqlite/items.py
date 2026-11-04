"""Read-only item-state lookups: existence, trash state, type, parents, children.

Exists to serve the WRITE gate in `application/services/` without putting any SQL
there. Every precondition a write needs is a read -- does this key resolve, is it
already in the trash, what type is it, what hangs off it -- and ZoteroSuite's rule
is that consumers do not duplicate Zotero SQL (README, "Rules of the road"). So
the reads live here, in the canonical read layer, and `zotero_core.application` reaches
Zotero through exactly one channel: the linker plugin's HTTP endpoint.

core stays READ-ONLY. Nothing here opens the database read-write.

WHICH READ MODE SERVED THE ANSWER
---------------------------------
It is reported rather than hidden, because the two modes do not answer the same
question. `mode=ro` is the honest read: it respects locking and plays back a hot
rollback journal. But Zotero holds the database locked while it runs, and the
write path REQUIRES Zotero to be running (that precondition inverts relative to
Calibre, which requires its GUI closed) -- so in normal operation `mode=ro` raises
and the `immutable=1` fallback is what actually serves the row. Measured
2026-08-13 against the live 329 MB database with Zotero 9.0.6 up:

    mode=ro        FAIL OperationalError: database is locked
    immutable=1    OK  deletedItems=277 items=3405

`immutable=1` tells SQLite the file cannot change, so it skips locking and skips
hot-journal playback: a point-in-time view of the main file's pages. That is fine
for a precondition -- a stale read blocks a write that would have been fine, which
is the safe direction -- but it is NOT authority for "the write did not land".
`ItemStates.read_mode` travels with the answer so the write path can tell a
verified result from a snapshot that may simply be behind.

LIBRARY SCOPE
-------------
Every lookup is scoped to ONE library, default the user library (libraryID 1,
confirmed `type='user'` in `libraries`). This matches
`Zotero.Items.getByLibraryAndKey(Zotero.Libraries.userLibraryID, key)`, which is
how `linker/bootstrap.js` resolves the same keys. Unscoped lookups would make the
precheck and the plugin disagree about which keys exist -- this database has six
group libraries as well.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from zotero_core.domain.entities.models import (
    ItemState,
    ItemStates,
    TagUsage,
    TrashedItem,
    ZoteroAttachment,
    ZoteroAttachments,
)
from zotero_core.domain.read_mode import ReadMode
from zotero_core.infrastructure.sqlite.annotations import DEFAULT_ZOTERO_DB
from zotero_core.infrastructure.sqlite.connect import (
    DEFAULT_BUSY_TIMEOUT_MS,
    USER_LIBRARY_ID,
    open_readonly,
)


class ZoteroItemStore:
    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        library_id: int = USER_LIBRARY_ID,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ):
        # Resolved at CALL time rather than as a default argument. `db_path=
        # DEFAULT_ZOTERO_DB` in the signature binds the constant when the `def`
        # executes, so a test redirecting the constant to keep itself off the REAL
        # library would silently still open the real library.
        self.db_path = Path(db_path or DEFAULT_ZOTERO_DB).expanduser()
        self.library_id = library_id
        self.busy_timeout_ms = busy_timeout_ms

    def item_states(self, keys: list[str] | tuple[str, ...]) -> ItemStates:
        """Look up every key at once. Keys that do not resolve come back exists=False.

        A key absent from this library is reported, not omitted: the caller has to
        be able to tell "you asked about 5 keys and 2 are not here" from "you asked
        about 3 keys".
        """
        wanted = list(dict.fromkeys(keys))
        if not wanted:
            return ItemStates(states={}, read_mode=ReadMode.NONE)

        conn, mode = self._connect()
        try:
            placeholders = ",".join("?" * len(wanted))
            rows = conn.execute(
                f"""
                SELECT i.key,
                       it.typeName,
                       COALESCE(idv.value, n.title, ''),
                       parent.key,
                       (SELECT COUNT(*) FROM collectionItems ci WHERE ci.itemID = i.itemID),
                       (SELECT COUNT(*) FROM deletedItems d WHERE d.itemID = i.itemID)
                  FROM items i
                  JOIN itemTypes it ON it.itemTypeID = i.itemTypeID
             LEFT JOIN itemAttachments a ON a.itemID = i.itemID
             LEFT JOIN itemNotes n ON n.itemID = i.itemID
             LEFT JOIN itemAnnotations ann ON ann.itemID = i.itemID
             LEFT JOIN items parent
                    ON parent.itemID = COALESCE(a.parentItemID, n.parentItemID, ann.parentItemID)
             LEFT JOIN fields f ON f.fieldName = 'title'
             LEFT JOIN itemData idt ON idt.itemID = i.itemID AND idt.fieldID = f.fieldID
             LEFT JOIN itemDataValues idv ON idv.valueID = idt.valueID
                 WHERE i.libraryID = ? AND i.key IN ({placeholders})
                """,
                (self.library_id, *wanted),
            ).fetchall()
            children = self._children(conn, wanted)
        finally:
            conn.close()

        found = {
            row[0]: ItemState(
                key=row[0],
                exists=True,
                trashed=bool(row[5]),
                item_type=row[1] or "",
                title=row[2] or "",
                parent_key=row[3],
                child_keys=tuple(children.get(row[0], ())),
                collection_count=int(row[4] or 0),
            )
            for row in rows
        }
        return ItemStates(
            states={k: found.get(k, ItemState(key=k, exists=False, trashed=False)) for k in wanted},
            read_mode=mode,
        )

    def _children(self, conn: sqlite3.Connection, keys: list[str]) -> dict[str, list[str]]:
        """Attachments and notes filed under each key.

        Two SELECTs UNIONed rather than one join: `itemAttachments` and `itemNotes`
        are separate tables with separate parentItemID columns, and joining both to
        `items` in one pass fans the rows out.
        """
        placeholders = ",".join("?" * len(keys))
        rows = conn.execute(
            f"""
            SELECT p.key, c.key
              FROM itemAttachments x
              JOIN items c ON c.itemID = x.itemID
              JOIN items p ON p.itemID = x.parentItemID
             WHERE p.libraryID = ? AND p.key IN ({placeholders})
            UNION ALL
            SELECT p.key, c.key
              FROM itemNotes n
              JOIN items c ON c.itemID = n.itemID
              JOIN items p ON p.itemID = n.parentItemID
             WHERE p.libraryID = ? AND p.key IN ({placeholders})
            """,
            (self.library_id, *keys, self.library_id, *keys),
        ).fetchall()
        out: dict[str, list[str]] = {}
        for parent_key, child_key in rows:
            out.setdefault(parent_key, []).append(child_key)
        return out

    def trashed_count(self) -> tuple[int, str]:
        """How many items are in THIS library's trash, and the read mode that served it.

        ⚠ WAS `SELECT COUNT(*) FROM deletedItems` with no library scope, while this file's
        own docstring three methods down says "Scoped to `library_id` like every other read
        here". It was the single unscoped read in the package: on a machine with group
        libraries it answered about all seven while every sibling answered about one, so
        comparing it against any other count compared two different populations.
        """
        conn, read_mode = self._connect()
        try:
            count = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM deletedItems d
                    JOIN items i ON i.itemID = d.itemID
                    WHERE i.libraryID = ?
                    """,
                    (self.library_id,),
                ).fetchone()[0]
            )
        finally:
            conn.close()
        return count, read_mode

    def trashed_items(self) -> tuple[tuple[TrashedItem, ...], str]:
        """WHAT is in the trash, not just how many -- with the date each was deleted.

        New capability rather than a repair. Trashed rows are excluded from `search.items`
        by SQL and belong to no collection, so before this there was no way to name them:
        measured, 22 of 25 were unreachable through the public surface, and none could be
        dated. "Clear the 2024 trash" was not a question this package could answer.
        """
        conn, read_mode = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT i.key,
                       COALESCE(idv.value, ''),
                       it.typeName,
                       d.dateDeleted
                FROM deletedItems d
                JOIN items i ON i.itemID = d.itemID
                JOIN itemTypes it ON it.itemTypeID = i.itemTypeID
                LEFT JOIN itemData id
                       ON id.itemID = i.itemID
                      AND id.fieldID = (SELECT fieldID FROM fields WHERE fieldName = 'title')
                LEFT JOIN itemDataValues idv ON idv.valueID = id.valueID
                WHERE i.libraryID = ?
                ORDER BY d.dateDeleted DESC
                """,
                (self.library_id,),
            ).fetchall()
        finally:
            conn.close()
        return (
            tuple(
                TrashedItem(
                    key=row[0],
                    title=row[1] or "(no title)",
                    item_type=row[2] or "",
                    date_deleted=row[3] or "",
                )
                for row in rows
            ),
            read_mode,
        )

    # ----------------------------------------------------------------------
    # prior state, for writes that overwrite rather than add
    #
    # Three of cookjohn's write tools REPLACE a whole collection of values rather
    # than merging: `write_tag` action='set', and `write_metadata`'s `creators`
    # array. calibre-core has the scar from exactly this shape -- a plain
    # identifier write replaced the whole set and silently deleted book 256's
    # `zotero` identifier, which was the link to its Zotero item. Nothing in the
    # output announced the loss. So a caller about to replace has to be able to see
    # what is there first, and the write path has to be able to record it.
    # ----------------------------------------------------------------------

    def item_tags(self, keys: list[str] | tuple[str, ...]) -> dict[str, tuple[str, ...]]:
        """Tags per item key, sorted. Keys with no tags map to an empty tuple."""
        wanted = list(dict.fromkeys(keys))
        if not wanted:
            return {}
        conn, _ = self._connect()
        try:
            placeholders = ",".join("?" * len(wanted))
            rows = conn.execute(
                f"""
                SELECT i.key, t.name
                  FROM items i
                  JOIN itemTags it ON it.itemID = i.itemID
                  JOIN tags t ON t.tagID = it.tagID
                 WHERE i.libraryID = ? AND i.key IN ({placeholders})
                """,
                (self.library_id, *wanted),
            ).fetchall()
        finally:
            conn.close()
        out: dict[str, list[str]] = {k: [] for k in wanted}
        for key, name in rows:
            out[key].append(name)
        return {k: tuple(sorted(v)) for k, v in out.items()}

    def all_tags(self) -> tuple[tuple[TagUsage, ...], str]:
        """Every tag in this library with the number of items carrying it.

        Scoped to `library_id` like every other read here -- an unscoped version would mix
        a group library's vocabulary into the personal one, which is exactly the shape of
        error tag hygiene is trying to find.

        Sorted by descending count then name: the question this answers is almost always
        "which tags matter", and a 245-item tag and a 1-item tag are not the same finding.
        """
        conn, read_mode = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT t.name, COUNT(it.itemID) AS n
                  FROM tags t
                  JOIN itemTags it ON it.tagID = t.tagID
                  JOIN items i ON i.itemID = it.itemID
                 WHERE i.libraryID = ?
                 GROUP BY t.tagID
                 ORDER BY n DESC, t.name COLLATE NOCASE
                """,
                (self.library_id,),
            ).fetchall()
        finally:
            conn.close()
        return tuple(TagUsage(name=r[0], item_count=int(r[1])) for r in rows), read_mode

    def items_with_tag(self, name: str) -> tuple[tuple[str, ...], str]:
        """Item keys carrying EXACTLY this tag name, case-sensitively.

        Case-SENSITIVE on purpose, and it is the whole point: `art` and `Art` are two
        different rows in `tags`, and a merge has to address them separately. A
        case-insensitive match here would make the two indistinguishable and the merge
        impossible to target.
        """
        conn, read_mode = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT i.key
                  FROM items i
                  JOIN itemTags it ON it.itemID = i.itemID
                  JOIN tags t ON t.tagID = it.tagID
                 WHERE i.libraryID = ? AND t.name = ?
                 ORDER BY i.key
                """,
                (self.library_id, name),
            ).fetchall()
        finally:
            conn.close()
        return tuple(r[0] for r in rows), read_mode

    def item_fields(self, key: str) -> dict[str, str]:
        """Every populated metadata field on one item, as {fieldName: value}."""
        conn, _ = self._connect()
        try:
            return dict(
                conn.execute(
                    """
                    SELECT f.fieldName, v.value
                      FROM items i
                      JOIN itemData d ON d.itemID = i.itemID
                      JOIN fields f ON f.fieldID = d.fieldID
                      JOIN itemDataValues v ON v.valueID = d.valueID
                     WHERE i.libraryID = ? AND i.key = ?
                    """,
                    (self.library_id, key),
                )
            )
        finally:
            conn.close()

    def pdf_attachments(
        self,
        limit: int | None = None,
        *,
        storage_dir: Path | None = None,
    ) -> ZoteroAttachments:
        """Every stored PDF attachment in the library, with its parent's title and collection.

        This is the enumeration a corpus builder needs: `(key, path, title, collection)` per PDF.
        It lives here because ZoteroSuite's rule is that consumers do not duplicate Zotero SQL
        (README, "Rules of the road") -- omni-rag carried its own copy of this query in a script
        called `prototype_zotero.py` for months, which is how `omnirag papers ingest` ended up
        existing with no enumerator behind it at all.

        The PATH is resolved by globbing the storage folder, NOT by parsing `itemAttachments.path`.
        The stored value is `storage:<filename>`, and the filename on disk can diverge from it;
        the folder name is the attachment key and is authoritative. An attachment whose folder
        holds no PDF is skipped rather than returned with a path that does not exist.

        `title` falls back to the file's stem when the parent has none, and is **never
        truncated** -- a truncated title silently merges distinct works under one label, which
        is a real defect class in a consumer keying rows on a display string.

        ⚠ Scoped to `library_id` like every other read here, so a group library's PDFs are not
        enumerated into a user-library corpus.

        ⚠ A paper in SEVERAL collections reports the first by NAME, and the `ORDER BY` is
        load-bearing. A bare `LIMIT 1` returns whichever row the query plan reaches first, so the
        answer is arbitrary and can change under a schema or plan change with no data change at
        all -- and a consumer keying a filter on it (omnirag's `--field`) would see a paper
        silently move between filters on re-ingest. Measured against the live library: three
        collections present in omni-rag's index (`Cranberry Lab`, `Energy Lab`, `ENGL 1105`) are
        no longer what the unordered pick returns for those papers. Alphabetical is arbitrary too,
        but it is STABLE, which is the property that matters.

        ⚠ Returns duplicates as-is. Zotero legitimately holds the same paper under two attachment
        keys, and which copy to keep is the CONSUMER's policy (title, date, file size), not a read
        layer's. Dedupe downstream.

        ⚠ Excludes an attachment in the trash, but NOT one whose PARENT is trashed -- matching the
        query this replaced, so the enumeration does not change under consumers mid-migration.
        Tightening it to the parent is a deliberate change, with a re-ingest behind it.
        """
        storage = storage_dir or (self.db_path.parent / "storage")
        conn, mode = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT a.key,
                       (SELECT v.value
                          FROM itemData d
                          JOIN fields f ON f.fieldID = d.fieldID
                          JOIN itemDataValues v ON v.valueID = d.valueID
                         WHERE d.itemID = ia.parentItemID AND f.fieldName = 'title'),
                       (SELECT c.collectionName
                          FROM collectionItems ci
                          JOIN collections c ON c.collectionID = ci.collectionID
                         WHERE ci.itemID = ia.parentItemID
                         ORDER BY c.collectionName
                         LIMIT 1)
                  FROM itemAttachments ia
                  JOIN items a ON a.itemID = ia.itemID
                 WHERE a.libraryID = ?
                   AND ia.contentType = 'application/pdf'
                   AND ia.path LIKE 'storage:%'
                   AND ia.itemID NOT IN (SELECT itemID FROM deletedItems)
                 ORDER BY a.key
                """,
                (self.library_id,),
            ).fetchall()
        finally:
            conn.close()

        out: list[ZoteroAttachment] = []
        for key, title, collection in rows:
            pdfs = sorted((storage / key).glob("*.pdf"))
            if not pdfs:
                continue
            name = (title or pdfs[0].stem).strip().replace("\n", " ")
            out.append(
                ZoteroAttachment(
                    attachment_key=key,
                    path=pdfs[0],
                    title=name,
                    collection=(collection or None),
                )
            )
            if limit is not None and len(out) >= limit:
                break
        return ZoteroAttachments(items=tuple(out), read_mode=mode)

    def base_field_map(self, item_type: str) -> dict[str, str]:
        """{baseFieldName: actualFieldName} for one item type. Empty when it maps nothing.

        Zotero stores a handful of fields under a name that depends on the item type,
        with a shared BASE name used to address them generically. `publicationTitle` is
        the one that bites: on a `conferencePaper` the value lives in
        `proceedingsTitle`, on a `bookSection` in `bookTitle`, on a `webpage` in
        `websiteTitle` -- ten item types in the live library map that base alone
        (measured 2026-08-13). A write addressed to the base name lands in the mapped
        field, so reading the base name back finds nothing and a verifier comparing
        written names against stored names calls a successful write unverified.

        Read out of `baseFieldMappingsCombined` rather than hard-coded, for the reason
        the annotation reads already follow: the mapping is the database's own data and
        it moves with Zotero's schema version. A table this method invents would be a
        second answer that drifts.
        """
        conn, _ = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT base.fieldName, actual.fieldName
                  FROM baseFieldMappingsCombined m
                  JOIN itemTypes t ON t.itemTypeID = m.itemTypeID
                  JOIN fields base ON base.fieldID = m.baseFieldID
                  JOIN fields actual ON actual.fieldID = m.fieldID
                 WHERE t.typeName = ?
                """,
                (item_type,),
            ).fetchall()
        except sqlite3.OperationalError:
            # The table is absent. Returning {} degrades to comparing names literally,
            # which is what this method exists to improve on -- so the caller loses the
            # improvement and nothing else. Raising would turn a SUCCESSFUL metadata
            # write into an exception on its verification step, which is the one outcome
            # worse than an unhelpful verdict.
            return {}
        finally:
            conn.close()
        return dict(rows)

    def item_creators(self, key: str) -> tuple[dict[str, str], ...]:
        """Creators on one item, IN ORDER.

        Order is part of the data -- first author is not an arbitrary member of a
        set -- so this returns a tuple ordered by `itemCreators.orderIndex` rather
        than a dict. `fieldMode = 1` is Zotero's single-field name (organisations),
        which is why an institutional author comes back as `name` rather than a
        split first/last.
        """
        conn, _ = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT ct.creatorType, c.firstName, c.lastName, c.fieldMode
                  FROM items i
                  JOIN itemCreators ic ON ic.itemID = i.itemID
                  JOIN creators c ON c.creatorID = ic.creatorID
                  JOIN creatorTypes ct ON ct.creatorTypeID = ic.creatorTypeID
                 WHERE i.libraryID = ? AND i.key = ?
                 ORDER BY ic.orderIndex
                """,
                (self.library_id, key),
            ).fetchall()
        finally:
            conn.close()
        creators = []
        for creator_type, first, last, field_mode in rows:
            if field_mode == 1:
                creators.append({"creatorType": creator_type, "name": last or ""})
            else:
                creators.append(
                    {
                        "creatorType": creator_type,
                        "firstName": first or "",
                        "lastName": last or "",
                    }
                )
        return tuple(creators)

    def attachment_info(self, attachment_key: str, *, base_path: str | Path | None = None) -> dict:
        """Where one attachment's file actually is, and whether it is there.

        Exists so a write can verify that a LINKED file resolves. That is not a
        hypothetical failure mode on this machine: 31 linked attachments currently point
        at Calibre directories that were renamed, so the items look like they have a PDF
        and open to an error. Nothing catches that at write time because nothing looked.

        linkMode: 0 imported file, 1 imported URL (both live under storage/<key>/),
        2 linked file, 3 linked URL (no file at all).

        ⚠ A linked path may be stored as `attachments:<relative>`, relative to Zotero's
        BASE DIRECTORY -- and that base is NOT in the database. Zotero keeps it in
        `prefs.js` in the profile, which this layer does not read. So a relative path
        cannot be resolved from sqlite alone, and reporting `file_exists: False` for one
        would be a FALSE NEGATIVE on the majority of this library's attachments (772 of
        1368 are linked). Pass `base_path` to resolve them; without it such a row comes
        back `file_exists: None` and `relative_to_base: True`, which is "I cannot tell"
        rather than "it is missing".
        """
        conn, read_mode = self._connect()
        try:
            row = conn.execute(
                """
                SELECT ia.linkMode, COALESCE(ia.path, ''), COALESCE(ia.contentType, '')
                  FROM itemAttachments ia
                  JOIN items i ON i.itemID = ia.itemID
                 WHERE i.key = ? AND i.libraryID = ?
                """,
                (attachment_key, self.library_id),
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            return {"exists": False, "read_mode": read_mode}

        link_mode, raw_path, content_type = row
        info: dict = {
            "exists": True,
            "read_mode": read_mode,
            "link_mode": link_mode,
            "stored": link_mode in (0, 1),
            "content_type": content_type,
            "raw_path": raw_path,
        }
        if link_mode == 3 or not raw_path:
            # A linked URL has no file, and neither does a row with an empty path.
            info["path"] = None
            info["file_exists"] = None
            return info

        if link_mode in (0, 1):
            # Imported: the file lives in storage/<key>/, and the stored path is
            # `storage:<filename>`. Glob rather than trust the name, matching
            # `pdf_attachments`.
            folder = self.db_path.parent / "storage" / attachment_key
            found = sorted(folder.glob("*")) if folder.is_dir() else []
            real = next((f for f in found if not f.name.startswith(".")), None)
            info["path"] = str(real) if real else None
            info["file_exists"] = real is not None
            return info

        path = raw_path
        if path.startswith("attachments:"):
            rel = path[len("attachments:") :]
            if base_path is None:
                info["path"] = rel
                info["relative_to_base"] = True
                info["file_exists"] = None
                info["note"] = (
                    "stored relative to Zotero's base directory, which lives in prefs.js "
                    "and not in the database — pass base_path to resolve it"
                )
                return info
            path = str(Path(base_path).expanduser() / rel)
        resolved = Path(path).expanduser()
        info["path"] = str(resolved)
        info["relative_to_base"] = False
        info["file_exists"] = resolved.exists()
        return info

    def _connect(self) -> tuple[sqlite3.Connection, ReadMode]:
        """Delegates to `connect.open_readonly`.

        Kept as a method because it is the seam tests inject through, and because
        `busy_timeout_ms` is per-store state. The logic moved out when `collections`
        and `search` needed the same opener -- `duplicates` was already reaching
        across for it, and a fourth caller would have entrenched a private as an
        informal public API.
        """
        return open_readonly(self.db_path, busy_timeout_ms=self.busy_timeout_ms)
