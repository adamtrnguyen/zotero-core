"""Tests for the rest of the CRUD surface: create, update, tags, notes, collections.

The trash/restore half lives in `test_writes.py`. This file covers the verbs that ride
cookjohn's MCP instead of the linker, and the two properties that make the surface ONE
surface rather than two clients stapled together:

  * no verb's signature names a transport
  * every verb reports which transport served it

Plus the gates that are specific to these verbs, of which the important ones guard
cookjohn's two REPLACE-shaped tools — `write_tag` action='set' and `write_metadata`'s
`creators` array — both of which destroy a list as a side effect of setting one.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import replace
from pathlib import Path

import pytest

from zotero_core.application.services.collections import (
    add_items_to_collection,
    create_collection,
    delete_collection,
    move_items_between_collections,
    remove_items_from_collection,
    update_collection,
)
from zotero_core.application.services.verbs import (
    add_tags,
    create_annotation,
    create_item,
    import_attachment,
    link_attachment,
    remove_tags,
    replace_creators,
    restore_items,
    set_tags,
    trash_items,
    update_metadata,
    write_note,
)
from zotero_core.domain.errors import Reason, WriteBlocked
from zotero_core.infrastructure.sqlite.duplicates import check_duplicate, clean_doi, clean_isbn

from .conftest import FakeCookjohn


def _kw(session):
    """The collaborators every verb needs, as one kwarg.

    WAS `{"store": ..., "linker": ..., "cookjohn": ...}`. One `session` replaces the trio
    because the verbs take one now -- and because a test that had to name three
    collaborators to call one verb was documenting the coupling this refactor removed.
    """
    return {"session": session}


# --------------------------------------------------------------------------
# the surface is ONE surface — the property the refinement asked for
# --------------------------------------------------------------------------

VERBS = [
    create_item, link_attachment, import_attachment, write_note, update_metadata,
    replace_creators, add_tags, remove_tags, set_tags, create_collection,
    update_collection, delete_collection, add_items_to_collection,
    remove_items_from_collection, trash_items,
]


@pytest.mark.parametrize("verb", VERBS, ids=lambda v: v.__name__)
def test_no_verb_requires_the_caller_to_pick_a_transport(verb):
    """The whole design constraint, now provable more strongly than before.

    This asserted `linker`, `cookjohn` and `store` were each present and each defaulted to
    None -- "if any verb made one REQUIRED, the caller would be back to knowing which
    plugin serves which operation". The seams were real, but `= None` meant every verb also
    imported a concrete adapter to fall back on, which is what put infrastructure in the
    application layer and left the suite rewriting module globals to substitute a fake.

    Same property, opposite check: a verb takes ONE opaque `session` and names no transport
    at all. The caller cannot pick a plugin because there is no parameter to pick one with.
    """
    signature = inspect.signature(verb)
    assert "session" in signature.parameters, f"{verb.__name__} has no session seam"
    for name in ("linker", "cookjohn", "store"):
        assert name not in signature.parameters, (
            f"{verb.__name__} still names {name} -- the caller can pick a transport"
        )


@pytest.mark.parametrize("verb", VERBS, ids=lambda v: v.__name__)
def test_no_verb_leaks_a_port_or_a_plugin_name_into_its_signature(verb):
    names = " ".join(inspect.signature(verb).parameters).lower()
    for leak in ("23119", "23121", "mcp", "url", "endpoint"):
        assert leak not in names, f"{verb.__name__} exposes {leak!r}"


def test_the_success_envelope_has_one_shape(zotero, session):
    """The frame is built in ONE place now (`results.ok`), not spelled out 16 times.

    Before this, renaming a field or adding one every verb should carry was a 16-place
    edit with no way to notice a site that was missed. `errors.py` already made the
    argument for the failure path -- a machine-readable code beats matching substrings of
    an English sentence -- and a success envelope assembled independently 16 times has
    the same weakness one level up: the consumer is matching a shape nobody guarantees.
    """
    kw = _kw(session)
    zotero.add("PARENT12", "The Parent", item_type="book")
    for result in (
        create_item("book", {"title": "New"}, **kw),
        add_tags("PARENT12", ["x"], **kw),
        trash_items(["PARENT12"], **kw),
    ):
        assert result["ok"] is True
        assert isinstance(result["op"], str) and result["op"]
        assert result["transport"] in {"cookjohn", "linker", "both", "none"}


def test_an_explicit_null_undo_call_survives_the_builder(zotero, session):
    """OMITTED and EXPLICITLY NULL mean different things and both occur.

    `update_metadata` sets `undo_call` to None on purpose when nothing was overwritten --
    cookjohn has no field-remove, so no inverse is expressible. An envelope builder that
    dropped null keys would have turned "there is no undo for this call" into "this verb
    does not journal at all", silently, under cover of a refactor.
    """
    zotero.add("PARENT12", "The Parent", item_type="book")
    out = update_metadata("PARENT12", {"volume": "9"}, **_kw(session))
    assert "undo_call" in out
    assert out["undo_call"] is None


def test_the_builder_refuses_an_unknown_transport():
    from zotero_core.application.results import ok

    with pytest.raises(ValueError, match="unknown transport"):
        ok("x", transport="cookjhon")


def test_each_verb_reports_which_transport_served_it(zotero, tmp_path, session):
    """EVERY verb, not a spot-check, and asserted against the TABLE.

    The transport used to be hardcoded three times per verb, independently and unlinked:
    what `require_zotero(needs=...)` demands, what `session.<name>.call/post` actually
    invokes, and the literal string in the result. Nothing checked that the three agreed,
    so a verb could demand one plugin, use another, and report a third -- and the only
    coverage was two verbs asserted by hand here.

    `_ToolSpec.transport` now declares it, and this walks the whole table.
    """
    from zotero_core.interfaces.write_mcp import TOOLS

    declared = {spec.name: spec.transport for spec in TOOLS}
    kw = _kw(session)
    seen: dict[str, str] = {}

    pdf = tmp_path / "f.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    zotero.add("PARENT12", "The Parent", item_type="book", tags=["keepme"])
    zotero.add("SECOND12", "The Second", item_type="book")
    zotero.add("ATTACH12", "The PDF", item_type="attachment", parent="PARENT12")
    src = zotero.add_collection("Src")
    dst = zotero.add_collection("Dst")
    zotero.add_to_collection("PARENT12", src)

    def record(tool: str, result: dict) -> None:
        seen[tool] = result["transport"]

    record("zotero_create_item", create_item("book", {"title": "New"}, **kw))
    record("zotero_link_attachment", link_attachment("PARENT12", str(pdf), **kw))
    record("zotero_import_attachment", import_attachment("PARENT12", str(pdf), **kw))
    record("zotero_write_note", write_note("<p>n</p>", parent_item_key="PARENT12", **kw))
    record(
        "zotero_create_annotation",
        create_annotation(
            "ATTACH12",
            {"pageIndex": 0, "rects": [[10.0, 20.0, 30.0, 40.0]]},
            annotation_text="covered text",
            **kw,
        ),
    )
    record("zotero_create_collection", create_collection("Fresh", **kw))
    record("zotero_update_metadata", update_metadata("PARENT12", {"volume": "1"}, **kw))
    record("zotero_add_tags", add_tags("PARENT12", ["x"], **kw))
    record("zotero_remove_tags", remove_tags("PARENT12", ["x"], **kw))
    record("zotero_set_tags", set_tags("PARENT12", ["only"], force=True, **kw))
    record(
        "zotero_replace_creators",
        replace_creators(
            "PARENT12", [{"creatorType": "author", "lastName": "Z"}], force=True, **kw
        ),
    )
    record("zotero_update_collection", update_collection(dst, name="Dst2", **kw))
    record("zotero_add_items_to_collection", add_items_to_collection(dst, ["SECOND12"], **kw))
    record(
        "zotero_remove_items_from_collection",
        remove_items_from_collection(dst, ["SECOND12"], **kw),
    )
    record(
        "zotero_move_items_between_collections",
        move_items_between_collections(src, dst, ["PARENT12"], **kw),
    )
    record("zotero_trash_items", trash_items(["SECOND12"], **kw))
    record("zotero_restore_items", restore_items(["SECOND12"], **kw))
    record("zotero_delete_collection", delete_collection(dst, **kw))

    mismatched = {
        name: (declared[name], got) for name, got in seen.items() if declared[name] != got
    }
    assert not mismatched, f"declared != reported: {mismatched}"

    # and every plugin-backed tool in the table was actually exercised
    plugin_backed = {n for n, t in declared.items() if t in {"cookjohn", "linker"}}
    assert plugin_backed - set(seen) == set(), f"never exercised: {plugin_backed - set(seen)}"


# --------------------------------------------------------------------------
# CREATE
# --------------------------------------------------------------------------

def test_create_demands_a_title(cookjohn, session):
    """cookjohn will create a titleless item quite happily, and it is then
    near-unfindable in the GUI — the analogue of calibre-core's mandatory title rule."""
    with pytest.raises(WriteBlocked) as e:
        create_item("journalArticle", {"abstractNote": "no title"}, **_kw(session))
    assert e.value.code == Reason.MISSING_REQUIRED_FIELD
    assert cookjohn.calls == []


