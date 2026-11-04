"""Collection CRUD. All of it rides cookjohn's MCP; the caller never sees that.

Collections are the one part of the surface where the plugin exposes a genuinely
dangerous option, and it is a keyword argument on an innocuous-sounding verb:

    delete_collection(collectionKey, deleteItems=True)

cookjohn's own description says "WARNING: This is a destructive operation... Set
deleteItems=true to also send items in the collection to trash." So one boolean turns
"delete this folder" into "trash everything filed in it". It is gated here, refused
without `force=True`, and the membership is journalled first -- because the inverse of
that cascade is a list of item keys nobody would otherwise have.

Deleting a collection is NOT deleting its items by default, which is worth stating
plainly: the items stay in the library, they just stop being filed there. That makes
the default form recoverable only in part -- the collection itself is gone from the
`collections` table and cannot be restored from the trash the way an item can, since
Zotero's trash holds items. So the manifest records the name, the parent, and every
member key, which together are enough to rebuild it by hand.
"""

from __future__ import annotations

from zotero_core.application.results import ok
from zotero_core.application.services.session import WriteSession
from zotero_core.domain.errors import Reason, WriteBlocked
from zotero_core.domain.services.identity import is_key


def _check_collection_key(collection_key: str) -> str:
    if not isinstance(collection_key, str) or not is_key(collection_key):
        raise WriteBlocked(
            Reason.MALFORMED_ITEM_KEY,
            f"{collection_key!r} is not a collection key (8 uppercase alphanumerics)",
            {"collection_key": collection_key},
        )
    return collection_key


def _find_collection(cookjohn, collection_key: str) -> dict:
    """Resolve a collection through cookjohn, refusing if it does not exist.

    Collections are resolved through the PLUGIN rather than through SQL, unlike items.
    That asymmetry is deliberate: the item path needs a bulk existence check over
    dozens of keys, where a query beats dozens of round trips, while a collection
    operation touches exactly one and the plugin's answer carries the name and parent
    that the manifest needs anyway.
    """
    reply = cookjohn.call("get_collection_details", {"collectionKey": collection_key})
    if not isinstance(reply, dict) or not reply:
        raise WriteBlocked(
            Reason.UNKNOWN_COLLECTION_KEY,
            f"no collection with key {collection_key}",
            {"collection_key": collection_key, "reply": reply},
        )
    return reply


def _member_keys(cookjohn, collection_key: str) -> list[str]:
    """Item keys filed in a collection, for the manifest. Best effort by design.

    A failure to enumerate must not block the operation: the enumeration exists to
    make an undo possible, and refusing to delete an empty-looking collection because
    a read hiccuped would be the gate protecting itself rather than the library. What
    it must not do is report an empty list as if it were confirmed -- so a failure
    surfaces as the string marker below, which reads as unknown in the manifest.
    """
    try:
        reply = cookjohn.call(
            "get_collection_items", {"collectionKey": collection_key, "limit": 1000}
        )
    except WriteBlocked:
        return ["<enumeration failed>"]
    keys: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for field in ("key", "itemKey"):
                value = node.get(field)
                if isinstance(value, str) and is_key(value):
                    keys.append(value)
                    return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(reply)
    return list(dict.fromkeys(keys))


def create_collection(
    name: str,
    *,
    parent_collection: str | None = None,
    session: WriteSession,
) -> dict:
    """Create a collection, refusing a name that already exists at the same level.

    The duplicate check is a real gate rather than politeness: Zotero permits two
    sibling collections with identical names, and `calibre2zotero` and
    `calibre-zotero-jump` BOTH look up their target collection by NAME ("calibre").
    A second collection with that name makes which one they find depend on iteration
    order, and half the imports quietly land in the wrong folder.
    """
    if not (name or "").strip():
        raise WriteBlocked(Reason.MISSING_REQUIRED_FIELD, "collection name is required")
    if parent_collection:
        _check_collection_key(parent_collection)
    info = session.require("cookjohn")

    existing = session.cookjohn.call("search_collections", {"q": name, "limit": 50})
    clashes = _same_name_siblings(existing, name, parent_collection)
    if clashes:
        raise WriteBlocked(
            Reason.DUPLICATE_ITEM,
            f"a collection named {name!r} already exists at this level ({clashes}) — two "
            "siblings with one name make a name-based lookup nondeterministic",
            {"name": name, "existing": clashes},
        )

    arguments: dict = {"name": name}
    if parent_collection:
        arguments["parentCollection"] = parent_collection
    reply = session.cookjohn.call("create_collection", arguments)
    key = session.cookjohn.find_key(reply, prefer=("collectionKey", "key"))
    if not key:
        raise WriteBlocked(
            Reason.COOKJOHN_RETURNED_NO_KEY,
            "create_collection returned no collection key — nothing verifiable was created",
            {"reply": reply},
        )
    return ok(
        "create_collection",
        transport="cookjohn",
        collection_key=key,
        name=name,
        parent_collection=parent_collection,
        cookjohn=reply,
        verification=_verify_collection(
            session.collections, key, name=name, parent_key=parent_collection
        ),
        undo_call=f"delete_collection({key!r})",
        versions=info,
    )


