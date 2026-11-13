"""`scripts/branch-harmony`: own deltas, lag, file flags, rules and blame attribution on a synthetic repo."""
from datetime import datetime, timezone
from os import environ
from pathlib import Path
from runpy import run_path
from subprocess import run

import pytest

bh = run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "branch-harmony"))

RULES = [
    {"paths": ["site/wrangler.toml"], "class": "intrinsic", "reason": "config"},
    {"paths": ["job/*"], "lanes": ["a"], "class": "accidental", "reason": "dup"},
]


VIEW = "".join(f"{c}\n" for c in "abcdefghij")


class Repo:
    def __init__(self, path: Path):
        self.path = path
        self.t = 1_700_000_000
        self.env = {**environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
        self.git("init", "-q", "-b", "cloud")
        self.git("config", "user.name", "T")
        self.git("config", "user.email", "t@example.com")

    def git(self, *args: str) -> str:
        return run(["git", *args], cwd=self.path, env=self.env, check=True, capture_output=True, text=True).stdout

    def commit(self, msg: str, files: dict[str, str | None]) -> str:
        for p, text in files.items():
            f = self.path / p
            if text is None:
                self.git("rm", "-q", p)
                continue
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
            self.git("add", p)
        self.t += 3600
        date = f"@{self.t} +0000"
        run(["git", "commit", "-q", "-m", msg], cwd=self.path, check=True,
            env={**self.env, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
        return self.git("rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    r = Repo(tmp_path)
    r.commit("base", {
        "site/src/view.ts": VIEW,
        "site/src/fmt.ts": "x = 1\n",
        "README.md": "readme\n",
        "specs/s.md": "spec\n",
    })
    r.git("branch", "a")
    r.git("branch", "b")
    r.git("checkout", "-q", "a")
    r.commit("a: config", {"site/wrangler.toml": "name = 'a'\n"})
    r.commit("[cloud] a: view fix", {"site/src/view.ts": VIEW.replace("b\n", "B\n") + "k\n"})
    r.commit("a: job helper", {"job/gen.py": "print(1)\n"})
    r.commit("a: reformat", {"site/src/fmt.ts": "x  =  1\n"})
    r.commit("a: spec", {"specs/s.md": "spec 2\n"})
    r.commit("a: shared", {"README.md": "readme a\n"})
    r.git("checkout", "-q", "b")
    r.commit("b: job helper", {"job/gen.py": "print(1)\n"})
    r.commit("b: shared", {"README.md": "readme b\n"})
    r.git("checkout", "-q", "cloud")
    r.commit("cloud: same reformat", {"site/src/fmt.ts": "x  =  1\n"})
    r.commit("cloud: more", {"site/src/other.ts": "o\no\n"})
    return r


LANES = {"a": ("a", "cloud"), "b": ("b", "cloud")}
REFS = {"cloud": "cloud"}


def rows(lane) -> list[tuple]:
    return sorted((f.path, f.status, f.add, f.dele, f.rule, tuple(f.flags)) for f in lane.files)


def test_measure_own_delta_lag_flags_and_rules(repo: Repo):
    a, b = bh["measure"](LANES, REFS, None, RULES, cwd=repo.path)
    assert rows(a) == [
        ("README.md", "M", 1, 1, "unclassified", ("shared",)),
        ("job/gen.py", "A", 1, 0, "accidental", ("dup",)),
        ("site/src/fmt.ts", "M", 1, 1, "lag", ("parent-equal", "leak")),
        ("site/src/view.ts", "M", 2, 1, "unclassified", ("leak",)),
        ("site/wrangler.toml", "A", 1, 0, "intrinsic", ()),
    ]
    assert rows(b) == [
        ("README.md", "M", 1, 1, "unclassified", ("shared",)),
        ("job/gen.py", "A", 1, 0, "unclassified", ("dup",)),
    ]
    assert (a.add, a.dele, a.lag_commits, a.lag_lines) == (6, 3, 2, 4)
    assert (b.add, b.dele, b.lag_commits, b.lag_lines) == (2, 1, 2, 4)
    rec = a.record(None, "now")
    assert {k: rec[k] for k in ("own", "surfaces", "classes", "flags", "lag")} == {
        "own": {"add": 6, "dele": 3, "lines": 9, "files": 5},
        "surfaces": {"job": 1, "other": 2, "site/other": 1, "site/src": 5},
        "classes": {"accidental": 1, "intrinsic": 1, "lag": 2, "unclassified": 5},
        "flags": {"dup": 1, "leak": 5, "parent-equal": 2, "shared": 2},
        "lag": {"commits": 2, "lines": 4, "oldest": rec["lag"]["oldest"]},
    }


def test_cosmetic_and_merge_only(repo: Repo):
    repo.git("checkout", "-q", "b")
    repo.commit("b: whitespace", {"site/src/view.ts": VIEW.replace("b\n", "b  \n")})
    repo.git("checkout", "-q", "cloud")
    repo.commit("cloud: view", {"site/src/view.ts": VIEW + "z\n"})
    repo.git("checkout", "-q", "b")
    # A merge that resolves `fmt.ts` to content neither side has: its delta comes from the merge alone.
    run(["git", "merge", "-q", "--no-commit", "cloud"], cwd=repo.path, env=repo.env, capture_output=True)
    (repo.path / "site/src/view.ts").write_text(VIEW.replace("b\n", "b  \n") + "z\n")
    (repo.path / "site/src/fmt.ts").write_text("x = 2\n")
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "merge cloud")
    (b,) = bh["measure"]({"b": LANES["b"]}, REFS, None, [], cwd=repo.path)
    assert rows(b) == [
        ("README.md", "M", 1, 1, "unclassified", ()),
        ("job/gen.py", "A", 1, 0, "unclassified", ()),
        ("site/src/fmt.ts", "M", 1, 1, "unclassified", ("leak", "merge-only")),
        ("site/src/view.ts", "M", 1, 1, "unclassified", ("cosmetic", "leak")),
    ]


def test_at_measures_first_parent_tips_before_a_time(repo: Repo):
    t = int(repo.git("log", "-1", "--format=%ct", "a~3").strip())
    at = datetime.fromtimestamp(t + 1, timezone.utc).isoformat()
    (a,) = bh["measure"]({"a": LANES["a"]}, REFS, at, RULES, cwd=repo.path)
    assert a.tip == repo.git("rev-parse", "a~3").strip()
    assert rows(a) == [
        ("job/gen.py", "A", 1, 0, "accidental", ()),
        ("site/src/view.ts", "M", 2, 1, "unclassified", ("leak",)),
        ("site/wrangler.toml", "A", 1, 0, "intrinsic", ()),
    ]
    assert (a.lag_commits, a.lag_lines) == (0, 0)


def test_pending_blames_surviving_leak_lines(repo: Repo):
    (a,) = bh["measure"]({"a": LANES["a"]}, REFS, None, RULES, cwd=repo.path)
    fix = repo.git("rev-parse", "a~4").strip()
    assert [(p.sha, p.subject, p.kind, p.lines, p.files) for p in bh["pending"](a, cwd=repo.path)] == [
        (fix, "[cloud] a: view fix", "tagged", 2, ["site/src/view.ts"]),
    ]


def test_renames_count_once_and_scope_excludes(repo: Repo):
    repo.git("checkout", "-q", "a")
    repo.git("mv", "site/src/view.ts", "site/src/view2.ts")
    repo.commit("a: rename", {"specs/t.md": "x\n", "pnpm-lock.yaml": "lock\n"})
    (a,) = bh["measure"]({"a": LANES["a"]}, REFS, None, RULES, cwd=repo.path)
    assert [(f.path, f.old, f.status, f.add, f.dele) for f in a.files if "view" in f.path] == [
        ("site/src/view2.ts", "site/src/view.ts", "R", 2, 1),
    ]
    assert sorted(f.path for f in a.files) == [
        "README.md", "job/gen.py", "site/src/fmt.ts", "site/src/view2.ts", "site/wrangler.toml",
    ]
