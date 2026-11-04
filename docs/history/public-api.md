# The published API: why `__all__` carries writes and the factory

Records moved out of `zotero_core/__init__.py` (2026-11-04 history wave).

## 2026-08-19: consumers broke by reaching past the surface

- `arxiv-bulk` imported defaults from `read.annotations` / `read.bbt` directly -- reaching past
  the public surface into modules that were about to move -- and broke when they moved. The
  fix published `DEFAULT_BBT_RPC_URL`, `DEFAULT_ZOTERO_DB` and `USER_LIBRARY_ID`.
- The same day, `__all__` was still read-only, a leftover from when `core/` was a read-only
  package. `youtube2zotero/promote.py` needs writes, so it imported
  `zotero_core.write.transports.cookjohn` and `zotero_core.write.verbs` by path -- paths that
  then moved (`transports/` to `infrastructure/`, `write/` to `application/`). Publishing the
  names it uses (`CookjohnClient`, `LinkerClient`, `import_attachment`, `update_metadata`,
  `write_note`) made both moves invisible to it, twice, with no edit on its side.

## The lazy write verbs (PEP 562)

A module-level `from zotero_core.write.verbs import ...` in the root `__init__.py` made every
layer, `domain` included, depend on `write`, and import-linter caught it:
`infrastructure.service -> zotero_core -> write.verbs`. `__getattr__` defers the import to
first attribute access, so no import-time edge exists.

## 2026-08-20: `build_context` was not published, and `arxiv-bulk` broke again

`ZoteroContext(zotero_db_path=...)` had been the documented way in. The read-side port refactor
gave `ZoteroContext` eight required ports, so the constructor changed while the name did not,
and the replacement (`build_context`) was not on the public surface. Publishing a type is not
publishing a way to construct one. `build_context` and `build_write_session` are now in
`__all__`, and `tests/test_public_api.py` asserts the surface can construct, not merely
resolve.
