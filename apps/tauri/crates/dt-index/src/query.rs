//! The path filter's query (`q=` in syntax `qs=`): `site/functions/_lib/`
//! `querySyntax.ts` (parsing), `queryAst.ts` (the AST) and `pathQuery.ts`
//! (the predicate). Every test is case-insensitive, over a node's full index
//! path.

use regex::{Regex, RegexBuilder};

/// The fewest literal characters in a row a positive `simple` term needs.
pub const MIN_TERM: usize = 3;

#[derive(Clone, Debug)]
pub enum Matcher {
    /// A substring of the lowercased path.
    Sub(String),
    /// Literal pieces joined by "any characters within one segment".
    Glob(Vec<String>),
    /// A regex (case-insensitive) over the path.
    Regex(String),
}

/// `(OR of AND-groups of positive matchers) AND NOT (any negative matcher)`.
#[derive(Clone, Debug)]
pub struct Ast {
    pub alts: Vec<Vec<Matcher>>,
    pub neg: Vec<Matcher>,
}

enum Test {
    Sub(String),
    Re(Regex),
    /// Over the lowercased path.
    LowerRe(Regex),
}

impl Test {
    fn of(m: &Matcher) -> Test {
        match m {
            Matcher::Sub(s) => Test::Sub(s.clone()),
            Matcher::Glob(ps) => {
                // Unanchored, so a `*` at either end matches nothing extra
                // (and a leading `[^/]*` makes the search quadratic).
                let lo = ps.iter().position(|p| !p.is_empty()).unwrap_or(ps.len());
                let hi = ps.iter().rposition(|p| !p.is_empty()).map_or(lo, |i| i + 1);
                match &ps[lo..hi.max(lo)] {
                    [one] => Test::Sub(one.clone()),
                    inner => Test::LowerRe(Regex::new(&inner.iter().map(|p| regex::escape(p)).collect::<Vec<_>>().join("[^/]*")).unwrap()),
                }
            }
            Matcher::Regex(src) => Test::Re(RegexBuilder::new(src).case_insensitive(true).build().unwrap()),
        }
    }

    fn test(&self, path: &str, lower: &str) -> bool {
        match self {
            Test::Sub(s) => lower.contains(s.as_str()),
            Test::Re(r) => r.is_match(path),
            Test::LowerRe(r) => r.is_match(lower),
        }
    }
}

/// A compiled query: `pos ∧ ¬neg` on the full path.
pub struct Query {
    pub ast: Ast,
    alts: Vec<Vec<Test>>,
    negs: Vec<Test>,
}

impl Query {
    pub fn new(ast: Ast) -> Query {
        let alts = ast.alts.iter().map(|a| a.iter().map(Test::of).collect()).collect();
        let negs = ast.neg.iter().map(Test::of).collect();
        Query { ast, alts, negs }
    }

    /// Every test is a substring / glob: once a path holds one, so does
    /// every path below it.
    pub fn monotone(&self) -> bool {
        !self.ast.alts.iter().flatten().chain(&self.ast.neg).any(|m| matches!(m, Matcher::Regex(_)))
    }

    pub fn has_neg(&self) -> bool {
        !self.negs.is_empty()
    }

    pub fn pos(&self, path: &str) -> bool {
        let l = path.to_lowercase();
        self.alts.iter().any(|a| a.iter().all(|t| t.test(path, &l)))
    }

    pub fn neg(&self, path: &str) -> bool {
        if self.negs.is_empty() {
            return false;
        }
        let l = path.to_lowercase();
        self.negs.iter().any(|t| t.test(path, &l))
    }

    pub fn matches(&self, path: &str) -> bool {
        self.pos(path) && !self.neg(path)
    }
}

fn regex_error(src: &str) -> Option<String> {
    RegexBuilder::new(src).case_insensitive(true).build().err().map(|e| format!("invalid regex: {e}"))
}

fn pieces_matcher(mut pieces: Vec<String>) -> Matcher {
    if pieces.len() == 1 { Matcher::Sub(pieces.pop().unwrap()) } else { Matcher::Glob(pieces) }
}

