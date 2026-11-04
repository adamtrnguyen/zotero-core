"""What a docstring or comment under SRC may not carry: a date, an emoji, or incident narrative.

A date or a "this used to ..." sentence is a record. In the code it reads as a claim about the
present and rots; in docs/history/<topic>.md it sits beside the measurement that explains it. An
emoji is emphasis, and emphasis belongs in a runbook, not in a contract. A docstring or comment
keeps the contract and the present-tense rationale, nothing else.

String literals are not checked: output the program prints may carry any of these. Adjust SRC and
the patterns; `prose: ok <reason>` on the same line exempts one deliberate use.
"""

from __future__ import annotations

import ast
import io
import pathlib
import re
import tokenize

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
EXEMPT = "prose: ok"
DATE = re.compile(r"\b(19|20)\d\d-\d\d(-\d\d)?\b")
# Pictographs and warning marks; a symbol the code is QUOTING (a star in a title) is not emphasis.
EMOJI = re.compile("[\U0001f300-\U0001faff\u26a0\u26d4\u2705\u274c]")
NARRATIVE = re.compile(
    r"\b(used to|previously|formerly|originally|until recently|turned out|incident"
    r"|post-?mortem|hotfix)\b",
    re.IGNORECASE,
)


def _prose_lines(source: str) -> list[tuple[int, str]]:
    """(line number, text) for every docstring line and every comment.

    Code on a comment's line is excluded."""
    lines = source.splitlines()
    prose: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
        ):
            first = node.body[0]
            value = first.value if isinstance(first, ast.Expr) else None
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                prose.extend(
                    (n, lines[n - 1])
                    for n in range(first.lineno, (first.end_lineno or first.lineno) + 1)
                )
    prose.extend(
        (tok.start[0], tok.string)
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type == tokenize.COMMENT
    )
    return prose


def _offenders(pattern: re.Pattern[str]) -> list[str]:
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        prose = _prose_lines(path.read_text(encoding="utf-8"))
        found.extend(
            f"{path.relative_to(SRC.parent)}:{n}"
            for n, text in prose
            if pattern.search(text) and EXEMPT not in text
        )
    return found


def test_no_docstring_or_comment_carries_a_date():
    assert not _offenders(DATE), (
        f"a dated note is a record; move it to docs/history: {_offenders(DATE)}"
    )


def test_no_docstring_or_comment_carries_an_emoji():
    assert not _offenders(EMOJI), f"emphasis is not a contract: {_offenders(EMOJI)}"


def test_no_docstring_or_comment_tells_a_history():
    found = _offenders(NARRATIVE)
    assert not found, (
        f"what the code USED to do belongs in docs/history, not beside the code: {found}"
    )
