"""Tests for the gated write path.

These exist because `writes.py` is the only sanctioned route for mutating the
Zotero library, and the endpoint it drives enforces nothing. `linker/bootstrap.js`
will happily trash three of five keys and return HTTP 200 -- so every guarantee the
caller gets is a guarantee this module makes, and a silent regression here would
hand back a success-shaped partial write.

Every test runs against a throwaway database and a fake plugin. None touches the
real library, and the autouse fixture in conftest makes a forgotten injection fail
loudly rather than reach Adam's Zotero.
"""

from __future__ import annotations

import email.message
import io
import json
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest

from zotero_core.application.services.liveness import require_zotero
from zotero_core.application.services.verbs import check_keys, restore_items, trash_items
from zotero_core.domain.errors import ALL_REASONS, Reason, WriteBlocked
from zotero_core.domain.read_mode import ReadMode
from zotero_core.infrastructure.sqlite.items import ZoteroItemStore
from zotero_core.infrastructure.transports.cookjohn import CookjohnClient
from zotero_core.infrastructure.transports.linker import LinkerClient

from .conftest import PING, FakeLinker, StubProbe

# --------------------------------------------------------------------------
# check_keys — shape. Cheap, and the only gate that needs no I/O.
# --------------------------------------------------------------------------

def test_empty_batch_is_refused():
    with pytest.raises(WriteBlocked) as e:
        check_keys([])
    assert e.value.code == Reason.NO_ITEM_KEYS


def test_malformed_keys_are_named_individually():
    """The message has to say WHICH key is wrong; a batch of 40 is unsearchable."""
    with pytest.raises(WriteBlocked) as e:
        check_keys(["ABCD2345", "too-short", "lowercase"])
    assert e.value.code == Reason.MALFORMED_ITEM_KEY
    assert e.value.detail["malformed"] == ["too-short", "lowercase"]


def test_a_bare_string_is_treated_as_one_key():
    """A string is iterable, so `trash_items("ABCD2345")` would otherwise become
    eight one-character keys — eight confusing shape failures instead of a write."""
    assert check_keys("ABCD2345") == ["ABCD2345"]


def test_duplicate_keys_collapse():
    """Passing a key twice must not make the plugin's count disagree with reality —
    that count is what the verification step compares against."""
    assert check_keys(["ABCD2345", "ABCD2345", "WXYZ6789"]) == ["ABCD2345", "WXYZ6789"]


def test_the_observed_key_alphabet_is_accepted():
    """All 3405 keys in the live library draw on 23456789A-NP-Z. The gate is looser
    than that on purpose (see the regex comment), so these must all pass."""
    assert check_keys(["23456789", "ABCDEFGH", "IJKLMNPQ", "RSTUVWXY", "Z2345678"])


# --------------------------------------------------------------------------
# liveness — the precondition that INVERTS relative to Calibre
# --------------------------------------------------------------------------

def _stub_urlopen(monkeypatch, payload: dict | None = None, error: Exception | None = None):
    class _Resp:
        def __init__(self, body: bytes):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def _open(*_args, **_kwargs):
        if error:
            raise error
        return _Resp(json.dumps(payload).encode())

    monkeypatch.setattr("urllib.request.urlopen", _open)


def test_a_dead_zotero_blocks_the_write(zotero, zotero_down, session):
    """Calibre demands its GUI be CLOSED; Zotero demands the app be RUNNING, because
    the write channel is code executing inside it. Same gate, opposite sense."""
    zotero.add("ABCD2345", "A Paper")
    with pytest.raises(WriteBlocked) as e:
        trash_items(["ABCD2345"], session=replace(session, linker=LinkerClient()))
    assert e.value.code == Reason.ZOTERO_NOT_RUNNING


