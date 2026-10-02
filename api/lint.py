"""Stdlib lint for the shipping tree — the ``lint`` phase of ``run_gate.sh``.

No third-party linter is installable on this host: ``pip`` is absent and
neither ``ruff``, ``flake8``, ``pyflakes`` nor ``pycodestyle`` is present. So
the lint phase is built from checks that need nothing but the standard library,
and it enforces the plan's own mechanical rules:

1. Every module under the shipping tree compiles (AST parse + compile).
2. Every one of them declares ``from __future__ import annotations``
   (§1.2 / §2.1, minimum interpreter Python 3.10).
3. The banned tokens are absent from the shipping tree (§1.3, §2.1):
   ``sqlite3.version`` / ``sqlite3.version_info`` (removed in Python 3.14), and
   floating-point money helpers.
4. ``sqlite3.connect`` appears only in ``ledger/db.py``, and ``import sqlite3``
   only in ``ledger/`` — ``api/`` opens no connection of its own (§T3.1).
5. No tab characters in indentation.

Scope is the shipping tree (``ledger/``, ``api/``, ``app/``). The client is
money-path code — ``app/money.py`` is the float boundary — so the banned-token
scan reads it too. ``tests/`` belongs to the test-author: ``run_gate.sh``
byte-compiles it, but style-policing a directory this task does not own would be
reaching across a file boundary.

The banned-token scan is **token-aware**: comments and the text of string
literals are blanked before matching (stdlib ``tokenize``). A module docstring
that *states* the rule — "no ``float``, no ``Decimal``" — is documentation, not
a violation, and a raw-text matcher cannot tell the two apart; this one can.
Running the same regexes over the code alone is what lets the scan cover
``app/money.py`` without punishing it for documenting the very rule it obeys.

Usage: ``python3 -m api.lint`` — exit 0 when clean, 1 on any finding.
"""

from __future__ import annotations

import ast
import io
import re
import sys
import token
import tokenize
from pathlib import Path

TREE = ("ledger", "api", "app")
FUTURE_IMPORT_MODULE = "__future__"
FUTURE_IMPORT_NAME = "annotations"
CONNECTION_FACTORY = Path("ledger") / "db.py"

# pattern -> why it is banned. These are matched against the file's CODE only:
# comments and string-literal text are blanked by `code_only` first, so prose
# that names a banned token (a docstring stating the rule) is not a finding while
# a use in a code position is. The linter's own pattern table is exempted below.
BANNED: tuple[tuple[str, str], ...] = (
    (r"sqlite3\.version\b",
     "sqlite3.version / version_info were removed in Python 3.14; use sqlite3.sqlite_version (plan §1.3)"),
    (r"\bfloat\s*\(",
     "no floating point on the money path (plan §2.1)"),
    (r"\bround\s*\(",
     "no rounding on the money path (plan §2.1)"),
    (r"\bDecimal\b",
     "no Decimal on the money path; money is an integer count of minor units (plan §2.1)"),
    (r"\bparseFloat\b",
     "no float parsing of money (plan §2.3)"),
    (r"\btoFixed\b",
     "no fixed-point money strings (plan §2.3)"),
)

LINTER_PATH = Path(__file__).resolve()

# Token kinds whose *text* is not code: comments and string literals. Blanking
# these before matching is what makes the scan read code rather than prose.
# FSTRING_* exist from Python 3.12; the embedded expressions of an f-string
# tokenize as ordinary code and are deliberately left unmasked.
_NON_CODE_TOKENS = frozenset(
    value for name in ("COMMENT", "STRING", "FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")
    if (value := getattr(token, name, None)) is not None
)