def _same_name_siblings(reply, name: str, parent_collection: str | None) -> list[dict]:
    """Collections matching `name` exactly, at the same nesting level.

    Case-insensitive, because "Calibre" and "calibre" are the same folder to a human
    doing a name lookup and Zotero will happily hold both.
    """
    found: list[dict] = []
    wanted = name.strip().casefold()

    def walk(node):
        if isinstance(node, dict):
            node_name = node.get("name") or node.get("collectionName")
            if isinstance(node_name, str) and node_name.strip().casefold() == wanted:
                parent = node.get("parentCollection") or node.get("parentKey") or None
                if (parent or None) == (parent_collection or None):
                    found.append({"key": node.get("key"), "name": node_name})
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(reply)
    return found


def update_collection(
    collection_key: str,
    *,
    name: str | None = None,
    parent_collection: str | None = None,
    journal_dir: str | None = None,
    session: WriteSession,
) -> dict:
    """Rename or re-parent a collection, journalling the previous name and parent.

    `parent_collection=""` moves it to the top level, which is cookjohn's own
    convention. That is why the parameter defaults to None rather than "": the two
    mean different things here, and collapsing them would make every rename also
    move the collection to the root.
    """
    _check_collection_key(collection_key)
    if name is None and parent_collection is None:
        raise WriteBlocked(
            Reason.NOTHING_TO_DO,
            "neither name nor parent_collection given — refusing a no-op update",
            {"collection_key": collection_key},
        )
    info = session.require("cookjohn")
    before = _find_collection(session.cookjohn, collection_key)
    old_name = before.get("name") or before.get("collectionName")
    old_parent = before.get("parentCollection") or before.get("parentKey") or None

    # The inverse restores BOTH halves, whichever this call changes: a rename that also
    # re-parents must come back under the old parent. `""` is cookjohn's top level.
    manifest = session.journal.write_manifest(
        "update_collection",
        before={
            "collection_key": collection_key,
            "name": old_name,
            "parent_collection": old_parent,
        },
        inverse=(
            f"update_collection({collection_key!r}, name={old_name!r}, "
            f"parent_collection={(old_parent or '')!r})"
        ),
        journal_dir=journal_dir,
    )
    arguments: dict = {"collectionKey": collection_key}
    if name is not None:
        arguments["name"] = name
    if parent_collection is not None:
        arguments["parentCollection"] = parent_collection
    reply = session.cookjohn.call("update_collection", arguments)
    return ok(
        "update_collection",
        transport="cookjohn",
        collection_key=collection_key,
        before={"name": old_name, "parent_collection": old_parent},
        name=name,
        parent_collection=parent_collection,
        cookjohn=reply,
        verification=_verify_collection(
            session.collections, collection_key, name=name, parent_key=parent_collection
        ),
        undo_manifest=manifest,
        versions=info,
    )


def _verify_collection(collections, collection_key: str, *, name=None, parent_key=None) -> dict:
    """Confirm a collection exists and, where given, carries the expected name/parent.

    `name=None` means "do not check the name" rather than "expect no name" -- the same
    distinction `results.ok` draws between an absent key and an explicit null, and for
    the same reason: `update_collection` may rename, re-parent, or both, and checking a
    field the caller never set would invent a failure.
    """
    tree = collections.tree()
    node = next((n for n in tree.flat() if n.key == collection_key), None)
    if node is None:
        return {
            "verified": "unverified",
            "read_mode": tree.read_mode,
            "note": (
                "the collection does not read back. If read_mode is immutable=1 this may "
                "be a snapshot lagging the commit rather than a failed write"
            ),
        }
    disagreed = {}
    if name is not None and node.name != name:
        disagreed["name"] = {"expected": name, "found": node.name}
    if parent_key is not None and node.parent_key != (parent_key or None):
        disagreed["parent_key"] = {"expected": parent_key, "found": node.parent_key}
    if disagreed:
        return {"verified": "unverified", "read_mode": tree.read_mode, "disagreed": disagreed}
    return {"verified": True, "read_mode": tree.read_mode, "path": node.path}