def test_zotero_running_without_the_plugin_is_a_different_failure(monkeypatch):
    """:23119 answering but /zotero-linker/ping 404ing means Zotero is up and the
    plugin is not loaded. Reported distinctly because the fix is different — install
    the plugin, not start the app."""
    _stub_urlopen(
        monkeypatch,
        error=urllib.error.HTTPError(
            "http://127.0.0.1:23119/zotero-linker/ping",
            404,
            "Not Found",
            email.message.Message(),
            io.BytesIO(b""),
        ),
    )
    with pytest.raises(WriteBlocked) as e:
        require_zotero(needs=("linker",), linker=LinkerClient(), probe=StubProbe(running=True))
    assert e.value.code == Reason.LINKER_NOT_INSTALLED
    assert e.value.detail["http_status"] == 404


def test_a_closed_zotero_is_reported_as_closed_not_as_a_missing_plugin(monkeypatch):
    """THE BRANCH THE PROBE PORT EXISTS FOR, and it was never executed.

    `require_zotero` catches a transport's refusal and then asks the probe whether the
    APPLICATION is up, because the fix differs: install the plugin, or start Zotero. That
    second question is `liveness.py`'s `not probe.is_running()`.

    ⚠ Nothing exercised it. Every `StubProbe(...)` in the suite passed `running=True`, so
    the condition was always False; mutating it to `probe.is_running()` left all 471 tests
    green. The enriched `ZOTERO_NOT_RUNNING` refusal below — the one that names the probe
    URL and says every write channel runs inside the application — could not be reached.

    This test supplies the missing arm. It must FAIL if that `not` is dropped.
    """
    _stub_urlopen(monkeypatch, error=OSError("Connection refused"))
    probe = StubProbe(running=False)

    with pytest.raises(WriteBlocked) as e:
        require_zotero(needs=("linker",), linker=LinkerClient(), probe=probe)

    assert e.value.code == Reason.ZOTERO_NOT_RUNNING
    # The enrichment is the point: a bare transport failure cannot say WHY.
    assert "not running" in e.value.reason
    assert e.value.detail["probe"] == probe.url
    assert e.value.detail["needed"] == ["linker"]


def test_a_running_zotero_keeps_the_transports_own_unenriched_refusal(monkeypatch):
    """The other arm, pinning the branch from both sides.

    ⚠ The two arms do NOT differ by code. `LinkerClient.ping()` already raises
    `ZOTERO_NOT_RUNNING` for a refused connection, so both paths carry that code and an
    assertion on it cannot tell them apart -- my first attempt at this test asserted
    exactly that and failed. What the probe adds is the ENRICHMENT: a `probe` URL and a
    `needed` list in `detail`, and a reason naming the application rather than the socket.

    So the distinguishing fact is the absence of that enrichment when Zotero is up.
    """
    _stub_urlopen(monkeypatch, error=OSError("Connection refused"))

    with pytest.raises(WriteBlocked) as e:
        require_zotero(needs=("linker",), linker=LinkerClient(), probe=StubProbe(running=True))

    # the transport's own refusal, re-raised untouched -- no probe was consulted for it
    assert "probe" not in e.value.detail


def test_something_else_answering_on_the_path_is_refused(monkeypatch):
    _stub_urlopen(monkeypatch, payload={"plugin": "some-other-plugin", "version": "9"})
    with pytest.raises(WriteBlocked) as e:
        require_zotero(needs=("linker",), linker=LinkerClient(), probe=StubProbe(running=True))
    assert e.value.code == Reason.NOT_THE_LINKER


def test_a_live_ping_is_accepted_and_returned(monkeypatch):
    """Keyed by transport, because an operation may need one plugin or both and the
    result has to record which versions actually performed the write."""
    _stub_urlopen(monkeypatch, payload=PING)
    info = require_zotero(needs=("linker",), linker=LinkerClient(), probe=StubProbe(running=True))
    assert info["linker"]["version"] == "0.3.0"
    assert "cookjohn" not in info


def test_liveness_is_checked_before_anything_is_sent(zotero, zotero_down, session):
    """Ordering, not just presence: a dead Zotero must fail before the database is
    read or a manifest is written."""
    zotero.add("ABCD2345")
    with pytest.raises(WriteBlocked) as e:
        trash_items(["ABCD2345"], session=replace(session, linker=LinkerClient()))
    assert e.value.code == Reason.ZOTERO_NOT_RUNNING