def code_only(source: str) -> str:
    """Return ``source`` with comment and string-literal text blanked to spaces.

    The banned-token scan has to see code, not prose. ``app/money.py``'s module
    docstring names ``Decimal``, ``parseFloat`` and ``toFixed`` precisely to
    state that they never touch money; a raw-text matcher reads that as a
    violation and the only fix a text matcher leaves is deleting the sentence,
    which is worse than the false positive.

    Blanking preserves length and newlines, so every surviving character keeps
    its original line and column and a finding still reports the real line
    number. Only the token is blanked, never the trailing newline, so the line
    count is unchanged. On a tokenization error the raw source is returned:
    callers only reach this for files that already compiled, so that path is
    defensive rather than expected.
    """
    line_starts = [0]
    for line in source.splitlines(keepends=True):
        line_starts.append(line_starts[-1] + len(line))

    masked = list(source)
    try:
        stream = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in stream:
            if tok.type not in _NON_CODE_TOKENS:
                continue
            (srow, scol), (erow, ecol) = tok.start, tok.end
            start = line_starts[srow - 1] + scol
            end = line_starts[erow - 1] + ecol
            for index in range(start, end):
                if masked[index] != "\n":
                    masked[index] = " "
    except (tokenize.TokenError, IndentationError):
        return source
    return "".join(masked)


def check_compiles(path: Path, source: str) -> list[str]:
    """Parse and compile. A syntax error is the loudest possible lint finding."""
    try:
        tree = ast.parse(source, filename=str(path))
        compile(tree, str(path), "exec")
    except SyntaxError as exc:
        return [f"{path}:{exc.lineno}: syntax error: {exc.msg}"]
    return []


def check_future_import(path: Path, tree: ast.Module) -> list[str]:
    body = list(tree.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]  # module docstring
    if not body:
        return [f"{path}: no module body"]
    first = body[0]
    if isinstance(first, ast.ImportFrom) and first.module == FUTURE_IMPORT_MODULE:
        if any(alias.name == FUTURE_IMPORT_NAME for alias in first.names):
            return []
    return [f"{path}: missing `from __future__ import annotations` (plan §1.2)"]


def check_banned(path: Path, source: str) -> list[str]:
    findings = []
    for lineno, line in enumerate(code_only(source).splitlines(), start=1):
        for pattern, why in BANNED:
            if re.search(pattern, line):
                findings.append(f"{path}:{lineno}: banned token /{pattern}/ — {why}")
    return findings


def check_connection_ownership(path: Path, source: str) -> list[str]:
    findings = []
    for lineno, line in enumerate(code_only(source).splitlines(), start=1):
        if "sqlite3.connect" in line and path != CONNECTION_FACTORY:
            findings.append(
                f"{path}:{lineno}: sqlite3.connect outside {CONNECTION_FACTORY} "
                "(plan §2.1: db.py is the only connection factory)")
        if re.search(r"\bimport\s+sqlite3\b", line) and path.parts[0] == "api":
            findings.append(
                f"{path}:{lineno}: api/ opens no database connection of its own "
                "(plan §T3.1)")
    return findings


def check_indentation(path: Path, source: str) -> list[str]:
    findings = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        if not line.strip():
            continue
        leading = line[: len(line) - len(line.lstrip(" \t"))]
        if "\t" in leading:
            findings.append(f"{path}:{lineno}: tab character in indentation")
    return findings


def lint_file(path: Path) -> list[str]:
    if path.resolve() == LINTER_PATH:
        return []
    source = path.read_text(encoding="utf-8")
    findings: list[str] = []
    compile_findings = check_compiles(path, source)
    findings += compile_findings
    if compile_findings:
        return findings
    tree = ast.parse(source, filename=str(path))
    findings += check_future_import(path, tree)
    findings += check_banned(path, source)
    findings += check_connection_ownership(path, source)
    findings += check_indentation(path, source)
    return findings


def main() -> int:
    paths = sorted(p for directory in TREE for p in Path(directory).rglob("*.py")
                   if p.is_file())
    if not paths:
        print(f"lint: no Python modules found under {TREE}", file=sys.stderr)
        return 1

    findings: list[str] = []
    for path in paths:
        findings += lint_file(path)

    print(f"lint: checked {len(paths)} modules under {TREE}")
    if findings:
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        print(f"lint: {len(findings)} finding(s)", file=sys.stderr)
        return 1
    print("lint: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
