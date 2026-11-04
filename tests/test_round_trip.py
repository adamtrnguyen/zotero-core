"""Write, then undo: the library must be back where it started.

Every verb that journals a replayable inverse is run from a NON-EMPTY state (existing
tags, fields, creators, members, parents), then `undo()` with no argument, and the whole
fixture library is compared before and after. A verb whose inverse restores only part of
what it changed fails here, whichever part that is.
"""

from __future__ import annotations

import sqlite3

import pytest

from zotero_core.application.services import collections as coll
from zotero_core.application.services import replay, verbs


def _snapshot(zotero) -> dict:
    """Everything a write here can change, for every item and collection in the fixture."""
    con = sqlite3.connect(f"file:{zotero.path}?mode=ro", uri=True)
    try:
        creators = {
            key: [
                tuple(row)
                for row in con.execute(
                    "SELECT c.firstName, c.lastName, c.fieldMode FROM itemCreators ic "
                    "JOIN creators c ON c.creatorID = ic.creatorID "
                    "WHERE ic.itemID = ? ORDER BY ic.orderIndex",
                    (item_id,),
                )
            ]
            for key, item_id in zotero.ids.items()
        }
    finally:
        con.close()
    return {
        "items": {
            key: {
                "fields": zotero.fields_of(key),
                "tags": zotero.tags_of(key),
                "trashed": zotero.is_trashed(key),
                "creators": creators[key],
            }
            for key in zotero.ids
        },
        "collections": {
            key: {
                "parent": zotero.collection_parent(key),
                "members": zotero.collection_members(key),
                "name": dict(_collection_names(zotero)).get(key),
            }
            for key in zotero.collections
        },
    }


def _collection_names(zotero):
    con = sqlite3.connect(f"file:{zotero.path}?mode=ro", uri=True)
    try:
        return con.execute("SELECT key, collectionName FROM collections").fetchall()
    finally:
        con.close()


def _library(zotero) -> None:
    """A small library that is not empty in any of the ways a verb could assume."""
    zotero.add(
        "ITEM0001",
        "First Paper",
        fields={"date": "2020", "publicationTitle": "Journal A"},
        tags=["keep", "read"],
        creators=[{"firstName": "Ada", "lastName": "Lovelace"}],
    )
    zotero.add(
        "ITEM0002",
        "Second Paper",
        tags=["other"],
        creators=[{"firstName": "Alan", "lastName": "Turing"}],
    )
    zotero.add("ITEM0003", "Already Trashed", trashed=True)
    zotero.add_collection("Parent", key="PARENT01")
    zotero.add_collection("Source", key="SOURCE01", parent="PARENT01")
    zotero.add_collection("Target", key="TARGET01")
    for key in ("ITEM0001", "ITEM0002"):
        zotero.add_to_collection(key, "SOURCE01")
    zotero.add_to_collection("ITEM0003", "TARGET01")


WRITES = [
    pytest.param(
        lambda s: verbs.update_metadata("ITEM0001", {"date": "2021"}, session=s),
        id="update_metadata",
    ),
    pytest.param(
        lambda s: verbs.replace_creators(
            "ITEM0001", [{"firstName": "Grace", "lastName": "Hopper"}], force=True, session=s
        ),
        id="replace_creators",
    ),
    pytest.param(
        lambda s: verbs.set_tags("ITEM0001", ["new"], force=True, session=s), id="set_tags"
    ),
    pytest.param(lambda s: verbs.remove_tags("ITEM0001", ["read"], session=s), id="remove_tags"),
    pytest.param(lambda s: verbs.trash_items(["ITEM0001"], session=s), id="trash_items"),
    pytest.param(lambda s: verbs.restore_items(["ITEM0003"], session=s), id="restore_items"),
    pytest.param(
        lambda s: coll.update_collection("SOURCE01", name="Renamed", session=s),
        id="update_collection:rename",
    ),
    pytest.param(
        lambda s: coll.update_collection("SOURCE01", parent_collection="", session=s),
        id="update_collection:reparent",
    ),
    pytest.param(
        lambda s: coll.remove_items_from_collection("SOURCE01", ["ITEM0001"], session=s),
        id="remove_items_from_collection",
    ),
    pytest.param(
        lambda s: coll.move_items_between_collections(
            "SOURCE01", "TARGET01", ["ITEM0001"], session=s
        ),
        id="move_items_between_collections",
    ),
    pytest.param(
        lambda s: coll.move_items_between_collections(
            "SOURCE01", "TARGET01", ["ITEM0002"], session=s
        ),
        id="move_items_between_collections:already_in_target",
        marks=pytest.mark.xfail(
            strict=True,
            reason="the inverse is one move back, which also unfiles an item that was "
            "already in the target; one replayable call cannot express add-only-to-source",
        ),
    ),
]


@pytest.mark.parametrize("write", WRITES)
def test_undo_restores_the_library_exactly(write, zotero, session, request):
    _library(zotero)
    if request.node.callspec.id.endswith("already_in_target"):
        # ITEM0002 is filed in BOTH before the move.
        zotero.add_to_collection("ITEM0002", "TARGET01")
    before = _snapshot(zotero)

    write(session)
    assert _snapshot(zotero) != before, "the write changed nothing, so this tests nothing"
    replay.undo(session=session)

    assert _snapshot(zotero) == before