# --------------------------------------------------------------------------
# the existence gate — the important one
#
# bootstrap.js:129 only 404s when NO key resolves. Five keys with two typos trashes
# three and returns {"ok": true, "trashed": 3, "missing": [...]}. Same defect class
# as `calibredb add` exiting 0 having added nothing.
# --------------------------------------------------------------------------

def test_one_unknown_key_refuses_the_whole_batch(zotero, session):
    zotero.add("ABCD2345", "Real Paper")
    zotero.add("WXYZ6789", "Also Real")
    with pytest.raises(WriteBlocked) as e:
        trash_items(["ABCD2345", "WXYZ6789", "NOTHERE2"], session=session)
    assert e.value.code == Reason.UNKNOWN_ITEM_KEYS
    assert e.value.detail["missing"] == ["NOTHERE2"]


def test_a_refused_batch_sends_nothing(zotero, linker, session):
    """The assertion that matters. If the POST still went out, the two real keys
    would be in the trash and the caller would have been told the call failed."""
    zotero.add("ABCD2345", "Real Paper")
    with pytest.raises(WriteBlocked):
        trash_items(["ABCD2345", "NOTHERE2"], session=session)
    assert linker.posts == []
    assert not zotero.is_trashed("ABCD2345")


def test_a_group_library_key_does_not_resolve(zotero, linker, session):
    """The plugin resolves keys with `getByLibraryAndKey(userLibraryID, ...)`, so a
    key that exists only in a group library is NOT writable through it. The precheck
    is scoped the same way, or the two would disagree about what exists — this
    database has six group libraries."""
    zotero.add("GROUPKEY", "In a group library", library_id=2)
    with pytest.raises(WriteBlocked) as e:
        trash_items(["GROUPKEY"], session=session)
    assert e.value.code == Reason.UNKNOWN_ITEM_KEYS
    assert linker.posts == []


# --------------------------------------------------------------------------
# no-op gates — a write that changes nothing must not report success
# --------------------------------------------------------------------------

def test_trashing_an_already_trashed_item_is_refused(zotero, session):
    """calibre-core refuses a removal that removes nothing for the same reason: the
    caller has misidentified something, and a cheerful ok=True hides it."""
    zotero.add("ABCD2345", "Already Gone", trashed=True)
    with pytest.raises(WriteBlocked) as e:
        trash_items(["ABCD2345"], session=session)
    assert e.value.code == Reason.ALREADY_TRASHED
    assert e.value.detail["hint"] == "force=True"
    assert e.value.detail["titles"] == {"ABCD2345": "Already Gone"}


def test_restoring_something_that_is_not_trashed_is_refused(zotero, session):
    zotero.add("ABCD2345", "Right Where It Was")
    with pytest.raises(WriteBlocked) as e:
        restore_items(["ABCD2345"], session=session)
    assert e.value.code == Reason.NOT_TRASHED


def test_force_skips_the_noop_and_proceeds_with_the_rest(zotero, linker, session):
    zotero.add("ABCD2345", "Already Gone", trashed=True)
    zotero.add("WXYZ6789", "Still Here")
    out = trash_items(["ABCD2345", "WXYZ6789"], force=True, session=session)
    assert out["skipped"] == ["ABCD2345"]
    assert out["item_keys"] == ["WXYZ6789"]
    assert linker.posts == [("trash-items", {"itemKeys": ["WXYZ6789"]})]


def test_force_does_not_relax_the_existence_gate(zotero, session):
    """force is for no-ops only. It must not become a general override — calibre-core
    keeps the same line: 'force relaxes WHICH field, never the preconditions'."""
    zotero.add("ABCD2345", "Real")
    with pytest.raises(WriteBlocked) as e:
        trash_items(["ABCD2345", "NOTHERE2"], force=True, session=session)
    assert e.value.code == Reason.UNKNOWN_ITEM_KEYS


