"""Reading the write journal, and replaying an inverse.

NAMED `replay`, NOT `undo`, and that is not cosmetic: `write/__init__` re-exports the
`undo` FUNCTION, which binds that name on the package and makes a module of the same name
permanently unreachable -- `import zotero_core.application.undo` silently hands back the
function, with an AttributeError on first use. The public verb keeps the good name; the
module takes a different one.

THE GAP THIS CLOSES
-------------------
Ten verbs write a manifest containing the call that reverses them. NOTHING READ THEM.
`write_manifest` had no counterpart: no lister, no parser, no replay, not even a `glob`
of the journal directory anywhere in the package. The `undo_call` string in a result
envelope was advisory text a human retyped by hand -- and the manifest's own docstring
argues that "prose like 'restore it in the GUI' is not an undo; something a human can
paste is", which was true and still left the pasting to the human.

WHY NOT `eval`
--------------
`inverse` is a literal call string like

    add_items_to_collection('C4XFPWCD', ['SQQTHCIX'])

Replaying it means turning text into a call, and the lazy way to do that is `eval`. That
would execute arbitrary code out of a file in `/tmp` that any process can write -- in a
package whose entire premise is gating what reaches the library. Instead the string is
PARSED with `ast`, the callee must be a bare name on a whitelist of undo verbs, and every
argument must satisfy `ast.literal_eval`. Anything else is refused and reported, not run.

WHAT CANNOT BE UNDONE, AND WHY IT SAYS SO
-----------------------------------------
Not every manifest carries a replayable inverse, and the honest thing is to name the
reason rather than fail obscurely:

  no inverse recorded   `write_note(action="update")` deliberately does not capture the
                        previous body, and `update_metadata` records None when nothing
                        was overwritten -- cookjohn has no field-remove, so there is no
                        inverse to express.
  placeholder           `delete_collection`'s inverse is prose with a `<new key>` in it:
                        recreating a collection gives it a NEW key, so the second half of
                        the undo cannot be written before the first half runs.
  unknown verb          a manifest from an older or newer version of this package.

`REPLAYABLE` is deliberately a subset of the write surface: it holds the verbs that
reverse another verb, not every verb. `trash_items` is here because it reverses
`restore_items`; `delete_collection` is NOT, because undoing a deletion by deleting
something is never right.
"""

from __future__ import annotations

import ast
import glob
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from zotero_core.application.results import ok
from zotero_core.domain.errors import Reason, WriteBlocked
from zotero_core.domain.ports.journal import Journal

PLACEHOLDER_MARKERS = ("<", ">")

# `write_manifest` names files `{op}-{YYYYmmdd}-{HHMMSS}-{micros}.json`, so the OP NAME
# comes first and a plain filename sort is alphabetical by operation, not chronological.
# Caught by running this against the real journal: an `update_metadata` from 00:46 sorted
# ahead of a `trash_items` from 15:47 purely because "u" > "t", and `undo` with no
# argument therefore offered to undo the wrong thing. Sort on the stamp, not the name.
_STAMP_RE = re.compile(r"(\d{8})-(\d{6})-(\d{6})\.json$")


def _sort_key(path: str) -> tuple:
    match = _STAMP_RE.search(path)
    if match:
        return (1, match.group(1) + match.group(2) + match.group(3))
    # No parseable stamp: fall back to mtime, and sort below anything stamped.
    try:
        return (0, str(int(os.path.getmtime(path))))
    except OSError:
        return (0, "")


@dataclass(frozen=True)
class Entry:
    """One journal manifest, and whether it can actually be replayed."""

    path: str
    op: str
    written_at: str
    inverse: str | None
    before: dict
    replayable: bool
    blocked_reason: str | None


def _replayable_verbs() -> dict[str, Callable[..., dict]]:
    """The whitelist. Imported lazily so this module stays importable without a live
    Zotero, and so `application/__init__` can export `undo` without a circular import."""
    from zotero_core.application.services import collections as coll
    from zotero_core.application.services import verbs

    return {
        "add_items_to_collection": coll.add_items_to_collection,
        "remove_items_from_collection": coll.remove_items_from_collection,
        "move_items_between_collections": coll.move_items_between_collections,
        "update_collection": coll.update_collection,
        "update_metadata": verbs.update_metadata,
        "replace_creators": verbs.replace_creators,
        "set_tags": verbs.set_tags,
        "add_tags": verbs.add_tags,
        "remove_tags": verbs.remove_tags,
        "trash_items": verbs.trash_items,
        "restore_items": verbs.restore_items,
    }


