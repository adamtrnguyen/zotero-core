# The write journal: manifests, the database copy, and where they live

Records moved out of `infrastructure/journal.py` (2026-11-04 history wave), plus the policy on
durability.

## 2026-08-13: a manifest, not a database copy

`zotero.sqlite` measured 329 MB, and Zotero kept five rotating daily copies beside it
(`zotero.sqlite.bak` .. `.4.bak`), so copying it per call would have spent ~330 MB duplicating
what already existed. On 2026-11-04 the file is ~15 MB (`ls -l ~/Zotero/zotero.sqlite`:
14,974,976 bytes), which is why the docstrings now cite the command rather than a size.

## Call-time defaults

- `write_manifest(journal_dir=None)` once bound `DEFAULT_JOURNAL_DIR` as a default ARGUMENT, so
  the test suite could not be redirected off the shared directory and wrote 216 manifests into
  `/tmp/zotero-write-journal`.
- `copy_database(dest_dir=None)` crashed: every verb passes its own `journal_dir` straight
  through (default None), so `trash_items(keys, copy_db=True)` reached `os.makedirs(None)` and
  raised TypeError AFTER the manifest was written and BEFORE the write was sent. Resolving it
  inside also keeps a 329 MB file out of the shared directory during a test run.
- 2026-08-20: `FileJournal`'s docstring said the module functions were "still what the tests
  exercise directly". Measured: no test called `write_manifest` or `copy_database` directly --
  every path goes through `session.journal` -- so that testability argument was removed.

## 2026-11-04: `FileJournal` writes to its own directory (c51bfe7)

`FileJournal(default_dir)` stored the directory, but `write_manifest` and `copy_database`
passed only the caller's `journal_dir`, so `None` fell through to the MODULE default while undo
(`replay.list_entries`) reads `journal_dir or journal.default_dir`. A journal built with its own
directory wrote where undo never looks and lost its history; the fixtures hid it by pointing
both at the same path. The comment that explained the fallback is now a one-line rule.

## Policy: the journal lives in `/tmp` and is ephemeral

`DEFAULT_JOURNAL_DIR = "/tmp/zotero-write-journal"` is a deliberate choice, not an oversight:
the manifest exists to undo a mistake noticed in the same sitting, and macOS clears `/tmp` on
reboot. The DURABLE undo for a trash is Zotero's own trash. Consequences a caller must know: a
manifest written before a reboot is gone, so `zotero_list_undo` / `zotero_undo` cannot reach
it, and a `delete_collection` member list exists only in that manifest. This is documented in
the tool descriptions; moving the journal is a separate decision.

## 2026-11-04: `copy_database` copied the wrong companion file

The live database is WAL-mode (header bytes 18-19 `02 02`; `zotero.sqlite-wal` and `-shm`
beside it), but `copy_database` copied the main file plus `zotero.sqlite-journal`, which a WAL
database never has. A committed write still in the WAL was therefore missing from the backup,
and the docstrings and the `copy_db` tool description promised a rollback journal "~330 MB per
call". The test `test_the_database_copy_takes_the_journal_with_it` asserted the bug: it wrote a
fake `-journal` and checked it was copied.

Now `sqlite.connect.backup_database` takes the copy with SQLite's backup API from a `mode=ro`
connection (WAL frames included, the source never opened for write), probing a table first
because `Connection.backup` retries a busy source forever, and with a deadline in the progress
callback. When Zotero's lock refuses a reader it falls back to a byte copy of the main file and
its `-wal` / `-journal`, named `<backup>-wal` so opening the backup replays it; the result's
`method` says which. The test fixture database is WAL-mode too, so the suite exercises the
same file layout as the live library.

## 2026-11-04: a second undo re-applied the original

`undo()` replays an inverse through the ordinary verb, which journals its own manifest -- whose
inverse is the original write. That manifest was then the newest replayable entry, so a second
no-arg `undo()` redid what the first had undone (trash -> undo restores -> undo re-trashes).
Now an undo journals an `undo` record linking the manifest it undid to the one its replay
wrote: the undone manifest is no longer replayable, and the replay's manifest is skipped by a
no-arg undo (naming it is a deliberate redo). Undo now walks back through the journal.

The same fix found the ordering it depends on was a coin toss: `write_manifest` stamped
`time.time_ns() % 1_000_000` -- nanoseconds within the MILLISECOND, which wraps a thousand
times a second -- as if it were microseconds within the second, so "newest" among writes made
close together (a write and its undo) was arbitrary. The stamp is now microseconds within the
second from one clock reading, and a taken filename is bumped rather than overwritten.

## 2026-11-04: write-then-undo round trips, and the one that does not close

`tests/test_round_trip.py` runs every verb that journals a replayable inverse from a non-empty
library, undoes it with a no-arg `undo()`, and compares the whole fixture before and after.
Against the source before the update_collection fix it failed on a re-parent. One case is a
strict xfail: moving an item that is already filed in the target. The inverse is one move back,
which also unfiles it from the target where it already was; restoring that state needs
"add back to the source only", which one replayable call cannot express.

## `replay.py`: the gap it closed

Moved from the module docstring. Ten verbs wrote a manifest containing the call that reverses
them and NOTHING read them: `write_manifest` had no counterpart -- no lister, no parser, no
replay, not even a `glob` of the journal directory anywhere in the package. The `undo_call`
string in a result envelope was advisory text a human retyped, although the manifest's own
docstring argued that "prose like 'restore it in the GUI' is not an undo; something a human can
paste is". The "Ten verbs" count then went stale in the tool description and README (eleven
verbs journal, nine of them replayably); the text now names the rule, not a count.
