"""Opening zotero.sqlite read-only, and saying which mode answered.

ONE opener for the whole read layer. This logic lived as `ZoteroItemStore._connect`,
and `duplicates.check_duplicate` already reached across to call it -- the package's only
cross-module private access. `collections` and `search` would have been the third and
fourth, so it is a module now rather than a private that everyone pretends not to touch.

WHY THE MODE IS RETURNED
------------------------
Zotero uses a ROLLBACK JOURNAL, not WAL. With the app running, a plain `mode=ro` read
intermittently loses to the writer and fails with "database is locked (5)" -- observed
live, not theorised. `immutable=1` always answers, but it answers from a POINT-IN-TIME
snapshot: it tells sqlite the file cannot change, so the journal is ignored.

Those two are not interchangeable, and the difference is invisible in the rows. A caller
checking whether a write landed needs to know it was told by a snapshot, so every read in
this layer carries `read_mode` out to its caller rather than quietly picking one.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from zotero_core.domain.read_mode import ReadMode


# Zotero's own user library. Group libraries get their own ids; every query in this layer
# is scoped, because an unscoped one silently mixes a group library into a personal count.
class ZoteroReadError(RuntimeError):
    """The database could not be opened read-only, or is not where we were told.

    Defined HERE, in the module that raises it, rather than in `annotations.py` where it
    used to live. That location was an accident of history and it inverted the
    dependency: `connect` is the lowest read module and every other reader depends on
    it, so importing an exception upward from `annotations` made a cycle the moment
    `annotations` needed the shared opener.

    ⚠ THE `annotations.ZoteroAnnotationError` ALIAS IS GONE, and removing it fixed a
    live mislabel rather than just tidying a name. This docstring already admitted the
    alias "was always a misnomer: it is raised for 'cannot open the database', which has
    nothing to do with annotations" -- and then kept it because it was "not worth
    breaking". What that hid: `read_mcp._CODES` listed
    `(ZoteroAnnotationError, "annotation_read_failed")`, which READS as annotation-scoped
    but, being this class, matched every failure from all seven stores. Measured
    2026-08-19 against a missing database:

        collection_tree  -> ZoteroReadError -> code 'annotation_read_failed'
        list_libraries   -> ZoteroReadError -> code 'annotation_read_failed'

    An alias is not free when something branches on the type: it makes a wrong branch
    look right at the call site.
    """


USER_LIBRARY_ID = 1

DEFAULT_BUSY_TIMEOUT_MS = 5000

# The PROBE gets its own, much shorter timeout, and this is a performance fix worth
# understanding rather than a tuning knob.
#
# `busy_timeout` exists so a real query rides out a brief write burst. Applying it to the
# probe means the opposite: with Zotero running and holding the rollback-journal lock,
# `mode=ro` cannot succeed no matter how long we wait, so the probe blocks for the FULL
# timeout and only then falls back. Measured 2026-08-19 against the live library:
# 5,151 ms to open a connection, versus 19 ms for the query it was opening for. Every
# read in this package paid that whenever Zotero was open, which is nearly always.
#
# A short probe trades a slightly higher chance of choosing the `immutable=1` snapshot --
# reported honestly in `read_mode`, never hidden -- for a two-order-of-magnitude speedup.
PROBE_TIMEOUT_MS = 150


def open_readonly(
    db_path: str | Path,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> tuple[sqlite3.Connection, ReadMode]:
    """Open read-only, honest mode first. Returns the connection AND the mode that won."""
    path = Path(db_path)
    if not path.exists():
        raise ZoteroReadError(f"Zotero database not found: {path}")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        conn.execute(f"PRAGMA busy_timeout={int(PROBE_TIMEOUT_MS)}")
        # Touch a table: `connect` succeeds lazily, so the lock only surfaces on the
        # first real read. Without this the fallback never fires and the caller gets
        # "database is locked" from the middle of a query instead.
        conn.execute("SELECT 1 FROM items LIMIT 1").fetchone()
        # The probe won, so this really is a live read -- give the caller the full
        # timeout for the queries that follow, which is what it was always meant for.
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        return conn, ReadMode.LIVE
    except sqlite3.Error:
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True, timeout=5.0)
            conn.execute("SELECT 1 FROM items LIMIT 1").fetchone()
            return conn, ReadMode.SNAPSHOT
        except sqlite3.Error as exc:
            raise ZoteroReadError(f"Could not open Zotero database read-only: {exc}") from exc