def _members_of(collections, collection_key: str) -> tuple[set[str], str]:
    members = collections.items(collection_key, include_trashed=True)
    return {m.item_key for m in members.members}, members.read_mode


def _verify_membership(collections, collection_key: str, keys, *, present: bool) -> dict:
    """Confirm each key IS (or is NOT) in the collection after the write.

    `present=True` for a filing, False for an unfiling -- one function because the
    question is the same one asked in two directions, and splitting it would be two
    places to keep in step.

    This REPORTS rather than refuses. Filing an item that is already in a collection,
    or unfiling one that was never in it, both succeed in Zotero and are no-ops; making
    them errors would break calls that work today. The report lets a caller tell a no-op
    from a real change -- `move_items_between_collections` refuses
    the same situation, but only because a silent no-op there produces a WRONG RESULT
    (the items end up filed in the target and never removed from the source) rather than
    merely a pointless one.
    """
    after, read_mode = _members_of(collections, collection_key)
    wanted = set(keys)
    wrong = sorted(wanted - after) if present else sorted(wanted & after)
    if not wrong:
        return {"verified": True, "read_mode": read_mode}
    return {
        "verified": "unverified",
        "read_mode": read_mode,
        ("missing_from_collection" if present else "still_in_collection"): wrong,
        "note": (
            "the membership does not read back as expected. If read_mode is immutable=1 "
            "this may be a snapshot lagging the commit rather than a failed write"
        ),
    }


def _verify_gone(collections, collection_key: str) -> dict:
    """Confirm a collection is ABSENT. The one verification that inverts.

    Every other read-back asks "is the thing I wrote there?"; this asks "is the thing I
    removed gone?", so a hit is the failure and a miss is the success.

    A "still there" answer is NOT proof the delete failed. The read layer falls back to
    an `immutable=1` snapshot when Zotero holds the write lock, and a snapshot taken
    before the commit still shows the collection. Reported as `unverified` with the read
    mode attached rather than as a failure -- claiming a successful delete failed sends
    the caller to rebuild something that is already gone, which for a collection means
    recreating it under a NEW key and re-filing every member.
    """
    tree = collections.tree()
    still_there = any(node.key == collection_key for node in tree.flat())
    if not still_there:
        return {"verified": True, "read_mode": tree.read_mode}
    return {
        "verified": "unverified",
        "read_mode": tree.read_mode,
        "note": (
            "the collection still reads as present. If read_mode is immutable=1 this may "
            "be a snapshot taken before the commit rather than a failed delete — check "
            "Zotero before rebuilding, because a rebuild creates a NEW key"
        ),
    }


def delete_collection(
    collection_key: str,
    *,
    delete_items: bool = False,
    force: bool = False,
    journal_dir: str | None = None,
    session: WriteSession,
) -> dict:
    """Delete a collection. Items stay in the library unless `delete_items=True`.

    `delete_items=True` sends every member to the trash and REQUIRES force=True. One
    boolean turning "delete this folder" into "trash everything in it" is precisely
    the kind of option that should not be reachable by a default, and the membership
    is journalled first so the cascade has an inverse.

    Note the asymmetry with items, which is real and not hidden: a trashed item can
    be restored, a deleted collection cannot. Zotero's trash holds items. So the
    manifest records name, parent and every member key -- enough to rebuild the
    collection by hand, which is the only undo there is.
    """
    _check_collection_key(collection_key)
    info = session.require("cookjohn")
    before = _find_collection(session.cookjohn, collection_key)
    members = _member_keys(session.cookjohn, collection_key)

    if delete_items and not force:
        raise WriteBlocked(
            Reason.REFUSING_CASCADE_DELETE,
            f"delete_items=True would send {len(members)} item(s) to the trash as well as "
            "deleting the collection — pass force=True to mean it",
            {
                "collection_key": collection_key,
                "name": before.get("name") or before.get("collectionName"),
                "members": members,
                "hint": "force=True, or delete_items=False to keep the items",
            },
        )

    name = before.get("name") or before.get("collectionName")
    manifest = session.journal.write_manifest(
        "delete_collection",
        before={
            "collection_key": collection_key,
            "name": name,
            "parent_collection": before.get("parentCollection") or before.get("parentKey"),
            "members": members,
            "delete_items": delete_items,
        },
        # A collection cannot be restored from the trash, so the inverse is a rebuild
        # rather than an undo, and the manifest is the only record of what to rebuild.
        inverse=(
            f"create_collection({name!r}) then add_items_to_collection(<new key>, {members!r})"
        ),
        journal_dir=journal_dir,
    )
    arguments: dict = {"collectionKey": collection_key}
    if delete_items:
        arguments["deleteItems"] = True
    reply = session.cookjohn.call("delete_collection", arguments)
    return ok(
        "delete_collection",
        transport="cookjohn",
        collection_key=collection_key,
        name=name,
        members_at_deletion=members,
        items_trashed=bool(delete_items),
        cookjohn=reply,
        verification=_verify_gone(session.collections, collection_key),
        undo_manifest=manifest,
        versions=info,
    )


