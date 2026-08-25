from __future__ import annotations

from zotero_core.application.results import rows
from zotero_core.domain.entities.gui import ReaderContext, ReaderState, WindowState
from zotero_core.domain.entities.models import Annotation, ZoteroSource
from zotero_core.domain.ports.annotation_catalogue import AnnotationCatalogue
from zotero_core.domain.ports.catalogue import Catalogue
from zotero_core.domain.ports.citation_keys import CitationKeys
from zotero_core.domain.ports.collection_catalogue import CollectionCatalogue
from zotero_core.domain.ports.duplicates import DuplicateFinder
from zotero_core.domain.ports.gui_bridge import GuiBridge
from zotero_core.domain.ports.library_catalogue import LibraryCatalogue
from zotero_core.domain.ports.search_catalogue import SearchCatalogue
from zotero_core.domain.services.identity import is_key


class ZoteroContext:
    """The read facade: every read an agent or the CLI can ask for, in one object.

    ⚠ THIS USED TO CONSTRUCT ITS OWN COLLABORATORS from a db path and two URLs --
    `self.items = ZoteroItemStore(zotero_db_path)`, six times over -- which is the same
    defect the write path had, in the layer that could least afford it: this class is what
    BOTH the CLI and the MCP adapter wrap, so a caller could not point it anywhere without
    an environment variable, and a test could not substitute anything at all.

    It also lived in `infrastructure/`, below the layer it belongs to. It is a use case: it
    composes seven collaborators and applies policy across them (which library to read,
    whether a string is a key or a citekey, whether a duplicate blocks). None of that is a
    driver detail.

    Now every collaborator arrives as a port. `interfaces/factory.build_context()` is the
    one place that decides which real adapters those are.
    """

    def __init__(
        self,
        *,
        bridge: GuiBridge,
        annotations: AnnotationCatalogue,
        bbt: CitationKeys,
        items: Catalogue,
        collections: CollectionCatalogue,
        search: SearchCatalogue,
        libraries: LibraryCatalogue,
        duplicates: DuplicateFinder,
    ):
        self.bridge = bridge
        self.annotations = annotations
        self.bbt = bbt
        # ⚠ The item store's ABSENCE was once the whole problem. This class is what both
        # the CLI and the MCP adapter wrap, so for as long as it held no item store,
        # `items.py` and `duplicates.py` were unreachable from every agent-facing surface
        # -- and the gap got filled by a third-party plugin, which is exactly what this
        # package exists to prevent.
        self.items = items
        self.collections = collections
        self.search = search
        self.libraries = libraries
        self.duplicates = duplicates

    def _collections_for(self, library_id: int | None) -> CollectionCatalogue:
        """The default store, or a fresh one aimed at another library.

        Constructing one is cheap -- it holds paths and an int, opening nothing -- so
        this stays a per-call choice rather than server state.
        """
        if library_id is None:
            return self.collections
        return self.collections.for_library(library_id)

    def _search_for(self, library_id: int | None) -> SearchCatalogue:
        if library_id is None:
            return self.search
        return self.search.for_library(library_id)

    def list_libraries(self) -> dict:
        """Every library with a live item count -- the user's, plus any groups.

        Every other read here is scoped to ONE library and defaults to the user's. That
        default is right and it is also invisible: without this, a caller cannot tell
        "you have nothing" from "you are looking in the wrong library".
        """
        libraries, read_mode = self.libraries.list_libraries()
        return rows(
            "libraries",
            [
                {
                    'library_id': lib.library_id,
                    'type': lib.library_type,
                    'name': lib.name,
                    'editable': lib.editable,
                    'item_count': lib.item_count,
                    'collection_count': lib.collection_count,
                }
                for lib in libraries
            ],
            read_mode=read_mode,
        )

    def ping(self) -> dict:
        return self.bridge.ping()

    def get_window_state(self) -> WindowState:
        return self.bridge.get_window_state()


    def get_open_readers(self) -> list[ReaderState]:
        return self.get_window_state().readers

    def get_active_reader(self) -> ReaderState | None:
        """The reader on the active tab, as raw state -- no annotations, no citekeys.

        The cheap question: WHICH document is in front of the user. `get_active_reader_context`
        is the expensive one, which joins annotations onto it.
        """
        for reader in self.get_open_readers():
            if reader.is_active_tab:
                return reader
        return None

    def get_active_reader_context(
        self,
        *,
        include_annotations: bool = True,
        include_citekeys: bool = True,
        annotation_types: set[str] | None = None,
    ) -> ReaderContext | None:
        """The active reader with its annotations joined on, or None if nothing is open.

        ⚠ THE `[0] if contexts else None` REDUCTION LIVED IN BOTH ADAPTERS. `read_mcp` and
        `cli` each called `get_open_reader_context(active_only=True, ...)` and then took the
        first element themselves -- the same three lines, free to drift, in the two places
        least able to notice. "Active" is a fact about the library, not about a transport, so
        the collapse belongs here.
        """
        contexts = self.get_open_reader_context(
            active_only=True,
            include_annotations=include_annotations,
            include_citekeys=include_citekeys,
            annotation_types=annotation_types,
        )
        return contexts[0] if contexts else None

    def get_annotations(
        self,
        attachment_key: str,
        *,
        types: set[str] | None = None,
        include_text: bool = True,
        include_comments: bool = True,
    ) -> list[Annotation]:
        return self.annotations.get_annotations(
            attachment_key,
            types=types,
            include_text=include_text,
            include_comments=include_comments,
        )

    def resolve_pdf_attachment_key(
        self,
        identifier: str,
        *,
        is_attachment_key: bool = False,
    ) -> tuple[str | None, str | None]:
        if is_attachment_key:
            return None, identifier

        parent_key = identifier
        if not _looks_like_item_key(identifier):
            item = self.bbt.search_item(identifier)
            if not item:
                return None, None
            parent_key = (item.get("id") or "").split("/")[-1]

        if not parent_key:
            return None, None
        return parent_key, self.annotations.get_pdf_attachment_key(parent_key)

    def get_item(self, item_key: str) -> dict:
        """Everything about one item in a single read: state, fields, creators, tags.

        Several queries rather than one because they fan out differently (fields and tags
        are one-to-many, creators are ordered), and a caller almost always wants all
        four -- an agent deciding what to do next needs the type, and reading creators
        used to require deliberately triggering a `refusing_to_replace` refusal and
        parsing `detail.current_creators` out of it.

        `read_mode` travels with the answer. With Zotero running, `mode=ro` loses to the
        rollback journal and `immutable=1` serves a point-in-time snapshot instead; a
        caller that cannot tell those apart cannot know whether it read the present.
        """
        states = self.items.item_states([item_key])
        state = states.get(item_key)
        # `item_states` returns an entry for EVERY requested key and marks absence with
        # `exists=False` -- it does not omit them -- so `.get() is None` never fires.
        if state is None or not state.exists:
            return {
                "ok": False,
                "error": "not_found",
                "item_key": item_key,
                "read_mode": states.read_mode,
            }
        return {
            "ok": True,
            "item_key": item_key,
            "item_type": state.item_type,
            "title": state.title,
            "trashed": state.trashed,
            "parent_key": state.parent_key,
            "child_keys": list(state.child_keys),
            "collection_count": state.collection_count,
            "fields": self.items.item_fields(item_key),
            "creators": [dict(creator) for creator in self.items.item_creators(item_key)],
            "tags": list(self.items.item_tags([item_key]).get(item_key, ())),
            "read_mode": states.read_mode,
        }

    def check_duplicate(
        self,
        *,
        title: str | None = None,
        doi: str | None = None,
        isbn: str | None = None,
        calibre_uuid: str | None = None,
        creators: tuple = (),
    ) -> dict:
        """Is this already in the library? Answerable WITHOUT attempting a write.

        The logic is unchanged -- it is the same gate `write.verbs.create_item` runs.
        What is new is that it can be asked. Until now the only way to reach a verdict
        was to attempt a create and read it out of the refusal, so "check before you
        add" cost a write attempt.
        """
        return self.duplicates.check(
            title=title,
            doi=doi,
            isbn=isbn,
            calibre_uuid=calibre_uuid,
            creators=creators,
        )

    def list_pdfs(self, *, limit: int | None = None) -> dict:
        """Enumerate stored PDF attachments with parent title and collection."""
        attachments = self.items.pdf_attachments(limit=limit)
        return rows(
            "attachments",
            [
                {
                    'attachment_key': att.attachment_key,
                    'path': str(att.path),
                    'title': att.title,
                    'collection': att.collection,
                }
                for att in attachments
            ],
            read_mode=attachments.read_mode,
        )

    def trash_count(self) -> dict:
        """How many items are in the trash, in the same envelope as every other read.

        ⚠ Returned `{"trashed": N}` — no `count`, no `read_mode` — while five of the seven
        sibling reads returned both. A caller could not tell a live count from a snapshot
        one, and could not read this envelope the way it reads the others.
        """
        count, read_mode = self.items.trashed_count()
        return {"count": count, "read_mode": read_mode}

    def tags(self) -> dict:
        """Every tag in the library with its item count, most-used first.

        There was no way to ask this at all: `item_tags` answers per-item, so enumerating
        the vocabulary meant walking every item or writing SQL outside the package. Tag
        hygiene is unanswerable without it — the 35 case-colliding groups this found
        (`art`/`Art`, `philosophy`/`Philosophy`, a three-way `decontamination`) were
        invisible to every existing read.
        """
        tags, read_mode = self.items.all_tags()
        return rows(
            "tags",
            [{"name": t.name, "item_count": t.item_count} for t in tags],
            read_mode=read_mode,
        )

    def items_with_tag(self, name: str) -> dict:
        """Item keys carrying EXACTLY this tag, case-sensitively.

        Case-sensitive is the point: `art` and `Art` are separate rows, and a merge has to
        address them separately. A case-insensitive match would make them indistinguishable
        and the merge impossible to target.
        """
        keys, read_mode = self.items.items_with_tag(name)
        return rows("item_keys", list(keys), read_mode=read_mode, tag=name)

    def trash_items(self) -> dict:
        """WHAT is in the trash, each with the date it was deleted.

        The companion to `trash_count`, and the reason it exists: a count alone cannot answer
        "which of these did I trash in 2024", and trashed rows are reachable through no other
        read in this package -- they are excluded from `search.items` and belong to no
        collection.
        """
        items, read_mode = self.items.trashed_items()
        return rows(
            "items",
            [
                {
                    'key': item.key,
                    'title': item.title,
                    'item_type': item.item_type,
                    'date_deleted': item.date_deleted,
                }
                for item in items
            ],
            read_mode=read_mode,
        )

    def collection_tree(self, *, library_id: int | None = None) -> dict:
        """The whole collection tree, nested, with breadcrumb paths and item counts."""
        tree = self._collections_for(library_id).tree()
        # ⚠ NOT `rows()`, and this is the exception that proves its rule. `rows` derives
        # `count` from the payload it is handed, because a `count` that does not describe
        # the rows beside it is how the two drift. Here the payload is a nested TREE: the
        # list is the 16 top-level roots while the count is all 84 collections
        # (`tree.flat()`). Converting this site silently changed 84 to 16 — caught only
        # because every envelope was captured before the refactor and compared after.
        #
        # `truncated` is tree-specific too. A tree is not a row set, so it keeps its own
        # frame rather than being bent into one.
        return {
            "count": len(tree.flat()),
            "read_mode": tree.read_mode,
            "truncated": tree.truncated,
            "collections": [_node_to_dict(node) for node in tree.roots],
        }

    def collection_items(
        self, collection_key: str, *, include_trashed: bool = False,
        library_id: int | None = None,
    ) -> dict:
        """Direct members of one collection -- the read nothing else could answer."""
        members = self._collections_for(library_id).items(
            collection_key, include_trashed=include_trashed
        )
        return rows(
            "items",
            [
                {
                    'item_key': m.item_key,
                    'title': m.title,
                    'item_type': m.item_type,
                    'trashed': m.trashed,
                }
                for m in members.members
            ],
            read_mode=members.read_mode,
            collection_key=collection_key,
        )

    def item_collections(self, item_keys: list[str], *, library_id: int | None = None) -> dict:
        """Which collections each item is filed in. The inverse; new capability."""
        return {"items": self._collections_for(library_id).collections_of(item_keys)}

    def find_collections(self, name: str, *, library_id: int | None = None) -> dict:
        found, read_mode = self._collections_for(library_id).find(name)
        return rows(
            "collections",
            [
                {
                    'key': n.key,
                    'name': n.name,
                    'path': n.path,
                    'item_count': n.item_count,
                }
                for n in found
            ],
            read_mode=read_mode,
        )

    def search_items(
        self,
        query: str,
        *,
        fuzzy: bool = True,
        limit: int = 25,
        item_type: str | None = None,
        library_id: int | None = None,
    ) -> dict:
        hits, read_mode = self._search_for(library_id).items(
            query, fuzzy=fuzzy, limit=limit, item_type=item_type
        )
        return rows(
            "hits",
            [
                {
                    'item_key': h.item_key,
                    'title': h.title,
                    'item_type': h.item_type,
                    'creators': h.creators,
                    'score': h.score,
                    'matched_on': h.matched_on,
                }
                for h in hits
            ],
            read_mode=read_mode,
            query=query,
            fuzzy=fuzzy,
        )

    def search_annotations(
        self,
        query: str = "",
        *,
        color: str | None = None,
        annotation_types: set[str] | None = None,
        limit: int = 25,
    ) -> dict:
        hits, read_mode = self.search.annotations(
            query, color=color, types=annotation_types, limit=limit
        )
        return rows(
            "annotations",
            [
                {
                    'annotation_key': h.annotation_key,
                    'attachment_key': h.attachment_key,
                    'parent_key': h.parent_key,
                    'parent_title': h.parent_title,
                    'type': h.annotation_type,
                    'text': h.text,
                    'comment': h.comment,
                    'color': h.color,
                    'page_label': h.page_label,
                }
                for h in hits
            ],
            read_mode=read_mode,
            query=query,
        )

    def search_fulltext(self, query: str, *, limit: int = 25) -> dict:
        hits, read_mode = self.search.fulltext(query, limit=limit)
        return rows(
            "documents",
            [
                {
                    'attachment_key': h.attachment_key,
                    'parent_key': h.parent_key,
                    'title': h.title,
                    'match_count': h.match_count,
                    'snippets': list(h.snippets),
                }
                for h in hits
            ],
            read_mode=read_mode,
            query=query,
        )

    def attachment_text(self, attachment_key: str, *, max_chars: int = 20000) -> dict:
        text = self.search.attachment_text(attachment_key)
        if text is None:
            return {"ok": False, "error": "not_indexed", "attachment_key": attachment_key}
        return {
            "ok": True,
            "attachment_key": attachment_key,
            "chars": len(text),
            "truncated": len(text) > max_chars,
            "text": text[:max_chars],
        }

    def get_sources_with_annotations(self, *, include_citekeys: bool = True, include_all: bool = False) -> dict:
        """Sources carrying annotations, in the standard envelope.

        ⚠ Returned a BARE LIST, which cannot carry a `read_mode` at all — so this was the
        one read in the package that could not say whether it had been served live or from
        an `immutable=1` snapshot. The annotation store already recorded it in
        `last_read_mode`; nothing outside the tests ever read it.
        """
        sources = self.annotations.get_sources_with_annotations(include_all=include_all)
        if include_citekeys:
            sources = self._with_citekeys(sources)
        return rows(
            "sources",
            list(sources),
            read_mode=self.annotations.last_read_mode,
        )

    def _with_citekeys(self, sources: list[ZoteroSource]) -> list[ZoteroSource]:
        """Better BibTeX keys, joined on. Split out so the envelope above reads as one
        thing rather than as two returns with a branch between them."""
        citekeys = self.bbt.citation_keys([source.parent_key for source in sources])
        return [
            ZoteroSource(
                parent_key=source.parent_key,
                attachment_key=source.attachment_key,
                title=source.title,
                authors=source.authors,
                annotation_count=source.annotation_count,
                citekey=citekeys.get(source.parent_key, ""),
            )
            for source in sources
        ]

    def get_open_reader_context(
        self,
        *,
        active_only: bool = False,
        include_annotations: bool = True,
        include_citekeys: bool = True,
        annotation_types: set[str] | None = None,
    ) -> list[ReaderContext]:
        readers = self.get_open_readers()
        if active_only:
            readers = [reader for reader in readers if reader.is_active_tab]

        citekeys = {}
        if include_citekeys:
            parent_keys = [reader.parent_key for reader in readers if reader.parent_key]
            citekeys = self.bbt.citation_keys(parent_keys)

        contexts: list[ReaderContext] = []
        for reader in readers:
            anns = None
            if include_annotations and reader.attachment_key:
                anns = self.get_annotations(reader.attachment_key, types=annotation_types)
            contexts.append(
                ReaderContext(
                    reader=reader,
                    citekey=citekeys.get(reader.parent_key or "", ""),
                    annotations=anns,
                )
            )
        return contexts


def _looks_like_item_key(value: str) -> bool:
    """Is this identifier a key, or something to look up in Better BibTeX?

    ⚠ CHANGED 2026-08-19. This was `len(v) == 8 and v.isalnum() and v.upper() == v`, and
    `str.isalnum()` is UNICODE-AWARE -- so it accepted full-width Latin, Greek capitals and
    Roman-numeral characters that the write layer's regex rejects. The two layers disagreed
    about what a key is: a string could pass here, be treated as a key rather than sent to
    BBT, and then be refused downstream as malformed.

    It now asks the one shared rule. The consequence is deliberate: input that cannot be a
    valid key is no longer mistaken for one, and goes to BBT instead -- which is the right
    place for anything that is not a key.
    """
    return is_key(value)


def _node_to_dict(node) -> dict:
    return {
        "key": node.key,
        "name": node.name,
        "path": node.path,
        "depth": node.depth,
        "parent_key": node.parent_key,
        "item_count": node.item_count,
        "subcollections": [_node_to_dict(child) for child in node.subcollections],
    }
