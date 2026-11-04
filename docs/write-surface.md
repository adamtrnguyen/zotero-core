# zotero-core

The one CRUD surface for the local Zotero library. Everything that mutates Zotero
goes through here, and **the caller never picks a transport.**

```python
from zotero_core import WriteBlocked, build_write_session
from zotero_core.application.services.verbs import (
    add_tags,
    create_item,
    restore_items,
    trash_items,
)

# ONE session per operation, assembled by the composition root. Every verb requires it, so
# no concrete adapter is constructed inside the application layer.
session = build_write_session()

try:
    item = create_item("book", {"title": "Systems Thinking"}, calibre_uuid=uuid, session=session)
    add_tags(item["item_key"], ["systems", "to-read"], session=session)
except WriteBlocked as e:
    print(e.code, e.reason)  # e.g. "duplicate_item", plus prose
    print(e.as_dict())  # the shape an MCP tool returns

trash_items([item["item_key"]], session=session)  # recoverable
restore_items([item["item_key"]], session=session)  # the inverse, always available
```

## Why it exists

Zotero's write capability was scattered across two plugins on two ports, and a
caller had to know which served which verb:

| | transport | port |
|---|---|---|
| create / update / notes / tags / collections | cookjohn `zotero-mcp-plugin` | 23121 (MCP JSON-RPC) |
| linked attachments / trash / restore | `zotero-linker` | 23119 (plain HTTP) |
| reads | `zotero_core.infrastructure` → `zotero.sqlite` | — |

Two consumers learned that split by copying a client. `importers/calibre2zotero/
sync.py:139` and `calibre-zotero-jump/ui.py:24` carry near-identical MCP clients,
and they drifted where it mattered most: **they disagree about how to tell whether
a book is already in Zotero.** One scans Zotero's `extra` for `calibre-uuid:`, the
other trusts a `zotero` identifier held on the Calibre side — answers that diverge
exactly when a push half-succeeded. Meanwhile `linker/` v0.3.0 had been running
with `trash-items` and `restore-items` registered and **zero** consumers.

So this is not a delete client. A module owning only Delete would have made it four
scattered places instead of three.

## Where it lives

One package owns reads and writes. The layers are `interfaces > infrastructure > application >
domain` (`.importlinter`): the verbs live in `application/services/`, the transports and the
journal in `infrastructure/`, the error vocabulary in `domain/errors.py`. Every write
precondition is a read, so the direction is acyclic, and `zotero.sqlite` is never opened
read-write. How this came to be one package: `docs/history/package-structure.md`.

## The surface

| | verbs | transport |
|---|---|---|
| **create** | `create_item`, `link_attachment`, `import_attachment`, `write_note`, `create_collection` | cookjohn + linker |
| **read** | *not here* — `zotero_core.infrastructure` owns reads and is already correct | core |
| **update** | `update_metadata`, `add_tags`, `remove_tags`, `set_tags`, `replace_creators`, `write_note(action=…)`, `update_collection`, `add_items_to_collection`, `remove_items_from_collection` | cookjohn |
| **delete** | `trash_items` / `restore_items`, `delete_collection` | linker + cookjohn |

Every result carries a `transport` field, so the choice is *invisible in the
signature but visible in the output* — hidden for callers, available for debugging.
Tests assert both: no verb requires a transport argument, and none leaks a port or
plugin name into its parameter names.

## The MCP adapter

```bash
uv sync --extra mcp
zotero-core-mcp                # stdio server, registered as `zotero`; `len(write_mcp.TOOLS)` for the write count
```

**Saving a paper from a URL** is `zotero_save_paper` (preview: `zotero_resolve_paper`). It
adds no write path: `application/services/papers.py` resolves the URL through the
`PaperResolver` port (arXiv API, DOI content negotiation, `citation_*` meta tags), then calls
`create_item` -- so the duplicate gate applies, and a repeat arXiv save is a DOI BLOCK --
and `import_attachment` for the PDF. A PDF that cannot be fetched is reported in the result,
not raised, because the item already exists. A bot-wall page or one without scholarly
metadata is refused (`paper_unresolved`). This replaced ZotLink, whose generic scraper saved
bot walls ("Client Challenge", "Verifying your browser | OpenReview") as papers.

Every verb above, one tool each (`zotero_trash_items`, `zotero_set_tags`, …), plus
`zotero_write_preflight` — which probes both plugins and optionally resolves keys
without writing. The `zotero_` prefix is not decoration: cookjohn's own MCP registers
`write_item`, `write_note`, `write_metadata` and `create_collection` under those bare
names, and an agent's tool list has to be unambiguous about which surface is gated.

The adapter implements no verb of its own. It is the same gates, and it exists because
cookjohn's MCP **has no delete verb at all** — trash and restore live on `linker/`,
which speaks plain HTTP — so removing an item meant driving `uv run python` one-liners.

Three things it deliberately does not expose:

- **reads** — `zotero_core.infrastructure` owns them and the `zotero-core` server already
  serves them. A second answer to "what is in the library" is the failure this
  package exists to end.
- **`store` / `linker` / `cookjohn`** — injection seams. As tool parameters they would
  let a caller aim the write path at an arbitrary URL.
- **`journal_dir`** — the journal is only an audit trail if every write lands in one
  place.

