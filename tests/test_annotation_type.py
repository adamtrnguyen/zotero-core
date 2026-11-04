"""One declaration of Zotero's annotation vocabulary.

It existed three times: a dict in `read/annotations.py`, read again in `read/search.py` with
a DIFFERENT fallback, and transcribed by hand into the MCP schema.
"""

from __future__ import annotations

import pytest

from zotero_core.domain.annotation_type import (
    ANNOTATION_TYPE,
    ANNOTATION_TYPE_NAMES,
    AnnotationType,
    label_for,
)


@pytest.mark.parametrize(
    ("type_id", "label"),
    [(1, "highlight"), (2, "note"), (3, "image"), (4, "ink"), (5, "underline"), (6, "text")],
)
def test_every_known_type_resolves(type_id, label):
    assert label_for(type_id) == label
    assert AnnotationType(type_id).label == label


def test_it_compares_equal_to_the_raw_database_value():
    """Rows come back as ints; anything comparing against one must keep working."""
    assert AnnotationType.HIGHLIGHT == 1
    assert AnnotationType.TEXT in {5, 6}


def test_both_read_paths_now_give_the_SAME_name_for_an_unknown_type():
    """THE REGRESSION.

    `read/annotations.py` answered `f"type{n}"` and `read/search.py` answered `str(n)` --
    two different names for the same row, from the same table, depending on which query
    found it. A caller filtering on the name would match one and miss the other.
    """
    assert label_for(7) == "type7"
    assert label_for(99) == "type99"
    # and it is self-describing, unlike a bare "7", which is indistinguishable from a page
    # label in a result a caller is scanning
    assert not label_for(7).isdigit()


def test_the_schema_list_is_derived_not_transcribed():
    """The third copy was a hand-maintained tuple whose own comment asked to be kept in
    step by hand. It is generated from the enum now."""
    from zotero_core.interfaces import read_mcp

    assert read_mcp.ANNOTATION_TYPE_NAMES == ANNOTATION_TYPE_NAMES
    assert ANNOTATION_TYPE_NAMES == tuple(t.label for t in AnnotationType)


def test_the_dict_shape_is_declared_once_in_the_domain():
    """WAS `test_the_legacy_dict_shape_still_resolves_where_it_did`, which pinned a
    re-export in `sqlite/annotations.py` on the grounds that the name was "imported by
    name in the test suite and by the MCP adapter". Only the first half was true -- the
    adapter imports `ZoteroAnnotationError` from that module, never `ANNOTATION_TYPE` --
    so the shim existed for its own tests. Removed; the domain is the one declaration.
    """
    assert ANNOTATION_TYPE == {
        1: "highlight",
        2: "note",
        3: "image",
        4: "ink",
        5: "underline",
        6: "text",
    }


def test_adding_a_type_to_the_enum_would_reach_every_consumer():
    """The property that makes this worth doing: one declaration, three consumers."""
    assert len(ANNOTATION_TYPE) == len(ANNOTATION_TYPE_NAMES) == len(list(AnnotationType))
