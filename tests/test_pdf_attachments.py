"""`ZoteroItemStore.pdf_attachments()` — the enumeration a corpus builder needs.

Added 2026-08-18. This query used to live in `omni-rag/scripts/prototype_zotero.py`, which called
itself "a spike" while being the only thing that could give a paper its title and collection —
`omnirag papers ingest` existed with no enumerator behind it and would have written filename titles
and an empty collection. ZoteroSuite's rule is that consumers do not duplicate Zotero SQL, so it
lives in core now and these are the tests it never had.
"""

from __future__ import annotations


def _pdf(zotero, key: str, name: str = "paper.pdf") -> None:
    """Put a real file at <storage>/<KEY>/<name>. The enumerator globs the disk, not the DB path.

    Derived from the builder's own db path rather than from `tmp_path`: the fixture nests the
    database under `tmp_path/Zotero/`, so a hand-built `tmp_path/storage` silently misses and every
    positive assertion reads as "the query returned nothing".
    """
    d = zotero.path.parent / "storage" / key
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(b"%PDF-1.4\n")


def test_title_comes_from_the_parent_and_the_collection_from_its_membership(zotero):
    coll = zotero.add_collection("Data Val")
    zotero.add("PARENT01", title="A Practitioner's Guide to Agentic RL")
    zotero.add("ATTACH01", item_type="attachment", parent="PARENT01", stored=True, title="")
    zotero.add_to_collection("PARENT01", coll)
    _pdf(zotero, "ATTACH01")

    got = zotero.store().pdf_attachments()

    assert len(got) == 1
    a = got.items[0]
    assert a.attachment_key == "ATTACH01"
    assert a.title == "A Practitioner's Guide to Agentic RL"
    assert a.collection == "Data Val"
    assert a.path.name == "paper.pdf"


def test_an_item_in_no_collection_reports_none_not_a_sentinel(zotero):
    """core returns the raw fact. Mapping "no collection" onto a magic string is the consumer's
    policy — omni-rag slugs it to `zotero` — and a read layer inventing it would make a real
    collection named "Zotero" indistinguishable from an uncollected item."""
    zotero.add("PARENT02", title="Unfiled Paper")
    zotero.add("ATTACH02", item_type="attachment", parent="PARENT02", stored=True, title="")
    _pdf(zotero, "ATTACH02")

    a = zotero.store().pdf_attachments().items[0]
    assert a.collection is None


def test_a_trashed_attachment_is_not_enumerated(zotero):
    zotero.add("PARENT03", title="Deleted Attachment's Parent")
    zotero.add("ATTACH03", item_type="attachment", parent="PARENT03", stored=True, title="")
    _pdf(zotero, "ATTACH03")
    zotero.trash("ATTACH03")

    assert len(zotero.store().pdf_attachments()) == 0


def test_a_linked_attachment_is_not_enumerated(zotero):
    """Only `storage:` files are guaranteed to sit at <storage>/<KEY>/. A linked file lives
    wherever the user put it, so returning one would hand back a path that does not exist."""
    zotero.add("PARENT04", title="Linked Not Stored")
    zotero.add("ATTACH04", item_type="attachment", parent="PARENT04", stored=False, title="")
    _pdf(zotero, "ATTACH04")  # even with a file present, the row must not qualify

    assert len(zotero.store().pdf_attachments()) == 0


def test_an_attachment_whose_folder_holds_no_pdf_is_skipped(zotero):
    """Skipped rather than returned with a non-existent path: a caller would only discover it
    when the parser failed, one stage later and with a worse error."""
    zotero.add("PARENT05", title="Row Without A File")
    zotero.add("ATTACH05", item_type="attachment", parent="PARENT05", stored=True, title="")
    # deliberately no _pdf(...)

    assert len(zotero.store().pdf_attachments()) == 0


def test_a_missing_parent_title_falls_back_to_the_file_stem(zotero):
    zotero.add("PARENT06", title="", item_type="journalArticle")
    zotero.add("ATTACH06", item_type="attachment", parent="PARENT06", stored=True, title="")
    _pdf(zotero, "ATTACH06", name="2310.03744v2.pdf")

    a = zotero.store().pdf_attachments().items[0]
    assert a.title == "2310.03744v2"


def test_a_long_title_is_never_truncated(zotero):
    """A truncated title silently merges distinct works under one label, which is the
    `book_collision` defect class in a consumer keying rows on the display string."""
    long = "Perceptual Organization " * 8
    zotero.add("PARENT07", title=long.strip())
    zotero.add("ATTACH07", item_type="attachment", parent="PARENT07", stored=True, title="")
    _pdf(zotero, "ATTACH07")

    assert zotero.store().pdf_attachments().items[0].title == long.strip()


def test_another_library_is_not_enumerated_into_this_one(zotero):
    """Every read here is library-scoped; this database shape has group libraries too."""
    zotero.add("PARENT08", title="Group Paper", library_id=2)
    zotero.add(
        "ATTACH08", item_type="attachment", parent="PARENT08", stored=True, title="", library_id=2
    )
    _pdf(zotero, "ATTACH08")

    assert len(zotero.store().pdf_attachments()) == 0


def test_limit_bounds_the_enumeration(zotero):
    for i in range(3):
        zotero.add(f"PARENT1{i}", title=f"Paper {i}")
        zotero.add(
            f"ATTACH1{i}", item_type="attachment", parent=f"PARENT1{i}", stored=True, title=""
        )
        _pdf(zotero, f"ATTACH1{i}")

    assert len(zotero.store().pdf_attachments(limit=2)) == 2
    assert len(zotero.store().pdf_attachments()) == 3


def test_the_read_mode_travels_with_the_enumeration(zotero):
    """A short enumeration under `immutable=1` is a stale snapshot, not an emptied library — a
    caller diffing this against its own index would otherwise read it as a deletion."""
    zotero.add("PARENT20", title="Paper")
    zotero.add("ATTACH20", item_type="attachment", parent="PARENT20", stored=True, title="")
    _pdf(zotero, "ATTACH20")

    got = zotero.store().pdf_attachments()
    assert got.read_mode in {"mode=ro", "immutable=1"}


def test_a_multi_collection_paper_reports_a_STABLE_collection(zotero):
    """A bare `LIMIT 1` returns whatever the plan reaches first, so a consumer keying a filter on
    it can see a paper move between filters with no data change. Alphabetical is arbitrary but
    stable, and stable is the property that matters."""
    zotero.add("PARENT30", title="Filed Twice")
    zotero.add("ATTACH30", item_type="attachment", parent="PARENT30", stored=True, title="")
    _pdf(zotero, "ATTACH30")
    for name in ("Zebra Collection", "Aardvark Collection", "Middle Collection"):
        zotero.add_to_collection("PARENT30", zotero.add_collection(name))

    seen = {zotero.store().pdf_attachments().items[0].collection for _ in range(5)}
    assert seen == {"Aardvark Collection"}, f"unstable or wrong pick: {seen}"
