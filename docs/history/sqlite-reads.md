# sqlite reads: read modes, scoping, and the reads that hid rows

Records moved out of `infrastructure/sqlite/*`, `domain/*` and `application/services/context.py`
(2026-11-04 history wave).

## 2026-08-13: `mode=ro` loses to a running Zotero

From `infrastructure/sqlite/items.py` and `application/services/verbs.py`. Measured against the
live 329 MB database with Zotero 9.0.6 up:

    mode=ro        FAIL OperationalError: database is locked
    immutable=1    OK  deletedItems=277 items=3405

The same day, every trash/restore round trip run against the real library read back
immediately, all through `immutable=1`: live evidence that the snapshot is fresh enough for a
read-back, though not authority for "the write did not land". Ten item types in the live
library mapped the `publicationTitle` base field alone.

(The live database is WAL-mode as of 2026-11-04 -- header bytes 18-19 are `02 02`, and
`zotero.sqlite-wal` / `-shm` sit beside it -- and about 15 MB. Docstrings that said "rollback
journal, not WAL" were written against the earlier measurement; see `journal.md`.)

## 2026-08-19: the probe timeout

From `infrastructure/sqlite/connect.py`. With Zotero running and holding its lock, `mode=ro`
cannot succeed however long it waits, so a probe using the full `busy_timeout` blocked for the
whole timeout before falling back. Measured against the live library: 5,151 ms to open a
connection, versus 19 ms for the query it was opening for. Every read paid that whenever Zotero
was open. `PROBE_TIMEOUT_MS = 150` trades a slightly higher chance of the snapshot for a
two-order-of-magnitude speedup.

## 2026-08-19: one opener, one error class

From `infrastructure/sqlite/connect.py` and `infrastructure/sqlite/annotations.py`.

- The opener lived as `ZoteroItemStore._connect`, and `duplicates.check_duplicate` already
  reached across to call it -- the package's only cross-module private access. `collections`
  and `search` would have been the third and fourth.
- `ZoteroReadError` lived in `annotations.py`, which inverted the dependency (`connect` is the
  lowest read module) and made a cycle once `annotations` needed the shared opener.
- The `annotations.ZoteroAnnotationError` alias was kept as "not worth breaking" while its own
  docstring admitted it "was always a misnomer". What that hid: `read_mcp._CODES` listed
  `(ZoteroAnnotationError, "annotation_read_failed")`, which matched every failure from all
  seven stores. Measured against a missing database:

        collection_tree  -> ZoteroReadError -> code 'annotation_read_failed'
        list_libraries   -> ZoteroReadError -> code 'annotation_read_failed'

  The code became `database_unavailable`, shared with `sqlite3.OperationalError`.
- `ZoteroAnnotationStore._connect` hardcoded `immutable=1` and probed nothing, the only reader
  not using `open_readonly`, while `read/__init__.py` claimed every read reports its mode: an
  annotation read could never be live even with Zotero closed.

## 2026-08-19: libraries

From `infrastructure/sqlite/libraries.py`. Measured: seven libraries, six of them groups, four
of those non-empty -- 109 items and 10 collections outside the user library, all old coursework
(ENGE1215_A4, D7 resources, A4_U2 Project, companionbot3000).

## 2026-08-20: reads that hid rows or dropped the read mode

- `get_sources_with_annotations` hid 5 of 18 sources (`infrastructure/sqlite/annotations.py`,
  `domain/entities/models.py`). The query started from `items parent` and required
  `att.contentType = 'application/pdf'`, dropping 4 annotated `text/html` snapshots (77
  annotations between them) and 1 STANDALONE PDF filtered out by the parent JOIN -- including
  the second half of a duplicated paper. `ZoteroSource.content_type` was added that day.
- `trashed_count` was `SELECT COUNT(*) FROM deletedItems` with no library scope, the single
  unscoped read in the package, answering about all seven libraries while every sibling
  answered about one. `trash_count` also returned `{"trashed": N}` with no `count` and no
  `read_mode`, while five of its seven sibling reads returned both.
- `find_collections` threw away the read mode it already had (from `self.tree()`), the only
  collection read whose envelope had none.
- `get_sources_with_annotations` returned a BARE LIST, which cannot carry a `read_mode`; the
  annotation store recorded it in `last_read_mode` and nothing outside the tests read it.
- `TrashedItem` was the first timestamp the package read. `grep -rn
  "dateAdded|dateModified|dateDeleted"` over `src/` returned zero hits before it:
  `deletedItems` was only counted or used as a `NOT IN` filter, and 22 of the 25 trashed items
  could not be named, because trashed rows are excluded from `search.items` and belong to no
  collection.
- `collection_tree` is the one envelope not built by `rows()`. Converting it silently changed
  `count` from 84 (all collections, `tree.flat()`) to 16 (top-level roots); caught because
  every envelope was captured before the refactor and compared after.
- `get_item` exists partly because reading creators required deliberately triggering a
  `refusing_to_replace` refusal and parsing `detail.current_creators` out of it.

## `pdf_attachments`

From `infrastructure/sqlite/items.py`. omni-rag carried its own copy of this query in a script
called `prototype_zotero.py` for months, which is how `omnirag papers ingest` existed with no
enumerator behind it. The collection pick was a bare `LIMIT 1`; measured against the live
library, three collections in omni-rag's index (`Cranberry Lab`, `Energy Lab`, `ENGL 1105`) were
no longer what the unordered pick returned for those papers, so it became `ORDER BY` name. The
trashed-parent exclusion was left as it was to match the query this replaced, so the
enumeration did not change under consumers mid-migration.

## Attachment files

From `infrastructure/sqlite/items.py` and `application/services/verbs.py`. 31 linked attachments
pointed at Calibre directories that had been renamed, so the items looked like they had a PDF
and opened to an error. 772 of 1368 attachments were linked, many base-relative, which is why
an unresolvable path is `file_exists: None` rather than a failure.
