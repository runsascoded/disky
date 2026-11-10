"""cw-s3's digest config file (`job/digest.yml`): the `cw` template plus this
deployment's site, plot host, icons, buckets and quotas (the presets on `cloud`
carry template shape only). Loading it yields exactly `CW`, it sets every field
on its own, and a dry run with it renders the golden thread over the shared
fixture scans (the file's buckets re-keyed to the fixture's names)."""
from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import yaml
from test_digest_golden import HERO, P, _dry_run, cw_root, golden

from dt_cloud import digest as DG

CONFIG = Path(__file__).parents[2] / "job" / "digest.yml"
TIB = 1024**4
Q100 = DG.Quota(100 * TIB, "100 TiB", "100Ti")  # US-EAST-06A, US-WEST-04A, RNO2A (shared by its buckets)
Q08A = DG.Quota(500 * TIB, "500 TiB", "500Ti")  # US-EAST-08A, shared by its buckets
CW = DG.DigestConfig(
    template="cw",
    title="CoreWeave usage",
    site_url="https://cw.oa.dev",
    root="gs://{DATA_BUCKET}/snapshots/cw",
    state="digest/cw/{channel}/{variant}",
    quota_marks="arrow-chip",
    params="qs,pp,lp",
    total_headline=True,
    reply_lines=True,
    preview="r2://oa-cw-s3-usage-index/digest/preview/{channel}/{variant}",
    discord_state="digest/discord/{webhook}",
    discord_webhook_env=None,
    icons_base="https://oa-cw-s3-usage-icons.pages.dev",
    op_icon="mark.png",
    icons_dir="job/icons-cw",
    plot_project="oa-cw-s3-usage-icons",
    plot_branch="cw",
    plot_base="https://cw.oa-cw-s3-usage-icons.pages.dev",
    variant="sender",
    reply_hour=12,
    provisional=True,
    alert_pct=90,
    primary="marin-us-east-02a",
    buckets={
        "marin-us-east-02a": DG.Bucket("02a", DG.Quota(910 * TIB, "1 PB", "1P")),
        "hero-checkpoints": DG.Bucket("hero", Q08A, "08a", shape="square"),
        "marin-us-east-06a": DG.Bucket("06a", Q100, shape="diamond"),
        "rhoarnet-us-east-08a": DG.Bucket("rhoarnet", Q08A, "08a", shape="square"),
        "marin-us-east-08a": DG.Bucket("08a", Q08A, "08a", shape="square"),
        "marin-us-west-04a": DG.Bucket("04a", Q100, shape="triangle"),
        "marin-us-east-01a": DG.Bucket("01a"),
        "marinfold-exp368-rno2a": DG.Bucket("marinfold", Q100, "rno2a", shape="star"),
        "marin-rno2a": DG.Bucket("rno2a", Q100, "rno2a", shape="star"),
    },
    prices={},
)


def test_config_is_cw_s3s():
    assert DG.load_config("cw", CONFIG) == CW
    assert DG.load_config("gcs", CONFIG) == CW  # the file names its template


def test_config_is_complete():
    # overlaid on a base where every field is junk, the file alone still yields the config
    junk = DG.DigestConfig(**{f.name: f"<{f.name}>" for f in fields(DG.DigestConfig)})
    assert sorted(yaml.safe_load(CONFIG.read_text())) == sorted(f.name for f in fields(DG.DigestConfig))
    assert DG.config_from_dict(yaml.safe_load(CONFIG.read_text()), junk) == CW


def test_dry_run_with_config(monkeypatch, tmp_path: Path):
    # the fixture scans' buckets are neutral names: re-key the file's buckets to them
    cfg = yaml.safe_load(CONFIG.read_text())
    names = {"marin-us-east-02a": P, "hero-checkpoints": HERO}
    cfg["primary"] = names[cfg["primary"]]
    cfg["buckets"] = {names.get(b, b): v for b, v in cfg["buckets"].items()}
    # the OP is the site's (conftest's stand-in here; its goldens are `site/src/digest/parity.test.ts`'s): the golden
    # pins the replies the job renders under the file's settings
    config = tmp_path / "digest.yml"
    config.write_text(yaml.safe_dump(cfg))
    root = cw_root(tmp_path)
    args = ["-C", str(config), "-n", "-r", str(root), "-m", "2026-09"]
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    out = _dry_run(monkeypatch, tmp_path / "a", ["digest", "-T", "cw", *args])
    golden("cw-s3-dry-run.txt", out)
    # `cw-digest` is `digest -T cw`
    assert _dry_run(monkeypatch, tmp_path / "b", ["cw-digest", *args]) == out
