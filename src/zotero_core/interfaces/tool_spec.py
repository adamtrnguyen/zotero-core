"""One tool declaration, shared by both MCP adapters.

WHY IT IS SHARED
----------------
`_ToolSpec` existed twice, byte-identical apart from a docstring and one extra field, and
so did `as_tool_schema()` and the dispatch that consumes it. Both adapters exist BECAUSE of
this shape -- each replaced a three-place-per-tool change (a `types.Tool(...)` literal, an
`if name == ...` branch, and hand-written argument coercion) with one table entry. Declaring
the table's row type twice reintroduced, one level up, exactly the duplication the table was
built to remove.

WHY IT LIVES IN `interfaces/` AND NOT `domain/`
----------------------------------------------
It is an MCP concern, not a Zotero one: `properties` is JSON Schema and `as_tool_schema()`
emits it. The domain does not know what a tool is. It stays out of `domain/` for the same
reason the `mcp-only-in-the-adapter` contract exists -- and note this module itself imports
no `mcp`, so that contract is unaffected either way.

`ToolSpec` (read) and `WriteToolSpec` (write) rather than one class with an optional field:
a read tool has no transport, and giving it one defaulted to `"cookjohn"` would state
something false about every one of the twenty.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    """One tool: its name, the verb it calls, and the arguments it accepts.

    `verb` is the real function, not a wrapper, and every argument is passed by KEYWORD --
    so there is no per-verb adapter that can drift from the schema.
    """

    name: str
    verb: Callable[..., Any]
    description: str
    properties: dict[str, dict] = field(default_factory=dict)
    required: tuple[str, ...] = ()

    def as_tool_schema(self) -> dict:
        return {
            "type": "object",
            "properties": dict(self.properties),
            "required": list(self.required),
        }


@dataclass(frozen=True)
class WriteToolSpec(ToolSpec):
    """A write tool, which additionally declares WHICH PLUGIN performs it.

    Declared because the transport was previously hardcoded three times per verb,
    independently and unlinked: what `require_zotero(needs=...)` demands, what
    `session.<name>.call/post` invokes, and the literal `"transport": "..."` in the result.
    Nothing asserted the three agreed, so a verb could demand one plugin, use another and
    report a third.

    `"both"` is preflight, which probes each separately on purpose; `"none"` is a verb that
    touches no plugin at all.
    """

    transport: str = "cookjohn"


def dispatch(
    index: Mapping[str, ToolSpec],
    name: str,
    arguments: dict[str, Any],
    *,
    extra: dict[str, Any] | None = None,
) -> Any:
    """Route one tool call to its verb. Raises rather than returning an envelope.

    `Mapping`, not `dict`, and that is not style: `dict` is INVARIANT in its value type, so
    a `dict[str, WriteToolSpec]` is not a `dict[str, ToolSpec]` and the write adapter could
    not pass its own index. `Mapping` is covariant, which is what lets one function serve a
    base-class table and a subclass table.

    ONE function for both adapters. The two dispatches were identical line for line --
    every difference between them was a comment -- which is the same duplication the
    `TOOLS` table exists to remove, one level up.

    Errors travel as exceptions so each adapter has exactly ONE place that turns them into
    a result. On the write side that is also what makes `code` reliable: a `WriteBlocked`
    raised by a gate and one raised by a transport are handled identically.
    """
    spec = index.get(name)
    if spec is None:
        raise ValueError(f"Unknown tool: {name}")

    unknown = sorted(set(arguments) - set(spec.properties))
    if unknown:
        # REFUSED, not dropped, and both adapters had a reason. On the read side a
        # misspelled `include_annotaions` used to vanish and hand back the default. On the
        # write side a caller passing `journal_dir` or `store` would get a silent no-op on
        # the argument it cared about, and one misspelling `force` would get the refusal it
        # was trying to bypass.
        raise ValueError(f"{name} does not accept {unknown} (accepted: {sorted(spec.properties)})")

    missing = [key for key in spec.required if key not in arguments]
    if missing:
        raise ValueError(f"{name} requires {missing}")

    # An explicit null on an OPTIONAL argument is dropped so the verb's own default applies
    # -- some clients send every declared property, and `action=None` would otherwise defeat
    # `write_note`'s default of "create". A null on a REQUIRED argument is passed THROUGH on
    # purpose: the verb's own gate answers it with a coded refusal (`unknown_item_type`,
    # `no_item_keys`), which is a better answer than a ValueError from here.
    call_args = {
        key: value for key, value in arguments.items() if value is not None or key in spec.required
    }
    # `extra` is what the ADAPTER injects, never what the client sent: the write side
    # passes the `WriteSession` built by `interfaces/factory.py`. It is merged after the
    # unknown-argument check on purpose -- a session is not a tool parameter, so it must
    # not appear in `properties`, and a client must not be able to supply one.
    return spec.verb(**call_args, **(extra or {}))
