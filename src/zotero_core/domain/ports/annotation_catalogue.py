"""Annotations, and the PDF attachment they hang off.

Separate from `Catalogue` because these are a different noun read from different tables, and
because the write path never touches them: nothing in `application/services/` annotates. A
port earns its place by having a consumer, and this one's consumer is the read facade.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AnnotationCatalogue(Protocol):
    """Read-only access to stored annotations."""

    #: Which mode served the LAST read. On the port, not folded into the return types,
    #: because the adapter chose that shape deliberately: "these methods return plain
    #: lists that several callers unpack positionally". It was recorded and never read
    #: outside tests, so a caller of `get_sources_with_annotations` had no way to learn it
    #: had been handed a snapshot.
    last_read_mode: str

    def get_annotations(
        self,
        attachment_key: str,
        *,
        types: set[str] | None = None,
        include_text: bool = True,
        include_comments: bool = True,
    ) -> list[Any]: ...

    def get_pdf_attachment_key(self, parent_key: str) -> str | None: ...

    def get_sources_with_annotations(self, *, include_all: bool = False) -> list[Any]: ...
