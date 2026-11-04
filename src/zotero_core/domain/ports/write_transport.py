"""The two plugins a write travels through, as the application sees them.

WHY TWO PORTS AND NOT ONE `WriteTransport`
------------------------------------------
The refactor plan called for a single `WriteTransport`. The code says otherwise, and the
code is right: the two clients share no method. Measured off every call site in the write
layer — `cookjohn.call` and `linker.post`, and `ping` on both — so a merged port would
have to be the UNION, and every implementation would then declare a method it cannot
serve. Two ports each describe something real; one would describe neither.

They stay separate for the same reason the transports themselves do: neither knows the
other exists, which is the property that lets a verb need one and not the other.

WHY `Protocol` AND NOT `ABC`
----------------------------
Structural, so the adapters satisfy these by SHAPE and never import them — which keeps
`infrastructure` free of an upward import and keeps the port definitions honest, since a
port nobody imports cannot drift into a base class carrying behaviour.

Not because `cookjohn.py` is vendored anywhere: it is not, and it imports
`zotero_core.domain` regardless. See docs/history/package-structure.md.

`@runtime_checkable` because `arete` already uses that form for `ContentCache`, and
because it makes the fakes assertable as real implementations rather than as subclasses
that happen to work.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Cookjohn(Protocol):
    """The MCP plugin that performs item and collection writes."""

    def call(self, tool: str, arguments: dict): ...

    def ping(self) -> dict: ...

    def find_key(self, payload, prefer: tuple[str, ...] = ()) -> str | None:
        """Dig the key of a just-created thing out of THIS transport's reply.

        On the port because reply SHAPE is a property of the transport, not of the verb:
        cookjohn answers `write_item` with a nested `data.itemKey` and `create_collection`
        with a flat `key`, and a different backend would answer differently. A verb that
        imported one backend's parser could not be pointed at another -- which is exactly
        the constraint that matters while cookjohn is being replaced.
        """
        ...


@runtime_checkable
class Linker(Protocol):
    """The HTTP plugin that performs attachment and selection work."""

    def post(self, path: str, payload: dict) -> dict: ...

    def ping(self) -> dict: ...