def test_create_blocks_on_a_matching_doi(zotero, cookjohn, session):
    zotero.add("ABCD2345", "Already Here", fields={"DOI": "10.1145/3592433"})
    with pytest.raises(WriteBlocked) as e:
        create_item(
            "journalArticle",
            {"title": "Totally Different Title", "DOI": "10.1145/3592433"},
            **_kw(session),
        )
    assert e.value.code == Reason.DUPLICATE_ITEM
    assert cookjohn.calls == []


def test_doi_matching_survives_the_three_forms_zotero_stores(zotero):
    """Translators disagree: bare, URL, and `doi:` prefixed all appear in the live
    field. Comparing them raw makes one DOI look like three and turns the strongest
    available signal into a miss."""
    assert clean_doi("https://doi.org/10.1145/3592433") == "10.1145/3592433"
    assert clean_doi("doi:10.1145/3592433") == "10.1145/3592433"
    assert clean_doi("HTTP://DX.DOI.ORG/10.1145/3592433") == "10.1145/3592433"

    zotero.add("ABCD2345", "Stored As A URL", fields={"DOI": "https://doi.org/10.1/x"})
    result = check_duplicate(zotero.store(), title="Anything", doi="doi:10.1/x")
    assert result["verdict"] == "block"


def test_isbn_matching_ignores_hyphenation(zotero):
    zotero.add("ABCD2345", "A Book", item_type="book", fields={"ISBN": "978-1-119-74482-5"})
    assert clean_isbn("978-1-119-74482-5") == "9781119744825"
    assert check_duplicate(zotero.store(), title="X", isbn="9781119744825")["verdict"] == "block"


def test_create_blocks_on_the_calibre_uuid_stamp(zotero, session):
    """THE divergence this package was built to end. `calibre2zotero` deduped on this
    stamp while `calibre-zotero-jump` deduped on a `zotero` identifier held on the
    Calibre side; the two disagree exactly when a push half-succeeded. 769 items in the
    live library carry the stamp."""
    uuid = "0f4b1c22-3d3e-4a55-9b77-1c2d3e4f5a6b"
    zotero.add("ABCD2345", "Some Book", item_type="book",
               fields={"extra": f"calibre-uuid: {uuid}"})
    with pytest.raises(WriteBlocked) as e:
        create_item(
            "book", {"title": "Same Book, Different Title"},
            calibre_uuid=uuid, **_kw(session),
        )
    assert e.value.code == Reason.DUPLICATE_ITEM
    assert [s["signal"] for s in e.value.detail["duplicate_check"]["signals"]] == ["calibre-uuid"]


def test_a_same_title_and_author_only_warns_and_still_creates(zotero, session):
    """Two editions and a preprint/published pair both look like this, and both are
    legitimately two items. So it is recorded in the result, not refused."""
    zotero.add("ABCD2345", "Systems Thinking, Systems Practice",
               creators=[{"creatorType": "author", "lastName": "Checkland"}])
    out = create_item(
        "book",
        {"title": "Systems Thinking, Systems Practice"},
        creators=[{"creatorType": "author", "firstName": "Peter", "lastName": "Checkland"}],
        **_kw(session),
    )
    assert out["ok"] is True
    assert out["duplicate_check"]["verdict"] == "warn"


def test_a_different_author_with_the_same_title_is_not_a_duplicate(zotero):
    """Lang vs Artin 'Algebra' are different books."""
    zotero.add("ABCD2345", "Algebra", creators=[{"creatorType": "author", "lastName": "Lang"}])
    result = check_duplicate(
        zotero.store(), title="Algebra",
        creators=[{"creatorType": "author", "lastName": "Artin"}],
    )
    assert result["verdict"] == "ok"


