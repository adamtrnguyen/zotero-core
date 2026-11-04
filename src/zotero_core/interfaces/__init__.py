"""Adapters. The only layer allowed to import `mcp`.

  cli         JSON CLI, consumed by Obsidian
  read_mcp    the read tools
  write_mcp   the gated write tools
  mcp_server  the one registered server, over both tables
  factory     the composition root

`mcp` is imported INSIDE `main()`, never at module scope, so that importing a verb does
not require an async runtime; that is what keeps `dependencies = []` true. Two contracts
and two AST-level tests hold that line.
"""
