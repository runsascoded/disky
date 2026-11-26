"""A run's jobs on one image (`image_pin`): a coordinator resolves its image's tag to a digest once, and everything it
submits runs that digest (cw's 2026-11-26 fold: a promote mid-fold sent its `fold-shard` jobs to other code)."""
from dataclasses import dataclass
from types import SimpleNamespace as NS

import pytest

from dt_cloud import image_pin as ip
from dt_cloud import scan_head as H

TAG = "us-central1-docker.pkg.dev/p/cw-s3-usage/scan:latest"
D = "sha256:" + "ab" * 32
PINNED = f"us-central1-docker.pkg.dev/p/cw-s3-usage/scan@{D}"


def test_resolve_a_tag_to_its_digest_once_and_keep_everything_else():
    asked = []

    def digest_of(url):
        asked.append(url)
        return D
    assert [ip.resolve(i, digest_of) for i in (TAG, PINNED, "img", "docker.io/library/python:3.13")] == [PINNED, PINNED, "img", "docker.io/library/python:3.13"]
    assert asked == ["https://us-central1-docker.pkg.dev/v2/p/cw-s3-usage/scan/manifests/latest"]


def test_a_tag_the_registry_cant_resolve_fails_loudly():
    with pytest.raises(RuntimeError) as e:
        ip.resolve(TAG, lambda url: "")
    assert str(e.value) == f"{TAG}: the registry gave no digest (''); not running on a moving tag"


def test_pin_profile_and_env():
    @dataclass(frozen=True)
    class P:
        image: str | None
        gen: str = "g"
    logged = []
    assert [ip.pin_profile(P(TAG), lambda url: D, logged.append), ip.pin_profile(P(PINNED), lambda url: "x"), ip.pin_profile(P(None))] == [
        P(PINNED), P(PINNED), P(None)]
    assert logged == [f"image: {TAG} → {PINNED} (every job this run submits)"]
    assert ip.pinned_env({"A": "1", "JOB_IMAGE": TAG}, PINNED) == {
        "A": "1", "JOB_IMAGE": PINNED, "STATIC_NAMES_IMAGE": PINNED, "INTERVAL_STORE_IMAGE": PINNED}


def test_the_head_pins_a_batch_runs_image_and_hands_it_to_every_task(monkeypatch):
    """`pin_image`: the head's own digest (`$DT_SCAN_IMAGE`) as is; a google-batch profile's tag resolved now; nothing
    for a dry run or a local run. `run_config` exports it as each image variable a task's submits read, out of the task
    hash; `params` drops the profile's tag-valued ones so the task's script can't export them over it."""
    monkeypatch.setattr(ip, "_registry_digest", lambda url: D)
    prof = lambda kind: NS(executor=NS(kind=kind, image=TAG))  # noqa: E731
    assert [H.pin_image(prof("google-batch"), None, False), H.pin_image(prof("google-batch"), PINNED, False),
            H.pin_image(prof("google-batch"), None, True), H.pin_image(prof("local"), None, False)] == [PINNED, PINNED, None, None]
    assert H.run_config("run-1", "{}", PINNED).split("\n")[1:6] == [
        "    DT_RUN_ID = 'run-1'", "    SCAN_RUN_ID = 'run-1'", f"    JOB_IMAGE = '{PINNED}'",
        f"    STATIC_NAMES_IMAGE = '{PINNED}'", f"    INTERVAL_STORE_IMAGE = '{PINNED}'"]
    with pytest.raises(ValueError) as e:
        H.run_config("run-1", "{}", "img'; rm -rf /")
    assert str(e.value) == "image \"img'; rm -rf /\": Nextflow exports it into each task's script unescaped"


def test_a_pinned_runs_tasks_dont_export_the_profiles_image_tags(tmp_path):
    """With the run's digest (`DT_IMAGE_PIN`), `params` leaves the profile's `STATIC_NAMES_IMAGE` (a tag) out of the tasks'
    exported env, which would override `run.config`'s digest; without one, the profile's stays."""
    from pathlib import Path

    from dt_cloud.scan_profile import load

    prof = tmp_path / "p.yml"
    prof.write_text(f"deployment: fix\nexecutor: {{kind: local}}\nwork: {tmp_path}/nf\ndata: {tmp_path}/data\n"
                    "layout: {layer2: 'l2/{scan}/', snapshots: ''}\nsources: [s3://a]\n"
                    "skip: [static-names, digest, interval-store, warm-cache, probe]\noptions: {list: {import: true}}\n"
                    f"env: {{DISKY_LABELS: 'app=disky,deployment=fix', STATIC_NAMES_IMAGE: '{TAG}'}}\n")
    p = load(Path(prof))
    env = {"DISKY_LABELS": "app=disky,deployment=fix"}
    got = [sorted(H.params(p, "{}", "2026-10-16", "G", "1", "", "S", e)["env"]) for e in (env, {**env, H.PIN_ENV: PINNED})]
    assert got == [["DISKY_LABELS", "STATIC_NAMES_IMAGE"], ["DISKY_LABELS"]]