def test_a_trashed_duplicate_does_not_block(zotero, session):
    """Something in the trash is on its way out. Refusing to re-create it would leave
    the caller stuck behind a decision someone else already made — but the signal is
    still reported so they can tell the difference."""
    zotero.add("ABCD2345", "On Its Way Out", trashed=True, fields={"DOI": "10.1/x"})
    out = create_item(
        "journalArticle", {"title": "Fresh Copy", "DOI": "10.1/x"},
        **_kw(session),
    )
    assert out["ok"] is True
    assert out["duplicate_check"]["verdict"] == "ok"
    assert out["duplicate_check"]["signals"][0]["trashed"] is True


def test_force_creates_through_a_block(zotero, session):
    zotero.add("ABCD2345", "Already Here", fields={"DOI": "10.1/x"})
    out = create_item(
        "journalArticle", {"title": "Deliberate Second Copy", "DOI": "10.1/x"},
        force=True, **_kw(session),
    )
    assert out["ok"] is True
    assert out["duplicate_check"]["verdict"] == "block"


def test_the_calibre_uuid_stamp_is_appended_not_assigned_over(cookjohn, session):
    """`extra` is free text carrying other people's conventions — Better BibTeX keeps
    its Citation Key there. Replacing the field to add one line would delete them."""
    out = create_item(
        "book",
        {"title": "A Book", "extra": "Citation Key: smith2020"},
        calibre_uuid="0f4b1c22-3d3e-4a55-9b77-1c2d3e4f5a6b",
        **_kw(session),
    )
    written = cookjohn.calls[0][1]["fields"]["extra"]
    assert "Citation Key: smith2020" in written
    assert "calibre-uuid: 0f4b1c22-3d3e-4a55-9b77-1c2d3e4f5a6b" in written
    assert out["ok"] is True


def test_an_import_that_returns_no_key_is_also_a_failure(zotero, tmp_path, session):
    """The SIBLING of the test below, and it did not exist until 2026-08-19.

    `import_attachment` put `find_key(reply)` -- which can be None -- straight into its
    envelope beside `"ok": True`. So cookjohn answering without a key produced a success
    the caller could not act on, and no way to tell whether the file had been attached.
    `create_item` has raised for exactly this since it was written; the asymmetry was the
    bug, and an untested fix is how it would come back.
    """
    from zotero_core.application.services.verbs import import_attachment

    zotero.add("PARENT01", title="A Paper")
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    silent = FakeCookjohn(zotero, no_key=True)
    with pytest.raises(WriteBlocked) as e:
        import_attachment(
            "PARENT01", str(pdf), session=replace(session, cookjohn=silent)
        )
    assert e.value.code == Reason.COOKJOHN_RETURNED_NO_KEY


def test_a_create_that_returns_no_key_is_a_failure(zotero, session):
    """cookjohn can answer without a key. Reporting ok=True would make a no-op
    indistinguishable from a create — calibre-core guards `calibredb` the same way."""
    silent = FakeCookjohn(zotero, no_key=True)
    with pytest.raises(WriteBlocked) as e:
        create_item("book", {"title": "A Book"}, session=replace(session, cookjohn=silent))
    assert e.value.code == Reason.COOKJOHN_RETURNED_NO_KEY


def test_create_can_file_the_new_item_into_a_collection(zotero, session):
    """Two transports, one call, and the caller names neither: the item is created
    through cookjohn and filed through cookjohn, but a linked attachment in the same
    flow would go through the linker without the signature changing."""
    collection = zotero.add_collection("calibre")
    out = create_item(
        "book", {"title": "A Book"}, collection_key=collection, **_kw(session)
    )
    assert out["collection"]["ok"] is True
    assert out["item_key"] in zotero.collection_members(collection)


# --------------------------------------------------------------------------
# attachments — link vs import is a lasting choice, so it is two verbs
# --------------------------------------------------------------------------

def test_a_relative_path_is_refused(zotero, linker, session):
    """Zotero resolves a relative path against its OWN working directory, so this
    produces an attachment pointing at nothing — and it fails silently, because linking
    never reads the file."""
    zotero.add("PARENT12", "The Parent", item_type="book")
    with pytest.raises(WriteBlocked) as e:
        link_attachment("PARENT12", "relative/path.pdf", **_kw(session))
    assert e.value.code == Reason.FILE_NOT_FOUND
    assert linker.posts == []


def test_a_missing_file_is_refused_before_the_link_is_made(
    zotero, linker, tmp_path, session
):
    zotero.add("PARENT12", "The Parent", item_type="book")
    with pytest.raises(WriteBlocked) as e:
        link_attachment("PARENT12", str(tmp_path / "absent.pdf"), **_kw(session))
    assert e.value.code == Reason.FILE_NOT_FOUND
    assert linker.posts == []


def test_link_and_import_use_different_transports(zotero, tmp_path, session):
    """The distinction is the reason `linker/` exists: cookjohn's import makes a stored
    COPY into ~/Zotero/storage, which then syncs to the NAS. For a 20 GB library already
    on disk that is the wrong default, so the two are separate verbs."""
    zotero.add("PARENT12", "The Parent", item_type="book")
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    kw = _kw(session)

    linked = link_attachment("PARENT12", str(pdf), **kw)
    imported = import_attachment("PARENT12", str(pdf), **kw)
    assert linked["transport"] == "linker"
    assert imported["transport"] == "cookjohn"
    assert linked["undo_call"].startswith("trash_items(")


# --------------------------------------------------------------------------
# UPDATE — metadata
# --------------------------------------------------------------------------

def test_an_update_journals_the_values_it_overwrites(zotero, tmp_path, session):
    """The only record that an overwrite destroyed anything."""
    zotero.add("ABCD2345", "Old Title", fields={"date": "2019"})
    out = update_metadata(
        "ABCD2345", {"date": "2024"},
        journal_dir=str(tmp_path / "j"), **_kw(session),
    )
    assert out["fields_overwritten"] == {"date": "2019"}
    manifest = json.loads(Path(out["undo_manifest"]).read_text())
    assert manifest["before"]["fields"] == {"date": "2019"}
    assert manifest["inverse"] == "update_metadata('ABCD2345', {'date': '2019'})"


