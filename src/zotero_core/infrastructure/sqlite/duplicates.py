"""Is this thing already in Zotero? Read-only, and answered BEFORE a create.

Exists because the two things that create Zotero items today disagree about the
answer. `importers/calibre2zotero/sync.py` scans Zotero's `extra` field for
`calibre-uuid:<uuid>`; `calibre-zotero-jump/ui.py` instead trusts a `zotero`
identifier on the CALIBRE side. Those are different questions:

  * the `extra` scan asks "does Zotero already hold this book"
  * the Calibre identifier asks "does Calibre think it pushed this book"

They diverge exactly when a push half-succeeded -- item created, identifier never
stamped back -- and then one client re-creates what the other can already see. Several
hundred items in the live library carry a `calibre-uuid:` stamp, so this is not a corner.
(The exact number moves with every import; `check_duplicate(calibre_uuid=…)` is the thing
that answers, not a figure written down here.)

VERDICT TIERS, same three as calibre-core
-----------------------------------------
  block -- no judgement required: the same DOI, the same ISBN, or the same
           calibre-uuid. These are identifiers; a match is an identity claim.
  warn  -- plausible but needs a human: same normalised title AND same first-author
           surname. Two editions and a preprint/published pair both look like this,
           and both are legitimately two items.
  ok    -- nothing matched.

Everything here runs off the catalogue -- no file is opened, no network call is
made -- so the whole check is three queries over a few thousand rows.

ON THE NORMALISER
-----------------
`_norm_title` is `domain.policy.normalize_title`, shared with the search layer so the two
cannot drift. Kana voicing marks are preserved there (phonemic); Latin accents fold
(decoration).

This is still not calibre-core's `dedup_key`, which additionally
handles Hangul Jamo decomposition and ordinal-before-"edition" stripping. If
cross-library title matching becomes load-bearing -- which is what calibre2zotero does
-- the normaliser is the thing to extract into a shared package rather than reimplement
a third time.
"""

from __future__ import annotations

from zotero_core.domain.services.identifiers import _calibre_uuids, clean_doi, clean_isbn
from zotero_core.domain.services.policy import normalize_title as _norm_title
from zotero_core.domain.services.policy import surname_of as _surname
from zotero_core.infrastructure.sqlite.connect import open_readonly
from zotero_core.infrastructure.sqlite.items import ZoteroItemStore


