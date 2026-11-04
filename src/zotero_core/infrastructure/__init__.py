"""The read half: what is in the library, and what the GUI is currently showing.

Sources, in the order they are trusted:
  sqlite    items, duplicates, annotations   -- the catalogue itself
  http      bridge (live window state), bbt (citekeys)

Every sqlite read opens the database read-only and REPORTS WHICH MODE SERVED IT
(`sqlite.connect.open_readonly`): with Zotero running, `mode=ro` can fail on Zotero's
lock and `immutable=1` answers from the main file alone, without the WAL, instead. A
caller that cannot tell those apart cannot know whether it just read the present or the
recent past.
"""