def test_a_field_that_did_not_exist_is_not_reported_as_having_been_empty(session, 
    zotero, tmp_path
):
    """A field that DID NOT EXIST is not a field holding "". Recording the absent ones
    as `""` and handing that back as the undo would write empty strings instead of
    removing the fields — an inverse leaving the item different from how it started.

    Found live: `update_metadata` on a real item with no date produced
    `undo_call: update_metadata('UUV3HWD2', {'date': '', 'language': ''})`. cookjohn's
    write_metadata has no remove, so for those fields there is NO expressible inverse,
    and the result says so instead of offering a wrong one."""
    zotero.add("ABCD2345", "A Paper")  # no date, no language
    out = update_metadata(
        "ABCD2345", {"date": "2026", "language": "en"},
        journal_dir=str(tmp_path / "j"), **_kw(session),
    )
    assert out["fields_overwritten"] == {}
    assert out["fields_added"] == ["date", "language"]
    assert out["undo_call"] is None
    manifest = json.loads(Path(out["undo_manifest"]).read_text())
    assert manifest["before"]["fields_absent_before"] == ["date", "language"]
    assert manifest["inverse"] is None


def test_zoteros_own_normalisation_is_not_reported_as_a_failed_write(session, 
    zotero, tmp_path
):
    """Strict equality on field values produces FALSE alarms. Observed live on
    2026-08-13: writing `date="2026"` came back stored as `"2026-00-00 2026"`, because
    Zotero parses the date field into its own multipart form. The first version of this
    check called that successful write "unverified" and blamed the snapshot read —
    pointing the error at entirely the wrong thing.

    A stored value CONTAINING what was written is `normalized`, and the stored form is
    reported so the caller sees what Zotero made of their input."""

    class Normalising(FakeCookjohn):
        def _write_metadata(self, arguments):
            for name, value in (arguments.get("fields") or {}).items():
                # The exact transformation the live Zotero applied to date="2026".
                stored = f"{value}-00-00 {value}" if name == "date" else value
                self.builder.set_field(arguments["itemKey"], name, stored)
            return {"success": True}

    zotero.add("ABCD2345", "A Paper")
    out = update_metadata(
        "ABCD2345", {"date": "2026"}, journal_dir=str(tmp_path / "j"),
        session=replace(session, cookjohn=Normalising(zotero)),
    )
    assert out["verification"]["verified"] is True
    assert out["verification"]["normalized_by_zotero"] == {"date": "2026-00-00 2026"}
    assert "disagreed" not in out["verification"]


def test_a_field_with_no_trace_of_the_write_is_still_unverified(session, 
    zotero, tmp_path
):
    """The normalisation tolerance must not swallow a write that did not land at all."""
    zotero.add("ABCD2345", "A Paper", fields={"date": "1999"})
    out = update_metadata(
        "ABCD2345", {"date": "2026"}, journal_dir=str(tmp_path / "j"),
        session=replace(session, cookjohn=FakeCookjohn(zotero, apply=False)),
    )
    assert out["verification"]["verified"] == "unverified"
    assert out["verification"]["disagreed"] == ["date"]


def test_a_venue_write_remapped_onto_the_types_own_field_is_verified_not_disagreed(session, 
    zotero, tmp_path
):
    """Zotero maps a BASE field name onto the field the item type actually carries, and
    cookjohn writes through that mapping: `publicationTitle` on a conferencePaper is
    stored as `proceedingsTitle`. Observed live 2026-08-13 — `write_metadata` answered
    ok, the value was in the library, and reading back the name that was WRITTEN found
    nothing, so this reported a successful write as `disagreed` and sent the caller to
    check Zotero by hand.

    Reported as `mapped_to_type_field` rather than folded into `normalized_by_zotero`:
    the value is intact and it is the NAME that moved, which is what a caller re-reading
    the field later needs to know."""
    zotero.add("ABCD2345", "A Paper", item_type="conferencePaper")
    out = update_metadata(
        "ABCD2345", {"publicationTitle": "NeurIPS 2026"},
        journal_dir=str(tmp_path / "j"), **_kw(session),
    )
    assert out["verification"]["verified"] is True
    assert out["verification"]["mapped_to_type_field"] == {
        "publicationTitle": "proceedingsTitle"
    }
    assert "disagreed" not in out["verification"]
    # The value really is under the mapped name and NOT under the written one.
    stored = zotero.fields_of("ABCD2345")
    assert stored["proceedingsTitle"] == "NeurIPS 2026"
    assert "publicationTitle" not in stored


def test_the_remap_is_read_from_the_database_not_assumed_per_type(session, 
    zotero, tmp_path
):
    """The same base field maps to a DIFFERENT field per item type — bookTitle on a
    bookSection, proceedingsTitle on a conferencePaper — so a hard-coded pair would be
    right for one type and wrong for the other nine. `baseFieldMappingsCombined` is the
    database's own data and moves with Zotero's schema version."""
    zotero.add("ABCD2345", "A Chapter", item_type="bookSection")
    out = update_metadata(
        "ABCD2345", {"publicationTitle": "A Big Handbook"},
        journal_dir=str(tmp_path / "j"), **_kw(session),
    )
    assert out["verification"]["mapped_to_type_field"] == {"publicationTitle": "bookTitle"}
    assert zotero.fields_of("ABCD2345")["bookTitle"] == "A Big Handbook"


def test_a_type_that_maps_nothing_still_compares_names_literally(session, 
    zotero, tmp_path
):
    """A journalArticle stores `publicationTitle` under that very name. The mapping
    lookup must not invent a rename where the type has none."""
    zotero.add("ABCD2345", "A Paper", item_type="journalArticle")
    out = update_metadata(
        "ABCD2345", {"publicationTitle": "JMLR"},
        journal_dir=str(tmp_path / "j"), **_kw(session),
    )
    assert out["verification"]["verified"] is True
    assert "mapped_to_type_field" not in out["verification"]
    assert zotero.fields_of("ABCD2345")["publicationTitle"] == "JMLR"


def test_the_remap_does_not_rescue_a_write_that_never_landed(
    zotero, tmp_path, session
):
    """The mapping tolerance must not swallow a failed write the way strict equality must
    not produce false alarms. A conferencePaper whose plugin applied nothing is still
    `disagreed`, and the field is not reported as merely relocated."""
    zotero.add("ABCD2345", "A Paper", item_type="conferencePaper")
    out = update_metadata(
        "ABCD2345", {"publicationTitle": "NeurIPS 2026"},
        journal_dir=str(tmp_path / "j"),
        session=replace(session, cookjohn=FakeCookjohn(zotero, apply=False)),
    )
    assert out["verification"]["verified"] == "unverified"
    assert out["verification"]["disagreed"] == ["publicationTitle"]
    assert "mapped_to_type_field" not in out["verification"]


def test_an_update_merges_and_leaves_other_fields_alone(zotero, tmp_path, session):
    zotero.add("ABCD2345", "A Paper", fields={"date": "2019", "publisher": "ACM"})
    update_metadata(
        "ABCD2345", {"date": "2024"},
        journal_dir=str(tmp_path / "j"), **_kw(session),
    )
    after = zotero.fields_of("ABCD2345")
    assert after["date"] == "2024"
    assert after["publisher"] == "ACM"
    assert after["title"] == "A Paper"


