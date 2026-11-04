"""Domain entities, split by PROVENANCE -- where the data actually comes from.

    models.py   rows this package reads out of zotero.sqlite
    gui.py      the zotero-bridge HTTP payload: what the app is showing right now

Kept apart because the bridge types are parsers of a wire format (`from_bridge` accepts
camelCase and snake_case), while the SQL-derived ones are the Zotero nouns.

Empty by convention: nothing is re-exported from a layer. The package ROOT is the
documented exception, because `zotero_core.__all__` is the published API -- see
`tests/test_public_api.py`.
"""
