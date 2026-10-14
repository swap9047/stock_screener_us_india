"""Every tracked .py file must parse on Python 3.11, which CI runs.

2026-10-14: a chart title used f"...{_when(df, "INDIA")}..." -- a string inside
an f-string reusing the f-string's own quote. Python 3.12 (PEP 701) accepts
that; 3.11 raises SyntaxError, so CI went red on every check that parses
app.py, while every local run (3.12+) was green.

On 3.11 the file is simply compiled. On 3.12+ that would pass, so the
tokenizer is used instead: inside an f-string, a nested string (or f-string)
that opens with an enclosing f-string's quote is the 3.12-only form.

Offline: reads only this repo's sources.
"""

import io
import subprocess
import sys
import tokenize
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


def _quote(token_text):
    body = token_text.lstrip("rRfFbBuU")
    return body[:3] if body[:3] in ('"""', "'''") else body[:1]


def reused_quotes(src):
    """(line, text) of each string nested in an f-string that opens with an
    enclosing f-string's quote. Needs Python 3.12+ (FSTRING_* tokens)."""
    hits, stack = [], []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.FSTRING_START:
            q = _quote(tok.string)
            if q in stack:
                hits.append((tok.start[0], tok.string))
            stack.append(q)
        elif tok.type == tokenize.FSTRING_END:
            stack.pop()
        elif stack and tok.type == tokenize.STRING and _quote(tok.string) in stack:
            hits.append((tok.start[0], tok.string))
    return hits


files = [f for f in subprocess.run(["git", "ls-files", "*.py"], cwd=REPO, capture_output=True,
                                   text=True).stdout.split() if f]
check(len(files) > 20, f"found the repo's Python files ({len(files)})")

if sys.version_info < (3, 12):
    bad = []
    for f in files:
        try:
            compile((REPO / f).read_text(), f, "exec")
        except SyntaxError as e:
            bad.append(f"{f}:{e.lineno} {e.msg}")
    check(not bad, f"every file compiles on {sys.version.split()[0]} ({bad})")
else:
    bad = [f"{f}:{line} {text}" for f in files for line, text in reused_quotes((REPO / f).read_text())]
    check(not bad, f"no f-string reuses its own quote inside -- a SyntaxError on 3.11 ({bad})")
    sample = 'x = f"a {g(df, "INDIA")} b"\ny = f"a {g(df, \'US\')} b"\n'
    check([t for _, t in reused_quotes(sample)] == ['"INDIA"'],
          "the scan catches the 2026-10-14 form and allows the other quote")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