def test_force_does_not_relax_the_liveness_gate(zotero, zotero_down, session):
    zotero.add("ABCD2345", "Real")
    with pytest.raises(WriteBlocked) as e:
        trash_items(["ABCD2345"], force=True, session=replace(session, linker=LinkerClient()))
    assert e.value.code == Reason.ZOTERO_NOT_RUNNING


def test_a_batch_that_is_entirely_a_noop_is_refused_even_with_force(zotero, linker, session):
    """force must not turn an empty write into a reported success."""
    zotero.add("ABCD2345", "Already Gone", trashed=True)
    with pytest.raises(WriteBlocked) as e:
        trash_items(["ABCD2345"], force=True, session=session)
    assert e.value.code == Reason.NOTHING_TO_DO
    assert linker.posts == []


# --------------------------------------------------------------------------
# the write, end to end against the fixture
# --------------------------------------------------------------------------

def test_trash_then_restore_round_trips(zotero, session):
    """The property that makes trash safe to offer at all."""
    zotero.add("ABCD2345", "A Paper")
    store = zotero.store()

    trashed = trash_items(["ABCD2345"], session=replace(session, store=store))
    assert trashed["ok"] is True
    assert zotero.is_trashed("ABCD2345")

    restored = restore_items(["ABCD2345"], session=replace(session, store=store))
    assert restored["ok"] is True
    assert not zotero.is_trashed("ABCD2345")


def test_the_result_names_the_call_that_undoes_it(zotero, session):
    zotero.add("ABCD2345", "A Paper")
    out = trash_items(["ABCD2345"], session=session)
    assert out["undo_call"] == "restore_items(['ABCD2345'])"
    assert out["verification"]["verified"] is True


def test_the_endpoints_are_the_plugins_own(zotero, linker, session):
    """Guards the wiring: `trash-items` and `restore-items` are what bootstrap.js
    registers, and a typo here is a 404 at runtime rather than a test failure."""
    zotero.add("ABCD2345", "A Paper")
    zotero.add("WXYZ6789", "Another", trashed=True)
    store = zotero.store()
    trash_items(["ABCD2345"], session=replace(session, store=store))
    restore_items(["WXYZ6789"], session=replace(session, store=store))
    assert [path for path, _ in linker.posts] == ["trash-items", "restore-items"]


def test_the_ping_metadata_travels_with_the_result(zotero, session):
    """Which Zotero and which plugin version performed the write — the thing you
    want recorded when a write behaves unexpectedly six weeks later."""
    zotero.add("ABCD2345", "A Paper")
    out = trash_items(["ABCD2345"], session=session)
    assert out["versions"]["linker"]["zoteroVersion"] == "9.0.6"
    assert out["versions"]["linker"]["version"] == "0.3.0"


def test_a_trash_does_not_require_the_cookjohn_plugin(zotero, session):
    """The two plugins fail independently, so a verb must only demand the one it uses.
    Trash rides the linker; a missing cookjohn must not block it. The fake cookjohn
    would raise on any call, so reaching a result proves none was made."""
    zotero.add("ABCD2345", "A Paper")

    class Exploding(CookjohnClient):
        def ping(self):
            raise AssertionError("trash must not probe cookjohn")

        def call(self, tool, arguments):
            raise AssertionError("trash must not call cookjohn")

    out = trash_items(
        ["ABCD2345"], session=replace(session, cookjohn=Exploding("http://unused"))
    )
    assert out["transport"] == "linker"
    assert "cookjohn" not in out["versions"]


# --------------------------------------------------------------------------
# attachments and notes — the question nobody had run
# --------------------------------------------------------------------------

