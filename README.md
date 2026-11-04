# zotero-core

**The one place that knows what a local Zotero library is.** Reads and gated writes,
in one versioned package.

Zotero 9, local only — no Web API, no Zotero cloud. Attachments sync to the NAS via
WebDAV; everything here talks to the desktop app on this machine.

```
src/zotero_core/
  domain/          entities, values, ports, policy    pure — no sqlite, no network, no mcp
  application/     read facade, gated verbs, undo     depends on domain ports only
  infrastructure/  sqlite/ (read-only), http/ (bridge, BBT, papers),
                   transports/ (cookjohn, linker), journal
  interfaces/      cli, MCP server, factory           the only layer that may import mcp
```

`.importlinter` enforces `interfaces > infrastructure > application > domain`, plus narrower
rules; `uv run --directory core --all-extras lint-imports` lists them. Why the read and write
packages became one: [`docs/history/package-structure.md`](docs/history/package-structure.md).

## Install

```bash
uv sync --all-extras          # `mcp` extra: needed only by the two adapters
```

Zero runtime dependencies. The sqlite adapters are stdlib `sqlite3`; both write transports
are stdlib `urllib` + `json`. That is load-bearing, not incidental: `omni-rag` imports the
read path inside its ARC entrypoints, where an async runtime pulled in by a tag verb is a
real cost.

`calibre-zotero-jump` cannot import this package (Calibre's embedded Python cannot see a uv
virtualenv) and keeps its own client; nothing here constrains that copy.

## Read

```python
from zotero_core import build_context, ZoteroItemStore, check_duplicate

# `ZoteroContext` takes eight ports; `build_context` is the published factory that supplies
# the real adapters (docs/history/public-api.md).
zc = build_context()
state = zc.get_window_state()
active = zc.get_active_reader()
annotations = zc.get_annotations(active.attachment_key) if active else []
_, pdf_key = zc.resolve_pdf_attachment_key("U7Y49M4I")

store = ZoteroItemStore()
store.item_states(["ARTINWQZ"])  # exists / trashed / type / parent / children
store.item_fields("ARTINWQZ")  # {fieldName: value}
store.item_creators("ARTINWQZ")  # in orderIndex order
```

Every sqlite read reports **which mode served it**. With Zotero running, `mode=ro`
intermittently fails on Zotero's lock and `immutable=1` answers from a
point-in-time snapshot instead — a caller that cannot tell those apart cannot know
whether it read the present or the recent past.

```bash
zotero-core window-state --pretty          # what Zotero is showing right now
zotero-core item NUQP6L46 --pretty         # type, fields, creators, tags, trash state
zotero-core duplicate --title "..." --author Welling
zotero-core pdfs --limit 5 --pretty
zotero-core trash-count
zotero-core annotations F6G6KC7G --pretty
zotero-core sources --pretty
```

The CLI and the MCP tools are held at **parity by a test** — every catalogue read is
reachable both ways.

## Write

Explicit import: `import zotero_core` stays cheap and read-only.

```python
from zotero_core import build_write_session, WriteBlocked
from zotero_core.application.services.verbs import create_item, add_tags, trash_items

session = build_write_session()  # every collaborator, wired once
create_item("book", {"title": "A Book"}, session=session)
add_tags("ARTINWQZ", ["read"], session=session)
```

Every verb takes a required `session`. Three verbs are also published from the package root
(`import_attachment`, `update_metadata`, `write_note`), which is what a consumer should prefer.

Zotero must be **running** with the plugin that verb needs. Keys are shape-checked then
resolved before anything is sent. Creates are duplicate-checked. Verbs that REPLACE a
list rather than adding to it refuse unless asked twice. Whatever is about to be
overwritten is journalled with the call that reverses it. Results are re-read.

**No hard erase, ever** — the strongest thing here is a move to the trash, and
`restore_items` ships alongside. A test greps `dir()` for `purge|destroy|erase` to keep it
that way.

