# Package structure: one package, the layers, the ports, the vendoring premise

Records moved out of docstrings and comments under `src/` (2026-11-04 history wave). The code
keeps the rules; this file keeps how they were arrived at.

## 2026-08-19: zotero-context and zotero-writes merge into one package

From `zotero_core/__init__.py`.

The split was defensible and it was enforced: `writes -> core, never the reverse` had its own
import-linter contract. What it could not survive was that the read half was never finished.
`core` shipped `items.py` and `duplicates.py` and exposed NEITHER from its CLI or MCP surface,
and never had collection or search queries at all. So an agent asking "what is in collection X"
had to go to a third-party plugin, and the charter sentence -- "a second answer to what is in
the library is the failure this package exists to end" -- described a gap rather than a
guarantee.

Two packages also meant two of everything that was really one thing: two pyprojects agreeing on
`mcp<2` and `ty==0.0.55` in duplicated comments, two lockfiles, and one test suite that lived in
`writes/` and reached `core` incidentally -- `core` had no `tests/` directory and no
`.importlinter` of its own.

The direction the split protected became a LAYER, not a package boundary, and is still checked
(`.importlinter`, contract `layers`). `domain` was a rule that could not previously be expressed
at all.

The root docstring at the time drew the layers as `domain/ read/ write/ interfaces/` and said
the module exported "the READ surface only", with writes an explicit opt-in through
`zotero_core.application`. Both had gone stale: the layers are
`domain/ application/ infrastructure/ interfaces/`, and `__all__` publishes write verbs too
(see `public-api.md`).

## 2026-08-19: the twelve Zotero nouns leave the SQL layer

From `domain/entities/models.py` and `domain/entities/__init__.py`.

Twelve entity types were defined inside the SQL layer. `ItemState`, `Library`, `ItemHit` and the
rest sat in `read/items.py`, `read/libraries.py`, `read/search.py` and `read/collections.py`, so
the package's actual Zotero nouns were declared by the module that queried for them, while
`domain/entities.py` held only the six bridge DTOs that model an HTTP response. That is what
made `domain/` a shared-utilities bucket rather than a domain.

`domain/entities.py` was then one module mixing both. Six of the nine types existed solely to
parse a bridge response -- `WindowState` keeps the untransformed payload in `raw: dict`, and
every `from_bridge` accepts both camelCase and snake_case, which is adapter behaviour wearing a
domain label. The two SQL-derived ones were sitting next to window-state parsers. The split into
`models.py` (sqlite) and `gui.py` (bridge) is by provenance.

Moving them cost consumers nothing: tests and `omni-rag` couple to these types structurally,
and the four public ones still resolve from `zotero_core`.

## 2026-08-19: layer packages stop re-exporting

From `application/__init__.py`.

`application/__init__.py` (then `write/__init__.py`) re-exported thirty-odd names. It imported
`infrastructure.journal`, `infrastructure.transports.cookjohn` and
`infrastructure.transports.linker` purely to republish them, which put a concrete adapter in
the application layer's import graph however clean the modules below it were -- importing ANY
`zotero_core.application.*` module executes that file first. The layer could not sit below
`infrastructure` while it existed.

Two of the names it published had also gone stale: `zotero_is_running` had moved to
`infrastructure/probe.py` behind a port, and `write_manifest` / `copy_database` /
`DEFAULT_JOURNAL_DIR` are infrastructure. A re-export list is a second declaration of the
surface, and second declarations drift.

## 2026-08-19 to 2026-08-20: ports, and the deferred imports they replaced

From `domain/ports/collection_catalogue.py`, `domain/ports/search_catalogue.py`,
`domain/ports/catalogue.py`, `infrastructure/sqlite/{collections,search}.py`,
`application/services/{context,liveness,replay}.py`, `interfaces/{read_mcp,write_mcp,factory}.py`.

- The four collection call sites reached `ZoteroCollectionStore` through a DEFERRED import
  inside the function body -- `from zotero_core.infrastructure... import ZoteroCollectionStore`
  at lines 243, 268, 319 and 527 of the collection verbs. import-linter sees function-level
  imports, which is how they were found.
- `items` was MISSING from the first draft of `SearchCatalogue`, and `ty` found it: the surface
  had been derived by grepping `_collections_for(...).X` and `_search_for(...).X` together,
  which merged two different `.items` (a collection's membership listing, and the fuzzy title
  search). A port derived from a sloppy reading type-checks against nothing.
- The read facade served per-library calls by CONSTRUCTING a second store from a stashed db
  path -- `ZoteroSearchStore(self._db_path, library_id=...)` -- an application-layer facade
  building an adapter. `for_library` on each store replaced that.
- `Catalogue.db_path` was first declared `str | Path`. A Protocol attribute is invariant, so
  `ZoteroItemStore` (offering only `Path`) did not satisfy it; `ty` caught it.
- 2026-08-20: `Catalogue` was WIDENED with `pdf_attachments` and `trashed_count`, read by the
  read facade rather than a write gate, because `omni-rag` already consumed `pdf_attachments`
  through the same store. Eight methods against a class with more.
