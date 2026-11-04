# Paper resolver: what replaced ZotLink

Moved out of `domain/ports/paper_resolver.py` (2026-11-04 history wave). The decision itself is
recorded in the suite's `docs/DESIGN.md` (2026-09-25).

ZotLink was a separate MCP server and a private fork of ~9,400 lines, whose job this port does.
Its own log (2026-03 to 2026-09) showed what was actually used: arXiv almost exclusively, a
generic page scraper that saved bot-wall pages ("Client Challenge", "Verifying your browser |
OpenReview") as papers, and site extractors that were selected zero times. So the port states
the one question worth asking -- what is at this URL -- and the saving goes through the
existing gated verbs, `create_item` and `import_attachment`.