def parse_inverse(inverse: str) -> tuple[str, list, dict]:
    """`"verb('A', ['B'], force=True)"` -> `("verb", ["A", ["B"]], {"force": True})`.

    Raises ValueError on anything that is not a single call to a bare name with literal
    arguments. That refusal is the security boundary -- see the module docstring.
    """
    tree = ast.parse(inverse.strip(), mode="eval")
    call = tree.body
    if not isinstance(call, ast.Call):
        raise ValueError("inverse is not a call")
    if not isinstance(call.func, ast.Name):
        raise ValueError("inverse callee is not a plain name")
    try:
        args = [ast.literal_eval(node) for node in call.args]
        kwargs = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords if kw.arg}
    except (ValueError, SyntaxError) as exc:
        raise ValueError(f"inverse has a non-literal argument: {exc}") from exc
    return call.func.id, args, kwargs


def _classify(inverse: str | None) -> tuple[bool, str | None]:
    if not inverse:
        return False, "no inverse recorded — this operation cannot express one"
    if any(marker in inverse for marker in PLACEHOLDER_MARKERS):
        return False, f"inverse is a template, not a call: {inverse}"
    try:
        verb, _args, _kwargs = parse_inverse(inverse)
    except ValueError as exc:
        return False, str(exc)
    if verb not in _replayable_verbs():
        return False, f"`{verb}` is not a replayable undo verb"
    return True, None


def list_entries(
    journal_dir: str | None = None, *, limit: int = 25, journal: Journal
) -> list[Entry]:
    """Journal manifests, NEWEST FIRST, each with whether it can be replayed.

    Sorted by the microsecond STAMP inside the filename, not by the filename -- see
    `_sort_key` for why those are different and what it cost.
    """
    directory = journal_dir or journal.default_dir
    entries: list[Entry] = []
    paths = sorted(glob.glob(os.path.join(directory, "*.json")), key=_sort_key, reverse=True)
    for path in paths[:limit]:
        try:
            with open(path) as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            # A truncated manifest is worth listing as unusable rather than hiding.
            entries.append(
                Entry(path, "?", "", None, {}, False, "manifest is unreadable or truncated")
            )
            continue
        inverse = payload.get("inverse")
        replayable, blocked = _classify(inverse)
        entries.append(
            Entry(
                path=path,
                op=payload.get("op", "?"),
                written_at=payload.get("written_at", ""),
                inverse=inverse,
                before=payload.get("before", {}) or {},
                replayable=replayable,
                blocked_reason=blocked,
            )
        )
    return entries


def undo(
    manifest_path: str | None = None,
    *,
    journal_dir: str | None = None,
    dry_run: bool = False,
    session: Any,
) -> dict:
    """Replay one manifest's inverse. Defaults to the most recent replayable entry.

    `dry_run` resolves and validates everything and returns the call it WOULD make
    without making it -- which is the mode to use first, because an undo is itself a
    write and lands in the journal like any other.
    """
    entries = list_entries(journal_dir, limit=200, journal=session.journal)
    if manifest_path:
        matches = [e for e in entries if e.path == manifest_path or e.path.endswith(manifest_path)]
        if not matches:
            raise WriteBlocked(
                Reason.MISSING_REQUIRED_FIELD,
                f"no manifest matching {manifest_path!r} in "
                f"{journal_dir or session.journal.default_dir}",
                {"available": [e.path for e in entries[:10]]},
            )
        entry = matches[0]
    else:
        replayable = [e for e in entries if e.replayable]
        if not replayable:
            raise WriteBlocked(
                Reason.NOTHING_TO_DO,
                "no replayable manifest in the journal",
                {
                    "checked": len(entries),
                    "blocked": [{"op": e.op, "why": e.blocked_reason} for e in entries[:10]],
                },
            )
        entry = replayable[0]

    if not entry.replayable:
        raise WriteBlocked(
            Reason.NOTHING_TO_DO,
            f"that manifest cannot be replayed: {entry.blocked_reason}",
            {"path": entry.path, "op": entry.op, "inverse": entry.inverse},
        )

    verb_name, args, kwargs = parse_inverse(entry.inverse or "")
    verb = _replayable_verbs()[verb_name]
    plan = {
        "manifest": entry.path,
        "undoing": entry.op,
        "written_at": entry.written_at,
        "call": entry.inverse,
        "verb": verb_name,
    }
    if dry_run:
        return ok(
            "undo:dry_run",
            transport="none",
            would_call=plan,
        )

    # ⚠ WAS `verb(*args, **{**kwargs, **injected})`, whose comment read: "the injected
    # transports/store are for tests; a real caller passes nothing and the verb builds its
    # own session". Verbs no longer build their own anything -- the session is required and
    # comes from the caller, so the replayed verb runs against exactly the collaborators the
    # undo itself was given. A replay that silently built a DIFFERENT session than the
    # caller's would be replaying against another database.
    result = verb(*args, **kwargs, session=session)
    return ok(
        "undo",
        transport="none",
        undone=plan,
        result=result,
    )
