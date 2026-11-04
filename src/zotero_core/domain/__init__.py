"""Pure domain: entities, values and policy. No sqlite, no HTTP, no mcp.

    read_mode.py        ReadMode -- which mode served a sqlite read
    annotation_type.py  the annotation vocabulary
    entities/           the types, split by provenance
    ports/              the Protocols application depends on
    services/           pure functions: identity, policy, identifiers

Rules that would otherwise be duplicated across adapters live here, once.
`services/identity.py` holds the one rule for what a Zotero key is, and
`tests/test_identity.py` checks no other copy exists in the package.

`.importlinter`'s `domain-is-pure` contract enforces the "no driver" half.
History of the key-rule consolidation: docs/history/identity-and-normalisation.md.
"""
