"""The shared job launchers on `cloud` (`job/static-names.sh`, `job/build.sh`, `job/rerg`; specs/job-consolidation.md),
run as shell scripts against stubbed `gcloud` / `dt-cloud` / `pqtk`: `bash -n` doesn't catch a launcher that parses
but breaks at run time (an apostrophe inside `${IMAGE:?…}` opened a quote and killed `resume-after`)."""
from json import dumps, loads
from os import environ
from pathlib import Path
from re import fullmatch, search
from subprocess import run

import pytest

ROOT = Path(__file__).resolve().parents[2]
ACCOUNT = "0123456789abcdef0123456789abcdef"
LABELS = {"app": "disky", "deployment": "demo"}
PROFILE = {
    "name": "demo",
    "project": "demo-proj",
    "region": "us-east4",
    "bucket": "demo-data",
    "scratch": "demo-scratch",
    "image": "registry.example/demo/scan@sha256:abc",
    "sa": "job@demo-proj.iam.gserviceaccount.com",
    "r2_bucket": "demo-index",
    "r2_secrets": {"key_id": "r2-key-id", "secret": "r2-secret"},
}


def stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)


@pytest.fixture
def sn(tmp_path):
    """Runs `job/static-names.sh` dry over a demo profile, `gcloud` stubbed: `storage ls` finds everything staged, any
    other call fails. Returns `(run, gcloud_calls)`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "gcloud.log"
    stub(bin_dir, "gcloud", f'echo "$*" >> {calls}\n[ "$1 $2" = "storage ls" ] || {{ echo "unexpected gcloud $*" >&2; exit 1; }}')

    def go(*args: str, profile: dict = PROFILE, **env: str):
        path = tmp_path / "static-names.json"
        path.write_text(dumps(profile))
        full = {
            "PATH": f"{bin_dir}:{environ['PATH']}", "HOME": environ.get("HOME", str(tmp_path)), "DRY": "1",
            "STATIC_NAMES_PROFILE": str(path), "STATIC_NAMES_ENV": str(tmp_path / "none.env"),
            "DISKY_LABELS": "app=disky,deployment=demo", "CLOUDFLARE_ACCOUNT_ID": ACCOUNT, **env,
        }
        (tmp_path / "none.env").write_text("")
        return run(["bash", "job/static-names.sh", *args], cwd=ROOT, env=full, capture_output=True, text=True)

    return go, lambda: calls.read_text().splitlines() if calls.exists() else []


def resolved_profile(path: Path) -> dict:
    """What `dt-cloud static-names profile` resolves the profile file at `path` to (what every task gets inline)."""
    return loads(run(["dt-cloud", "static-names", "profile"], cwd=ROOT, check=True, capture_output=True, text=True,
                     env={**environ, "STATIC_NAMES_PROFILE": str(path)}).stdout)


def task_of(spec: dict) -> dict:
    [group] = spec["taskGroups"]
    return group["taskSpec"]


def test_static_names_resume_after(sn, tmp_path):
    go, gcloud = sn
    result = go("resume-after", "drill-job", "2026-10-12T1042", JOB_ID="sn-resume-test")
    assert (result.returncode, result.stderr) == (0, "")
    spec = loads(result.stdout)
    task = task_of(spec)
    variables = task["environment"]["variables"]
    assert loads(variables.pop("STATIC_NAMES_PROFILE")) == resolved_profile(tmp_path / "static-names.json")
    assert task["environment"] == {"variables": {
        "GCP_PROJECT": "demo-proj",
        "R2_ENDPOINT": f"https://{ACCOUNT}.r2.cloudflarestorage.com",
        "CLOUDFLARE_ACCOUNT_ID": ACCOUNT,
        "DISKY_LABELS": "app=disky,deployment=demo",
        "SCAN_ID": "2026-10-12T1042",
    }}
    container = task["runnables"][0]["container"]
    assert container == {
        "imageUri": "registry.example/demo/scan@sha256:abc",
        "entrypoint": "bash",
        "commands": ["-c", "set -euo pipefail; BATCH_REGION=us-east4 dt-cloud job watch -L -i 30 drill-job && cd /app && "
                     "STATIC_NAMES_IMAGE=registry.example/demo/scan@sha256:abc STATIC_NAMES_SPOT=0 "
                     "dt-cloud static-names runs add -c -M 2026-10-12T1042"],
        "volumes": ["/mnt/disks/gcs/demo-data:/gcs/demo-data:ro", "/mnt/disks/gcs/demo-scratch:/gcs/demo-scratch:ro",
                    "/mnt/disks/stage:/stage:rw"],
    }
    policy = spec["allocationPolicy"]
    assert (task["maxRetryCount"], policy["instances"][0]["policy"]["machineType"],
            policy["instances"][0]["policy"]["provisioningModel"], policy["instances"][0]["policy"]["disks"],
            policy["serviceAccount"], policy["location"]) == (
        0, "n2-highmem-4", "STANDARD", [{"newDisk": {"type": "local-ssd", "sizeGb": "375"}, "deviceName": "stage"}],
        {"email": "job@demo-proj.iam.gserviceaccount.com"}, {"allowedLocations": ["regions/us-east4"]},
    )
    labels = {**LABELS, "component": "static-names", "scan_id": "2026-10-12t1042"}
    assert (spec["labels"], policy["labels"]) == ({"purpose": "static-names", "stage": "resume", **labels}, labels)
    assert gcloud() == []


def staged_names() -> tuple[str, str]:
    tree = run(["git", "rev-parse", "HEAD:cloud/src/dt_cloud"], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()
    rev = search(r"pyrmts/archive/([0-9a-f]*)", (ROOT / "cloud/pyproject.toml").read_text()).group(1)
    return tree, rev


def test_static_names_r2_batch(sn):
    go, gcloud = sn
    tree, rev = staged_names()
    result = go("r2-batch", "2026-11-01", JOB_ID="sn-r2-test")
    assert (result.returncode, result.stderr) == (0, "")
    spec = loads(result.stdout)
    task = task_of(spec)
    task["environment"]["variables"].pop("STATIC_NAMES_PROFILE")
    assert task["environment"] == {
        "variables": {"R2_BUCKET": "demo-index", "R2_ENDPOINT": f"https://{ACCOUNT}.r2.cloudflarestorage.com",
                      "DISKY_LABELS": "app=disky,deployment=demo"},
        "secretVariables": {"R2_ACCESS_KEY_ID": "projects/demo-proj/secrets/r2-key-id/versions/latest",
                            "R2_SECRET_ACCESS_KEY": "projects/demo-proj/secrets/r2-secret/versions/latest"},
    }
    container = task["runnables"][0]["container"]
    assert container["commands"] == ["-c", (
        "set -euo pipefail; mkdir -p /stage/src /stage/tmp /stage/out && "
        f"cp -r /gcs/demo-data/static-names/src/{tree}/dt_cloud /gcs/demo-data/static-names/src/pyrmts-{rev}/pyrmts /stage/src/ && "
        "cd /stage && PYTHONPATH=/stage/src python3 -u -m dt_cloud.static_names r2-copy  -g 2026-11-01 "
    )]
    # NO_SCRATCH: only the data bucket and the local SSD
    assert container["volumes"] == ["/mnt/disks/gcs/demo-data:/gcs/demo-data:ro", "/mnt/disks/stage:/stage:rw"]
    assert task["volumes"] == [
        {"gcs": {"remotePath": "demo-data"}, "mountPath": "/mnt/disks/gcs/demo-data", "mountOptions": ["--implicit-dirs"]},
        {"deviceName": "stage", "mountPath": "/mnt/disks/stage"},
    ]
    assert spec["labels"] == {"purpose": "static-names", "stage": "r2-copy", **LABELS, "component": "static-names"}
    assert gcloud() == [
        f"storage ls gs://demo-data/static-names/src/{tree}/dt_cloud/__init__.py",
        f"storage ls gs://demo-data/static-names/src/pyrmts-{rev}/pyrmts/intervals.py",
    ]


@pytest.mark.parametrize("args", [("resume-after", "drill-job", "2026-10-12T1042"), ("run", "build", "4")])
def test_static_names_without_an_image_names_the_setting(sn, args):
    go, _ = sn
    result = go(*args, profile={k: v for k, v in PROFILE.items() if k != "image"}, JOB_ID="sn-test")
    assert (result.returncode, result.stdout) == (1, "")
    assert fullmatch(r"job/static-names\.sh: line \d+: IMAGE: no image: set image in the profile, or IMAGE\n", result.stderr)


@pytest.fixture
def argv_stub(tmp_path):
    """A stub binary that prints its argv, one line per call."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    def make(name: str) -> Path:
        stub(bin_dir, name, 'printf "%s\\n" "$*"')
        return bin_dir / name

    return bin_dir, make


