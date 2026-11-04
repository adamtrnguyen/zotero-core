"""What a Zotero key looks like. One rule, one place.

Every key check in the package calls `is_key` / `find_embedded_key` from here;
`tests/test_identity.py` checks no other copy exists. The rule is a regex, never
`str.isalnum()`: `isalnum` is Unicode-aware and accepts full-width Latin, Greek capitals
and Roman-numeral characters, so a string could pass as a key in one layer and be refused
as malformed in another.

WHY [A-Z0-9] AND NOT ZOTERO'S REAL ALPHABET
-------------------------------------------
Zotero's actual alphabet is narrower (`23456789ABCDEFGHIJKLMNPQRSTUVWXYZ`: no 0, 1, or O),
but this gate stays at [A-Z0-9] deliberately. A stricter class could only add a way to
WRONGLY refuse a key some future Zotero mints, and it would buy nothing: a typo that lands
inside the alphabet is caught by the existence gate, which is the check that actually
protects the caller.

`calibre-zotero-jump` keeps its own copy of the pattern, outside this package and beyond
its tests: Calibre's embedded Python cannot import a uv virtualenv.
History of the five copies: docs/history/identity-and-normalisation.md.
"""

from __future__ import annotations

import re

#: The shape of a Zotero item or collection key. Anchored: a key IS this, whole.
KEY_PATTERN = r"^[A-Z0-9]{8}$"

#: The same shape unanchored, for finding a key embedded in prose (cookjohn sometimes
#: answers with a sentence rather than a field). Separate constant because the anchoring
#: is the difference between "this string is a key" and "this string contains one".
EMBEDDED_KEY_PATTERN = r"\b([A-Z0-9]{8})\b"

_KEY_RE = re.compile(KEY_PATTERN)
_EMBEDDED_KEY_RE = re.compile(EMBEDDED_KEY_PATTERN)


def is_key(value: str) -> bool:
    """Is this string a Zotero key? The one answer, for both layers."""
    return bool(_KEY_RE.match(value))


def find_embedded_key(text: str) -> str | None:
    """The first key-shaped token in some text, or None. Last resort, not a validator."""
    match = _EMBEDDED_KEY_RE.search(text)
    return match.group(1) if match else None
