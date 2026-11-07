"""`dt_cloud.bench.query`: the Python port of the site's query syntax,
predicate and index plan. The case tables mirror
`site/functions/_lib/querySyntax.test.ts` and `searchQuery.test.ts`."""

import pytest

from dt_cloud.bench.query import (
    Ast,
    Branch,
    QueryError,
    compile_query,
    glob,
    monotone,
    neg_sql,
    parse,
    parse_simple,
    plan_negative,
    plan_positive,
    plan_sql,
    pos_sql,
    regex,
    sub,
    term_branch,
)


def ast(alts, neg=()):
    return Ast(tuple(tuple(a) for a in alts), tuple(neg))


@pytest.mark.parametrize(
    "q, want",
    [
        ("tomat", ast([[sub("tomat")]])),
        ("TTL", ast([[sub("ttl")]])),
        ("ckpt|tmp", ast([[sub("ckpt")], [sub("tmp")]])),
        ("abc def|ghi", ast([[sub("abc"), sub("def")], [sub("ghi")]])),
        ("ckpt -run", ast([[sub("ckpt")]], [sub("run")])),
        ("-x", ast([[]], [sub("x")])),
        ("-x -y", ast([[]], [sub("x"), sub("y")])),
        ("ckpt -x*y", ast([[sub("ckpt")]], [glob("x", "y")])),
        ("a*ckpt", ast([[glob("a", "ckpt")]])),
        ("*.pt", ast([[glob("", ".pt")]])),
        ("tmp/*/ckpt", ast([[glob("tmp/", "/ckpt")]])),
        ('"a b"', ast([[sub("a b")]])),
        ('"-draft"', ast([[sub("-draft")]])),
        ('-"a b"', ast([[]], [sub("a b")])),
        ('"a|b"', ast([[sub("a|b")]])),
        ('"x*y"', ast([[sub("x*y")]])),
        ('"open', ast([[sub("open")]])),
        ("ttl|", ast([[sub("ttl")]])),
        ("abc | -x", ast([[sub("abc")], []], [sub("x")])),
        ("/ckpt.*final/", ast([[regex("ckpt.*final")]])),
        ("/gr/", ast([[regex("gr")]])),
    ],
)
def test_parse_simple(q, want):
    assert parse_simple(q) == want


def test_parse_nothing_and_errors():
    assert [parse_simple(q) for q in ("", "  ", "|", "||", '""')] == [None] * 5
    errs = []
    for q in ("gr", "ckpt gr", "a*b*cd", '"ab"', "ckpt - x", "/a(/"):
        with pytest.raises(QueryError) as e:
            parse_simple(q)
        errs.append(e.value.code)
    assert errs == ["short-term"] * 5 + ["invalid-regex"]
    with pytest.raises(QueryError) as e:
        parse_simple("gr")
    assert str(e.value) == "type at least 3 characters (“gr”)"


def test_parse_regex_syntax():
    assert [parse("ckpt.*final", "regex"), parse("  ^bk/tmp/ ", "regex"), parse("a|b -x", "regex"), parse(" ", "regex")] == [
        ast([[regex("ckpt.*final")]]),
        ast([[regex("^bk/tmp/")]]),
        ast([[regex("a|b -x")]]),
        None,
    ]
    with pytest.raises(QueryError) as e:
        parse("x", "glob")
    assert (e.value.code, str(e.value)) == ("unknown-syntax", "unknown query syntax 'glob' (want simple|regex)")


def test_predicate():
    p = compile_query(parse_simple("ckpt -tmp|grug"))
    paths = ["bk/ckpt", "bk/tmp/ckpt", "bk/runs/grug", "bk/GRUG-x/tmp", "bk/x", "bk/ckpt-final.pt"]
    assert [p(x) for x in paths] == [True, False, True, False, False, True]
    assert [p.pos(x) for x in paths] == [True, True, True, True, False, True]
    only_neg = compile_query(parse_simple("-tmp"))
    assert [only_neg(x) for x in ("", "bk", "bk/tmp", "bk/TMP2")] == [True, True, False, False]
    g = compile_query(parse_simple("ckpt*final"))
    assert [g(x) for x in ("a/ckpt-run-b-final", "a/ckpt/final", "a/CKPTFINAL")] == [True, False, True]
    r = compile_query(parse("\\.safetensors$", "regex"))
    assert [r(x) for x in ("a/m.SafeTensors", "a/m.safetensors/x")] == [True, False]


def test_monotone():
    assert [monotone(parse_simple(q)) for q in ("ckpt -tmp", "*.pt", "/x.*y/")] == [True, True, False]


def accepts(q: str, names: list[str]) -> list[str]:
    import re

    bs = plan_positive(parse_simple(q, min_term=1))

    def ok(b: Branch, n: str) -> bool:
        n = n.lower()
        return n.startswith(b.arg) if b.op == "starts" else b.arg in n if b.op == "contains" else re.search(b.arg, n) is not None

    return [n for n in names if any(ok(b, n) for b in bs)]


def test_term_branch():
    assert [term_branch(m) for m in (sub("ttl"), sub("run-a/ckpt"), glob("ckpt", "final"), glob("tmp/", "/ck", "pt"), glob("tmp/run", "ckpt"), sub("ckpt/"), regex("x"))] == [
        Branch("contains", "ttl"),
        Branch("starts", "ckpt"),
        Branch("regex", "ckpt[^/]*final"),
        Branch("regex", "^ck[^/]*pt"),
        Branch("regex", "^run[^/]*ckpt"),
        None,
        None,
    ]
    assert accepts("TTL", ["ttl=7d", "iris-TTL", "tl", "x"]) == ["ttl=7d", "iris-TTL"]
    assert accepts("ckpt final", ["run-final", "ckpt", "x"]) == ["run-final", "ckpt"]
    assert accepts("run-a/CKPT", ["ckpt", "ckpt-final.pt", "x-ckpt"]) == ["ckpt", "ckpt-final.pt"]
    assert accepts("ckpt*final", ["ckpt-run-b-final", "CKPTFINAL", "final-ckpt", "ckpt"]) == ["ckpt-run-b-final", "CKPTFINAL"]
    assert accepts("tmp/*/ck*pt", ["ckpt", "ck-x-pt", "x-ckpt"]) == ["ckpt", "ck-x-pt"]
    assert accepts("tmp/run*ckpt", ["run-a-ckpt", "a-run-ckpt"]) == ["run-a-ckpt"]


def test_plans():
    assert [plan_positive(parse_simple(q)) for q in ("ckpt/", "ttl|ckpt/", "-x", "abc|-x", "/ttl/")] == [None] * 5
    assert plan_negative(parse_simple("ckpt -tmp -run*b")) == [Branch("contains", "tmp"), Branch("regex", "run[^/]*b")]
    assert plan_negative(parse_simple("ckpt")) is None


def test_sql():
    a = parse_simple("ckpt*fin's -tmp|x/y")
    assert pos_sql(a) == "((regexp_matches(lp, 'ckpt[^/]*fin''s')) OR (contains(lp, 'x/y')))"
    assert neg_sql(a) == "(contains(lp, 'tmp'))"
    assert plan_sql(plan_positive(a)) == "(regexp_matches(l, 'ckpt[^/]*fin''s') OR starts_with(l, 'y'))"
    assert [pos_sql(parse_simple("-x")), neg_sql(parse_simple("abc")), plan_sql(None)] == ["true", "false", "false"]
    assert pos_sql(parse("a\\.b$", "regex")) == "((regexp_matches(path, 'a\\.b$', 'i')))"
