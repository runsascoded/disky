import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / 'site' / 'toml-check'


def run(*files: Path) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(SCRIPT), *map(str, files)], capture_output=True, text=True)
    return r.returncode, r.stderr


def test_good_file_passes(tmp_path: Path):
    f = tmp_path / 'wrangler.toml'
    f.write_text('name = "x"\n[vars]\nBATCH_REGION = "us-east1"\n')
    assert run(f) == (0, '')


def test_bad_file_names_file_and_line(tmp_path: Path):
    f = tmp_path / 'wrangler.toml'
    f.write_text('name = "x"\n[vars]\nBATCH_REGION = \\"us-east1\\"\n')
    assert run(f) == (1, f'toml-check: {f}:3:16: Invalid value (at line 3, column 16)\n')


def test_every_bad_file_is_reported(tmp_path: Path):
    good, bad1, bad2 = tmp_path / 'a.toml', tmp_path / 'b.toml', tmp_path / 'c.toml'
    good.write_text('a = 1\n')
    bad1.write_text('a = \n')
    bad2.write_text('[x\n')
    assert run(good, bad1, bad2) == (
        1,
        f'toml-check: {bad1}:1:5: Invalid value (at line 1, column 5)\n'
        f'toml-check: {bad2}:1:3: Expected \']\' at the end of a table declaration (at line 1, column 3)\n',
    )