@pytest.mark.parametrize("args, expected", [
    (["-m", "candidate", "-n"], f"image build -c {ROOT}/job/build.json -m candidate -n"),
    (["promote", "abc123def456", "-x", "sha256:0f"], f"image promote -c {ROOT}/job/build.json abc123def456 -x sha256:0f"),
])
def test_build_sh_hands_its_checkouts_config_to_dt_cloud_image(argv_stub, args, expected):
    bin_dir, make = argv_stub
    make("dt-cloud")
    result = run(["bash", "job/build.sh", *args], cwd=ROOT, env={"PATH": f"{bin_dir}:{environ['PATH']}"}, capture_output=True, text=True)
    assert (result.returncode, result.stdout, result.stderr) == (0, expected + "\n", "")


RERG_ENV = {
    "RERG_PROJECT": "demo-proj", "RERG_REGION": "us-east4", "RERG_IMAGE": "registry.example/pqtk:1",
    "RERG_SA": "rerg@demo-proj.iam.gserviceaccount.com", "RERG_STAGING": "gs://demo-scratch/pqtk",
}


def test_rerg_runs_pqtk_per_glob_from_its_env_file(argv_stub, tmp_path):
    bin_dir, make = argv_stub
    pqtk = make("pqtk")
    env_file = tmp_path / "rerg.env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in RERG_ENV.items()))
    result = run(["bash", "job/rerg", "-r", "1000", "-i", "gs://demo-data/a/*.parquet", "gs://demo-data/b/*.parquet"], cwd=ROOT,
                 env={"PATH": environ["PATH"], "PQTK": str(pqtk), "RERG_ENV": str(env_file)}, capture_output=True, text=True)
    tail = ("-i -r 1000 --runner gcp-batch --project demo-proj --region us-east4 --image registry.example/pqtk:1 "
            "--sa rerg@demo-proj.iam.gserviceaccount.com --staging gs://demo-scratch/pqtk --machine n2-standard-4")
    assert (result.returncode, result.stderr) == (0, "")
    assert result.stdout.splitlines() == [f"re-rg gs://demo-data/a/*.parquet {tail}", f"re-rg gs://demo-data/b/*.parquet {tail}"]


def test_rerg_refuses_without_config(argv_stub, tmp_path):
    bin_dir, make = argv_stub
    pqtk = make("pqtk")
    result = run(["bash", "job/rerg", "gs://demo-data/a/*.parquet"], cwd=ROOT,
                 env={"PATH": environ["PATH"], "PQTK": str(pqtk), "RERG_ENV": str(tmp_path / "missing.env")}, capture_output=True, text=True)
    assert (result.returncode, result.stdout, result.stderr) == (2, "", f"RERG_ENV={tmp_path / 'missing.env'}: no such file\n")
