"""MCP JSON-RPC transport to the cookjohn zotero-mcp-plugin on :23121.

The one cookjohn client in the suite: every Python consumer that can import this package
writes through it, so there is one answer to "is this already in Zotero". The exception is
`calibre-zotero-jump`, a Calibre plugin running inside Calibre's embedded Python, which
cannot see a uv virtualenv; it keeps its own `_Mcp` in `ui.py`, outside these tests.

Stdlib-only means no third-party packages (`dependencies = []`); importing
`zotero_core.domain` is fine. How the duplicate clients came to be retired, and the
vendoring claim that was never true: docs/history/package-structure.md.

THE SSE UNWRAP, WHICH LOOKS LIKE A HACK AND IS NOT
--------------------------------------------------
The server may answer a plain POST with an `text/event-stream` body -- `event: ...`
then `data: {...}` lines -- rather than bare JSON, depending on the Accept header it
sees. Both hand-copied clients carry the same unwrap, which is the strongest
evidence available that it is load-bearing rather than cargo: two independent
call sites hit it. The last `data:` line is the result.

WHY NOT THE OFFICIAL MCP CLIENT LIBRARY
---------------------------------------
`mcp` is a real dependency with its own async runtime, and this package's whole
transport need is one POST with a two-line handshake. core deliberately keeps `mcp`
behind an optional extra for the same reason. Adding it as a hard dependency here
would put an async client in the import path of a Calibre plugin that cannot use it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from zotero_core.domain.errors import Reason, WriteBlocked
from zotero_core.domain.services.identity import find_embedded_key, is_key

DEFAULT_COOKJOHN_URL = "http://127.0.0.1:23121/mcp"


class CookjohnClient:
    """Stateless JSON-RPC over HTTP. The handshake runs once, lazily."""

    def __init__(self, url: str | None = None, *, timeout: float = 300.0):
        # Resolved at CALL time -- see the same note in `linker.py`. A default argument
        # would bind the constant at import and make the test fixture's redirect a
        # no-op.
        self.url = url or DEFAULT_COOKJOHN_URL
        self.timeout = timeout
        self._id = 0
        self._initialized = False
        self.server_info: dict = {}

    def find_key(self, payload, prefer: tuple[str, ...] = ()) -> str | None:
        """Satisfies `ports.write_transport.Cookjohn.find_key`.

        Delegates to the module function below rather than duplicating it: that function
        is still the implementation and is still what its own tests exercise directly.
        """
        return find_key(payload, prefer=prefer)

    def ping(self) -> dict:
        """Liveness. Returns serverInfo, which is what identifies the plugin."""
        self._initialize()
        return dict(self.server_info)

    def _initialize(self) -> None:
        if self._initialized:
            return
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                # Named so the plugin's own logs attribute a write to this package
                # rather than to whichever consumer happened to call it.
                "clientInfo": {"name": "zotero-writes", "version": "0.2"},
            },
        )
        self.server_info = result.get("serverInfo") or {}
        self._initialized = True

    def _rpc(self, method: str, params: dict) -> dict:
        self._id += 1
        body = json.dumps(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        ).encode()
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                raw = resp.read().decode()
        except urllib.error.HTTPError as exc:
            raise WriteBlocked(
                Reason.COOKJOHN_REFUSED,
                f"{method} returned HTTP {exc.code} from the zotero-mcp plugin",
                {"url": self.url, "http_status": exc.code, "method": method},
            ) from exc
        except OSError as exc:
            raise WriteBlocked(
                Reason.COOKJOHN_NOT_INSTALLED,
                f"no answer from {self.url} — the cookjohn zotero-mcp plugin is not "
                f"running on this port ({exc})",
                {"url": self.url, "error": str(exc)},
            ) from exc

        if raw.startswith("event:") or "\ndata:" in raw or raw.startswith("data:"):
            lines = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
            if not lines:
                raise WriteBlocked(
                    Reason.COOKJOHN_REFUSED,
                    f"{method} returned an event stream with no data lines",
                    {"body": raw[:300]},
                )
            raw = lines[-1]
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise WriteBlocked(
                Reason.COOKJOHN_REFUSED,
                f"{method} did not return JSON",
                {"body": raw[:300]},
            ) from exc
        if "error" in payload:
            raise WriteBlocked(
                Reason.COOKJOHN_REFUSED,
                f"{method}: {payload['error']}",
                {"method": method, "error": payload["error"]},
            )
        return payload.get("result", {})

    def call(self, tool: str, arguments: dict):
        """Invoke one MCP tool. Returns parsed JSON when the tool emits it, else text.

        `isError` is checked explicitly: an MCP tool failure comes back as a
        successful JSON-RPC response with that flag set, so a client that only
        checks the transport reads a failed write as a completed one.
        """
        self._initialize()
        result = self._rpc("tools/call", {"name": tool, "arguments": arguments})
        if result.get("isError"):
            raise WriteBlocked(
                Reason.COOKJOHN_REFUSED,
                f"{tool} failed: {_text_of(result)[:300]}",
                {"tool": tool, "arguments": _redact(arguments), "result": result},
            )
        text = _text_of(result)
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return text


# Key shapes come from `domain/services/identity.py`, the one place that owns the rule.

# Field names cookjohn uses for the key of a thing it just created, most specific
# first. `write_item` answers with a nested `data.itemKey`; `create_collection` with a
# flat `key`. That inconsistency is the reason this function exists.
_KEY_FIELDS = ("itemKey", "collectionKey", "noteKey", "attachmentKey", "item_key", "key")


def find_key(payload, prefer: tuple[str, ...] = ()) -> str | None:
    """Dig an 8-character key out of whatever cookjohn returned.

    Lives HERE, in the transport, because it is a fact about cookjohn's replies rather
    than about any one verb -- and because the first draft of this package had two
    copies of it, one in `writes.py` and one in `collections.py`, differing only in
    which field name they checked first. Two copies of a parser is the exact defect
    this package was built to end; ruff's complexity check is what surfaced it.

    `prefer` puts a field at the front, so a collection verb can insist on
    `collectionKey` when a reply happens to carry both. Named fields are always tried
    before the blind regex, so a key mentioned in a sentence cannot outrank the real one.
    """
    for field in (*prefer, *_KEY_FIELDS):
        found = _keyed(payload, field)
        if found:
            return found
    return _any_key(payload)


def _keyed(payload, field: str) -> str | None:
    """Search the tree for one named field holding a key-shaped string."""
    if isinstance(payload, dict):
        value = payload.get(field)
        if isinstance(value, str) and is_key(value):
            return value
        for value in payload.values():
            found = _keyed(value, field)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _keyed(value, field)
            if found:
                return found
    return None


def _any_key(payload) -> str | None:
    """Last resort: the first key-shaped token anywhere, including inside prose."""
    if isinstance(payload, str):
        return find_embedded_key(payload)
    if isinstance(payload, dict):
        payload = list(payload.values())
    if isinstance(payload, list):
        for value in payload:
            found = _any_key(value)
            if found:
                return found
    return None


def _text_of(result: dict) -> str:
    return "\n".join(
        block.get("text", "") for block in result.get("content", []) if block.get("type") == "text"
    )


def _redact(arguments: dict) -> dict:
    """Keep note/abstract bodies out of error details.

    An exception's `detail` ends up in logs and MCP responses, and a failed
    `write_note` carries the entire note body in its arguments. Truncating keeps the
    error readable and keeps a page of prose out of an error report.
    """
    out = {}
    for key, value in arguments.items():
        if isinstance(value, str) and len(value) > 120:
            out[key] = value[:120] + f"… ({len(value)} chars)"
        else:
            out[key] = value
    return out
