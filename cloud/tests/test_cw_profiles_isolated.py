"""cw's scan profiles read through `dt_cloud.scan_profile` touch only the isolated targets: the cw image, job SA, data
bucket and R2 serving bucket (`tests/test_cw_isolation_wiring.py` covers the rest of cw's wiring; this part needs
`dt_cloud`, so it runs with the overlay's tests)."""
from pathlib import Path

import pytest

from dt_cloud.scan_profile import load

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "oa-internal-450019"
IMAGE = f"us-central1-docker.pkg.dev/{PROJECT}/cw-s3-usage/scan:latest"
DATA = "oa-cw-s3-usage-data"
R2_SECRETS = {
    "R2_ENDPOINT": "cw-s3-r2-endpoint",
    "R2_ACCESS_KEY_ID": "cw-s3-isolated-r2-access-key-id",
    "R2_SECRET_ACCESS_KEY": "cw-s3-isolated-r2-secret-access-key",
}
CW_BUCKETS = ("marin-us-east-02a", "hero-checkpoints", "marin-us-east-06a", "rhoarnet-us-east-08a", "marin-us-east-08a",
              "marin-us-west-04a", "marin-us-east-01a", "marin-us-east-04a", "marinfold-exp368-rno2a", "marin-rno2a")


@pytest.mark.parametrize("profile, store, sources", [
    ("cw.yml", "primary", [f"s3://{b}" for b in CW_BUCKETS]),
    ("cw-meta.yml", "meta", [f"gcs://{DATA}", "r2://oa-cw-s3-usage-index"]),
])
def test_scan_profiles_use_only_isolated_targets(profile: str, store: str, sources: list[str]) -> None:
    p = load(ROOT / "job/profiles" / profile)
    assert {"store": p.store, "image": p.executor.image, "sa": p.executor.service_account, "data": p.data,
            "work": p.work, "sources": [s.uri for s in p.sources], "r2": p.env["R2_BUCKET"],
            "r2_keys": {k: v for k, v in p.secrets.items() if k.startswith("R2_")}} == {
        "store": store, "image": IMAGE, "sa": f"cw-s3-job@{PROJECT}.iam.gserviceaccount.com", "data": f"gs://{DATA}",
        "work": f"gs://{DATA}/nf", "sources": sources, "r2": "oa-cw-s3-usage-index",
        "r2_keys": {k: v for k, v in R2_SECRETS.items() if k != "R2_ENDPOINT" or profile == "cw.yml"},
    }
