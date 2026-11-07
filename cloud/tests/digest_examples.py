"""Example digest configs the digest tests run on: one per template, with
neutral hosts and buckets. A deployment's real values live in its own
`job/digest.yml` (passed with `-C`); the presets in `dt_cloud.digest` carry
template shape only."""
from pathlib import Path

from dt_cloud import digest as E

TIB = 1024**4

GCS_EXAMPLE = E.DigestConfig(
    template="gcs",
    title="GCS usage",
    site_url="https://site.example.org",
    root="gs://{DATA_BUCKET}/snapshots",
    state="digest",
    discord_webhook_env="DISCORD_WEBHOOK",
    icons_base="https://icons.example.org",
    plot_project="icons",
    plot_base="https://icons.example.org",
    prices={"1": 0.02, "2": 0.01, "3": 0.004, "4": 0.0012},
)

CW_EXAMPLE = E.DigestConfig(
    template="cw",
    title="S3 usage",
    site_url="https://cw.example.org",
    root="gs://{DATA_BUCKET}/snapshots/cw",
    state="digest/cw/{channel}/{variant}",
    icons_base="https://icons.example.org",
    icons_dir="job/icons-cw",
    plot_project="icons",
    plot_branch="cw",
    plot_base="https://cw.icons.example.org",
    primary="main-data",
    buckets={
        "main-data": E.Bucket("main", E.Quota(910 * TIB, "1 PB", "1P")),
        "hot-data": E.Bucket("hot", E.Quota(100 * TIB, "100 TiB", "100Ti")),
    },
)


def config_file(cfg: E.DigestConfig, path: Path) -> Path:
    """``cfg`` as the YAML a deployment passes with ``-C``."""
    import yaml

    d = {
        k: getattr(cfg, k)
        for k in ("template", "title", "site_url", "root", "state", "discord_state", "discord_webhook_env", "icons_base",
                  "icons_dir", "plot_project", "plot_branch", "plot_base", "variant", "reply_hour", "provisional", "primary")
    }
    d["buckets"] = {
        name: {
            "label": b.label,
            **({"quota": {"bytes": b.quota.bytes, "name": b.quota.name, "short": b.quota.short}} if b.quota else {}),
            **({"zone": b.zone} if b.zone else {}),
        }
        for name, b in cfg.buckets.items()
    }
    d["prices"] = dict(cfg.prices)
    path.write_text(yaml.safe_dump(d, sort_keys=False))
    return path
