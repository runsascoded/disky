"""Every test runs as a deployment with a neutral data bucket and site
(`dt_cloud.deploy`): dt-cloud has no default of its own. A test of the unset
case removes them with `monkeypatch.delenv`. The layer-2 template and the
static-names profile start unset. No cost labels (`DISKY_LABELS`)
leak in from the shell: a test that wants them sets them.

A test against the D1 schema takes `d1_lineage`: it runs once per migrations
lineage this branch carries (each directory under `site/migrations/`)."""
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

MIGRATIONS = Path(__file__).resolve().parents[2] / "site/migrations"
LINEAGES = sorted(d.name for d in MIGRATIONS.iterdir() if d.is_dir())


@dataclass(frozen=True)
class D1Lineage:
    """One D1 migrations lineage: `site/migrations/<name>/NNNN_<stem>.sql`."""
    name: str

    @property
    def dir(self) -> Path:
        return MIGRATIONS / self.name

    def migrations(self) -> list[Path]:
        return sorted(self.dir.glob("*.sql"))

    def named(self, stem: str) -> Path:
        """The one migration named `NNNN_<stem>.sql`, whatever its number here."""
        hits = [f for f in self.migrations() if re.sub(r"^\d+_", "", f.name) == f"{stem}.sql"]
        if len(hits) != 1:
            raise LookupError(f"{self.name}: {len(hits)} migrations named `{stem}`: {[f.name for f in hits]}")
        return hits[0]


@pytest.fixture(params=LINEAGES)
def d1_lineage(request) -> D1Lineage:
    return D1Lineage(request.param)


@pytest.fixture(autouse=True)
def _deployment_env(monkeypatch):
    monkeypatch.setenv("DATA_BUCKET", "my-data")
    monkeypatch.setenv("SITE_URL", "https://site.example.org")
    monkeypatch.delenv("DISKY_LABELS", raising=False)
    # Deployment config the generic code requires (no defaults): unset unless a test sets it.
    for v in ("LAYER2_PREFIX", "STATIC_NAMES_PROFILE", "STATIC_NAMES_GEN", "SCAN_RUN_ID", "SCAN_ID"):
        monkeypatch.delenv(v, raising=False)