@pytest.mark.parametrize("item_type", ["attachment", "note", "annotation"])
def test_child_items_are_trashable_by_key(zotero, item_type, session):
    """`getByLibraryAndKey` takes any item key, so the gate must not assume a
    top-level item.

    Of the three, only `attachment` has also been run against the real library
    (MAQ3PAG9, 2026-08-13). `note` and `annotation` are fixture-only here — they take
    the same code path and the plugin resolves them identically, but that is an
    inference, and this docstring is not going to dress it up as a measurement."""
    zotero.add("PARENT12", "The Parent", item_type="book")
    zotero.add("CHILD123", "The Child", item_type=item_type, parent="PARENT12")
    out = trash_items(["CHILD123"], session=session)
    assert out["affected"][0]["item_type"] == item_type
    assert zotero.is_trashed("CHILD123")


def test_a_parents_children_are_recorded_before_it_is_trashed(zotero, session):
    """Recorded for context, NOT because the undo needs them: trashing a parent does
    not give its children their own deletedItems rows (measured live 2026-08-13, see
    `write_undo_manifest`). The manifest carries them so a human can see what else
    went off screen with it."""
    zotero.add("PARENT12", "The Parent", item_type="book")
    zotero.add("CHILD123", "The PDF", item_type="attachment", parent="PARENT12")
    zotero.add("CHILD456", "A Note", item_type="note", parent="PARENT12")

    out = trash_items(["PARENT12"], session=session)
    assert sorted(out["affected"][0]["child_keys"]) == ["CHILD123", "CHILD456"]
    manifest = json.loads(Path(out["undo_manifest"]).read_text())
    assert sorted(manifest["before"]["items"][0]["child_keys"]) == ["CHILD123", "CHILD456"]


# --------------------------------------------------------------------------
# the undo manifest — this path's translation of "back up before mutating"
# --------------------------------------------------------------------------

def test_the_manifest_is_written_before_the_post(zotero, tmp_path, session):
    """If it were written afterwards, a write that crashed mid-batch would leave no
    record of what to undo — which is the one moment the record is needed."""
    zotero.add("ABCD2345", "A Paper")

    class Exploding(FakeLinker):
        def post(self, path, payload):
            raise WriteBlocked(Reason.LINKER_REFUSED, "boom")

    with pytest.raises(WriteBlocked):
        trash_items(
            ["ABCD2345"],
            journal_dir=str(tmp_path / "journal"),
            session=replace(session, linker=Exploding(zotero)),
        )
    written = list((tmp_path / "journal").glob("trash_items-*.json"))
    assert len(written) == 1
    assert json.loads(written[0].read_text())["before"]["items"][0]["trashed_before"] is False


def test_the_manifest_records_the_prestate_and_the_inverse(zotero, tmp_path, session):
    zotero.add("ABCD2345", "A Paper")
    zotero.add_to_collection("ABCD2345", zotero.add_collection("Reading"))
    out = trash_items(
        ["ABCD2345"], journal_dir=str(tmp_path / "j"), session=session
    )
    manifest = json.loads(Path(out["undo_manifest"]).read_text())
    assert manifest["op"] == "trash_items"
    # A literal call, not prose. "restore it in the GUI" is not an undo; something a
    # human can paste is.
    assert manifest["inverse"] == "restore_items(['ABCD2345'])"
    assert manifest["before"]["items"][0] == {
        "key": "ABCD2345",
        "title": "A Paper",
        "item_type": "journalArticle",
        "trashed_before": False,
        "parent_key": None,
        "child_keys": [],
        "collection_count": 1,
    }


def test_a_manifest_written_with_no_journal_dir_honours_the_redirect(zotero, session):
    """Guards the seam the conftest fixture depends on.

    `journal_dir` resolves DEFAULT_JOURNAL_DIR at CALL time. As a default ARGUMENT it was
    bound at import, the autouse redirect silently did nothing, and the suite wrote 216
    manifests into the shared `/tmp/zotero-write-journal` — polluting the audit trail for
    real writes. If this test fails, that is happening again."""
    import zotero_core.infrastructure.journal as journal_mod

    zotero.add("ABCD2345", "A Paper")
    out = trash_items(["ABCD2345"], session=session)
    assert out["undo_manifest"].startswith(journal_mod.DEFAULT_JOURNAL_DIR)
    assert "/tmp/zotero-write-journal" not in out["undo_manifest"]


