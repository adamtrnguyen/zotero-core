"""Write use cases: the verbs, their gates, and the undo that replays them.

Empty by convention -- nothing is re-exported from a layer package. Importing any
`zotero_core.application.*` module executes this file first, so a re-export here would put
whatever it names (an adapter, say) in the application layer's import graph. The ROOT
`zotero_core/__init__.py` is the one documented exception, because its `__all__` is the
published API that other repos import. History: docs/history/package-structure.md.
"""
