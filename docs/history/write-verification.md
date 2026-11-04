# Write verification: the live writes that shaped the read-backs

Records moved out of `application/services/*`, `infrastructure/http/bridge.py` and
`infrastructure/transports/linker.py` (2026-11-04 history wave).

## 2026-08-13: two false alarms from strict equality

From `application/services/verbs.py` (`_verify_fields`).

- Writing `date="2026"` to a real item came back stored as `"2026-00-00 2026"`: Zotero parses
  the date into its own multipart form. An equality check reported that successful write as
  unverified and blamed the snapshot read. Hence `normalized_by_zotero`.
- A `publicationTitle` venue write to a `conferencePaper` landed in `proceedingsTitle`; a
  read-back of the written name found nothing and reported a successful write as `disagreed`.
  Hence `mapped_to_type_field`. `item_type=""` keeps the literal comparison every caller had
  before the mapping was read.

`_verify_creators` cites these as "false alarms twice on real writes".

## 2026-08-13: trashing an attachment, measured

From `trash_items`. `bootstrap.js` resolves with `getByLibraryAndKey`, which accepts any item
key, so attachments looked likely to work but nobody had run one. Linked-file attachment
MAQ3PAG9 was trashed and restored cleanly (see README). The library then held 277 trashed
items.

## `import_attachment` with no key

`import_attachment` put a `None` attachment key straight into its envelope beside `"ok": True`,
so a caller got a success it could not act on. It now raises `COOKJOHN_RETURNED_NO_KEY`, as
`create_item` does.

## `_tag_op` without a verdict

`_tag_op` always re-read the tags but handed back `tags_before` and `tags_after` and left the
caller to diff them.

## 2026-08-20: bridge errors

From `infrastructure/http/bridge.py`.

- `ping()` used `self.url.replace("/window-state", "/ping")` while `DEFAULT_PING_URL` sat three
  lines above, defined and never read.
- `get_window_state_raw` reported an `HTTPError` as "not reachable at {url}" -- false, because
  the bridge answered -- and discarded the `{"ok": false, "error": ...}` body the plugin sends
  with a 500. `transports/linker.py` already read the body off the exception.

## Linker consumers

From `infrastructure/transports/linker.py`. The docstring said `linker/bootstrap.js` v0.3.0
registers `trash-items` and `restore-items` "and has had ZERO consumers since it shipped". That
stopped being true when `verbs.py` began calling both (`trash_items`, `restore_items`).

## Move

From `application/services/collections.py`. Before `move_items_between_collections` there was
no move: a caller did add-then-remove, two calls, only the second journalled, no rollback. The
docstring's own author left an item in two collections that way while testing the surface.

## 2026-08-13: verified live (moved from `docs/write-surface.md`)

Against Zotero 9.0.6, zotero-linker 0.3.0, zotero-integrated-mcp 1.1.0, on a
throwaway item created for the purpose (`UUV3HWD2`, linked attachment `MAQ3PAG9`):

- **`trash-items` works on an ATTACHMENT.** This was the open question — nobody had
  run it. A linked-file attachment trashed and restored cleanly.
- **Trashing a parent does NOT give children their own `deletedItems` row.** The
  child still read `trashed=False`, so `restore_items(parent)` restores the whole
  thing and no fan-out is needed. An earlier draft assumed the opposite and was wrong.
- **`immutable=1` reflects a just-committed write.** This matters because Zotero holds
  the database locked while it runs, so the snapshot is the only read available:

  ```
  mode=ro        FAIL OperationalError: database is locked
  immutable=1    OK  deletedItems=277 items=3405
  ```

- **Duplicate detection fires on real data.** A `create_item` carrying a DOI already
  in the library (`10.3390/ijerph17124480`, passed in URL form against a bare stored
  value) refused with `duplicate_item` and created nothing.
  `create_collection("calibre")` refused against the existing `ZDUFVSBC`.
- **Two verification bugs were found by running it and are now fixed.** Zotero
  *normalises* field values — writing `date="2026"` comes back stored as
  `"2026-00-00 2026"` — so strict equality reported a successful write as
  "unverified" and blamed the snapshot read. And a field that did not previously
  exist was recorded as having held `""`, producing an `undo_call` that would write
  empty strings instead of removing the field. Both are now distinguished:
  `normalized_by_zotero` vs `disagreed`, and `fields_added` vs `fields_overwritten`
  with `undo_call: None` when no inverse is expressible.
- **A third verification bug: Zotero also rewrites the field NAME.** cookjohn writes
  through Zotero's base-field mapping, so `publicationTitle` on a `conferencePaper` is
  stored as `proceedingsTitle` (`bookTitle` on a `bookSection`, `websiteTitle` on a
  `webpage` — ten types map that base alone). `write_metadata` answered `ok`, the value
  was in the library, and reading back the name that was *written* found nothing — so a
  successful venue write reported `disagreed` and sent the caller to check Zotero by
  hand. The mapping is read from the database's own `baseFieldMappingsCombined` rather
  than hard-coded, and a relocated field now reports `mapped_to_type_field` — kept
  separate from `normalized_by_zotero` because the value is intact and it is the *name*
  that moved, which is what a caller re-reading the field needs to know.
- **`copy_db=True` crashed, and `ty` found it, not the suite.** Every verb passes its own
  `journal_dir` straight through and that parameter defaults to None, so the documented
  way to ask for the snapshot reached `os.makedirs(None)` — after the manifest was
  written and before the write was sent. Both existing tests passed an explicit
  `journal_dir`, which is exactly why it survived. `copy_database` now resolves
  `DEFAULT_JOURNAL_DIR` itself, as `write_manifest` always did.
- The library returned to its starting state; only the throwaway remains, in the trash.

## Superseded framing in `docs/write-surface.md`

Moved 2026-11-04. The document opened with:

> ℹ **Merged 2026-08-19.** This was `writes/README.md`, when the write surface was its own
> distribution. It became the `zotero_core.write` layer, and on 2026-08-20 that layer became
> **`zotero_core.application`** — the verbs live in `application/services/`, the transports
> and the journal moved to `infrastructure/`, and the error vocabulary to `domain/errors.py`.
> `src/zotero_core/write/` no longer exists.

and its "Where it lives" section read:

    ## Where it lives, and what `core/` is

    **`core/` is not the single Zotero owner. It is the read half; this is the write
    half.** ⚠ THE DIRECTION INVERTED on 2026-08-20: `interfaces > infrastructure > application > domain`,
    and `core/`'s "read-only forever" contract is **unamended**.

    The alternative was amending that contract so one package owned everything, with
    calibre-core as precedent — its docstring says an earlier version "over-read its own
    reason: the argument is against raw SQL, not against owning the gate." Sound there,
    and it does not transfer, for three reasons spelled out in
    `src/zotero_core/write/__init__.py`: ZoteroSuite never conflated the two rules (they are
    separate README bullets, so there is no tangle to undo); `zotero_core.infrastructure` has
    `dependencies = []` and feeds an Obsidian JSON CLI, while a CRUD surface needs two
    HTTP clients; and every write precondition is a read, so the direction is naturally
    acyclic.

    The suite-level **routing** rule in `../README.md` *was* amended, because it was
    already stale — it said all mutation goes through cookjohn, untrue since `linker/`
    shipped write endpoints, and never true for linked attachments, which cookjohn
    cannot create at all.