@pytest.mark.parametrize("item_type", ["note", "attachment", "annotation"])
def test_metadata_is_refused_on_items_that_have_none(session, 
    zotero, cookjohn, tmp_path, item_type
):
    """cookjohn's own description says metadata "only works on regular items". Catching
    it here turns a plugin-side failure into a precondition that names the actual type."""
    zotero.add("PARENT12", "The Parent", item_type="book")
    zotero.add("CHILD123", "The Child", item_type=item_type, parent="PARENT12")
    with pytest.raises(WriteBlocked) as e:
        update_metadata(
            "CHILD123", {"date": "2024"},
            journal_dir=str(tmp_path / "j"), **_kw(session),
        )
    assert e.value.code == Reason.WRONG_ITEM_TYPE
    assert e.value.detail["item_type"] == item_type
    assert cookjohn.calls == []


def test_an_update_that_changes_nothing_is_refused(zotero, cookjohn, tmp_path, session):
    zotero.add("ABCD2345", "A Paper", fields={"date": "2024"})
    with pytest.raises(WriteBlocked) as e:
        update_metadata(
            "ABCD2345", {"date": "2024"},
            journal_dir=str(tmp_path / "j"), **_kw(session),
        )
    assert e.value.code == Reason.NOTHING_TO_DO
    assert cookjohn.calls == []


# --------------------------------------------------------------------------
# UPDATE — the two REPLACE-shaped writes, which are the dangerous ones
#
# calibre-core's scar: `calibredb set_metadata --field identifiers:` REPLACES the set,
# so adding an ISBN silently deleted book 256's `zotero` identifier — the link to its
# Zotero item — and nothing in the output announced it.
# --------------------------------------------------------------------------

def test_replacing_creators_refuses_without_force_and_shows_what_is_there(session, 
    zotero, cookjohn, tmp_path
):
    zotero.add(
        "ABCD2345", "A Paper",
        creators=[
            {"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"},
            {"creatorType": "author", "firstName": "Alan", "lastName": "Turing"},
        ],
    )
    with pytest.raises(WriteBlocked) as e:
        replace_creators(
            "ABCD2345", [{"creatorType": "author", "lastName": "Hopper"}],
            journal_dir=str(tmp_path / "j"), **_kw(session),
        )
    assert e.value.code == Reason.REFUSING_TO_REPLACE
    # Naming what would be lost is the whole point of refusing.
    assert [c["lastName"] for c in e.value.detail["current_creators"]] == ["Lovelace", "Turing"]
    assert cookjohn.calls == []


def test_replacing_creators_with_force_journals_the_previous_list(session, 
    zotero, tmp_path
):
    zotero.add("ABCD2345", "A Paper",
               creators=[{"creatorType": "author", "lastName": "Lovelace"}])
    out = replace_creators(
        "ABCD2345", [{"creatorType": "author", "lastName": "Hopper"}],
        force=True, journal_dir=str(tmp_path / "j"), **_kw(session),
    )
    assert [c["lastName"] for c in out["creators_before"]] == ["Lovelace"]
    manifest = json.loads(Path(out["undo_manifest"]).read_text())
    assert manifest["before"]["creators"][0]["lastName"] == "Lovelace"
    assert "force=True" in manifest["inverse"]


def test_creator_order_is_preserved_because_first_author_is_not_arbitrary(zotero):
    zotero.add(
        "ABCD2345", "A Paper",
        creators=[
            {"creatorType": "author", "lastName": "First"},
            {"creatorType": "author", "lastName": "Second"},
            {"creatorType": "author", "lastName": "Third"},
        ],
    )
    got = zotero.store().item_creators("ABCD2345")
    assert [c["lastName"] for c in got] == ["First", "Second", "Third"]


def test_an_organisation_creator_comes_back_as_a_single_name(zotero):
    """fieldMode=1 is Zotero's single-field name. Splitting it into first/last would
    invent a surname for an institution."""
    zotero.add("ABCD2345", "A Report", creators=[{"creatorType": "author", "name": "NVIDIA"}])
    got = zotero.store().item_creators("ABCD2345")
    assert got == ({"creatorType": "author", "name": "NVIDIA"},)


# --------------------------------------------------------------------------
# tags — additive by default, replacement gated
# --------------------------------------------------------------------------

def test_set_tags_refuses_without_force_and_points_at_add_tags(session, 
    zotero, tmp_path
):
    """Tags in this library are curated knowledge no online source carries. A caller who
    wanted to ADD one and reached for 'set' would not find out until they were gone."""
    zotero.add("ABCD2345", "A Paper", tags=["optics", "to-read", "graphics"])
    with pytest.raises(WriteBlocked) as e:
        set_tags("ABCD2345", ["new-tag"],
                 journal_dir=str(tmp_path / "j"), **_kw(session))
    assert e.value.code == Reason.REFUSING_TO_REPLACE
    assert e.value.detail["current_tags"] == ["graphics", "optics", "to-read"]
    assert "add_tags" in e.value.detail["hint"]
    assert zotero.tags_of("ABCD2345") == ["graphics", "optics", "to-read"]


def test_add_tags_keeps_the_existing_ones(zotero, session):
    zotero.add("ABCD2345", "A Paper", tags=["optics"])
    out = add_tags("ABCD2345", ["to-read"], **_kw(session))
    assert out["tags_before"] == ["optics"]
    assert zotero.tags_of("ABCD2345") == ["optics", "to-read"]


def test_add_tags_needs_no_manifest_because_its_inverse_is_expressible(session, 
    zotero
):
    """A pure addition is undone by `remove_tags`, which the caller can already say.
    Journalling it would file noise and train people to skim manifests."""
    zotero.add("ABCD2345", "A Paper", tags=["optics"])
    out = add_tags("ABCD2345", ["to-read"], **_kw(session))
    assert out["undo_manifest"] is None


def test_set_tags_with_force_journals_the_whole_previous_list(session, 
    zotero, tmp_path
):
    zotero.add("ABCD2345", "A Paper", tags=["optics", "to-read"])
    out = set_tags("ABCD2345", ["only-this"], force=True,
                   journal_dir=str(tmp_path / "j"), **_kw(session))
    manifest = json.loads(Path(out["undo_manifest"]).read_text())
    assert manifest["before"]["tags"] == ["optics", "to-read"]
    assert zotero.tags_of("ABCD2345") == ["only-this"]