def add_items_to_collection(
    collection_key: str,
    item_keys,
    *,
    session: WriteSession,
) -> dict:
    """File items into a collection. No manifest — the inverse is a removal."""
    from zotero_core.application.services.verbs import check_keys, require_items

    _check_collection_key(collection_key)
    keys = check_keys(item_keys)
    info = session.require("cookjohn")
    require_items(session.store, keys)
    _find_collection(session.cookjohn, collection_key)
    before_members, _ = _members_of(session.collections, collection_key)
    reply = session.cookjohn.call(
        "add_items_to_collection", {"collectionKey": collection_key, "itemKeys": keys}
    )
    return ok(
        "add_items_to_collection",
        transport="cookjohn",
        collection_key=collection_key,
        item_keys=keys,
        # Informational, not a refusal: these were no-ops. Reported so a caller can tell
        # "I filed 3 items" from "I filed 1 and re-filed 2".
        already_present=sorted(set(keys) & before_members),
        cookjohn=reply,
        verification=_verify_membership(session.collections, collection_key, keys, present=True),
        undo_call=f"remove_items_from_collection({collection_key!r}, {keys!r})",
        versions=info,
    )


def remove_items_from_collection(
    collection_key: str,
    item_keys,
    *,
    journal_dir: str | None = None,
    session: WriteSession,
) -> dict:
    """Unfile items from a collection. The items stay in the library.

    Journalled, because it removes something -- and because the distinction between
    this and `trash_items` is exactly the one a caller in a hurry gets wrong. Nothing
    here deletes an item.
    """
    from zotero_core.application.services.verbs import check_keys, require_items

    _check_collection_key(collection_key)
    keys = check_keys(item_keys)
    info = session.require("cookjohn")
    require_items(session.store, keys)
    _find_collection(session.cookjohn, collection_key)

    before_members, _ = _members_of(session.collections, collection_key)
    manifest = session.journal.write_manifest(
        "remove_items_from_collection",
        before={"collection_key": collection_key, "item_keys": keys},
        inverse=f"add_items_to_collection({collection_key!r}, {keys!r})",
        journal_dir=journal_dir,
    )
    reply = session.cookjohn.call(
        "remove_items_from_collection", {"collectionKey": collection_key, "itemKeys": keys}
    )
    return ok(
        "remove_items_from_collection",
        transport="cookjohn",
        collection_key=collection_key,
        item_keys=keys,
        # Informational: these were never in the collection, so removing them changed
        # nothing. The undo manifest still names them, which is correct -- re-adding an
        # item that was not there is itself a no-op.
        not_in_collection=sorted(set(keys) - before_members),
        cookjohn=reply,
        verification=_verify_membership(session.collections, collection_key, keys, present=False),
        undo_manifest=manifest,
        undo_call=f"add_items_to_collection({collection_key!r}, {keys!r})",
        versions=info,
    )