def check_duplicate(
    store: ZoteroItemStore | None = None,
    *,
    title: str | None = None,
    doi: str | None = None,
    isbn: str | None = None,
    calibre_uuid: str | None = None,
    creators: list[dict[str, str]] | tuple[dict[str, str], ...] = (),
) -> dict:
    """Look for an existing item matching the one about to be created.

    Returns {"verdict": "block"|"warn"|"ok", "signals": [...]}, where each signal
    names the matched item key and says which evidence matched.

    Trashed items are reported but do NOT block: something in the trash is on its
    way out, and refusing to re-create it would leave the caller stuck behind a
    decision someone else already made. The signal carries `trashed: true` so a
    caller can tell the difference.
    """
    store = store or ZoteroItemStore()
    # Was `store._connect()` -- the package's only reach into another module's
    # private. Same opener, now a shared one.
    conn, read_mode = open_readonly(store.db_path, busy_timeout_ms=store.busy_timeout_ms)
    try:
        rows = conn.execute(
            """
            SELECT i.key,
                   MAX(CASE WHEN f.fieldName = 'title' THEN v.value END),
                   MAX(CASE WHEN f.fieldName = 'DOI'   THEN v.value END),
                   MAX(CASE WHEN f.fieldName = 'ISBN'  THEN v.value END),
                   MAX(CASE WHEN f.fieldName = 'extra' THEN v.value END),
                   (SELECT COUNT(*) FROM deletedItems d WHERE d.itemID = i.itemID)
              FROM items i
              JOIN itemData dt ON dt.itemID = i.itemID
              JOIN fields f ON f.fieldID = dt.fieldID
              JOIN itemDataValues v ON v.valueID = dt.valueID
             WHERE i.libraryID = ?
               AND f.fieldName IN ('title', 'DOI', 'ISBN', 'extra')
             GROUP BY i.itemID
            """,
            (store.library_id,),
        ).fetchall()
        # First author per item, for the title+author signal. Separate query rather
        # than another join: folding the creators fan-out into the itemData fan-out
        # multiplies rows, which is the mistake calibre-core documents in its own
        # duplicate check.
        first_authors = {
            key: _norm_title(last or name or "")
            for key, last, name in conn.execute(
                """
                SELECT i.key, c.lastName, c.firstName
                  FROM items i
                  JOIN itemCreators ic ON ic.itemID = i.itemID AND ic.orderIndex = 0
                  JOIN creators c ON c.creatorID = ic.creatorID
                 WHERE i.libraryID = ?
                """,
                (store.library_id,),
            )
        }
    finally:
        conn.close()

    want_doi = clean_doi(doi)
    want_isbn = clean_isbn(isbn)
    want_uuid = (calibre_uuid or "").strip().lower()
    want_title = _norm_title(title or "")
    want_surname = _surname(creators[0]) if creators else ""

    signals: list[dict] = []
    for key, row_title, row_doi, row_isbn, row_extra, deleted in rows:
        trashed = bool(deleted)
        if want_doi and clean_doi(row_doi) == want_doi:
            signals.append(
                {
                    "signal": "doi",
                    "item_key": key,
                    "trashed": trashed,
                    "detail": f"DOI {want_doi} on {row_title!r}",
                }
            )
        if want_isbn and clean_isbn(row_isbn) == want_isbn:
            signals.append(
                {
                    "signal": "isbn",
                    "item_key": key,
                    "trashed": trashed,
                    "detail": f"ISBN {want_isbn} on {row_title!r}",
                }
            )
        if want_uuid and want_uuid in {u.lower() for u in _calibre_uuids(row_extra)}:
            signals.append(
                {
                    "signal": "calibre-uuid",
                    "item_key": key,
                    "trashed": trashed,
                    "detail": f"calibre-uuid {want_uuid} on {row_title!r}",
                }
            )
        if (
            want_title
            and _norm_title(row_title or "") == want_title
            # Require the surname to match too, or every "Introduction" collides.
            # An absent surname on either side makes this signal unavailable rather
            # than automatically true.
            and want_surname
            and first_authors.get(key) == want_surname
        ):
            signals.append(
                {
                    "signal": "title+author",
                    "item_key": key,
                    "trashed": trashed,
                    "detail": f"{row_title!r} — {want_surname}",
                }
            )

    live = [s for s in signals if not s["trashed"]]
    hard = [s for s in live if s["signal"] in ("doi", "isbn", "calibre-uuid")]
    soft = [s for s in live if s["signal"] == "title+author"]
    verdict = "block" if hard else ("warn" if soft else "ok")
    return {"verdict": verdict, "signals": signals, "read_mode": read_mode}


class CatalogueDuplicateFinder:
    """Satisfies `domain.ports.duplicates.DuplicateFinder` against a real catalogue.

    Holds the store so the port's `check` needs no `store` argument -- the application
    asks the question, and which catalogue answers it is composition, decided in
    `interfaces/factory.py`. Delegates to `check_duplicate` above, which is unchanged and
    still the implementation.
    """

    def __init__(self, store=None):
        self.store = store

    def check(
        self,
        *,
        title: str | None = None,
        doi: str | None = None,
        isbn: str | None = None,
        calibre_uuid: str | None = None,
        creators: list[dict[str, str]] | tuple[dict[str, str], ...] = (),
    ) -> dict:
        return check_duplicate(
            self.store,
            title=title,
            doi=doi,
            isbn=isbn,
            calibre_uuid=calibre_uuid,
            creators=creators,
        )
