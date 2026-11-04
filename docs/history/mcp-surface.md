# MCP surface: one table per adapter, one server, one renderer

Records moved out of `interfaces/*` (2026-11-04 history wave).

## 2026-08-19: the read adapter gets a table

From `interfaces/read_mcp.py` and `interfaces/tool_spec.py`.

The read adapter was a 3-place change per tool -- a `types.Tool(...)` literal inside
`list_tools()`, an `if name == ...` branch in `call_core()`, and hand-written `arguments.get()`
coercion -- with nothing checking that the lists agreed about names, and no rejection of
undeclared arguments: a misspelled `include_annotaions` was silently dropped and the caller got
the default. On the write side a caller passing `journal_dir` or `store` got a silent no-op on
the argument it cared about, and one misspelling `force` got the refusal it meant to bypass.
The write adapter had solved this first ("two copies of one fact is the defect this package was
built to end"); the read adapter had 6 tools, never got the refactor, and grew none.

New the same day: tools that were implemented and unreachable. `read/items.py` and
`read/duplicates.py` had SEVEN public methods between them and not one was exposed from CLI or
MCP, so an agent could not read an item's type, fields, creators or tags, and could not ask "is
this already in the library" without attempting a create and parsing the refusal. Collection
reads followed (`read/collections.py`), including which collections an item is in, which nothing
answered before; then search (`read/search.py`) with fuzzy metadata matching -- Zotero's own
search is substring-only and returns zero for a query with one typo.

Before `_CODES`, a locked database, a missing key and a closed Zotero were indistinguishable:
every failure flattened to `{"ok": false, "error": str(exc)}`.

## 2026-08-20: one renderer, one reduction, frames through `ok()`

- `cli.print_json` called `json.dumps` itself, WITHOUT `default=str`, so a value the domain does
  not model (a `Path`, a `datetime` in `WriteBlocked.detail`) raised in the CLI while the write
  adapter, which had the fallback, survived the identical payload. Of three copies only
  `write_mcp._render` carried `default=str`; all three now use `rendering.render_json`.
- The `[0] if contexts else None` active-reader reduction was written out in both `read_mcp`
  and `cli`; it moved to `ZoteroContext.get_active_reader_context`.
- `list_undo` spelled its frame out by hand and OMITTED `transport`. Preflight's frame was also
  missing `transport`, found by an audit; it is deliberately not built by `ok()` because its
  `ok` is a health verdict.

## 2026-09-25: one server

From `interfaces/mcp_server.py`. The package merged on 2026-08-19 but kept two entry points
(`zotero-core-read-mcp`, `zotero-core-write-mcp`), and ZotLink stayed a third, so an agent saw
three Zotero servers -- `zotero-context`, `zotero-writes`, `zotlink` -- for one library.
`pyproject.toml` already named the end state: a single server with a `--read-only` switch,
"once the read surface is complete". `--read-only` does the one job the separate read server
did.

## The `zotero_list_undo` description

The description said "until now NOTHING read those manifests, so `undo_call` was advisory text
you retyped" -- true when `replay.py` landed, history since.
