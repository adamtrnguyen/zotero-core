# Identity and normalisation: one key rule, one annotation vocabulary, one normaliser

Records moved out of docstrings and comments under `src/` (2026-11-04 history wave).

## 2026-08-19: the key rule had five copies and four implementations

From `domain/__init__.py`, `domain/services/identity.py`, `application/services/context.py`,
`infrastructure/transports/cookjohn.py`.

    write/verbs.py                     re.compile(r"^[A-Z0-9]{8}$")
    write/collections.py               re.compile(r"^[A-Z0-9]{8}$")
    write/transports/cookjohn.py       re.compile(r"^[A-Z0-9]{8}$")
    write/transports/cookjohn.py       re.search(r"\b([A-Z0-9]{8})\b", ...)   inline
    read/service.py                    len(v) == 8 and v.isalnum() and v.upper() == v

`domain/` was created to hold this and never received it: the layer docstring, the
`.importlinter` rationale and the README all claimed the consolidation had happened (the
domain docstring said `_KEY_RE` "lived in three files") while all five copies stayed put.

The fourth disagreed with the other three. `str.isalnum()` is Unicode-aware:

    ARTINWQZ    regex True    isalnum True      a real key
    ＡＲＴＩＮＷＱＺ   regex False   isalnum True      full-width Latin
    ⅠⅠⅠⅠⅠⅠⅠⅠ   regex False   isalnum True      Roman-numeral characters
    ABCDΕΦΓΗ    regex False   isalnum True      Greek capitals

The read layer used it (`_looks_like_item_key` in the read facade) to decide "item key, or a
citekey for Better BibTeX?", so a string could pass there as a key and then be refused by the
write layer as malformed. Since the change, input that cannot be a valid key goes to BBT.

The `[A-Z0-9]` paragraph existed in exactly one of the five copies. At the time all 3405 keys
in the live library used only `23456789ABCDEFGHIJKLMNPQRSTUVWXYZ`.

A sixth copy was kept "on purpose" in `cookjohn.py` because the module was said to be vendored
into `calibre-zotero-jump`, with a test asserting the two patterns identical. Both premises were
false (see `package-structure.md`, the vendoring premise). The transport now calls
`is_key` / `find_embedded_key`, and the drift test became an absence check
(`tests/test_identity.py`). Copies inside this package went from five to zero; the plugin's
copy is outside it and always was.

## 2026-08-19: the annotation vocabulary had three copies, two disagreeing

From `domain/annotation_type.py`, `interfaces/read_mcp.py`.

    read/annotations.py   ANNOTATION_TYPE.get(row[3], f"type{row[3]}")   -> "type7"
    read/search.py        ANNOTATION_TYPE.get(type_id, str(type_id))     -> "7"
    interfaces/read_mcp   ANNOTATION_TYPE_NAMES = ("highlight", ...)     hand-maintained

Two names for the same unknown type, from the same table, depending on which query found it.
The third copy's comment asked to be kept in step by hand. A caller was expected to guess types
3-6 from a description advertising two by example. `ANNOTATION_TYPE` was also labelled
"Back-compat: the shape `read/annotations.py` exported" while that module re-exported it; the
re-export served only its own tests and was removed.

## 2026-08-19: kana voicing marks preserved

From `domain/services/policy.py`, `infrastructure/sqlite/duplicates.py`.

The normaliser dropped every combining mark, so 'が' collapsed to 'か' and two different
Japanese words compared equal. `read/duplicates.py` documented this as a known limitation, said
the normaliser "cannot import" calibre-core's better `dedup_key`, and that "a CJK-titled paper
will normalise less well here". Once the normaliser also backed search, an over-eager fold was a
wrong search result rather than only a wrong dedupe verdict, so it was fixed. The normaliser
had lived in `read/duplicates.py` as a private, and search needed the same one; it has been
`domain.policy.normalize_title`, shared, since 2026-08-19.

## The phrase match checks tokens

From `domain/services/policy.py`.

A plain `joined in candidate_normalized` matched across word boundaries: searching the library
for "ZZ Test" scored 0.97 against "...Static Analysis and Fuzz Testing", because "zz test" is a
substring of "fuzz testing". Found by an end-to-end run, not by review.
