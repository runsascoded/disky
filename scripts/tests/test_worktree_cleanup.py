from pathlib import Path
from runpy import run_path
from subprocess import run


cleanup = run_path(str(Path(__file__).resolve().parents[1] / "worktree-cleanup"))


def test_disposable_paths_are_narrow() -> None:
    cases = {
        "node_modules/package/index.js": True,
        ".venv/bin/python": True,
        "cloud/.pytest_cache": True,
        "site/dist/index.html": True,
        "site/tsconfig.tsbuildinfo": True,
        "tmp/bench/d1-gcs-local.sqlite": True,
        "tmp/bench/d1-gcs-local.sqlite-shm": True,
        "tmp/bench/d1-gcs-local.sqlite-wal": True,
        ".envrc": False,
        "infra/.pulumi/state.json": False,
        "site/.wrangler/state/v3/d1/local.sqlite": False,
        "tmp/bench/results.json": False,
        "tmp/bench/experiment.py": False,
        "tmp/bench/another.sqlite": False,
        "tmp/d1-gcs-local.sqlite": False,
        "tmp/source.parquet": False,
        "dist/source.txt": False,
    }
    assert {p: cleanup["disposable"](Path(p)) for p in cases} == cases


def test_inventory_preserves_config_state_results_and_unknown_inputs(tmp_path: Path) -> None:
    run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / ".git/info/exclude").write_text(".envrc\nnode_modules/\nsite/dist/\nsite/.wrangler/\ntmp/\n")
    files = [
        ".envrc",
        "node_modules/package/index.js",
        "site/dist/index.html",
        "site/.wrangler/state.json",
        "tmp/bench/d1-gcs-local.sqlite",
        "tmp/bench/results.json",
        "tmp/input.parquet",
    ]
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n")
    keep, discard = cleanup["inventory"](tmp_path)
    assert keep == [
        Path(".envrc"),
        Path("site/.wrangler/state.json"),
        Path("tmp/bench/results.json"),
        Path("tmp/input.parquet"),
    ]
    assert discard == [
        Path("node_modules"),
        Path("site/dist"),
        Path("tmp/bench/d1-gcs-local.sqlite"),
    ]
