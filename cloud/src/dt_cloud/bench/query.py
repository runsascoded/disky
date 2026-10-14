"""The path filter's query language, in Python: a port of the site's
`querySyntax.ts` (`simple`, `regex`), `pathQuery.ts` (`compileQuery`) and
`searchQuery.ts` (`termBranch`, `planPositive` / `planNegative`), plus their
DuckDB renderings — what the ground truth (`truth.py`) evaluates.

Semantics (specs/path-store-search.md §1): a query is `pos ∧ ¬neg` on the
lowercased full index path (`bucket/dir/sub`); `pos` is an OR of AND-groups of
matchers, `neg` an OR of matchers (the whole query's). A matcher is `sub` (a
substring), `glob` (literal pieces joined by `[^/]*`) or `regex` (a full-path
regex, case-insensitive). The TS side is the reference; `test_bench_query.py`
mirrors its case tables.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

MIN_TERM = 3


@dataclass(frozen=True)
class Matcher:
    kind: str  # 'sub' | 'glob' | 'regex'
    text: str = ""  # sub
    pieces: tuple[str, ...] = ()  # glob (≥ 2)
    source: str = ""  # regex

    def to_json(self) -> dict:
        if self.kind == "sub":
            return {"kind": "sub", "text": self.text}
        if self.kind == "glob":
            return {"kind": "glob", "pieces": list(self.pieces)}
        return {"kind": "regex", "source": self.source}


def sub(text: str) -> Matcher:
    return Matcher("sub", text=text)


def glob(*pieces: str) -> Matcher:
    return Matcher("glob", pieces=tuple(pieces))


def regex(source: str) -> Matcher:
    return Matcher("regex", source=source)


@dataclass(frozen=True)
class Ast:
    alts: tuple[tuple[Matcher, ...], ...]
    neg: tuple[Matcher, ...]

    def to_json(self) -> dict:
        return {"alts": [[m.to_json() for m in a] for a in self.alts], "neg": [m.to_json() for m in self.neg]}


class QueryError(ValueError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def _regex_ast(source: str) -> Ast:
    try:
        re.compile(source, re.IGNORECASE)
    except re.error as e:
        raise QueryError(f"invalid regex: {e}", "invalid-regex") from e
    return Ast(((regex(source),),), ())


def _pieces_matcher(pieces: list[str]) -> Matcher:
    return sub(pieces[0]) if len(pieces) == 1 else glob(*pieces)


def parse_simple(q: str, min_term: int = MIN_TERM) -> Ast | None:
    """`querySyntax.ts` `makeSimple().parse`: the AST, None for "nothing to
    filter by"; raises `QueryError` (`short-term`, `invalid-regex`)."""
    raw = q.strip()
    if not raw:
        return None
    if len(raw) > 2 and raw.startswith("/") and raw.endswith("/"):
        return _regex_ast(raw[1:-1])
    s = raw.lower()
    alts: list[dict] = [{"terms": [], "any": False}]
    neg: list[Matcher] = []

    def sep(c: str) -> bool:
        return c == "|" or c.isspace()

    i = 0
    while i < len(s):
        c = s[i]
        if c.isspace():
            i += 1
            continue
        if c == "|":
            alts.append({"terms": [], "any": False})
            i += 1
            continue
        is_neg = False
        if c == "-" and i + 1 < len(s) and not sep(s[i + 1]):
            is_neg = True
            i += 1
        pieces = [""]
        quoted = False
        while i < len(s) and (quoted or not sep(s[i])):
            ch = s[i]
            i += 1
            if ch == '"':
                quoted = not quoted
            elif ch == "*" and not quoted:
                pieces.append("")
            else:
                pieces[-1] += ch
        alt = alts[-1]
        alt["any"] = True
        if len(pieces) == 1 and not pieces[0]:
            continue
        if is_neg:
            neg.append(_pieces_matcher(pieces))
        elif max(len(p) for p in pieces) < min_term:
            raise QueryError(f"type at least {min_term} characters (“{'*'.join(pieces)}”)", "short-term")
        else:
            alt["terms"].append(_pieces_matcher(pieces))
    kept = [tuple(a["terms"]) for a in alts if a["any"]]
    if not any(kept) and not neg:
        return None
    return Ast(tuple(kept) if kept else ((),), tuple(neg))


def parse_regex(q: str) -> Ast | None:
    source = q.strip()
    return _regex_ast(source) if source else None


SYNTAXES: dict[str, Callable[[str], Ast | None]] = {"simple": parse_simple, "regex": parse_regex}


def parse(q: str, qs: str = "simple") -> Ast | None:
    if qs not in SYNTAXES:
        raise QueryError(f"unknown query syntax '{qs}' (want {'|'.join(SYNTAXES)})", "unknown-syntax")
    return SYNTAXES[qs](q)


# --- the predicate (`pathQuery.ts`) -------------------------------------------

_ESC = re.compile(r"[.*+?^${}()|\[\]\\]")


def esc(s: str) -> str:
    """JS's regex escape of a literal (`pathQuery.ts` `esc`); valid RE2 too."""
    return _ESC.sub(lambda m: "\\" + m.group(0), s)


