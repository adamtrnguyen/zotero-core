"""Serialising a result for the wire. ONE implementation, for all three surfaces.

WHY IT IS SHARED, AND WHAT THE THREE COPIES DISAGREED ABOUT
-----------------------------------------------------------
The same `json.dumps(to_jsonable(payload), ensure_ascii=False, indent=…)` was written three
times: `cli.print_json`, `write_mcp._render`, and inline in `read_mcp`'s `call_tool`. They
were not identical, and the difference was load-bearing rather than cosmetic:

    write_mcp._render     json.dumps(..., default=str)
    cli.print_json        json.dumps(...)              ← no default
    read_mcp (inline)     json.dumps(...)              ← no default

`default=str` is what stops an unexpected type raising inside the serialiser. Only the write
adapter had it — so **an un-serialisable value killed the read adapter and was survived by
the write one**, from the same payload. `_render`'s own docstring explains exactly why the
fallback matters ("falling back to `str` degrades one field; raising here would drop the
whole response and tell the caller nothing") and the other two surfaces never got it.

`to_jsonable` handles the shapes this package returns -- dataclasses, tuples, nested dicts.
`default=str` covers what it cannot know about: `detail` on a `WriteBlocked` is whatever the
gate put there, and a read row can carry a `Path` or a `datetime`. That is the one place a
value the domain never modelled reaches the wire.
"""

from __future__ import annotations

import json
from typing import Any

from zotero_core.domain.entities.models import to_jsonable


def render_json(payload: Any, *, indent: int | None = 2) -> str:
    """Serialise a result. `default=str` so an unexpected type cannot kill the caller.

    `indent=None` produces the compact form the CLI emits without `--pretty`.
    """
    return json.dumps(to_jsonable(payload), ensure_ascii=False, indent=indent, default=str)