`force` *is* exposed wherever a verb has it, defaulting to False in every schema that has
it; a
test asserts no schema ships it defaulted to True, and another asserts every verb that
has the gate exposes it. A `WriteBlocked` comes back as `as_dict()` — `{"ok": false,
"code", "reason", "detail"}` — so a caller branches on `code` instead of parsing a
traceback.

`mcp` is behind the extra and imported inside `main()`, never at module level. That is
the same pin core uses (`mcp>=1.26.0,<2` — 2.0.0 removed `@server.list_tools()`), and
`.importlinter` forbids every other module from importing it, because `dependencies = []`
is the promise this package makes — `omni-rag` imports it inside ARC entrypoints, where an
async runtime pulled in by a tag verb is a real cost.

## What is enforced

| Gate | Refuses when | `code` |
|---|---|---|
| liveness | Zotero is not running | `zotero_not_running` |
| liveness | Zotero up, linker not loaded | `linker_not_installed` |
| liveness | Zotero up, cookjohn not loaded | `cookjohn_not_installed` |
| liveness | something else answers on the path | `not_the_linker` |
| shape | empty batch / not an 8-char uppercase key | `no_item_keys`, `malformed_item_key` |
| shape | no title on create; bad note action | `missing_required_field` |
| existence | **any** item key does not resolve | `unknown_item_keys` |
| existence | collection key does not resolve | `unknown_collection_key` |
| type | metadata write on a note/attachment/annotation | `wrong_item_type` |
| duplicate | same DOI, ISBN or `calibre-uuid` on create | `duplicate_item` |
| duplicate | sibling collection with the same name | `duplicate_item` |
| replacement | `set_tags` / `replace_creators` without `force` | `refusing_to_replace` |
| cascade | `delete_collection(delete_items=True)` without `force` | `refusing_cascade_delete` |
| no-op | trashing what is trashed, adding a tag already there, … | `already_trashed`, `not_trashed`, `nothing_to_do` |
| files | relative or missing attachment path | `file_not_found` |
| mutator | plugin errored / returned no key / partially applied | `linker_refused`, `cookjohn_refused`, `cookjohn_returned_no_key`, `partial_apply` |
| verification | the state did not change | `verification_failed` |

`force=True` relaxes only the gate it is passed to. It never relaxes liveness or
existence.

Three preconditions are worth calling out:

- **Zotero must be RUNNING.** This inverts calibre-core's first gate, which requires
  the Calibre GUI to be *closed*. Every Zotero write channel is code executing
  inside the application, so a closed Zotero is not a safe state — it is no channel
  at all. The probe distinguishes "app down" from "that one plugin missing", because
  those are different jobs to fix.
- **Every key is resolved before anything is sent.** `bootstrap.js` 404s only when
  *no* key resolves (`if (ids.length === 0)`). Hand it five keys with two typos and
  it trashes three and returns HTTP 200 `{"ok": true, "trashed": 3, "missing":
  [...]}` — a success-shaped partial write, the same defect class as `calibredb add`
  exiting 0 having added nothing.
- **Two of cookjohn's tools REPLACE rather than merge.** `write_tag` action=`set`
  wipes every tag; `write_metadata`'s `creators` array wipes every creator.
  calibre-core carries the scar from this exact shape: a plain identifier write
  replaced the whole set and silently deleted book 256's `zotero` identifier — the
  link to its Zotero item — with nothing in the output announcing it. So `add_tags`
  and `remove_tags` are the normal path, and `set_tags` / `replace_creators` are
  *separately named* and refuse without `force=True`. Dropping a value should be a
  decision, not a side effect of setting a different one.

## Undo

Operations that overwrite or remove something write a JSON **manifest** to
`/tmp/zotero-write-journal` first: the prior state plus a literal call that reverses
it. Creates and pure additions do not, because the inverse of an addition is a
removal the caller can already express.

This is deliberately not a database copy, which is what calibre-core does.
Zotero already keeps rotating daily copies of `zotero.sqlite` beside it, so a per-call
copy duplicates what exists — and
restoring one requires closing Zotero and reverts every unrelated change since.
`copy_db=True` still takes the snapshot (database *and* rollback journal).

## Not implemented, deliberately

No hard erase, no empty-trash. `docs/DESIGN.md` excludes them and no plugin exposes
an endpoint for either. A test asserts the public surface contains no such verb, and
that trashing an item leaves its row intact. `delete_collection` deletes a *folder*,
not its contents — items stay in the library unless you explicitly opt into the
cascade.

Live runs against the real library, and the bugs they found: `docs/history/write-verification.md`.

## Tests

```bash
just qa              # from the suite root: lint fmt-check types layers dead spell test
uv run pytest        # pytest prints the count; it is not written down here
```

Both plugins and the database are stubbed, and the fakes *apply* their writes to the
fixture so post-write verification is exercised for real rather than mocked into
agreeing. An autouse fixture redirects every default and makes `urlopen` raise, so a
test that forgets to inject cannot reach the real library.

The liveness stub is autouse for a specific reason: calibre-core's GUI gate read real
process state, and 6 of its tests failed the moment Calibre was opened. A gate that
consults a running application makes that application a test fixture — and here the
sense is inverted, so without the stub the suite would pass only while Zotero happens
to be up.