def glob_re(pieces: tuple[str, ...] | list[str]) -> str:
    return "[^/]*".join(esc(p) for p in pieces)


def matcher_test(m: Matcher) -> Callable[[str, str], bool]:
    """(path, lowercased path) → whether `m` holds."""
    if m.kind == "sub":
        return lambda _p, l: m.text in l
    if m.kind == "glob":
        r = re.compile(glob_re(m.pieces))
        return lambda _p, l: r.search(l) is not None
    r = re.compile(m.source, re.IGNORECASE)
    return lambda p, _l: r.search(p) is not None


@dataclass(frozen=True)
class Pred:
    pos: Callable[[str], bool]
    neg: Callable[[str], bool] | None

    def __call__(self, path: str) -> bool:
        return self.pos(path) and not (self.neg and self.neg(path))


def compile_query(ast: Ast) -> Pred:
    alts = [[matcher_test(m) for m in a] for a in ast.alts]
    negs = [matcher_test(m) for m in ast.neg]

    def pos(path: str) -> bool:
        l = path.lower()
        return any(all(t(path, l) for t in a) for a in alts)

    def neg(path: str) -> bool:
        l = path.lower()
        return any(t(path, l) for t in negs)

    return Pred(pos, neg if negs else None)


def monotone(ast: Ast) -> bool:
    """Substring / glob matchers hold on every extension of a path they hold
    on; a regex need not (`\\.json$`)."""
    return not any(m.kind == "regex" for a in ast.alts for m in a) and not any(m.kind == "regex" for m in ast.neg)


# --- the index plan (`searchQuery.ts`): candidate last segments ----------------


@dataclass(frozen=True)
class Branch:
    """What a match root's (or excluded path's) last segment must satisfy for
    the term to have become true there: `contains` / `starts` a literal, or
    `regex` (anchored or not) over the lowercased segment."""

    op: str  # 'contains' | 'starts' | 'regex'
    arg: str


def term_branch(m: Matcher) -> Branch | None:
    """`searchQuery.ts` `termBranch`: None for a regex matcher or a term that
    ends in `/` (it constrains no segment)."""
    if m.kind == "regex":
        return None
    pieces = [m.text] if m.kind == "sub" else list(m.pieces)
    k = len(pieces) - 1
    while k >= 0 and "/" not in pieces[k]:
        k -= 1
    anchored = k >= 0
    tail = [pieces[k][pieces[k].rfind("/") + 1 :], *pieces[k + 1 :]] if anchored else pieces
    if anchored and len(tail) == 1 and not tail[0]:
        return None
    if len(tail) == 1:
        return Branch("starts" if anchored else "contains", tail[0])
    return Branch("regex", ("^" if anchored else "") + glob_re(tail))


def plan_terms(terms: tuple[Matcher, ...] | list[Matcher]) -> list[Branch] | None:
    out = []
    for t in terms:
        b = term_branch(t)
        if b is None:
            return None
        out.append(b)
    return out or None


def plan_positive(ast: Ast) -> list[Branch] | None:
    """None when some alternative has no positive term (`pos` is everywhere
    true) or a matcher is unplannable."""
    if any(not a for a in ast.alts):
        return None
    return plan_terms([m for a in ast.alts for m in a])


def plan_negative(ast: Ast) -> list[Branch] | None:
    return plan_terms(ast.neg)


def pos_everywhere(ast: Ast) -> bool:
    return any(not a for a in ast.alts)


# --- DuckDB renderings -----------------------------------------------------------


def lit(s: str) -> str:
    """A SQL string literal."""
    return "'" + s.replace("'", "''") + "'"


def matcher_sql(m: Matcher, path: str = "path", lower: str = "lp") -> str:
    """`m` on a row, given its path column and that column lowercased."""
    if m.kind == "sub":
        return f"contains({lower}, {lit(m.text)})"
    if m.kind == "glob":
        return f"regexp_matches({lower}, {lit(glob_re(m.pieces))})"
    return f"regexp_matches({path}, {lit(m.source)}, 'i')"


def pos_sql(ast: Ast, path: str = "path", lower: str = "lp") -> str:
    if pos_everywhere(ast):
        return "true"
    return "(" + " OR ".join("(" + " AND ".join(matcher_sql(m, path, lower) for m in a) + ")" for a in ast.alts) + ")"


def neg_sql(ast: Ast, path: str = "path", lower: str = "lp") -> str:
    if not ast.neg:
        return "false"
    return "(" + " OR ".join(matcher_sql(m, path, lower) for m in ast.neg) + ")"


def branch_sql(b: Branch, name: str = "l") -> str:
    """A branch's test on a lowercased-name column."""
    if b.op == "contains":
        return f"contains({name}, {lit(b.arg)})"
    if b.op == "starts":
        return f"starts_with({name}, {lit(b.arg)})"
    return f"regexp_matches({name}, {lit(b.arg)})"


def plan_sql(branches: list[Branch] | None, name: str = "l") -> str:
    if not branches:
        return "false"
    return "(" + " OR ".join(branch_sql(b, name) for b in branches) + ")"
