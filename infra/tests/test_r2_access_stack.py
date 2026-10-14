"""Exact private-resource and cron gates for the R2 collector; no API calls."""

import json
from pathlib import Path
from runpy import run_path

import pulumi
import pytest


ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "infra/cf/r2-access"
DEFINITION = json.loads((WORKER / "wrangler.json").read_text())


class Mocks(pulumi.runtime.Mocks):
    def __init__(self):
        self.resources: dict[str, dict] = {}
        self.types: dict[str, str] = {}

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources[args.name] = args.inputs
        self.types[args.name] = args.typ
        return f"{args.name}-id", args.inputs

    def call(self, args: pulumi.runtime.MockCallArgs):
        return {}


def stack(config: dict) -> tuple[Mocks, dict]:
    mocks = Mocks()
    pulumi.runtime.set_mocks(mocks, project="disky-r2-access", stack="rac", preview=False)
    pulumi.runtime.set_all_config({f"disky-r2-access:{key}": json.dumps(value) for key, value in config.items()})
    return mocks, run_path(str(WORKER / "__main__.py"))


@pytest.mark.parametrize("enabled", [False, True])
@pulumi.runtime.test
def test_private_stack_and_explicit_cron_enablement(enabled: bool):
    mocks, program = stack({"enabled": enabled, "secretReady": enabled})

    def check(_: object) -> None:
        assert mocks.types == {
            "archive": "cloudflare:index/r2Bucket:R2Bucket",
            "archive-private": "cloudflare:index/r2ManagedDomain:R2ManagedDomain",
            "archive-retention": "cloudflare:index/r2BucketLifecycle:R2BucketLifecycle",
            "collector": "cloudflare:index/workersScript:WorkersScript",
            "collector-private": "cloudflare:index/workersScriptSubdomain:WorkersScriptSubdomain",
            "collector-cron": "cloudflare:index/workersCronTrigger:WorkersCronTrigger",
        }
        account = DEFINITION["account_id"]
        bucket = "disky-r2-access-logs"
        worker = "disky-rac-r2-access"
        assert mocks.resources["archive"] == {"accountId": account, "name": bucket, "location": "enam", "storageClass": "Standard"}
        assert mocks.resources["archive-private"] == {"accountId": account, "bucketName": bucket, "enabled": False}
        assert mocks.resources["collector-private"] == {"accountId": account, "scriptName": worker, "enabled": False, "previewsEnabled": False}
        assert mocks.resources["archive-retention"] == {
            "accountId": account, "bucketName": bucket,
            "rules": [{"id": "raw-runs-retention", "enabled": True, "conditions": {"prefix": "runs/"},
                       "deleteObjectsTransition": {"condition": {"type": "Age", "maxAge": 2592000}}}],
        }
        assert mocks.resources["collector-cron"] == {
            "accountId": account, "scriptName": worker,
            "schedules": [{"cron": "13 * * * *"}, {"cron": "17 7 * * *"}] if enabled else [],
        }
        worker_inputs = dict(mocks.resources["collector"])
        assert worker_inputs.pop("content") == (ROOT / "tmp/r2-access-build/index.js").read_text()
        assert worker_inputs == {
            "accountId": account, "scriptName": worker, "mainModule": "index.js",
            "compatibilityDate": DEFINITION["compatibility_date"], "compatibilityFlags": ["nodejs_compat"],
            "keepBindings": ["secret_text"], "observability": {"enabled": True, "headSamplingRate": 1},
            "limits": {"cpuMs": 10000},
            "bindings": [{"name": "ARCHIVE", "type": "r2_bucket", "bucketName": bucket},
                         *[{"name": key, "type": "plain_text", "text": value} for key, value in sorted(DEFINITION["vars"].items())]],
        }

    return pulumi.Output.all(program["schedules"].id).apply(check)


@pytest.mark.parametrize("config, message", [
    ({"enabled": True}, "Install the runtime secret first, then set secretReady before enabling cron"),
    ({"retentionDays": 0}, "retentionDays must be between 7 and 90"),
    ({"retentionDays": 91}, "retentionDays must be between 7 and 90"),
])
def test_invalid_config_fails_before_registration(config: dict, message: str) -> None:
    with pytest.raises(pulumi.RunError) as error:
        stack(config)
    assert str(error.value) == message
