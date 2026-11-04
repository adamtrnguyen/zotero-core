"""Search: metadata, annotations, and fulltext.

WHY THIS EXISTS
---------------
There was no library search of ANY kind in this package -- not by title, citekey, author,
tag or DOI -- so every search an agent ran went to cookjohn's third-party plugin. Which
also means Zotero's own matching was the only matching available, and Zotero's is
SUBSTRING: measured on this library, `Langevan Dynmaics` returns ZERO results against a
paper that is definitely there. CLAUDE.md records the same gap on the Calibre side, where
substring matching measured 69% recall against calfuzz's 97%.

THREE SEARCHES, THREE DIFFERENT SHAPES
--------------------------------------
`items`      metadata. Small enough (1.4k rows) to score in Python, which is what buys
             fuzzy matching -- SQL cannot express "two typos away".

`annotations` `itemAnnotations.text` / `.comment`, with colour and type filters. Substring:
             annotations are short and the caller usually knows the phrase they
             highlighted.

`fulltext`   TWO STAGES, and the staging is the whole design. `fulltextItemWords` is a
             word -> item index with no positions and no counts, so it can answer "which
             items contain this word" and NOTHING about phrases or context. The extracted
             text lives in 1,325 `.zotero-ft-cache` files totalling 587 MB. Reading all
             of those per query is absurd; narrowing to candidates with the index first
             and then reading only those files makes a phrase query touch a handful.
"""

from __future__ import annotations

import re
from pathlib import Path

from zotero_core.domain.annotation_type import label_for
from zotero_core.domain.entities.models import AnnotationHit, FulltextHit, ItemHit
from zotero_core.domain.services.policy import (
    DEFAULT_FUZZY_THRESHOLD,
    normalize_title,
    score_tokens,
)
from zotero_core.infrastructure.sqlite.annotations import DEFAULT_ZOTERO_DB
from zotero_core.infrastructure.sqlite.connect import (
    DEFAULT_BUSY_TIMEOUT_MS,
    USER_LIBRARY_ID,
    open_readonly,
)

FT_CACHE_NAME = ".zotero-ft-cache"
DEFAULT_LIMIT = 25
DEFAULT_CONTEXT_CHARS = 160


_ITEMS_SQL = """
SELECT i.key,
       COALESCE(v.value, '') AS title,
       it.typeName,
       COALESCE((SELECT GROUP_CONCAT(c.lastName, ', ')
                   FROM itemCreators ic
                   JOIN creators c ON c.creatorID = ic.creatorID
                  WHERE ic.itemID = i.itemID), '') AS creators,
       COALESCE((SELECT GROUP_CONCAT(t.name, ', ')
                   FROM itemTags itg
                   JOIN tags t ON t.tagID = itg.tagID
                  WHERE itg.itemID = i.itemID), '') AS tags,
       COALESCE((SELECT dv.value FROM itemData d2
                   JOIN itemDataValues dv ON dv.valueID = d2.valueID
                  WHERE d2.itemID = i.itemID
                    AND d2.fieldID = (SELECT fieldID FROM fields WHERE fieldName='DOI')), '') AS doi
  FROM items i
  JOIN itemTypes it ON it.itemTypeID = i.itemTypeID
  LEFT JOIN itemData dat ON dat.itemID = i.itemID
       AND dat.fieldID = (SELECT fieldID FROM fields WHERE fieldName = 'title')
  LEFT JOIN itemDataValues v ON v.valueID = dat.valueID
 WHERE i.libraryID = :library_id
   AND it.typeName NOT IN ('attachment', 'note', 'annotation')
   AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
"""

_ANNOTATIONS_SQL = """
SELECT ann.key,
       att.key AS attachment_key,
       parent.key AS parent_key,
       COALESCE(v.value, '') AS parent_title,
       a.type, COALESCE(a.text, ''), COALESCE(a.comment, ''),
       COALESCE(a.color, ''), COALESCE(a.pageLabel, '')
  FROM itemAnnotations a
  JOIN items ann ON ann.itemID = a.itemID
  JOIN items att ON att.itemID = a.parentItemID
  LEFT JOIN itemAttachments ia ON ia.itemID = att.itemID
  LEFT JOIN items parent ON parent.itemID = ia.parentItemID
  LEFT JOIN itemData dat ON dat.itemID = parent.itemID
       AND dat.fieldID = (SELECT fieldID FROM fields WHERE fieldName = 'title')
  LEFT JOIN itemDataValues v ON v.valueID = dat.valueID
 WHERE ann.itemID NOT IN (SELECT itemID FROM deletedItems)
"""

_WORD_ITEMS_SQL = """
SELECT fiw.itemID
  FROM fulltextItemWords fiw
  JOIN fulltextWords fw ON fw.wordID = fiw.wordID
 WHERE fw.word = ?
"""

