"""Reading the write journal and replaying an inverse.

Ten verbs wrote a manifest containing the call that reverses them and NOTHING read them:
no lister, no parser, no replay, not even a `glob` of the journal directory anywhere in
the package. `undo_call` was advisory text a human retyped.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from zotero_core.application.services import replay as undo_mod
from zotero_core.domain.errors import WriteBlocked


def _manifest(journal: pathlib.Path, op: str, stamp: str, inverse, before=None) -> pathlib.Path:
    path = journal / f"{op}-{stamp}.json"
    path.write_text(
        json.dumps(
            {
                "op": op,
                "written_at": f"2026-08-19T{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}-0400",
                "inverse": inverse,
                "before": before or {},
            }
        )
    )
    return path


@pytest.fixture()
def journal(tmp_path):
    d = tmp_path / "journal"
    d.mkdir()
    return d


# --------------------------------------------------------------------------
# parsing -- the security boundary
# --------------------------------------------------------------------------


def test_parse_inverse_handles_positional_and_keyword_literals():
    verb, args, kwargs = undo_mod.parse_inverse(
        "move_items_between_collections('AAAA1111', 'BBBB2222', ['CCCC3333'], force=True)"
    )
    assert verb == "move_items_between_collections"
    assert args == ["AAAA1111", "BBBB2222", ["CCCC3333"]]
    assert kwargs == {"force": True}


def test_parse_inverse_handles_a_nested_dict_argument():
    """Real manifests contain these -- `update_metadata('K', {'extra': '...'})`."""
    verb, args, _ = undo_mod.parse_inverse("update_metadata('NCF9M8JG', {'extra': 'nas-id: 1'})")
    assert verb == "update_metadata"
    assert args[1] == {"extra": "nas-id: 1"}


@pytest.mark.parametrize(
    "hostile",
    [
        "__import__('os').system('touch /tmp/pwned')",
        "os.system('rm -rf ~')",
        "trash_items(open('/etc/passwd').read())",
        "trash_items(['A']); trash_items(['B'])",
        "lambda: 1",
        "2 + 2",
    ],
)
def test_parse_inverse_refuses_anything_that_is_not_a_literal_call(hostile):
    """THE security boundary. The journal lives in /tmp, which ANY process can write, and
    replaying means turning text into a call. `eval` would have been three characters and
    an arbitrary-code-execution hole in the package whose whole premise is gating what
    reaches the library."""
    with pytest.raises((ValueError, SyntaxError)):
        undo_mod.parse_inverse(hostile)


def test_a_hostile_manifest_is_listed_as_unreplayable_rather_than_run(journal, session):
    _manifest(journal, "trash_items", "20260819-120000-000001", "__import__('os').system('x')")
    entries = undo_mod.list_entries(str(journal), journal=session.journal)
    assert entries[0].replayable is False
    with pytest.raises(WriteBlocked):
        undo_mod.undo(journal_dir=str(journal), session=session)


def test_an_unknown_verb_is_not_replayable(journal, session):
    _manifest(journal, "trash_items", "20260819-120000-000001", "drop_everything(['A'])")
    assert (undo_mod.list_entries(
        str(journal), journal=session.journal
    )[0].blocked_reason or "").startswith("`drop_everything`")


# --------------------------------------------------------------------------
# listing
# --------------------------------------------------------------------------


def test_entries_are_newest_first_by_STAMP_not_by_filename(journal, session):
    """REGRESSION, found by running this against the real journal.

    `write_manifest` names files `{op}-{stamp}.json`, so the OP NAME comes first and a
    plain filename sort is alphabetical by operation. An `update_metadata` from 00:46
    sorted ahead of a `trash_items` from 15:47 purely because "u" > "t" -- so `undo` with
    no argument offered to undo the wrong operation entirely.
    """
    _manifest(journal, "update_metadata", "20260819-004659-000001", "add_tags('AAAA1111', ['x'])")
    _manifest(journal, "trash_items", "20260819-154733-000001", "restore_items(['BBBB2222'])")

    ops = [e.op for e in undo_mod.list_entries(str(journal), journal=session.journal)]
    assert ops == ["trash_items", "update_metadata"], "sorted by name, not by time"


def test_a_manifest_with_no_inverse_is_listed_but_blocked(journal, session):
    """`write_note(action='update')` deliberately does not capture the previous body, and
    `update_metadata` records None when nothing was overwritten."""
    _manifest(journal, "update_metadata", "20260819-120000-000001", None)
    entry = undo_mod.list_entries(str(journal), journal=session.journal)[0]
    assert entry.replayable is False
    assert "no inverse recorded" in (entry.blocked_reason or "")


def test_a_placeholder_inverse_is_blocked(journal, session):
    """`delete_collection`'s inverse is a template: recreating a collection gives it a
    NEW key, so the second half cannot be written before the first half runs."""
    _manifest(
        journal,
        "delete_collection",
        "20260819-120000-000001",
        "create_collection('X') then add_items_to_collection(<new key>, ['A'])",
    )
    entry = undo_mod.list_entries(str(journal), journal=session.journal)[0]
    assert entry.replayable is False
    assert "template" in (entry.blocked_reason or "")


def test_a_truncated_manifest_is_reported_not_hidden(journal, session):
    (journal / "trash_items-20260819-120000-000001.json").write_text("{not json")
    entry = undo_mod.list_entries(str(journal), journal=session.journal)[0]
    assert entry.replayable is False
    assert "unreadable" in (entry.blocked_reason or "")


def test_an_empty_journal_lists_nothing(journal, session):
    assert undo_mod.list_entries(str(journal), journal=session.journal) == []


def test_limit_is_respected(journal, session):
    for n in range(5):
        _manifest(journal, "trash_items", f"20260819-12000{n}-000001", "restore_items(['A'])")
    assert len(undo_mod.list_entries(str(journal), limit=2, journal=session.journal)) == 2


# --------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------


def test_dry_run_resolves_everything_and_calls_nothing(journal, zotero, session):
    zotero.add("AAAA1111")
    zotero.trash("AAAA1111")
    _manifest(journal, "trash_items", "20260819-120000-000001", "restore_items(['AAAA1111'])")

    result = undo_mod.undo(journal_dir=str(journal), dry_run=True, session=session)
    assert result["op"] == "undo:dry_run"
    assert result["would_call"]["verb"] == "restore_items"
    # nothing moved
    assert zotero.is_trashed("AAAA1111")


def test_undo_replays_the_inverse_for_real(journal, zotero, session):
    zotero.add("AAAA1111")
    zotero.trash("AAAA1111")
    _manifest(journal, "trash_items", "20260819-120000-000001", "restore_items(['AAAA1111'])")

    result = undo_mod.undo(
        journal_dir=str(journal), session=session
    )
    assert result["ok"] is True
    assert result["undone"]["undoing"] == "trash_items"
    assert not zotero.is_trashed("AAAA1111")


def test_undo_picks_the_most_recent_REPLAYABLE_entry(journal, zotero, session):
    """A blocked entry must not shadow a usable one behind it."""
    zotero.add("AAAA1111")
    zotero.trash("AAAA1111")
    _manifest(journal, "trash_items", "20260819-120000-000001", "restore_items(['AAAA1111'])")
    _manifest(journal, "write_note", "20260819-130000-000001", None)  # newer, blocked

    result = undo_mod.undo(
        journal_dir=str(journal), session=session
    )
    assert result["undone"]["undoing"] == "trash_items"


def test_undo_can_target_one_manifest_by_name(journal, zotero, session):
    zotero.add("AAAA1111")
    zotero.add("BBBB2222")
    zotero.trash("AAAA1111")
    zotero.trash("BBBB2222")
    older = _manifest(
        journal, "trash_items", "20260819-120000-000001", "restore_items(['AAAA1111'])"
    )
    _manifest(journal, "trash_items", "20260819-130000-000001", "restore_items(['BBBB2222'])")

    undo_mod.undo(
        older.name, journal_dir=str(journal), session=session
    )
    assert not zotero.is_trashed("AAAA1111")
    assert zotero.is_trashed("BBBB2222"), "targeted the wrong manifest"


def test_undo_refuses_when_nothing_is_replayable(journal, session):
    _manifest(journal, "write_note", "20260819-120000-000001", None)
    with pytest.raises(WriteBlocked) as excinfo:
        undo_mod.undo(journal_dir=str(journal), session=session)
    assert "no replayable manifest" in str(excinfo.value)
    # and it says WHY each candidate was rejected
    assert excinfo.value.detail["blocked"][0]["why"]


def test_undo_refuses_an_unknown_manifest_name(journal, session):
    with pytest.raises(WriteBlocked) as excinfo:
        undo_mod.undo("nope.json", journal_dir=str(journal), session=session)
    assert "no manifest matching" in str(excinfo.value)
