"""Recording what a write is about to change, so it can be undone.

WHY THIS IS A MANIFEST AND NOT A DATABASE COPY
----------------------------------------------
calibre-core copies metadata.db before every write because a DB rollback is the only
undo that exists there. Neither half of that holds for Zotero:

  * Zotero already keeps rotating daily copies of `zotero.sqlite` beside it
    (`zotero.sqlite.bak`, `.1.bak`, ...; `ls -l ~/Zotero/zotero.sqlite*` for sizes).
    Copying it per call duplicates what already exists.
  * restoring such a copy is not an undo anyone can safely take: it requires closing
    Zotero and reverts every unrelated change since the copy was made.

So "back up before mutating" is honoured per-operation, by recording the state the
write is about to overwrite plus the call that reverses it. `copy_database` is still
here for a caller who wants the full snapshot, with the cost stated.

WHICH OPERATIONS GET A MANIFEST, AND WHY NOT ALL OF THEM
-------------------------------------------------------
Only the ones that overwrite or remove something. A `create` has no prior state --
journalling "nothing was here" would be filing noise, and it invites the habit of
skimming manifests. What gets one:

    trash / restore          the trash flag
    metadata update          the previous value of every field being written
    tag set / creator replace  the entire previous list (these REPLACE, not merge)
    collection delete/rename   the name, parent, and membership
    remove items from collection  the membership being removed

Creates and pure additions (`add_tags`, `add_items_to_collection`) do not, because
the inverse of an addition is a removal the caller can already express.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from zotero_core.infrastructure.sqlite.connect import backup_database

# Ephemeral on purpose. The manifest exists to undo a mistake noticed in the same
# sitting; the DURABLE undo for a trash is Zotero's own trash, which lives in the
# database until someone empties it. Same convention as calibre-core's backup dir.
DEFAULT_JOURNAL_DIR = "/tmp/zotero-write-journal"


def write_manifest(
    op: str,
    *,
    before: dict,
    inverse: str | None = None,
    journal_dir: str | None = None,
) -> str:
    """Write one manifest and return its path.

    `journal_dir=None` resolves `DEFAULT_JOURNAL_DIR` at CALL time, not as a default
    argument, so redirecting the constant (as the test suite does) takes effect and test
    manifests stay out of the shared journal.

    `before` is whatever the operation is about to overwrite, in whatever shape that
    operation's undo needs -- deliberately not a fixed schema, because a tag
    replacement and a collection deletion do not have prior states of the same shape,
    and flattening them into one would lose the part that matters.

    `inverse` is a literal call string. Prose like "restore it in the GUI" is not an
    undo; something a human can paste is.
    """
    journal_dir = journal_dir or DEFAULT_JOURNAL_DIR
    os.makedirs(journal_dir, exist_ok=True)
    # Microseconds, not seconds: two writes inside the same second are ordinary in a
    # loop, and a second-resolution stamp would have the later one overwrite the
    # record of the earlier -- losing exactly the manifest most likely to be needed.
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000:06d}"
    path = os.path.join(journal_dir, f"{op}-{stamp}.json")
    with open(path, "w") as handle:
        json.dump(
            {
                "op": op,
                "written_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "inverse": inverse,
                "before": before,
            },
            handle,
            indent=2,
        )
    return path


def copy_database(dest_dir: str | None, db_path) -> dict:
    """Copy zotero.sqlite aside as one coherent point in time. Its full size per call.

    The database is WAL-mode, so a committed write can sit in `zotero.sqlite-wal` until
    Zotero checkpoints it; the main file alone is not the database. The copy is taken by
    `sqlite.connect.backup_database`: SQLite's backup API from a read-only connection
    when one can read, else a byte copy of the main file with its `-wal` / `-journal`
    named so the copy opens with them. `method` says which.

    Off by default everywhere. Restoring it requires closing Zotero and discards
    every unrelated change made since it was taken.

    `dest_dir=None` resolves `DEFAULT_JOURNAL_DIR` here, exactly as `write_manifest`
    does: every verb passes its own `journal_dir` (default None) straight through, and the
    test fixture's redirect of the constant must cover the database copy too.
    """
    dest_dir = dest_dir or DEFAULT_JOURNAL_DIR
    os.makedirs(dest_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(dest_dir, f"zotero.sqlite.backup-{stamp}")
    method, companions = backup_database(Path(db_path), Path(dest))
    out: dict = {
        "database": dest,
        "method": method,
        "wal": companions.get("-wal"),
        "journal": companions.get("-journal"),
    }
    out["bytes"] = sum(os.path.getsize(p) for p in (dest, *companions.values()))
    return out


class FileJournal:
    """The filesystem implementation of `domain.ports.journal.Journal`.

    An OBJECT wrapping the two functions above, because a port needs something to inject
    and a module is not injectable. The functions stay module-level: they are still the
    implementation and still importable. This class only gives the application layer
    something to hold that is not an import.

    Satisfies the port STRUCTURALLY -- it does not import it. That is what keeps the
    dependency pointing one way: the port describes the need, and infrastructure happens
    to meet it without ever naming it.
    """

    def __init__(self, default_dir: str = DEFAULT_JOURNAL_DIR):
        self.default_dir = default_dir

    def write_manifest(
        self,
        op: str,
        *,
        before: dict,
        inverse: str | None = None,
        journal_dir: str | None = None,
    ) -> str:
        # Default to THIS journal's directory, the one undo reads (replay.list_entries).
        return write_manifest(
            op, before=before, inverse=inverse, journal_dir=journal_dir or self.default_dir
        )

    def copy_database(self, dest_dir: str | None, db_path) -> dict:
        return copy_database(dest_dir or self.default_dir, db_path)
