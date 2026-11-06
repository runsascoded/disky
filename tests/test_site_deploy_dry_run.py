import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SITE = Path(__file__).parent.parent / 'site'

CONFIG = '''name = "demo-site"  # the Pages project
[vars]
PROD_HOST = "demo.example.org"
DEV_HOST = "dev.demo.example.org"
'''


def make_site(tmp_path: Path) -> Path:
    """A private copy of the scripts: no `deploy.env` of the branch we run on leaks into the run."""
    site = tmp_path / 'site'
    site.mkdir()
    for name in ('deploy', 'toml-check'):
        shutil.copy(SITE / name, site / name)
    return site


def run(site: Path, *args: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    base = {
        'PATH': os.path.dirname(sys.executable) + os.pathsep + os.environ['PATH'],
        'HOME': str(site),
    }
    r = subprocess.run(
        [str(site / 'deploy'), *args],
        capture_output=True,
        text=True,
        env={**base, **(env or {})},
    )
    return r.returncode, r.stdout, r.stderr


def write_config(site: Path, text: str = CONFIG, name: str = 'wrangler.toml') -> Path:
    f = site / name
    f.write_text(text)
    return f


def test_dry_run_prod_prints_the_plan_and_needs_no_credentials(tmp_path: Path):
    site = make_site(tmp_path)
    write_config(site)
    assert run(site, '--dry-run', '-P') == (
        0,
        'deploy --dry-run: nothing built, deployed, moved or pushed\n'
        '  config:  wrangler.toml (parses)\n'
        '  project: demo-site\n'
        '  mode:    prod\n'
        '  branch:  main\n'
        '  host:    demo.example.org\n'
        '  static:  STATIC_NAMES_FILE unset (STATIC_GEN not checked)\n'
        '  account: unset (not needed for --dry-run)\n'
        '  pointer: demo-site-prod\n'
        '  push:    no\n',
        '',
    )
    assert sorted(p.name for p in site.iterdir()) == ['deploy', 'toml-check', 'wrangler.toml']


def test_dry_run_dev_with_settings_from_the_environment(tmp_path: Path):
    site = make_site(tmp_path)
    write_config(site)
    env = {'CLOUDFLARE_ACCOUNT_ID': 'acct', 'DEV_BRANCH': 'preview', 'PTR_SHORT': 'ds'}
    assert run(site, '-n', '-d', env=env) == (
        0,
        'deploy --dry-run: nothing built, deployed, moved or pushed\n'
        '  config:  wrangler.toml (parses)\n'
        '  project: demo-site\n'
        '  mode:    dev\n'
        '  branch:  preview\n'
        '  host:    dev.demo.example.org\n'
        '  static:  STATIC_NAMES_FILE unset (STATIC_GEN not checked)\n'
        '  account: set (not needed for --dry-run)\n'
        '  pointer: ds-dev\n'
        '  push:    no\n',
        '',
    )


def test_dry_run_checks_another_config(tmp_path: Path):
    site = make_site(tmp_path)
    write_config(site, 'name = "other"\n', 'wrangler.other.toml')
    code, out, err = run(site, '--dry-run', '-P', '--config', 'wrangler.other.toml')
    assert (code, err) == (0, '')
    assert out.split('\n')[1:5] == [
        '  config:  wrangler.other.toml (parses)',
        '  project: other',
        '  mode:    prod',
        '  branch:  main',
    ]


def test_config_without_dry_run_is_refused(tmp_path: Path):
    site = make_site(tmp_path)
    write_config(site, name='wrangler.other.toml')
    assert run(site, '--config', 'wrangler.other.toml') == (
        1,
        '',
        'deploy: --config only goes with --dry-run (wrangler deploys wrangler.toml, whatever else is named)\n',
    )


def test_an_unknown_argument_is_refused(tmp_path: Path):
    """A misspelt `--dry-run` must not fall through to a real deploy."""
    site = make_site(tmp_path)
    write_config(site)
    assert run(site, '--dryrun', '-P') == (1, '', 'deploy: unknown argument --dryrun (nothing built or deployed)\n')


def test_dry_run_refuses_a_config_that_does_not_parse(tmp_path: Path):
    site = make_site(tmp_path)
    write_config(site, 'name = "demo-site"\n[vars]\nBATCH_REGION = \\"us-east1\\"\n')
    assert run(site, '--dry-run', '-P') == (
        1,
        '',
        'toml-check: wrangler.toml:3:16: Invalid value (at line 3, column 16)\n'
        'deploy: wrangler.toml does not parse (above); fix it, then deploy\n',
    )


def test_dry_run_refuses_a_config_without_a_name(tmp_path: Path):
    site = make_site(tmp_path)
    write_config(site, '[vars]\nA = "b"\n')
    assert run(site, '--dry-run', '-P') == (1, '', 'deploy: no name = ... in wrangler.toml\n')


def test_dry_run_refuses_a_missing_config(tmp_path: Path):
    site = make_site(tmp_path)
    assert run(site, '--dry-run', '-P') == (
        1,
        '',
        "deploy: no site/wrangler.toml: each deployment branch (gcs, cw-s3, m3) carries its own; cloud has only the r2 demo's"
        ' wrangler.r2*.toml (deployed by .github/workflows/deploy-r2.yml)\n',
    )


def static_names(tmp_path: Path, gen: str) -> dict[str, str]:
    (tmp_path / 'static-names.json').write_text(json.dumps({'gen': gen}))
    return {'STATIC_NAMES_FILE': 'static-names.json'}


def test_dry_run_checks_static_gen_against_the_profile(tmp_path: Path):
    site = make_site(tmp_path)
    write_config(site, CONFIG + 'STATIC_GEN = "g1"\n')
    assert run(site, '--dry-run', '-P', env=static_names(tmp_path, 'g2')) == (
        1,
        '',
        'deploy: wrangler.toml STATIC_GEN g1 ≠ static-names.json gen g2: set them equal'
        ' (or drop STATIC_GEN: the site follows static-names/CURRENT.json), then deploy\n',
    )
    code, out, err = run(site, '--dry-run', '-P', env=static_names(tmp_path, 'g1'))
    assert (code, err) == (0, '')
    assert out.split('\n')[6] == '  static:  static-names.json gen g1 (every STATIC_GEN equal)'


def test_a_sed_typo_on_the_name_line_fails_the_dry_run(tmp_path: Path):
    site = make_site(tmp_path)
    write_config(site)
    script = site / 'deploy'
    text = script.read_text()
    assert text.count("sed -n 's/^name") == 1
    script.write_text(text.replace("sed -n 's/^name", "sed -n's/^name"))
    code, out, err = run(site, '--dry-run', '-P')
    # sed's own complaint (BSD and GNU word it differently); the script's "deploy:" messages never come.
    assert (code, out, err.startswith('sed: ')) == (1, '', True)