def test_removing_tags_journals_and_removes_only_the_named_ones(session, 
    zotero, tmp_path
):
    zotero.add("ABCD2345", "A Paper", tags=["optics", "to-read", "graphics"])
    out = remove_tags("ABCD2345", ["to-read"],
                      journal_dir=str(tmp_path / "j"), **_kw(session))
    assert zotero.tags_of("ABCD2345") == ["graphics", "optics"]
    assert Path(out["undo_manifest"]).exists()


def test_adding_a_tag_the_item_already_has_is_refused(zotero, cookjohn, session):
    zotero.add("ABCD2345", "A Paper", tags=["optics"])
    with pytest.raises(WriteBlocked) as e:
        add_tags("ABCD2345", ["optics"], **_kw(session))
    assert e.value.code == Reason.NOTHING_TO_DO
    assert cookjohn.calls == []


def test_removing_a_tag_the_item_does_not_have_is_refused(zotero, tmp_path, session):
    zotero.add("ABCD2345", "A Paper", tags=["optics"])
    with pytest.raises(WriteBlocked) as e:
        remove_tags("ABCD2345", ["absent"],
                    journal_dir=str(tmp_path / "j"), **_kw(session))
    assert e.value.code == Reason.NOTHING_TO_DO


# --------------------------------------------------------------------------
# notes
# --------------------------------------------------------------------------

def test_a_child_note_is_created_under_its_parent(zotero, session):
    zotero.add("PARENT12", "The Parent", item_type="book")
    out = write_note("Some thoughts", parent_item_key="PARENT12", **_kw(session))
    assert out["ok"] is True
    assert out["undo_call"].startswith("trash_items(")


def test_updating_a_note_requires_its_key(session):
    with pytest.raises(WriteBlocked) as e:
        write_note("New body", action="update", **_kw(session))
    assert e.value.code == Reason.MISSING_REQUIRED_FIELD


def test_a_note_update_refuses_a_key_that_is_not_a_note(zotero, tmp_path, session):
    zotero.add("ABCD2345", "Not A Note")
    with pytest.raises(WriteBlocked) as e:
        write_note("New body", action="update", note_key="ABCD2345",
                   journal_dir=str(tmp_path / "j"), **_kw(session))
    assert e.value.code == Reason.WRONG_ITEM_TYPE


def test_an_empty_note_is_refused(session):
    with pytest.raises(WriteBlocked) as e:
        write_note("   ", parent_item_key=None, **_kw(session))
    assert e.value.code == Reason.MISSING_REQUIRED_FIELD


# --------------------------------------------------------------------------
# collections
# --------------------------------------------------------------------------

def test_a_duplicate_sibling_collection_name_is_refused(zotero, session):
    """Zotero permits two siblings with one name, and BOTH Calibre consumers look their
    target collection up BY NAME ("calibre"). A second one makes which they find depend
    on iteration order, so half the imports land in the wrong folder."""
    zotero.add_collection("calibre")
    with pytest.raises(WriteBlocked) as e:
        create_collection("calibre", **_kw(session))
    assert e.value.code == Reason.DUPLICATE_ITEM


def test_the_sibling_name_check_is_case_insensitive(zotero, session):
    """"Calibre" and "calibre" are the same folder to a human doing a name lookup."""
    zotero.add_collection("Calibre")
    with pytest.raises(WriteBlocked) as e:
        create_collection("calibre", **_kw(session))
    assert e.value.code == Reason.DUPLICATE_ITEM


def test_a_collection_is_created_and_names_its_undo(session):
    out = create_collection("Reading 2026", **_kw(session))
    assert out["ok"] is True
    assert out["undo_call"].startswith("delete_collection(")


def test_deleting_a_collection_with_its_items_refuses_without_force(session, 
    zotero, tmp_path
):
    """One boolean turning "delete this folder" into "trash everything in it" must not
    be reachable by a default."""
    collection = zotero.add_collection("Doomed")
    zotero.add("ABCD2345", "A Paper")
    zotero.add_to_collection("ABCD2345", collection)

    with pytest.raises(WriteBlocked) as e:
        delete_collection(collection, delete_items=True,
                          journal_dir=str(tmp_path / "j"), **_kw(session))
    assert e.value.code == Reason.REFUSING_CASCADE_DELETE
    assert e.value.detail["members"] == ["ABCD2345"]
    assert not zotero.is_trashed("ABCD2345")


def test_deleting_a_collection_leaves_its_items_in_the_library(session, 
    zotero, tmp_path
):
    """The default form is not a delete of the contents, and that distinction is the one
    a caller in a hurry gets wrong."""
    collection = zotero.add_collection("Doomed")
    zotero.add("ABCD2345", "A Paper")
    zotero.add_to_collection("ABCD2345", collection)

    out = delete_collection(collection, journal_dir=str(tmp_path / "j"),
                            **_kw(session))
    assert out["items_trashed"] is False
    assert not zotero.is_trashed("ABCD2345")


def test_a_collection_deletion_records_enough_to_rebuild_it(session, 
    zotero, tmp_path
):
    """A trashed ITEM can be restored; a deleted COLLECTION cannot — Zotero's trash holds
    items. So the manifest is the only undo there is, and it has to carry the membership."""
    collection = zotero.add_collection("Doomed")
    zotero.add("ABCD2345", "One")
    zotero.add("WXYZ6789", "Two")
    zotero.add_to_collection("ABCD2345", collection)
    zotero.add_to_collection("WXYZ6789", collection)

    out = delete_collection(collection, journal_dir=str(tmp_path / "j"),
                            **_kw(session))
    manifest = json.loads(Path(out["undo_manifest"]).read_text())
    assert manifest["before"]["name"] == "Doomed"
    assert sorted(manifest["before"]["members"]) == ["ABCD2345", "WXYZ6789"]
    assert "create_collection" in manifest["inverse"]


def test_an_unknown_collection_key_is_refused(tmp_path, session):
    with pytest.raises(WriteBlocked) as e:
        delete_collection("NOTHERE2", journal_dir=str(tmp_path / "j"),
                          **_kw(session))
    assert e.value.code == Reason.UNKNOWN_COLLECTION_KEY


def test_removing_items_from_a_collection_does_not_trash_them(session, 
    zotero, tmp_path
):
    """The distinction between this and `trash_items` is exactly the one worth being
    unambiguous about. Nothing here deletes an item."""
    collection = zotero.add_collection("Reading")
    zotero.add("ABCD2345", "A Paper")
    zotero.add_to_collection("ABCD2345", collection)

    out = remove_items_from_collection(
        collection, ["ABCD2345"], journal_dir=str(tmp_path / "j"),
        **_kw(session),
    )
    assert not zotero.is_trashed("ABCD2345")
    assert out["undo_call"] == f"add_items_to_collection({collection!r}, ['ABCD2345'])"