- `ZoteroContext` constructed its own collaborators from a db path and two URLs --
  `self.items = ZoteroItemStore(zotero_db_path)`, six times over -- so a caller could not point
  it anywhere without an environment variable and a test could not substitute anything. It also
  lived in `infrastructure/`, below the layer it belongs to. For as long as it held no item
  store at all, `items.py` and `duplicates.py` were unreachable from every agent-facing surface
  and a third-party plugin filled the gap.
- `read_mcp` kept its context in a module-global singleton `_CTX`, built lazily and cached
  forever; five test sites did `monkeypatch.setattr(read_mcp, "_CTX", ...)`. The write adapter
  had no `session` parameter either, and its test fixture's comment read "it is the only way
  in, because the MCP surface has no injection parameter by design".
- `liveness.py` held `zotero_is_running` and `ZOTERO_SERVER_URL`, calling `urlopen` directly
  (network I/O in the application layer); three test sites patched it. Both moved to
  `infrastructure/probe.py` behind `ZoteroProbe`.
- `replay.undo` called `verb(*args, **{**kwargs, **injected})`, with a comment saying the
  injected transports were for tests and "a real caller passes nothing and the verb builds its
  own session". Verbs stopped building sessions; the replay now runs on the caller's.
- `factory.build_context`, found by an audit rather than a test:
  1. It took `**overrides` and `.pop`ed names out, so a MISSPELLED port was silently ignored:
     `build_context(bbt_client=Fake())` returned a real `BetterBibTeXClient`. Every argument
     was also laundered through `Unknown`, so passing a non-port produced zero `ty`
     diagnostics there while constructing `ZoteroContext` directly produced eight.
  2. It used `x or Concrete()`, which discards a FALSY override. Measured: a stub whose
     `__bool__` returns False was thrown away and the real store used. `build_write_session`
     already used `is not None`.

## 2026-08-19: the vendoring premise, retired

From `domain/ports/write_transport.py`, `infrastructure/transports/cookjohn.py`,
`interfaces/__init__.py`, `interfaces/write_mcp.py`, `domain/services/policy.py`.

Several docstrings justified a rule by saying `transports/cookjohn.py` (and parts of the
normaliser) were "vendored verbatim into `calibre-zotero-jump`" and so had to stay stdlib-only
and import-standalone. The refactor plan used the same argument to choose `Protocol` over `ABC`
for the transport ports.

Checked 2026-08-19, it was false twice over: `calibre-zotero-jump/build.sh` zips three files
(`__init__.py`, `ui.py`, the import-name marker), none of them ours; `ui.py` contains the string
"cookjohn" zero times and reimplements the JSON-RPC client with its own `\b([A-Z0-9]{8})\b`, and
contains no fuzzy or normaliser code. And `cookjohn.py` imports `zotero_core.domain.errors` and
`zotero_core.domain.services.identity` at module scope, so it was never import-standalone.

The rules survive on their real reasons: `Protocol` keeps infrastructure free of an upward
import; `mcp` stays out of module scope because of `dependencies = []`. The stdlib-only
discipline that holds is about third-party packages, not intra-package imports.

`cookjohn.py`'s module docstring also described itself as "the third copy of this client",
existing so `importers/calibre2zotero/sync.py` (`class Mcp`) and `calibre-zotero-jump/ui.py`
(`class _Mcp`) could be deleted. `calibre2zotero` has since moved onto `zotero_core`;
`calibre-zotero-jump` cannot (Calibre's embedded Python) and keeps `_Mcp`.

## Transport declarations

From `interfaces/tool_spec.py`.

The transport was hardcoded three times per verb, independently and unlinked: what
`require_zotero(needs=...)` demanded, what `session.<name>.call/post` invoked, and the literal
`"transport": "..."` in the result. Nothing asserted the three agreed. `WriteToolSpec.transport`
declares it once.

## 2026-08-19 to 2026-08-20: the layer rename and inversion

From `.importlinter`. The file first named `interfaces > write > read > domain` (before that, a
`suite-direction` contract over two root packages), which described the merge rather than an
architecture: `read` was a layer named after a DIRECTION and held the sqlite driver, the HTTP
clients and a facade at one level. The mapping onto omni-rag's names:

    read/{connect,items,collections,search,annotations,duplicates,libraries}
                                         -> infrastructure/sqlite/     (the driver)
    read/{bridge,bbt}                    -> infrastructure/http/       (the network)
    write/transports/{cookjohn,linker}   -> infrastructure/transports/ (the network)
    write/journal.py                     -> infrastructure/journal.py  (the filesystem)
    write/errors.py                      -> domain/errors.py           (pure vocabulary)
    write/{verbs,collections,liveness,replay} -> application/services/

For a while the header comment said `write` "is NOT yet `application`" and the order "still
puts it ABOVE infrastructure", contradicting the contract 21 lines below it. The inversion became
true when the verbs stopped naming adapters (`application/` depends on `domain/ports/` only;
`interfaces/factory.py` injects). `transports`, `journal` and `errors` left the write spine
contract then. The `mcp-only-in-the-adapter` contract was also once justified by vendoring (see
above).

## The `mcp<2` pin

From `pyproject.toml`. An unpinned `mcp>=1.0.0` once resolved 2.0.0 and crashed the server at
import with `'Server' object has no attribute 'list_tools'`, which is how the read adapter sat
broken long enough never to be registered and never noticed. Before the merge this pin and its
comment existed twice, in two pyprojects.