**Undo is real, not advisory.** Verbs that overwrite or remove something journal the call
that reverses them, and `zotero_list_undo` / `zotero_undo` read that journal. Undo walks
back: an undone entry is not replayed twice, and an undo's own entry is skipped unless named.
`dry_run` validates the inverse without running the verb's gates; an undo is itself a write.
The journal lives in `/tmp/zotero-write-journal` on purpose and does not survive a reboot
(`docs/history/journal.md`).
The inverse is **parsed, never `eval`'d**: the callee must be a known undo verb and every
argument a literal, because the journal lives in `/tmp` where any process can write.
Entries that genuinely cannot be reversed say why — a note update does not capture the
previous body, and recreating a deleted collection gives it a new key.

Full rationale and transport split:
[`docs/write-surface.md`](docs/write-surface.md).

## MCP

```bash
uv run zotero-core-mcp              # the ONE server: reads, gated writes, paper saving
uv run zotero-core-mcp --read-only  # reads only, for a context that must not write
```

Registered once, as `zotero`. It concatenates the read and write tables and routes each
call to the adapter that owns it (`interfaces/mcp_server.py`), so each keeps its own error
envelope. `zotero_save_paper` and `zotero_resolve_paper` replace ZotLink
(see `docs/write-surface.md`; history in `docs/history/mcp-surface.md`).

Both adapters declare their tools in **one `TOOLS` table** from which the schema and the
dispatch are derived, so adding a tool is one entry rather than a three-place change
across two lists that must agree. **Undeclared arguments are refused, not dropped**, so a
misspelled argument cannot silently hand back the default.

Read tools, generated from the table rather than retyped
(`[t.name for t in read_mcp.TOOLS]`):

`get_zotero_window_state`, `get_zotero_active_reader`, `get_zotero_open_readers`, `get_zotero_annotations`, `resolve_zotero_pdf`, `get_zotero_sources`, `get_zotero_item`, `check_zotero_duplicate`, `list_zotero_pdfs`, `get_zotero_trash_count`, `get_zotero_collections`, `get_zotero_collection_items`, `get_zotero_item_collections`, `find_zotero_collections`, `search_zotero_items`, `search_zotero_annotations`, `search_zotero_fulltext`, `get_zotero_attachment_text`, `list_zotero_libraries`, `ping_zotero`.

Point either adapter somewhere else with `ZOTERO_CORE_DB`, `ZOTERO_CORE_BRIDGE_URL`,
`ZOTERO_CORE_BBT_URL`, the MCP counterpart of the CLI's `--db`.

**Search** (`infrastructure/sqlite/search.py`) is **fuzzy by default**.
Zotero's own search is substring-only: `Langevan Dynmaics` returns **zero** results there
and scores **0.875** here against the paper it means. Scoring is token-wise — a
whole-string ratio gives 0.395 on that pair, because the candidate title is three times
longer than the query. Every hit reports `matched_on`, so a surprising result explains
itself.

Fulltext search runs in **two stages**: Zotero's word index has no positions and no
counts, so it narrows candidates, and only the survivors' `.zotero-ft-cache` files are
opened for the phrase and its context. A query touches a handful of files rather than
the whole cache.

**Collections** (`infrastructure/sqlite/collections.py`): the tree with breadcrumb paths,
membership, and the inverse (which collections an item is in). Two properties worth knowing: item
counts EXCLUDE trashed items so they match the GUI, and everything is scoped to the user
library — this machine has group libraries, and some collections live in them
(`list_libraries()` and `collection_tree(library_id=…)` report the split).

## QA

```bash
just qa      # the gate: lint fmt-check types layers dead spell test, concurrently
just fix     # opt-in fixers: ruff check --fix, then ruff format
```

`just qa` runs every check at once and names every failure; it never rewrites files. The
pre-commit hooks run the ruff fixers on commit and `just qa` on push (`uvx pre-commit install`).
`ruff format` keeps `__all__` grouped by CRUD concern (`# noqa: RUF022`); it does not sort lists.
