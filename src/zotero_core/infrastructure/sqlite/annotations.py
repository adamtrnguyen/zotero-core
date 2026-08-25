from __future__ import annotations

import sqlite3
from pathlib import Path

from zotero_core.domain.annotation_type import label_for
from zotero_core.domain.entities.models import Annotation, ZoteroSource
from zotero_core.infrastructure.sqlite.connect import DEFAULT_BUSY_TIMEOUT_MS, open_readonly

DEFAULT_ZOTERO_DB = Path.home() / "Zotero" / "zotero.sqlite"


class ZoteroAnnotationStore:
    def __init__(
        self,
        db_path: str | Path = DEFAULT_ZOTERO_DB,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ):
        self.db_path = Path(db_path).expanduser()
        self.busy_timeout_ms = busy_timeout_ms
        # Set on every _connect. "" until the first read.
        self.last_read_mode: str = ""

    def get_annotations(
        self,
        attachment_key: str,
        *,
        types: set[str] | None = None,
        include_text: bool = True,
        include_comments: bool = True,
    ) -> list[Annotation]:
        if not attachment_key:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    ann.key,
                    pdf.key,
                    parent.key,
                    ia.type,
                    COALESCE(ia.pageLabel, ''),
                    COALESCE(ia.color, ''),
                    COALESCE(ia.text, ''),
                    COALESCE(ia.comment, ''),
                    COALESCE(ia.sortIndex, '')
                FROM itemAnnotations ia
                JOIN items ann ON ann.itemID = ia.itemID
                JOIN items pdf ON pdf.itemID = ia.parentItemID
                LEFT JOIN itemAttachments att ON att.itemID = pdf.itemID
                LEFT JOIN items parent ON parent.itemID = att.parentItemID
                WHERE pdf.key = ?
                ORDER BY ia.sortIndex
                """,
                (attachment_key,),
            ).fetchall()

        annotations = [self._row_to_annotation(row) for row in rows]
        if types:
            annotations = [ann for ann in annotations if ann.type in types]
        if not include_text:
            annotations = [self._without_text(ann) for ann in annotations]
        if not include_comments:
            annotations = [self._without_comment(ann) for ann in annotations]
        return annotations

    def get_sources_with_annotations(self, *, include_all: bool = False) -> list[ZoteroSource]:
        """Every attachment carrying annotations, with the item it belongs to.

        When ``include_all`` is True, returns ALL attachments (including those
        with zero annotations) so the Obsidian inserter can offer a full-library
        fuzzy search.

        ⚠ THIS USED TO HIDE 5 OF 18. The query started from `items parent` and required
        `att.contentType = 'application/pdf'`, so it dropped two whole shapes:

            4 × text/html   annotated web snapshots, 77 annotations between them
            1 × PDF         a STANDALONE attachment, filtered out by the parent JOIN

        Zotero annotates snapshots and EPUBs, not only PDFs, and an attachment with no parent
        is its own source rather than no source. The verb an agent reaches for to audit
        annotation coverage was reporting a clean library while hiding the interesting rows —
        including the second half of a duplicated paper.

        The type is now CARRIED (`ZoteroSource.content_type`) rather than filtered on: a
        caller wanting only PDFs can still say so, and one that does not is no longer lied to.
        `list_pdfs` still restricts to PDFs on purpose — it enumerates the PDF corpus for
        `omni-rag`, which is a different question.
        """
        having_clause = "" if include_all else "HAVING ann_count > 0"
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                WITH annotated AS (
                    SELECT
                        att.itemID       AS att_id,
                        a.key            AS att_key,
                        att.parentItemID AS parent_id,
                        att.contentType  AS content_type,
                        COUNT(ia.itemID) AS ann_count
                    FROM itemAttachments att
                    JOIN items a ON a.itemID = att.itemID
                    LEFT JOIN itemAnnotations ia ON ia.parentItemID = att.itemID
                    GROUP BY att.itemID
                    {having_clause}
                ),
                titles AS (
                    SELECT id.itemID, idv.value AS title
                    FROM itemData id
                    JOIN itemDataValues idv ON idv.valueID = id.valueID
                    JOIN fields f ON f.fieldID = id.fieldID
                    WHERE f.fieldName = 'title'
                ),
                authors AS (
                    SELECT ic.itemID, GROUP_CONCAT(c.lastName, ', ') AS author_list
                    FROM itemCreators ic
                    JOIN creators c ON c.creatorID = ic.creatorID
                    WHERE ic.orderIndex < 3
                    GROUP BY ic.itemID
                )
                SELECT
                    -- a standalone attachment is its OWN source, so it reports its own key
                    COALESCE(p.key, an.att_key),
                    an.att_key,
                    COALESCE(t.title, ta.title, ''),
                    COALESCE(au.author_list, ''),
                    an.ann_count,
                    COALESCE(an.content_type, '')
                FROM annotated an
                LEFT JOIN items p ON p.itemID = an.parent_id
                LEFT JOIN titles t ON t.itemID = an.parent_id
                LEFT JOIN titles ta ON ta.itemID = an.att_id
                LEFT JOIN authors au ON au.itemID = an.parent_id
                ORDER BY COALESCE(t.title, ta.title) COLLATE NOCASE
                """
            ).fetchall()
        return [
            ZoteroSource(
                parent_key=row[0] or "",
                attachment_key=row[1] or "",
                title=row[2] or "(no title)",
                authors=row[3] or "",
                annotation_count=int(row[4] or 0),
                content_type=row[5] or "",
            )
            for row in rows
        ]

    def get_pdf_attachment_key(self, parent_key: str) -> str | None:
        if not parent_key:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT child.key
                FROM items parent
                JOIN itemAttachments ia ON ia.parentItemID = parent.itemID
                JOIN items child ON child.itemID = ia.itemID
                LEFT JOIN itemAnnotations ann ON ann.parentItemID = child.itemID
                WHERE parent.key = ? AND ia.contentType = 'application/pdf'
                GROUP BY child.itemID
                ORDER BY COUNT(ann.itemID) DESC, child.itemID
                LIMIT 1
                """,
                (parent_key,),
            ).fetchone()
        return row[0] if row else None

    def _connect(self) -> sqlite3.Connection:
        """Open through the ONE opener, and remember which mode answered.

        ⚠ This used to hardcode `immutable=1` and probe nothing, which made it the only
        sqlite reader in the package not going through `connect.open_readonly` -- while
        `read/__init__.py` claimed every read reports the mode that served it. Two
        consequences, both real: an annotation read could never be a live read even when
        Zotero was closed and `mode=ro` would have succeeded, and a caller had no way to
        learn it had been handed a point-in-time snapshot.

        `last_read_mode` rather than a changed return type: these methods return plain
        lists that several callers unpack positionally.
        """
        conn, mode = open_readonly(self.db_path, busy_timeout_ms=self.busy_timeout_ms)
        self.last_read_mode = mode
        return conn

    def _row_to_annotation(self, row: sqlite3.Row | tuple) -> Annotation:
        page_label = row[4] or ""
        key = row[0] or ""
        attachment_key = row[1] or ""
        return Annotation(
            key=key,
            attachment_key=attachment_key,
            parent_key=row[2] or None,
            type=label_for(row[3]),
            page_label=page_label,
            color=row[5] or "",
            text=row[6] or "",
            comment=row[7] or "",
            sort_index=str(row[8] or ""),
            zotero_url=annotation_url(attachment_key, page_label, key),
        )

    @staticmethod
    def _without_text(annotation: Annotation) -> Annotation:
        return Annotation(**{**annotation.__dict__, "text": ""})

    @staticmethod
    def _without_comment(annotation: Annotation) -> Annotation:
        return Annotation(**{**annotation.__dict__, "comment": ""})


def annotation_url(attachment_key: str, page_label: str, annotation_key: str) -> str:
    page_part = f"page={page_label}&" if page_label else ""
    return f"zotero://open-pdf/library/items/{attachment_key}?{page_part}annotation={annotation_key}"
