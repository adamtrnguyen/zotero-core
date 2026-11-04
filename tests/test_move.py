"""`move_items_between_collections` -- the verb that did not exist.

Before this, a move was `add_items_to_collection` then `remove_items_from_collection`:
two calls, each with its own gates, only the second journalled, and no rollback. A
failure between them left the items in BOTH collections while the caller held a success
envelope from the add.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from zotero_core.application.services.collections import move_items_between_collections
from zotero_core.domain.errors import Reason, WriteBlocked


def _setup(zotero):
    """Two collections, two items, both filed in the source."""
    src = zotero.add_collection("Source")
    dst = zotero.add_collection("Target")
    zotero.add("AAAA1111", title="First")
    zotero.add("BBBB2222", title="Second")
    zotero.add_to_collection("AAAA1111", src)
    zotero.add_to_collection("BBBB2222", src)
    return src, dst


def test_a_move_leaves_the_items_only_in_the_target(zotero, session):
    src, dst = _setup(zotero)
    result = move_items_between_collections(src, dst, ["AAAA1111"], session=session)
    assert result["ok"] is True
    assert zotero.collection_members(dst) == ["AAAA1111"]
    assert zotero.collection_members(src) == ["BBBB2222"]


def test_a_move_verifies_both_sides(zotero, session):
    """The two-call route verified nothing at all."""
    src, dst = _setup(zotero)
    result = move_items_between_collections(src, dst, ["AAAA1111"], session=session)
    assert result["verification"] == {
        "verified": True,
        "still_in_source": [],
        "missing_from_target": [],
    }


def test_a_move_writes_one_manifest_with_a_real_inverse(zotero, session):
    """The two-call route journalled only the removal, so the inverse it recorded put
    the item back in the source without taking it out of the target."""
    src, dst = _setup(zotero)
    result = move_items_between_collections(src, dst, ["AAAA1111"], session=session)
    payload = json.loads(pathlib.Path(result["undo_manifest"]).read_text())
    assert payload["op"] == "move_items_between_collections"
    assert "move_items_between_collections" in payload["inverse"]
    # the inverse is the same move with the collections swapped
    assert payload["inverse"].index(repr(dst)) < payload["inverse"].index(repr(src))


def test_moving_several_items_at_once(zotero, session):
    src, dst = _setup(zotero)
    move_items_between_collections(
        src,
        dst,
        ["AAAA1111", "BBBB2222"],
        session=session,
    )
    assert sorted(zotero.collection_members(dst)) == ["AAAA1111", "BBBB2222"]
    assert zotero.collection_members(src) == []


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


def test_moving_to_the_same_collection_is_refused(zotero, session):
    src, _ = _setup(zotero)
    with pytest.raises(WriteBlocked) as excinfo:
        move_items_between_collections(src, src, ["AAAA1111"], session=session)
    assert excinfo.value.code == Reason.NOTHING_TO_DO


def test_moving_an_item_that_is_not_in_the_source_is_refused(zotero, session):
    """Through the two-call route this is a SILENT no-op that reports success: the add
    succeeds, the remove removes nothing, and the caller is told it worked."""
    src, dst = _setup(zotero)
    zotero.add("CCCC3333", title="Unfiled")
    with pytest.raises(WriteBlocked) as excinfo:
        move_items_between_collections(src, dst, ["CCCC3333"], session=session)
    assert excinfo.value.code == Reason.NOTHING_TO_DO
    assert excinfo.value.detail["not_in_source"] == ["CCCC3333"]
    # and nothing moved
    assert zotero.collection_members(dst) == []


def test_force_files_an_unsourced_item_and_names_it(zotero, session):
    src, dst = _setup(zotero)
    zotero.add("CCCC3333", title="Unfiled")
    result = move_items_between_collections(
        src,
        dst,
        ["AAAA1111", "CCCC3333"],
        force=True,
        session=session,
    )
    assert result["not_in_source"] == ["CCCC3333"]
    assert sorted(zotero.collection_members(dst)) == ["AAAA1111", "CCCC3333"]


def test_an_unknown_collection_is_refused(zotero, session):
    src, _ = _setup(zotero)
    with pytest.raises(WriteBlocked) as excinfo:
        move_items_between_collections(src, "NOSUCH00", ["AAAA1111"], session=session)
    assert excinfo.value.code == Reason.UNKNOWN_COLLECTION_KEY


def test_an_unknown_item_is_refused(zotero, session):
    src, dst = _setup(zotero)
    with pytest.raises(WriteBlocked) as excinfo:
        move_items_between_collections(src, dst, ["NOPE0000"], session=session)
    assert excinfo.value.code == Reason.UNKNOWN_ITEM_KEYS


def test_a_malformed_collection_key_is_refused(zotero, session):
    src, _ = _setup(zotero)
    with pytest.raises(WriteBlocked) as excinfo:
        move_items_between_collections(src, "too-short", ["AAAA1111"], session=session)
    assert excinfo.value.code == Reason.MALFORMED_ITEM_KEY


# --------------------------------------------------------------------------
# rollback -- the property the two-call route cannot have
# --------------------------------------------------------------------------


def test_a_failing_remove_rolls_the_add_back(zotero, cookjohn, session):
    """The library must end exactly where it started, and the code must say so.

    `rolled_back` is a DIFFERENT code from `partial_apply` on purpose: rolled-back means
    try again, partial means go and look at your library.
    """
    src, dst = _setup(zotero)
    real_call = cookjohn.call

    def fail_the_remove(tool, args):
        # Only the SOURCE remove fails; the rollback removes from the TARGET and must
        # be allowed through, or this tests the wrong path.
        if tool == "remove_items_from_collection" and args["collectionKey"] == src:
            raise RuntimeError("cookjohn fell over")
        return real_call(tool, args)

    cookjohn.call = fail_the_remove
    with pytest.raises(WriteBlocked) as excinfo:
        move_items_between_collections(src, dst, ["AAAA1111"], session=session)

    assert excinfo.value.code == Reason.ROLLED_BACK
    # exactly as it started: still in source, NOT in target
    assert zotero.collection_members(src) == ["AAAA1111", "BBBB2222"]
    assert zotero.collection_members(dst) == []


def test_a_failing_rollback_is_reported_as_partial_not_rolled_back(zotero, cookjohn, session):
    """The one path that leaves a state nobody asked for. It must not be quiet."""
    src, dst = _setup(zotero)
    real_call = cookjohn.call

    def fail_every_remove(tool, args):
        if tool == "remove_items_from_collection":
            raise RuntimeError("cookjohn fell over")
        return real_call(tool, args)

    cookjohn.call = fail_every_remove
    with pytest.raises(WriteBlocked) as excinfo:
        move_items_between_collections(src, dst, ["AAAA1111"], session=session)
    # both removes fail, so the rollback fails too
    assert excinfo.value.code == Reason.PARTIAL_APPLY
    assert "rollback_error" in excinfo.value.detail
    assert excinfo.value.detail["undo_manifest"]


def test_the_add_happens_before_the_remove(zotero, cookjohn, session):
    """Order is deliberate. The intermediate state must be "in both" -- recoverable and
    visible -- never "in neither", which looks like the items vanished."""
    src, dst = _setup(zotero)
    seen: list[str] = []
    real_call = cookjohn.call

    def record(tool, args):
        seen.append(tool)
        return real_call(tool, args)

    cookjohn.call = record
    move_items_between_collections(src, dst, ["AAAA1111"], session=session)
    assert seen.index("add_items_to_collection") < seen.index("remove_items_from_collection")
