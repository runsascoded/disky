import json
from pathlib import Path
from runpy import run_path
from subprocess import run

SCRIPT = Path(__file__).resolve().parents[1] / "port-migration"
port_migration = run_path(str(SCRIPT))


def lineages(root: Path) -> Path:
    """`base/` (0001, 0002, 0003) and `other/` (0001, 0027, 0028; `0027` twins base's 0002)."""
    for name in ["base/0001_init.sql", "base/0002_alpha.sql", "base/0003_beta.sql", "other/0001_init.sql", "other/0027_alpha.sql", "other/0028_gamma.sql"]:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(f"-- {name}\nCREATE TABLE t_{Path(name).stem[5:]} (x INTEGER);\n")
    (root / "twins.json").write_text('[\n  ["base/0002_alpha.sql", "other/0027_alpha.sql"]\n]\n')
    return root


def cli(root: Path, *args: str):
    return run([str(SCRIPT), "-m", str(root), *args], capture_output=True, text=True)


def test_ports_into_the_next_number_and_records_a_new_twin(tmp_path: Path) -> None:
    root = lineages(tmp_path)
    r = cli(root, "base/0003_beta.sql", "other")
    assert [r.returncode, r.stdout, r.stderr] == [0, "other/0029_beta.sql\n", ""]
    assert (root / "other/0029_beta.sql").read_bytes() == (root / "base/0003_beta.sql").read_bytes()
    assert (root / "twins.json").read_text() == (
        '[\n'
        '  ["base/0002_alpha.sql", "other/0027_alpha.sql"],\n'
        '  ["base/0003_beta.sql", "other/0029_beta.sql"]\n'
        ']\n'
    )


def test_a_third_lineage_joins_the_existing_group(tmp_path: Path) -> None:
    root = lineages(tmp_path)
    (root / "third").mkdir()
    r = cli(root, "other/0027_alpha.sql", "third")
    assert [r.returncode, r.stdout] == [0, "third/0001_alpha.sql\n"]
    assert json.loads((root / "twins.json").read_text()) == [["base/0002_alpha.sql", "other/0027_alpha.sql", "third/0001_alpha.sql"]]


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    root = lineages(tmp_path)
    before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
    r = cli(root, "-n", "base/0003_beta.sql", "other")
    assert [r.returncode, r.stdout] == [0, "other/0029_beta.sql\n"]
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) == before
    assert (root / "twins.json").read_text() == '[\n  ["base/0002_alpha.sql", "other/0027_alpha.sql"]\n]\n'


def test_refusals(tmp_path: Path) -> None:
    root = lineages(tmp_path)
    cases = {
        ("base/0002_alpha.sql", "other"): "other already has a `alpha` migration: 0027_alpha.sql",
        ("base/0003_beta.sql", "base"): "base/0003_beta.sql: already in lineage base",
        ("base/0003_beta.sql", "nope"): f"nope: no such lineage (no directory {root.resolve() / 'nope'})",
        ("base/0009_missing.sql", "other"): f"base/0009_missing.sql: not a migration under {root.resolve()} (`<lineage>/<NNNN_name>.sql`)",
    }
    got = {}
    for (src, lineage) in cases:
        r = cli(root, src, lineage)
        got[(src, lineage)] = (r.returncode, r.stdout, r.stderr)
    assert got == {k: (1, "", f"port-migration: {v}\n") for k, v in cases.items()}


def test_next_name_keeps_the_lineage_width_and_starts_an_empty_one_at_0001(tmp_path: Path) -> None:
    (tmp_path / "wide").mkdir()
    (tmp_path / "wide/000009_x.sql").write_text("")
    (tmp_path / "empty").mkdir()
    next_name = port_migration["next_name"]
    assert [next_name(tmp_path / "wide", "y.sql"), next_name(tmp_path / "empty", "y.sql")] == ["000010_y.sql", "0001_y.sql"]