def move_items_between_collections(
    from_collection_key: str,
    to_collection_key: str,
    item_keys,
    *,
    force: bool = False,
    journal_dir: str | None = None,
    session: WriteSession,
) -> dict:
    """Move items from one collection to another. ONE verb, ONE manifest, WITH ROLLBACK.

    WHY THIS EXISTS
    ---------------
    `add_items_to_collection` then `remove_items_from_collection` is two calls, only the
    second journalled, and NO rollback: if the remove fails the items are left in BOTH
    collections, silently, behind a success envelope from the add.

    WHAT MAKES IT SAFE
    ------------------
    Order is deliberate: ADD first, then remove. The intermediate state is "in both",
    which is recoverable and visible, rather than "in neither", which looks like the
    items vanished. If the remove fails, the add is undone and the library is exactly as
    it started -- reported as `rolled_back`, which is a DIFFERENT code from
    `partial_apply` because the two need different responses: rolled-back means try
    again, partial means go and look.

    Membership is checked BEFORE moving. Moving an item that is not in the source
    collection is a silent no-op through the two-call route -- the add succeeds, the
    remove removes nothing, and the result claims success. Here it refuses, unless
    `force`, in which case it files them into the target and says which were not in the
    source.
    """
    from zotero_core.application.services.verbs import check_keys, require_items

    _check_collection_key(from_collection_key)
    _check_collection_key(to_collection_key)
    if from_collection_key == to_collection_key:
        raise WriteBlocked(
            Reason.NOTHING_TO_DO,
            "source and target are the same collection — refusing a no-op move",
            {"collection_key": from_collection_key},
        )
    keys = check_keys(item_keys)
    info = session.require("cookjohn")
    require_items(session.store, keys)
    _find_collection(session.cookjohn, from_collection_key)
    _find_collection(session.cookjohn, to_collection_key)

    before = {
        m.item_key
        for m in session.collections.items(from_collection_key, include_trashed=True).members
    }
    absent = [key for key in keys if key not in before]
    if absent and not force:
        raise WriteBlocked(
            Reason.NOTHING_TO_DO,
            f"{len(absent)} of {len(keys)} item(s) are not in the source collection — "
            "moving them would only file them into the target",
            {"not_in_source": absent, "source_holds": sorted(before)},
        )

    target_before = {
        m.item_key
        for m in session.collections.items(to_collection_key, include_trashed=True).members
    }
    # The rollback undoes the add, and the add changed nothing for these.
    new_to_target = [key for key in keys if key not in target_before]

    manifest = session.journal.write_manifest(
        "move_items_between_collections",
        before={
            "from_collection_key": from_collection_key,
            "to_collection_key": to_collection_key,
            "item_keys": keys,
            "source_members_before": sorted(before),
            "already_in_target": sorted(set(keys) & target_before),
        },
        inverse=(
            f"move_items_between_collections({to_collection_key!r}, "
            f"{from_collection_key!r}, {keys!r})"
        ),
        journal_dir=journal_dir,
    )

    added = session.cookjohn.call(
        "add_items_to_collection", {"collectionKey": to_collection_key, "itemKeys": keys}
    )
    try:
        removed = session.cookjohn.call(
            "remove_items_from_collection",
            {"collectionKey": from_collection_key, "itemKeys": keys},
        )
    except Exception as exc:
        # Undo the add so the library is where it started: unfile only what the add
        # filed. If the undo ALSO fails the caller must be told loudly -- that is the one
        # path here that leaves a state nobody asked for.
        try:
            if new_to_target:
                session.cookjohn.call(
                    "remove_items_from_collection",
                    {"collectionKey": to_collection_key, "itemKeys": new_to_target},
                )
        except Exception as undo_exc:
            raise WriteBlocked(
                Reason.PARTIAL_APPLY,
                "the remove failed AND the rollback failed — items are in both "
                "collections; undo with the manifest",
                {
                    "error": str(exc),
                    "rollback_error": str(undo_exc),
                    "undo_manifest": manifest,
                },
            ) from exc
        raise WriteBlocked(
            Reason.ROLLED_BACK,
            f"could not remove from the source collection: {exc} — the add was undone, "
            "nothing changed",
            {"error": str(exc), "undo_manifest": manifest},
        ) from exc

    # Re-read BOTH sides. The two-call route verified nothing at all.
    after_source = {
        m.item_key
        for m in session.collections.items(from_collection_key, include_trashed=True).members
    }
    after_target = {
        m.item_key
        for m in session.collections.items(to_collection_key, include_trashed=True).members
    }
    still_in_source = sorted(set(keys) & after_source)
    missing_from_target = sorted(set(keys) - after_target)
    verified = not still_in_source and not missing_from_target

    return ok(
        "move_items_between_collections",
        transport="cookjohn",
        from_collection_key=from_collection_key,
        to_collection_key=to_collection_key,
        item_keys=keys,
        not_in_source=absent,
        cookjohn={"added": added, "removed": removed},
        verification={
            "verified": verified,
            "still_in_source": still_in_source,
            "missing_from_target": missing_from_target,
        },
        undo_manifest=manifest,
        undo_call=(
            f"move_items_between_collections({to_collection_key!r}, "
            f"{from_collection_key!r}, {keys!r})"
        ),
        versions=info,
    )