def test_the_329mb_database_copy_is_off_by_default(zotero, tmp_path, session):
    """Zotero already keeps five rotating daily copies of the 329 MB database beside
    it. Spending another ~330 MB per trash — whose undo is `restore_items` — is not
    a default anyone chose; it is available when asked for."""
    zotero.add("ABCD2345", "A Paper")
    out = trash_items(
        ["ABCD2345"], journal_dir=str(tmp_path / "j"), session=session
    )
    assert "database_backup" not in out
    assert list((tmp_path / "j").glob("zotero.sqlite.backup-*")) == []


def test_the_database_copy_takes_the_journal_with_it(zotero, tmp_path, session):
    """A copy of the main file alone, taken while Zotero holds a transaction open, can
    hold pages the journal was about to roll back. DESIGN.md's pattern is db+journal."""
    zotero.add("ABCD2345", "A Paper")
    zotero.path.with_name(zotero.path.name + "-journal").write_bytes(b"fake journal")
    out = trash_items(
        ["ABCD2345"],
        copy_db=True,
        journal_dir=str(tmp_path / "j"),
        session=session,
    )
    assert Path(out["database_backup"]["database"]).exists()
    assert Path(out["database_backup"]["journal"]).exists()


def test_the_database_copy_works_without_being_told_where_to_put_it(zotero, session):
    """`copy_db=True` with no `journal_dir` — the documented way to ask for the snapshot —
    used to raise TypeError from `os.makedirs(None)`, AFTER the manifest was written and
    BEFORE the write was sent. Both tests above pass an explicit `journal_dir`, which is
    precisely why it survived: every verb hands its own `journal_dir` straight through and
    that parameter defaults to None.

    Found by `ty`, not by the suite. Reachable from plain Python, and reachable through
    the MCP adapter, which exposes `copy_db` and deliberately withholds `journal_dir`."""
    zotero.add("ABCD2345", "A Paper")
    out = trash_items(["ABCD2345"], copy_db=True, session=session)
    backup = Path(out["database_backup"]["database"])
    assert backup.exists()
    # And it landed in the redirected journal dir, not the shared one — resolving the
    # default inside `copy_database` is what keeps a 329 MB file out of /tmp during tests.
    assert "/tmp/zotero-write-journal" not in str(backup)


# --------------------------------------------------------------------------
# post-write verification — a 200 is not evidence
# --------------------------------------------------------------------------

def test_a_write_that_did_not_land_is_caught(zotero, session):
    """The plugin reports ok:true; the database disagrees. calibre-core learned the
    same lesson from `calibredb` exiting 0 having done nothing."""
    zotero.add("ABCD2345", "A Paper")
    lying = FakeLinker(zotero, apply=False)
    with pytest.raises(WriteBlocked) as e:
        trash_items(["ABCD2345"], session=replace(session, linker=lying))
    assert e.value.code == Reason.VERIFICATION_FAILED
    assert e.value.detail["disagreed"] == ["ABCD2345"]


def test_a_snapshot_read_reports_unverified_rather_than_failed(zotero, monkeypatch, session):
    """Zotero holds the database locked while it runs, so the post-write read comes
    from `immutable=1` — a point-in-time view that can lag a just-committed write.
    Calling that a FAILED write would be asserting from a source that is not
    authority. It reports `unverified` and says to look in Zotero."""
    zotero.add("ABCD2345", "A Paper")
    store = zotero.store()
    real_connect = store._connect
    monkeypatch.setattr(store, "_connect", lambda: (real_connect()[0], ReadMode.SNAPSHOT))

    out = trash_items(
        ["ABCD2345"],
        session=replace(session, store=store, linker=FakeLinker(zotero, apply=False)),
    )
    assert out["ok"] is True
    assert out["verification"]["verified"] == "unverified"
    assert out["verification"]["read_mode"] == "immutable=1"


