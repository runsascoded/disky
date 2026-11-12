"""Every interval-store fixture: `fsb plan -x`'s exhaustive mix answered by `fsb bench` (1 and 4 threads, answers equal
across threads and runs), then each answer equal to the DuckDB oracle's. Run on a bench node (`cargo` and the repo's
venv), not the laptop."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
FIXTURES = HERE.parent.parent / "site/functions/_lib/fixtures/iv/interval-store"
FSB = HERE / "target/release/fsb"


@pytest.fixture(scope="session", autouse=True)
def build():
    subprocess.run(["cargo", "build", "-q", "--release"], cwd=HERE, check=True)


@pytest.mark.parametrize("gen", sorted(p.name for p in FIXTURES.iterdir() if p.is_dir()))
def test_fixture_equals_oracle(gen, tmp_path):
    g = FIXTURES / gen
    queries, results, answers = tmp_path / "q.jsonl", tmp_path / "r.jsonl", tmp_path / "a.jsonl"
    subprocess.run([FSB, "plan", g, "-x", "1", "-o", queries], check=True, capture_output=True)
    subprocess.run([FSB, "bench", g, queries, "-t", "1,4", "-r", "1", "-o", results, "-a", answers], check=True, capture_output=True)
    n = sum(1 for _ in queries.open())
    out = subprocess.run([sys.executable, HERE / "oracle/oracle.py", "-m", "2GB", "-t", "4", "-T", tmp_path / "duck", g, queries, answers],
                         capture_output=True, text=True)
    assert json.loads(out.stdout) == {"checked": n, "equal": n, "different": []}
    assert out.returncode == 0