def test_filing_items_into_a_collection_checks_they_exist_first(zotero, session):
    collection = zotero.add_collection("Reading")
    zotero.add("ABCD2345", "A Paper")
    with pytest.raises(WriteBlocked) as e:
        add_items_to_collection(collection, ["ABCD2345", "NOTHERE2"],
                                **_kw(session))
    assert e.value.code == Reason.UNKNOWN_ITEM_KEYS
    assert zotero.collection_members(collection) == []


def test_a_collection_update_with_nothing_to_change_is_refused(session, 
    zotero, tmp_path
):
    collection = zotero.add_collection("Reading")
    with pytest.raises(WriteBlocked) as e:
        update_collection(collection, journal_dir=str(tmp_path / "j"),
                          **_kw(session))
    assert e.value.code == Reason.NOTHING_TO_DO


def test_a_rename_journals_the_previous_name(zotero, tmp_path, session):
    collection = zotero.add_collection("Old Name")
    out = update_collection(collection, name="New Name",
                            journal_dir=str(tmp_path / "j"), **_kw(session))
    manifest = json.loads(Path(out["undo_manifest"]).read_text())
    assert manifest["before"]["name"] == "Old Name"


# --------------------------------------------------------------------------
# the fixture's own faithfulness
# --------------------------------------------------------------------------

def test_the_fake_cookjohn_mimics_the_real_shape_inconsistency(session):
    """`write_item` answers with a nested `data.itemKey`, `create_collection` with a flat
    `key`. That inconsistency is why `_find_key` exists, and a fake that answered
    uniformly would let a `_find_key` regression pass."""
    item = create_item("book", {"title": "A Book"}, **_kw(session))
    collection = create_collection("Somewhere", **_kw(session))
    assert "data" in item["cookjohn"] and "itemKey" in item["cookjohn"]["data"]
    assert "key" in collection["cookjohn"]
    assert item["item_key"] and collection["collection_key"]


# --------------------------------------------------------------------------
# read-back verification -- the two most dangerous verbs
# --------------------------------------------------------------------------


def test_replace_creators_reads_the_creators_back(zotero, session):
    """The most destructive item verb, and it reported nothing but its own inputs."""
    zotero.add(
        "ABCD2345",
        "A Paper",
        creators=[{"creatorType": "author", "lastName": "Old"}],
    )
    out = replace_creators(
        "ABCD2345",
        [{"creatorType": "author", "firstName": "Max", "lastName": "Welling"}],
        force=True,
        **_kw(session),
    )
    assert out["verification"]["verified"] is True
    assert out["verification"]["creators_after"][0]["lastName"] == "Welling"


def test_creator_verification_tolerates_absent_vs_empty_name_parts(zotero, session):
    """Writing only `lastName` gets `firstName: ""` back -- Zotero fills the other half.

    A naive dict `==` calls that a failed write, which is the false-alarm class that
    `_verify_fields`' docstring records hitting twice on real writes.
    """
    zotero.add("ABCD2345", "A Paper", creators=[{"creatorType": "author", "lastName": "Old"}])
    out = replace_creators(
        "ABCD2345",
        [{"creatorType": "author", "lastName": "Welling"}],   # no firstName
        force=True,
        **_kw(session),
    )
    assert out["verification"]["verified"] is True


def test_creator_verification_notices_a_reordering(zotero):
    """Order is meaning: first-author order drives duplicate detection, so the same
    creators in a different order is a real difference, not cosmetic."""
    from zotero_core.application.services.verbs import _verify_creators

    zotero.add("ABCD2345", "A Paper")
    zotero.replace_creators(
        "ABCD2345",
        [
            {"creatorType": "author", "lastName": "Second"},
            {"creatorType": "author", "lastName": "First"},
        ],
    )
    verdict = _verify_creators(
        zotero.store(),
        "ABCD2345",
        [
            {"creatorType": "author", "lastName": "First"},
            {"creatorType": "author", "lastName": "Second"},
        ],
    )
    assert verdict["verified"] == "unverified"
    assert "DIFFERENT ORDER" in verdict["note"]


def test_delete_collection_confirms_the_collection_is_gone(zotero, session):
    """The one verification that INVERTS: a hit is the failure, a miss is the success.

    It also exposed a fixture gap -- FakeCookjohn reported success and left the row in
    place, so nothing that re-read the tree could ever have passed.
    """
    key = zotero.add_collection("Doomed")
    out = delete_collection(key, **_kw(session))
    assert out["verification"]["verified"] is True
    assert out["verification"]["read_mode"]


def test_delete_verification_reports_unverified_rather_than_failed(zotero, session):
    """A "still there" read is NOT proof the delete failed -- it may be an immutable
    snapshot taken before the commit. Claiming failure would send the caller to rebuild
    a collection that is already gone, under a NEW key, re-filing every member."""
    from zotero_core.application.services.collections import _verify_gone

    key = zotero.add_collection("Still Here")
    verdict = _verify_gone(session.collections, key)
    assert verdict["verified"] == "unverified"
    assert "snapshot" in verdict["note"]


def test_create_collection_reads_the_collection_back(session):
    out = create_collection("Fresh", **_kw(session))
    assert out["verification"]["verified"] is True
    assert out["verification"]["path"] == "Fresh"


def test_update_collection_verifies_the_new_name(zotero, session):
    key = zotero.add_collection("Before")
    out = update_collection(key, name="After", **_kw(session))
    assert out["verification"]["verified"] is True


def test_collection_verification_only_checks_what_was_set(zotero, session):
    """`name=None` means "do not check the name", not "expect no name" -- checking a
    field the caller never set would invent a failure."""
    from zotero_core.application.services.collections import _verify_collection

    key = zotero.add_collection("Named")
    assert _verify_collection(session.collections, key)["verified"] is True
    assert _verify_collection(session.collections, key, name="Named")["verified"] is True
    bad = _verify_collection(session.collections, key, name="Wrong")
    assert bad["verified"] == "unverified"
    assert bad["disagreed"]["name"] == {"expected": "Wrong", "found": "Named"}


def test_write_note_verifies_type_and_parent_but_not_the_body(zotero, session):
    """PARTIAL on purpose, and the result says so: nothing in read/ reads a note body,
    so this catches a missing note or a wrong parent and cannot catch wrong text."""
    zotero.add("PARENT12", "The Parent", item_type="book")
    out = write_note("<p>hi</p>", parent_item_key="PARENT12", **_kw(session))
    v = out["verification"]
    assert v["verified"] is True
    assert v["item_type"] == "note"
    assert v["body_checked"] is False