def test_the_plugins_own_missing_list_is_treated_as_a_partial_apply(zotero, session):
    """The existence gate just confirmed every key, so a `missing` entry means the
    database read and the running Zotero disagree — a race. The write was partial and
    the caller has to hear it."""
    zotero.add("ABCD2345", "A Paper")
    zotero.add("WXYZ6789", "Another")
    racing = FakeLinker(zotero, missing=["WXYZ6789"])
    with pytest.raises(WriteBlocked) as e:
        trash_items(["ABCD2345", "WXYZ6789"], session=replace(session, linker=racing))
    assert e.value.code == Reason.PARTIAL_APPLY
    assert Path(e.value.detail["undo_manifest"]).exists()


# --------------------------------------------------------------------------
# the error contract, and what is deliberately absent
# --------------------------------------------------------------------------

def test_write_blocked_serialises_for_an_mcp_layer():
    """`code` is the stable field. An MCP tool that has to react differently to
    'Zotero is not running' than to 'that key does not exist' branches on this, not
    on substrings of an English sentence."""
    err = WriteBlocked(Reason.ALREADY_TRASHED, "prose for the human", {"noop": ["ABCD2345"]})
    assert err.as_dict() == {
        "ok": False,
        "code": "already_trashed",
        "reason": "prose for the human",
        "detail": {"noop": ["ABCD2345"]},
    }


def test_every_raised_code_is_a_declared_reason(zotero, session):
    """Guards against a hand-written code string drifting out of `Reason`, which
    would break a consumer branching on it with no test failure anywhere."""
    zotero.add("ABCD2345", "Already Gone", trashed=True)
    with pytest.raises(WriteBlocked) as e:
        trash_items(["ABCD2345"], session=session)
    assert e.value.code in ALL_REASONS


def test_no_verb_can_erase_an_item(zotero, session):
    """DESIGN.md excludes hard erase and empty-trash, and no plugin exposes an endpoint
    for either. Asserted rather than trusted, because the difference between this
    package being safe and unsafe is that nothing in it can destroy an item.

    `delete_collection` is the one verb whose NAME says delete, and it deletes a
    folder, not its contents — which is exactly why the name alone is not the check."""
    import zotero_core.application

    surface = " ".join(dir(zotero_core.application)).lower()
    for forbidden in ("erase", "empty_trash", "emptytrash", "purge", "destroy", "delete_item"):
        assert forbidden not in surface, f"public surface exposes {forbidden!r}"

    zotero.add("ABCD2345", "A Paper")
    trash_items(["ABCD2345"], session=session)
    # Trashing is a row in deletedItems, never a DELETE against items: the row survives.
    assert zotero.ids["ABCD2345"] == 1
    assert zotero.is_trashed("ABCD2345")


