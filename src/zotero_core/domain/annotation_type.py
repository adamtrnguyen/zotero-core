"""Zotero's annotation types: the int the database stores, the name a caller wants.

ONE DECLARATION
---------------
The database reader, the search reader and the MCP schema all derive the mapping from
this enum, so an unknown type gets one name everywhere and the schema's list cannot fall
out of step. History of the three copies: docs/history/identity-and-normalisation.md.

WHY AN ENUM AND NOT A DICT
--------------------------
The values are a closed vocabulary Zotero owns, and the MCP schema needs the names as a
list to make them DISCOVERABLE rather than guessed from examples. An enum gives the lookup,
the reverse lookup and that list from one declaration.

`int, Enum` for the same reason `ReadMode` is `str, Enum`: `enum.IntEnum` would work, but
the mixin form keeps `== 1` true for anything comparing against a raw database value, and
this package targets 3.10 where `StrEnum` does not exist (see `read_mode.py`).
"""

from __future__ import annotations

from enum import Enum


class AnnotationType(int, Enum):
    """The `itemAnnotations.type` column. Values are Zotero's, not ours."""

    HIGHLIGHT = 1
    NOTE = 2
    IMAGE = 3
    INK = 4
    UNDERLINE = 5
    TEXT = 6

    @property
    def label(self) -> str:
        """The name callers filter on: `"highlight"`, `"note"`, …"""
        return self.name.lower()


#: Every name, in database order. The MCP schema's `enum` list, derived rather than typed
#: out, so nothing has to keep it in step.
ANNOTATION_TYPE_NAMES: tuple[str, ...] = tuple(t.label for t in AnnotationType)

#: The `{id: name}` shape, derived from the enum rather than typed out a second time.
ANNOTATION_TYPE: dict[int, str] = {int(t): t.label for t in AnnotationType}


def label_for(type_id: int) -> str:
    """Name for a stored type id, with ONE fallback for a type Zotero adds later.

    The fallback shape is `type<N>`, not the bare `str(N)`: a caller filtering on `"7"`
    cannot tell a type name from a page label, while `"type7"` is self-describing. Every
    read path uses this one function, so a row gets the same name whichever query found it.
    """
    try:
        return AnnotationType(type_id).label
    except ValueError:
        return f"type{type_id}"
