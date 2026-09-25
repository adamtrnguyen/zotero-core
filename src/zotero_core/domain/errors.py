"""The one exception the write path raises, and the reason codes it carries.

Modelled on `calibre_core.writes.WriteBlocked`, with one deliberate improvement:
the reason is a CODE as well as prose. calibre-core's version carries prose only,
so a consumer that wants to react differently to "Zotero isn't running" than to
"that key doesn't exist" has to match substrings of an English sentence -- and the
sentence is the part most likely to be reworded. The MCP layer needs to branch,
so `code` is the stable field and `reason` is for the human reading the error.
"""

from __future__ import annotations


class Reason:
    """Stable machine-readable codes. Values are the contract; never reword them."""

    # liveness — the precondition that INVERTS relative to Calibre
    ZOTERO_NOT_RUNNING = "zotero_not_running"
    LINKER_NOT_INSTALLED = "linker_not_installed"
    COOKJOHN_NOT_INSTALLED = "cookjohn_not_installed"
    NOT_THE_LINKER = "not_the_linker"
    # input shape
    NO_ITEM_KEYS = "no_item_keys"
    MALFORMED_ITEM_KEY = "malformed_item_key"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    UNKNOWN_ITEM_TYPE = "unknown_item_type"
    # library state
    UNKNOWN_ITEM_KEYS = "unknown_item_keys"
    UNKNOWN_COLLECTION_KEY = "unknown_collection_key"
    ALREADY_TRASHED = "already_trashed"
    NOT_TRASHED = "not_trashed"
    NOTHING_TO_DO = "nothing_to_do"
    WRONG_ITEM_TYPE = "wrong_item_type"
    # creates
    DUPLICATE_ITEM = "duplicate_item"
    # a URL with no scholarly metadata behind it, or a bot wall in front of it
    PAPER_UNRESOLVED = "paper_unresolved"
    # destructive-by-replacement — see `writes.set_tags` / `replace_creators`
    REFUSING_TO_REPLACE = "refusing_to_replace"
    REFUSING_CASCADE_DELETE = "refusing_cascade_delete"
    FILE_NOT_FOUND = "file_not_found"
    # the mutators
    LINKER_REFUSED = "linker_refused"
    COOKJOHN_REFUSED = "cookjohn_refused"
    COOKJOHN_RETURNED_NO_KEY = "cookjohn_returned_no_key"
    PARTIAL_APPLY = "partial_apply"
    VERIFICATION_FAILED = "verification_failed"
    # a two-step verb whose second step failed and whose first step was undone.
    # Distinct from PARTIAL_APPLY on purpose: partial means the library is in a state
    # nobody asked for, rolled-back means it is exactly as it started.
    ROLLED_BACK = "rolled_back"


ALL_REASONS = frozenset(
    value
    for name, value in vars(Reason).items()
    if not name.startswith("_") and isinstance(value, str)
)


class WriteBlocked(Exception):
    """A gate refused the operation, or a write did not verify."""

    def __init__(self, code: str, reason: str, detail: dict | None = None):
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason
        self.detail = detail or {}

    def as_dict(self) -> dict:
        """The shape an MCP tool should return. `code` is what a client branches on."""
        return {"ok": False, "code": self.code, "reason": self.reason, "detail": self.detail}