def test_the_write_modules_issue_no_sql():
    """The suite's rule is that mutation never touches zotero.sqlite directly. The only
    SQL on this path is `zotero_core`, which is read-only — so the write modules
    must contain no database access at all.

    Parsed rather than grepped. The first version of this test searched the source text
    for 'update ' and failed on the word "update" in a docstring, which is the classic
    way a structural assertion becomes a prose lint. The AST only sees code."""
    import ast

    import zotero_core.application.services.collections as collections_mod
    import zotero_core.application.services.liveness as liveness_mod
    import zotero_core.application.services.verbs as writes_mod
    import zotero_core.infrastructure.journal as journal_mod
    import zotero_core.infrastructure.transports.cookjohn as cookjohn_mod
    import zotero_core.infrastructure.transports.linker as linker_mod

    for module in (
        writes_mod, collections_mod, linker_mod, cookjohn_mod, liveness_mod, journal_mod
    ):
        tree = ast.parse(Path(module.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "sqlite3", f"{module.__name__} imports sqlite3"
            if isinstance(node, ast.ImportFrom):
                assert node.module != "sqlite3", f"{module.__name__} imports from sqlite3"
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in (
                    "execute", "executescript", "executemany",
                ), f"{module.__name__} calls .{node.func.attr}()"


# --------------------------------------------------------------------------
# the fixture itself — if this drifts, every test above is meaningless
# --------------------------------------------------------------------------

def test_the_fixture_stores_trash_the_way_zotero_does(zotero):
    """Zotero has no `deleted` column on `items`; the trash is a row's presence in
    `deletedItems`. A fixture with a boolean flag would let SQL that cannot run
    against the real library pass here."""
    zotero.add("ABCD2345", "A Paper")
    states = zotero.store().item_states(["ABCD2345"])
    assert states["ABCD2345"].trashed is False
    zotero.trash("ABCD2345")
    assert zotero.store().item_states(["ABCD2345"])["ABCD2345"].trashed is True


def test_the_fixture_stores_titles_the_way_zotero_does(zotero):
    """Through itemData -> itemDataValues joined on fields.fieldName='title', not a
    column on items."""
    zotero.add("ABCD2345", "Distinctive Title Here")
    assert zotero.store().item_states(["ABCD2345"])["ABCD2345"].title == "Distinctive Title Here"


def test_a_note_keeps_its_title_on_itemnotes(zotero):
    """Notes are the exception: their title is a column on itemNotes. The read
    COALESCEs both sources, so this asserts the second one is reachable."""
    zotero.add("NOTEKEY1", "A Note Title", item_type="note")
    assert zotero.store().item_states(["NOTEKEY1"])["NOTEKEY1"].title == "A Note Title"


def test_an_unknown_key_comes_back_reported_not_omitted(zotero):
    """The caller has to be able to tell 'you asked about 2 and 1 is missing' from
    'you asked about 1'."""
    zotero.add("ABCD2345", "Real")
    states = zotero.store().item_states(["ABCD2345", "NOTHERE2"])
    assert set(states.states) == {"ABCD2345", "NOTHERE2"}
    assert states.missing == ("NOTHERE2",)
    assert states.live == ("ABCD2345",)


def test_the_store_reports_which_read_mode_answered(zotero):
    """An unlocked fixture answers on mode=ro. Against the live library with Zotero
    running it falls back to immutable=1 — and the write path's verification changes
    behaviour based on which, so the label has to be real."""
    zotero.add("ABCD2345", "Real")
    assert zotero.store().item_states(["ABCD2345"]).read_mode == "mode=ro"


def test_an_empty_lookup_costs_no_connection(zotero):
    assert zotero.store().item_states([]).read_mode == "none"


def test_a_missing_database_is_an_error_not_an_empty_answer(tmp_path):
    """Silently reporting 'no items exist' for an absent database would make every
    existence gate pass vacuously."""
    from zotero_core.infrastructure.sqlite.connect import ZoteroReadError

    with pytest.raises(ZoteroReadError):
        ZoteroItemStore(tmp_path / "absent.sqlite").item_states(["ABCD2345"])


# --------------------------------------------------------------------------
# the isolation itself
# --------------------------------------------------------------------------


def test_the_real_zotero_defaults_are_unreachable_from_a_test():
    """The guard that protects a real library from 371 tests, asserted rather than assumed.

    `_never_touch_the_real_zotero` is autouse, so this runs inside it. It had an opt-out
    (`if "real_defaults" in request.fixturenames`) that never worked -- no such fixture
    was ever defined -- so nothing here was ever verified either. Removed 2026-08-19;
    this is what replaces it.
    """
    from pathlib import Path

    from zotero_core.infrastructure.sqlite import items
    from zotero_core.infrastructure.transports import cookjohn, linker

    assert not Path(items.DEFAULT_ZOTERO_DB).exists()
    assert ":1/" in linker.DEFAULT_LINKER_URL
    assert ":1/" in cookjohn.DEFAULT_COOKJOHN_URL


def test_opening_a_real_http_connection_from_a_test_raises():
    import urllib.request

    with pytest.raises(AssertionError, match="real HTTP connection"):
        urllib.request.urlopen("http://127.0.0.1:23119/")
