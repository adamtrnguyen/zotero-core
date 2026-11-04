"""Turning a paper's URL into Zotero metadata and a PDF.

The port states one question -- what is at this URL -- and the saving itself goes through
the existing gated verbs, `create_item` and `import_attachment`. It replaces ZotLink; why:
docs/history/paper-resolver.md.

Behind a port for the reason `CitationKeys` is: it is the network, not our database or our
plugin, and a test must be able to answer it without one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ResolvedPaper:
    """What a URL resolved to, already in Zotero's vocabulary.

    `fields` and `creators` go to `create_item` unchanged, so they use Zotero field names
    (`abstractNote`, `publicationTitle`, `DOI`) rather than the source's.
    """

    source: str  # "arxiv" | "doi" | "meta"
    item_type: str
    fields: dict[str, str]
    creators: tuple[dict[str, str], ...] = ()
    pdf_url: str | None = None
    notes: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "item_type": self.item_type,
            "fields": dict(self.fields),
            "creators": [dict(c) for c in self.creators],
            "pdf_url": self.pdf_url,
            "notes": list(self.notes),
        }


@runtime_checkable
class PaperResolver(Protocol):
    """Resolves paper URLs and fetches their PDFs.

    `resolve` raises `WriteBlocked(Reason.PAPER_UNRESOLVED)` rather than returning a guess:
    a page with no scholarly metadata is refused, never saved under its HTML <title>.
    """

    def resolve(self, url: str) -> ResolvedPaper: ...

    def download_pdf(self, pdf_url: str) -> str:
        """Fetch a PDF to a temporary file and return its path. Raises if it is not a PDF."""
        ...