def test_add_items_reports_which_were_already_there(zotero, session):
    """REPORTS, not refuses. Filing an item already in a collection is a no-op in Zotero
    and making it an error would break calls that work today -- what was missing is that
    the caller could not tell a no-op from a real change."""
    coll = zotero.add_collection("Papers")
    zotero.add("ABCD2345", "One")
    zotero.add("BCDE3456", "Two")
    zotero.add_to_collection("ABCD2345", coll)

    out = add_items_to_collection(coll, ["ABCD2345", "BCDE3456"], **_kw(session))
    assert out["already_present"] == ["ABCD2345"]
    assert out["verification"]["verified"] is True


def test_remove_items_reports_which_were_never_there(zotero, session):
    coll = zotero.add_collection("Papers")
    zotero.add("ABCD2345", "One")
    zotero.add("BCDE3456", "Two")
    zotero.add_to_collection("ABCD2345", coll)

    out = remove_items_from_collection(
        coll, ["ABCD2345", "BCDE3456"], **_kw(session)
    )
    assert out["not_in_collection"] == ["BCDE3456"]
    assert out["verification"]["verified"] is True


def test_membership_verification_runs_in_both_directions(zotero, session):
    from zotero_core.application.services.collections import _verify_membership

    coll = zotero.add_collection("Papers")
    zotero.add("ABCD2345", "One")
    zotero.add_to_collection("ABCD2345", coll)
    # the COLLECTION catalogue, not the item store: this helper reads membership, and
    # taking the item store was what let it be handed something with no `.items()`.
    store = session.collections

    assert _verify_membership(store, coll, ["ABCD2345"], present=True)["verified"] is True
    absent = _verify_membership(store, coll, ["ABCD2345"], present=False)
    assert absent["verified"] == "unverified"
    assert absent["still_in_collection"] == ["ABCD2345"]


def test_link_attachment_verifies_the_file_is_actually_there(zotero, tmp_path, session):
    """The file check is the point. This library has 31 LIVE dangling links -- items that
    look like they have a PDF and open to an error -- and nothing caught them at write
    time because nothing looked."""
    zotero.add("PARENT12", "The Parent", item_type="book")
    pdf = tmp_path / "real.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    out = link_attachment("PARENT12", str(pdf), **_kw(session))
    v = out["verification"]
    assert v["item_type"] == "attachment"
    assert v["file"] in {"present", "unresolved"}


def test_attachment_verification_flags_a_missing_file_as_unverified(zotero):
    """A row that exists whose file does not is exactly the dangling-link shape.

    `stored=True` on purpose: a STORED attachment resolves to storage/<key>/, which is
    absolute and globbable, so absence is knowable. A LINKED one here would be stored
    relative to Zotero's base directory, and the honest answer for that is `unresolved`
    rather than `missing` -- see the next test.
    """
    from zotero_core.application.services.verbs import _verify_attachment

    zotero.add("PARENT12", "The Parent", item_type="book")
    zotero.add("ATTACH01", item_type="attachment", parent="PARENT12", title="", stored=True)
    verdict = _verify_attachment(zotero.store(), "ATTACH01", "PARENT12", expect_file=True)
    assert verdict["verified"] == "unverified"
    assert verdict["file"] == "missing"


def test_a_relative_linked_path_is_unresolved_not_missing(zotero):
    """The distinction that keeps this honest: 772 of this library's 1368 attachments are
    LINKED, and their paths are relative to a base that lives in prefs.js, not sqlite.
    Calling those missing would flag most of the library as broken."""
    from zotero_core.application.services.verbs import _verify_attachment

    zotero.add("PARENT12", "The Parent", item_type="book")
    zotero.add("ATTACH01", item_type="attachment", parent="PARENT12", title="")  # linked
    verdict = _verify_attachment(zotero.store(), "ATTACH01", "PARENT12", expect_file=True)
    assert verdict["file"] == "unresolved"
    assert verdict["verified"] is True


def test_attachment_verification_catches_a_wrong_parent(zotero):
    from zotero_core.application.services.verbs import _verify_attachment

    zotero.add("PARENT12", "The Parent", item_type="book")
    zotero.add("OTHER123", "Another", item_type="book")
    zotero.add("ATTACH01", item_type="attachment", parent="PARENT12", title="")
    verdict = _verify_attachment(zotero.store(), "ATTACH01", "OTHER123", expect_file=True)
    assert verdict["verified"] == "unverified"
    assert "expected parent" in verdict["disagreed"]


def test_tag_verbs_render_a_verdict_not_two_lists(zotero, session):
    """`_tag_op` always re-read the tags -- it just handed back before/after and left the
    caller to diff them. A read-back without a conclusion is not a verification."""
    zotero.add("ABCD2345", "A Paper", tags=["keep"])
    kw = _kw(session)

    assert add_tags("ABCD2345", ["new"], **kw)["verification"]["verified"] is True
    assert remove_tags("ABCD2345", ["new"], **kw)["verification"]["verified"] is True
    assert set_tags("ABCD2345", ["only"], force=True, **kw)["verification"]["verified"] is True


def test_an_add_tolerates_a_tag_another_plugin_wrote():
    """`/unread` lands on every item created through this package, put there by a
    reading-list plugin. Demanding an exact set would report every successful add as
    broken, so extras are reported rather than failed."""
    from zotero_core.application.services.verbs import _verify_tags

    verdict = _verify_tags("add", was=["keep"], now=["keep", "new", "/unread"], requested=["new"])
    assert verdict["verified"] is True
    assert verdict["also_present"] == ["/unread"]


def test_a_set_does_NOT_tolerate_a_survivor():
    """set_tags REPLACES. An extra tag after a set means the replacement did not take --
    the opposite conclusion from the same evidence after an add."""
    from zotero_core.application.services.verbs import _verify_tags

    verdict = _verify_tags("set", was=["old"], now=["only", "old"], requested=["only"])
    assert verdict["verified"] == "unverified"
    assert verdict["disagreed"] == ["old"]


def test_a_remove_that_left_the_tag_behind_is_unverified():
    from zotero_core.application.services.verbs import _verify_tags

    verdict = _verify_tags("remove", was=["gone", "keep"], now=["gone", "keep"], requested=["gone"])
    assert verdict["verified"] == "unverified"
    assert verdict["disagreed"] == ["gone"]
