"""Text policy: normalising a title, and scoring how well two strings match.

Pure. No sqlite, no network, no mcp -- enforced by the `domain-is-pure` contract. Dedupe
and search share this one normaliser; two copies is how 'Hébert' stops matching 'Hebert'
in one place and not the other.

WHY NOT rapidfuzz
-----------------
`calfuzz` (CalibreSuite) uses it and measures 97% recall against 69% for substring
matching. It is the better library. It is also a C extension, and this package has
`dependencies = []` on purpose. `difflib` is stdlib and gets most of the way; see
`fuzzy_score` for the algorithm and its measured behaviour.
"""

from __future__ import annotations

import difflib
import re
import unicodedata

# Combining marks that CHANGE THE PHONEME rather than decorate a letter. NFKD splits
# 'が' into 'か' + U+3099, and blindly dropping combining marks then merges two distinct
# kana. Accents on Latin letters are the opposite case -- 'Hébert' and 'Hebert' are the
# same name typed two ways, and merging them is the whole point.
_PHONEMIC_MARKS = frozenset({"゙", "゚"})


def normalize_title(title: str) -> str:
    """Casefold, strip decorative accents and punctuation, collapse whitespace.

    NFKD then dropping combining marks is what makes 'Hébert' match 'Hebert'.

    Kana voicing marks are PRESERVED (`_PHONEMIC_MARKS`): dropping them collapses 'が' to
    'か', so two different Japanese words would compare equal, which is a wrong search
    result as well as a wrong dedupe verdict.
    """
    if not title:
        return ""
    decomposed = unicodedata.normalize("NFKD", title)
    stripped = "".join(
        c for c in decomposed if not unicodedata.combining(c) or c in _PHONEMIC_MARKS
    )
    # Recompose so preserved marks rejoin their base character rather than trailing it.
    recomposed = unicodedata.normalize("NFC", stripped)
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", recomposed)).strip().casefold()


def surname_of(creator: dict[str, str]) -> str:
    """The surname a match should key on, normalised."""
    raw = creator.get("lastName") or creator.get("name") or ""
    # An organisation in single-field mode has no surname as such; key on the whole
    # name, which is what a human comparing two records would do.
    return normalize_title(raw.split(",")[0])


def fuzzy_score(query: str, candidate: str) -> float:
    """How well `query` matches `candidate`, 0.0 to 1.0. Order does not matter."""
    return score_tokens(normalize_title(query).split(), normalize_title(candidate))


def score_tokens(query_tokens: list[str], candidate_normalized: str) -> float:
    """`fuzzy_score` with the query already tokenised and the candidate already normalised.

    Exists for the search loop, which scores ONE query against thousands of candidates:
    re-normalising the query per candidate meant an NFKD pass and two regex substitutions
    per row per field, and that alone dominated the runtime.

    TOKEN-WISE, not whole-string. A whole-string ratio is dominated by length -- the real
    failure case is

        query     "Langevan Dynmaics"                                    (2 typos)
        candidate "Bayesian Learning via Stochastic Gradient Langevin Dynamics"

    where SequenceMatcher over the full strings scores 0.395 because the candidate is
    three times longer, though every query word matches a candidate word almost exactly.
    Scoring each query token against its BEST candidate token gives 0.875. (Both measured
    on this library, not estimated.) This is the shape rapidfuzz calls
    `partial_token_sort_ratio`.
    """
    if not query_tokens or not candidate_normalized:
        return 0.0

    joined = " ".join(query_tokens)
    if joined == candidate_normalized:
        return 1.0

    candidate_tokens = candidate_normalized.split()
    if not candidate_tokens:
        return 0.0

    # A phrase match is a strong signal, but not perfect: "learning" is inside "machine
    # learning" and must not outrank the exact title.
    #
    # Checked on TOKENS, not characters: a plain `joined in candidate_normalized` matches
    # across word boundaries ("zz test" is a substring of "fuzz testing").
    window = len(query_tokens)
    if any(
        candidate_tokens[i : i + window] == query_tokens
        for i in range(len(candidate_tokens) - window + 1)
    ):
        return 0.97

    matcher = difflib.SequenceMatcher(autojunk=False)
    total = sum(_best_token_match(matcher, token, candidate_tokens) for token in query_tokens)
    return total / len(query_tokens)


def _best_token_match(matcher: difflib.SequenceMatcher, token: str, candidates: list[str]) -> float:
    """The closest any candidate token comes to `token`."""
    # seq2 is the side SequenceMatcher builds an index for, so it is set ONCE here and
    # seq1 varies below. Rebuilding that index per comparison is the difference between
    # a usable search and a five-second one.
    matcher.set_seq2(token)
    best = 0.0
    for other in candidates:
        if token == other:
            return 1.0
        # Length gate first: tokens differing wildly in length are never a typo of one
        # another, and this is a subtraction rather than a matcher run.
        if abs(len(token) - len(other)) > max(2, len(token) // 2):
            continue
        matcher.set_seq1(other)
        # Two cheap upper bounds before the real O(n*m) comparison. Both are guaranteed
        # >= ratio(), so bailing when they cannot beat `best` is exact, not approximate.
        if matcher.real_quick_ratio() <= best or matcher.quick_ratio() <= best:
            continue
        best = max(best, matcher.ratio())
    return best


# Below this, a "match" is noise. Measured on this library: a 2-typo query against its
# true target scores ~0.87, while unrelated titles sit under 0.5.
DEFAULT_FUZZY_THRESHOLD = 0.72
