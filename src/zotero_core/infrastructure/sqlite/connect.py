"""Opening zotero.sqlite read-only, and saying which mode answered.

ONE opener for the whole read layer: every store opens through `open_readonly`, so every
read reports the mode that served it.

WHY THE MODE IS RETURNED
------------------------
`zotero.sqlite` is WAL-mode (header bytes 18-19 are 2; `zotero.sqlite-wal` sits beside
it). With the app running, a plain `mode=ro` read can lose to Zotero's lock and fail with
"database is locked". `immutable=1` always answers, but from a SNAPSHOT: it tells sqlite
the file cannot change, so the WAL is ignored and commits not yet checkpointed into the
main file are missing.

Those two are not interchangeable, and the difference is invisible in the rows. A caller
checking whether a write landed needs to know it was told by a snapshot, so every read in
this layer carries `read_mode` out to its caller rather than quietly picking one.
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

from zotero_core.domain.read_mode import ReadMode


# Zotero's own user library. Group libraries get their own ids; every query in this layer
# is scoped, because an unscoped one silently mixes a group library into a personal count.
class ZoteroReadError(RuntimeError):
    """The database could not be opened read-only, or is not where we were told.

    Defined HERE, in the lowest read module, because every other reader depends on this
    one; defining it in a reader above would make a cycle. Raised by every store, so its
    MCP error code is store-neutral (`database_unavailable` in `read_mcp._CODES`), and it has no
    alias: an alias of an exception something branches on makes a wrong branch look right.
    History: docs/history/sqlite-reads.md.
    """


USER_LIBRARY_ID = 1

DEFAULT_BUSY_TIMEOUT_MS = 5000

# The PROBE gets its own, much shorter timeout, and this is a performance fix worth
# understanding rather than a tuning knob.
#
# `busy_timeout` exists so a real query rides out a brief write burst. Applying it to the
# probe means the opposite: with Zotero running and holding its lock, `mode=ro` cannot
# succeed no matter how long we wait, so the probe would block for the FULL timeout and
# only then fall back -- seconds per open for a query that takes milliseconds. The
# measurement: docs/history/sqlite-reads.md.
#
# A short probe trades a slightly higher chance of choosing the `immutable=1` snapshot --
# reported honestly in `read_mode`, never hidden -- for a two-order-of-magnitude speedup.
PROBE_TIMEOUT_MS = 150

#: How long `backup_database` waits on a busy source before falling back to a file copy.
BACKUP_TIMEOUT_S = 30.0


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


def backup_database(db_path: Path, dest: Path) -> tuple[str, dict[str, str]]:
    """Copy the database to `dest` as one coherent point in time, never opening it for write.

    `sqlite-backup` when a read-only connection can read: SQLite's backup API copies the
    committed state, WAL frames included, into one self-contained file. When Zotero's lock
    refuses even that, `file-copy`: the main file plus whichever of `-wal` / `-journal`
    exists, each copied to `dest` + the same suffix so opening `dest` replays it. A byte
    copy is coherent only if no write was in flight, which is why the method is reported.

    Returns `(method, {suffix: copied path})`.
    """
    path = Path(db_path)
    if not path.exists():
        raise ZoteroReadError(f"Zotero database not found: {path}")
    try:
        source = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        try:
            source.execute(f"PRAGMA busy_timeout={int(PROBE_TIMEOUT_MS)}")
            # Touch a table first: a lock surfaces here as an error, whereas `backup`
            # retries a busy source forever.
            source.execute("SELECT 1 FROM items LIMIT 1").fetchone()
            deadline = time.monotonic() + BACKUP_TIMEOUT_S

            def _give_up_when_late(_status, _remaining, _total) -> None:
                if time.monotonic() > deadline:
                    raise sqlite3.OperationalError("backup outlasted its deadline")

            target = sqlite3.connect(dest)
            try:
                source.backup(target, progress=_give_up_when_late)
            finally:
                target.close()
        finally:
            source.close()
        return "sqlite-backup", {}
    except sqlite3.Error:
        dest.unlink(missing_ok=True)
    shutil.copy2(path, dest)
    companions: dict[str, str] = {}
    for suffix in ("-wal", "-journal"):
        companion = path.with_name(path.name + suffix)
        if companion.exists():
            copied = dest.with_name(dest.name + suffix)
            shutil.copy2(companion, copied)
            companions[suffix] = str(copied)
    return "file-copy", companions