_ATTACHMENT_KEYS_SQL = """
SELECT att.itemID, att.key,
       COALESCE(parent.key, att.key),
       COALESCE(v.value, '')
  FROM items att
  LEFT JOIN itemAttachments ia ON ia.itemID = att.itemID
  LEFT JOIN items parent ON parent.itemID = ia.parentItemID
  LEFT JOIN itemData dat ON dat.itemID = parent.itemID
       AND dat.fieldID = (SELECT fieldID FROM fields WHERE fieldName = 'title')
  LEFT JOIN itemDataValues v ON v.valueID = dat.valueID
 WHERE att.itemID IN ({placeholders})
"""


class ZoteroSearchStore:
    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        library_id: int = USER_LIBRARY_ID,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        storage_dir: str | Path | None = None,
    ):
        self.db_path = Path(db_path or DEFAULT_ZOTERO_DB).expanduser()
        self.library_id = library_id
        self.busy_timeout_ms = busy_timeout_ms
        self.storage_dir = Path(storage_dir) if storage_dir else self.db_path.parent / "storage"

    # ------------------------------------------------------------------
    # metadata
    # ------------------------------------------------------------------

    def for_library(self, library_id: int) -> ZoteroSearchStore:
        """A sibling reading the SAME database, scoped to another library.

        Satisfies the matching port. The read facade used to do this itself, constructing
        a concrete store from a stashed db path -- an application-layer facade building an
        adapter. Asking the current adapter for a sibling keeps "how do I make another one
        of me" with the class that knows, and returns `self` for the library it already reads.

        Cheap on purpose: constructing one holds paths and an int, and opens nothing.
        """
        if library_id == self.library_id:
            return self
        return ZoteroSearchStore(
            self.db_path, library_id=library_id, busy_timeout_ms=self.busy_timeout_ms
        )

    def items(
        self,
        query: str,
        *,
        fuzzy: bool = True,
        threshold: float = DEFAULT_FUZZY_THRESHOLD,
        limit: int = DEFAULT_LIMIT,
        item_type: str | None = None,
    ) -> tuple[tuple[ItemHit, ...], str]:
        """Search titles, creators, tags and DOI. Fuzzy by default.

        Scored in PYTHON, not SQL. The library is ~1.4k regular items, which is small
        enough to score exhaustively and is the only way to get "two typos away" -- SQL
        LIKE cannot express it. The field that matched is reported, because "why did
        this come back" is otherwise unanswerable for a fuzzy hit.
        """
        needle = query.strip()
        if not needle:
            return (), ""
        conn, read_mode = open_readonly(self.db_path, busy_timeout_ms=self.busy_timeout_ms)
        try:
            rows = conn.execute(_ITEMS_SQL, {"library_id": self.library_id}).fetchall()
        finally:
            conn.close()

        # Normalise the QUERY once, not once per row per field. That alone was most of
        # the original five-second runtime: an NFKD pass plus two regex substitutions
        # against every field of every one of ~1.4k rows.
        norm_query = normalize_title(needle)
        query_tokens = norm_query.split()
        hits: list[ItemHit] = []
        for key, title, typename, creators, tags, doi in rows:
            if item_type and typename != item_type:
                continue
            best_score = 0.0
            best_field = ""
            for field_name, value in (
                ("title", title),
                ("creator", creators),
                ("tag", tags),
                ("doi", doi),
            ):
                if not value:
                    continue
                norm_value = normalize_title(value)
                if fuzzy:
                    score = score_tokens(query_tokens, norm_value)
                else:
                    score = 1.0 if norm_query in norm_value else 0.0
                # Ties go to the earlier field: a title match explains itself better
                # than the same score against a tag.
                if score > best_score:
                    best_score, best_field = score, field_name
            cutoff = threshold if fuzzy else 1.0
            if best_score >= cutoff:
                hits.append(
                    ItemHit(
                        item_key=key,
                        title=title,
                        item_type=typename,
                        creators=creators,
                        score=round(best_score, 4),
                        matched_on=best_field,
                    )
                )
        hits.sort(key=lambda h: (-h.score, h.title))
        return tuple(hits[:limit]), read_mode

    # ------------------------------------------------------------------
    # annotations
    # ------------------------------------------------------------------

    def annotations(
        self,
        query: str = "",
        *,
        color: str | None = None,
        types: set[str] | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> tuple[tuple[AnnotationHit, ...], str]:
        """Search highlight text and comments, with colour and type filters.

        Substring rather than fuzzy: an annotation is a phrase the caller highlighted
        themselves, so they usually have the words right. Colour is how people encode
        meaning in Zotero -- "the yellow ones" is a real category.
        """
        conn, read_mode = open_readonly(self.db_path, busy_timeout_ms=self.busy_timeout_ms)
        try:
            rows = conn.execute(_ANNOTATIONS_SQL).fetchall()
        finally:
            conn.close()

        needle = query.strip().casefold()
        wanted_color = (color or "").strip().casefold()
        hits: list[AnnotationHit] = []
        for key, att_key, parent_key, parent_title, type_id, text, comment, colour, page in rows:
            type_name = label_for(type_id)
            if types and type_name not in types:
                continue
            if wanted_color and colour.casefold() != wanted_color:
                continue
            if needle and needle not in text.casefold() and needle not in comment.casefold():
                continue
            hits.append(
                AnnotationHit(
                    annotation_key=key,
                    attachment_key=att_key,
                    parent_key=parent_key or att_key,
                    parent_title=parent_title,
                    annotation_type=type_name,
                    text=text,
                    comment=comment,
                    color=colour,
                    page_label=page,
                )
            )
        return tuple(hits[:limit]), read_mode

    # ------------------------------------------------------------------
    # fulltext
    # ------------------------------------------------------------------

    def fulltext(
        self,
        query: str,
        *,
        limit: int = DEFAULT_LIMIT,
        context_chars: int = DEFAULT_CONTEXT_CHARS,
        max_snippets: int = 3,
    ) -> tuple[tuple[FulltextHit, ...], str]:
        """Search extracted PDF text. Index first, then read only the survivors.

        Stage 1 intersects `fulltextItemWords` across every word in the query -- fast,
        and it cuts 1,325 candidate documents to a handful. Stage 2 opens only those
        `.zotero-ft-cache` files to confirm the PHRASE and pull context, because the
        index knows nothing about adjacency.

        A word absent from the index short-circuits to no results, which is correct and
        also the cheap path.
        """
        # TWO tokenisations, deliberately. The index is keyed on Zotero's own normalised
        # words, so lookups must be normalised too. The phrase regex runs against the RAW
        # cache text, where accents and punctuation are intact, so it uses the raw words.
        index_words = [w for w in normalize_title(query).split() if w]
        raw_words = [w for w in query.split() if w]
        if not index_words or not raw_words:
            return (), ""

        conn, read_mode = open_readonly(self.db_path, busy_timeout_ms=self.busy_timeout_ms)
        try:
            # Seeded from the FIRST word rather than from None, so the intersection has
            # no "not started yet" state to narrow away afterwards. `index_words` is
            # non-empty by the guard above.
            first, rest = index_words[0], index_words[1:]
            candidates: set[int] = {row[0] for row in conn.execute(_WORD_ITEMS_SQL, (first,))}
            for word in rest:
                if not candidates:
                    break
                candidates &= {row[0] for row in conn.execute(_WORD_ITEMS_SQL, (word,))}
            if not candidates:
                return (), read_mode

            placeholders = ",".join("?" for _ in candidates)
            sql = _ATTACHMENT_KEYS_SQL.format(placeholders=placeholders)
            meta = conn.execute(sql, tuple(candidates)).fetchall()
        finally:
            conn.close()

        hits: list[FulltextHit] = []
        for _item_id, att_key, parent_key, title in meta:
            cache = self.storage_dir / att_key / FT_CACHE_NAME
            if not cache.exists():
                continue
            try:
                text = cache.read_text(errors="replace")
            except OSError:
                continue
            snippets, count = _snippets(text, raw_words, context_chars, max_snippets)
            if not count:
                continue
            hits.append(
                FulltextHit(
                    attachment_key=att_key,
                    parent_key=parent_key,
                    title=title,
                    snippets=snippets,
                    match_count=count,
                )
            )
        hits.sort(key=lambda h: (-h.match_count, h.title))
        return tuple(hits[:limit]), read_mode

    def attachment_text(self, attachment_key: str) -> str | None:
        """The extracted text of one attachment, or None if Zotero never indexed it."""
        cache = self.storage_dir / attachment_key / FT_CACHE_NAME
        if not cache.exists():
            return None
        try:
            return cache.read_text(errors="replace")
        except OSError:
            return None


def _snippets(
    text: str, words: list[str], context_chars: int, max_snippets: int
) -> tuple[tuple[str, ...], int]:
    r"""Find the phrase in `text` and return context windows around the first few hits.

    A REGEX over the original text, not a substring scan over a folded copy. Two reasons,
    both of which produced wrong output in the first version of this function:

    1. `str.casefold()` is not length-preserving -- 'ß' folds to 'ss' -- so offsets taken
       from a folded copy do not index the original, and every snippet slides.
    2. The cache files separate paragraphs with '\n\n', so a phrase spanning a line
       break has TWO whitespace characters where the query has one space. An exact
       substring never matches it. `\W+` between words handles that, and punctuation
       between words, at the same time.

    Offsets come straight from the match, so the snippet shown is the real text with its
    accents, case and punctuation intact.
    """
    if not words:
        return (), 0
    pattern = re.compile(r"\W+".join(re.escape(word) for word in words), re.IGNORECASE)

    found: list[str] = []
    count = 0
    for match in pattern.finditer(text):
        count += 1
        if len(found) < max_snippets:
            lo = max(0, match.start() - context_chars // 2)
            hi = min(len(text), match.end() + context_chars // 2)
            found.append(" ".join(text[lo:hi].split()))
    return tuple(found), count
