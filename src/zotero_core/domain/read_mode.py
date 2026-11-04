"""Which read mode served a query — the honesty mechanism, given a type.

WHY THIS IS A TYPE AND ALMOST NOTHING ELSE HERE IS
--------------------------------------------------
Neither `omni-rag` nor `arete` wraps a primitive in a class: `chunk_id`, `uuid`, `book`
and `deck` all travel as bare `str`. This one earns the exception on evidence rather than
principle. Before it existed the value was a bare `str` at ~45 sites, with:

  - exactly TWO legal values, produced in one place (`read.connect.open_readonly`),
  - a third (`"none"`) invented separately for the empty-batch case,
  - and TWO comparisons against a string literal (`write/verbs.py`) deciding whether a
    failed verification is a real failure or a stale snapshot.

That last one is the point. `after.read_mode == "mode=ro"` is load-bearing logic keyed on a
magic string: a typo there does not raise, it silently flips a verification verdict.

WHAT IT MEANS
-------------
Zotero uses a ROLLBACK JOURNAL, not WAL. With the app running, a plain `mode=ro` read
intermittently loses to the writer; `immutable=1` always answers, but from a POINT-IN-TIME
SNAPSHOT — it tells sqlite the file cannot change, so the journal is ignored.

    LIVE      the answer reflects the database as of now
    SNAPSHOT  the answer may predate a commit that has already happened
    NONE      nothing was read (an empty batch); not a claim about the database

`str, Enum` and NOT `enum.StrEnum`: `StrEnum` is 3.11+ and this package declares
`requires-python = ">=3.10"`. The mixin gives the same `== "mode=ro"` and `in {...}`
behaviour the tests assert, and `json.dumps` emits the value.

`__str__` is pinned back to `str.__str__` because the mixin does NOT do it for you: without
this line `f"{ReadMode.LIVE}"` renders `"ReadMode.LIVE"` rather than `"mode=ro"`. Nothing
interpolates it today; this is here so nothing has to notice when something does.
"""

from __future__ import annotations

from enum import Enum


class ReadMode(str, Enum):
    """How a sqlite read was served. Values are the CONTRACT — never reword them."""

    LIVE = "mode=ro"
    SNAPSHOT = "immutable=1"
    # Not a mode the database returned: no query ran. Reported rather than left blank so a
    # caller can tell "nothing was read" from "read, and it was live".
    NONE = "none"

    __str__ = str.__str__

    @property
    def is_snapshot(self) -> bool:
        """True when the answer may lag a commit that has already happened.

        The question every post-write verification actually wants to ask, and the reason
        a failed read-back is reported as `unverified` rather than as a failure: under a
        snapshot, "I cannot see it" and "it is not there" are indistinguishable.
        """
        return self is ReadMode.SNAPSHOT