/// `simple`: GitHub-search-like terms (`a b` AND, `a|b` OR, `-x` NOT, `*`
/// within one segment, `"…"` literal, `/…/` a whole-query regex).
pub fn parse_simple(q: &str, min_term: usize) -> Result<Option<Ast>, String> {
    let raw = q.trim();
    if raw.is_empty() {
        return Ok(None);
    }
    if raw.chars().count() > 2 && raw.starts_with('/') && raw.ends_with('/') {
        let src = &raw[1..raw.len() - 1];
        return match regex_error(src) {
            Some(e) => Err(e),
            None => Ok(Some(Ast { alts: vec![vec![Matcher::Regex(src.into())]], neg: vec![] })),
        };
    }
    let s: Vec<char> = raw.to_lowercase().chars().collect();
    let sep = |c: char| c == '|' || c.is_whitespace();
    let mut alts: Vec<(Vec<Matcher>, bool)> = vec![(vec![], false)];
    let mut neg = vec![];
    let mut i = 0;
    while i < s.len() {
        let c = s[i];
        if c.is_whitespace() {
            i += 1;
            continue;
        }
        if c == '|' {
            alts.push((vec![], false));
            i += 1;
            continue;
        }
        let mut is_neg = false;
        if c == '-' && i + 1 < s.len() && !sep(s[i + 1]) {
            is_neg = true;
            i += 1;
        }
        let mut pieces = vec![String::new()];
        let mut quoted = false;
        while i < s.len() && (quoted || !sep(s[i])) {
            let ch = s[i];
            i += 1;
            if ch == '"' {
                quoted = !quoted;
            } else if ch == '*' && !quoted {
                pieces.push(String::new());
            } else {
                pieces.last_mut().unwrap().push(ch);
            }
        }
        let alt = alts.last_mut().unwrap();
        alt.1 = true;
        if pieces.len() == 1 && pieces[0].is_empty() {
            continue;
        }
        if is_neg {
            neg.push(pieces_matcher(pieces));
        } else if pieces.iter().map(|p| p.chars().count()).max().unwrap_or(0) < min_term {
            return Err(format!("type at least {min_term} characters (“{}”)", pieces.join("*")));
        } else {
            alt.0.push(pieces_matcher(pieces));
        }
    }
    let kept: Vec<Vec<Matcher>> = alts.into_iter().filter(|a| a.1).map(|a| a.0).collect();
    if !kept.iter().any(|a| !a.is_empty()) && neg.is_empty() {
        return Ok(None);
    }
    Ok(Some(Ast { alts: if kept.is_empty() { vec![vec![]] } else { kept }, neg }))
}

/// `regex`: the whole query one regex over the full path.
pub fn parse_regex(q: &str) -> Result<Option<Ast>, String> {
    let src = q.trim();
    if src.is_empty() {
        return Ok(None);
    }
    match regex_error(src) {
        Some(e) => Err(e),
        None => Ok(Some(Ast { alts: vec![vec![Matcher::Regex(src.into())]], neg: vec![] })),
    }
}

/// `q` in syntax `qs` (default `simple`) → the query, `None` for no filter.
pub fn parse(q: &str, qs: Option<&str>) -> Result<Option<Query>, String> {
    let ast = match qs.filter(|s| !s.is_empty()) {
        None | Some("simple") => parse_simple(q, MIN_TERM)?,
        Some("regex") => parse_regex(q)?,
        Some(other) => return Err(format!("unknown query syntax '{other}' (want simple|regex)")),
    };
    Ok(ast.map(Query::new))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn q(s: &str) -> Query {
        parse(s, None).unwrap().unwrap()
    }

    #[test]
    fn simple_terms() {
        let t = q("ckpt -tmp");
        assert_eq!([t.matches("a/ckpt/x"), t.matches("a/tmp/ckpt"), t.matches("a/b")], [true, false, false]);
        let t = q("foo bar|baz");
        assert_eq!([t.matches("x/foo/bar"), t.matches("x/foo"), t.matches("x/BAZ")], [true, false, true]);
        let t = q("*.safetensors");
        assert_eq!([t.matches("a/m.safetensors"), t.matches("a/m/x.safetensors"), t.matches("a.b/.safetensors")], [true, true, true]);
        let t = q("run*ckpt");
        assert_eq!([t.matches("a/run-1-ckpt"), t.matches("a/run/ckpt")], [true, false]);
        let t = q("\"a b c\"");
        assert_eq!([t.matches("x/a b c"), t.matches("x/a/b/c")], [true, false]);
        let t = q("-node_modules");
        assert_eq!([t.matches("a/b"), t.matches("a/node_modules/x")], [true, false]);
        let t = q("/^users/.*\\.jpg$/");
        assert_eq!([t.matches("Users/r/a.JPG"), t.matches("x/Users/a.jpg")], [true, false]);
    }

    #[test]
    fn parse_results() {
        assert!(parse("  ", None).unwrap().is_none());
        assert!(parse("|", None).unwrap().is_none());
        assert_eq!(parse("ab", None).err(), Some("type at least 3 characters (“ab”)".to_string()));
        assert_eq!(parse("x", Some("nope")).err(), Some("unknown query syntax 'nope' (want simple|regex)".to_string()));
        assert_eq!(parse("a(", Some("regex")).err().map(|e| e.starts_with("invalid regex: ")), Some(true));
        assert_eq!(parse("ab", Some("regex")).unwrap().unwrap().matches("xAB"), true);
    }
}
