"""Save a paper from its URL: resolve it, create the item, attach the PDF.

Composition only. Every gate belongs to a verb that already exists: `create_item` refuses
a duplicate (arXiv saves carry arXiv's DOI, so a repeat save is a DOI BLOCK, not a title
warning) and files into the collection; `import_attachment` checks the parent and reads
the attachment back. This module adds no write path of its own, which is what lets it
replace ZotLink's `_save_via_connector` without adding an ungated one.

The PDF is best-effort ON PURPOSE. Once `create_item` has succeeded the item exists, so a
PDF that cannot be fetched is reported in the result, not raised: raising would tell the
caller the save failed, and a retry would then hit the duplicate gate for an item it was
told does not exist.
"""

from __future__ import annotations

import os

from zotero_core.application.results import ok
from zotero_core.application.services.session import WriteSession
from zotero_core.application.services.verbs import create_item, import_attachment
from zotero_core.domain.errors import WriteBlocked


def resolve_paper(url: str, *, session: WriteSession) -> dict:
    """What `save_paper` would create, and whether the library already has it. Writes nothing."""
    paper = session.papers.resolve(url)
    dup = session.duplicates.check(
        title=paper.fields.get("title"),
        doi=paper.fields.get("DOI"),
        isbn=None,
        calibre_uuid=None,
        creators=paper.creators,
    )
    return ok("resolve_paper", transport="none", paper=paper.as_dict(), duplicate_check=dup)


def save_paper(
    url: str,
    *,
    collection_key: str | None = None,
    tags: list[str] | None = None,
    attach_pdf: bool = True,
    force: bool = False,
    session: WriteSession,
) -> dict:
    paper = session.papers.resolve(url)
    created = create_item(
        paper.item_type,
        dict(paper.fields),
        creators=list(paper.creators) or None,
        tags=tags,
        collection_key=collection_key,
        force=force,
        session=session,
    )
    item_key = created["item_key"]

    pdf: dict | None = None
    if attach_pdf and paper.pdf_url:
        pdf = _attach_pdf(item_key, paper.pdf_url, session=session)
    elif attach_pdf:
        pdf = {"ok": False, "error": f"no PDF URL found via {paper.source}"}

    return ok(
        "save_paper",
        transport="cookjohn",
        item_key=item_key,
        title=paper.fields.get("title"),
        source=paper.source,
        item_type=paper.item_type,
        pdf=pdf,
        created=created,
        undo_call=f"trash_items(['{item_key}'])",
    )


def _attach_pdf(item_key: str, pdf_url: str, *, session: WriteSession) -> dict:
    try:
        path = session.papers.download_pdf(pdf_url)
    except Exception as exc:  # noqa: BLE001 - reported, see the module docstring
        return {"ok": False, "pdf_url": pdf_url, "error": str(exc)}
    try:
        return import_attachment(item_key, path, title="Full Text PDF", session=session)
    except WriteBlocked as exc:
        return {"pdf_url": pdf_url, **exc.as_dict()}
    finally:
        # cookjohn's import COPIES into Zotero storage, so the download is scratch.
        os.remove(path)
