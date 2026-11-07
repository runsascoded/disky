"""The shared Cloudflare program (`infra/cf/stack/`) under Pulumi's mocks: what
a config declares, with nothing but config. No account needed.

    uv run --project infra --with pytest pytest infra/tests
"""
import json
import runpy
from pathlib import Path

import pulumi
import pytest

PROGRAM = Path(__file__).resolve().parents[1] / "cf" / "stack" / "__main__.py"
BASE = {"accountId": "acct", "zoneId": "zone", "pagesProject": "dt", "domain": "disk.example.com", "d1Name": "dt-db"}


class Mocks(pulumi.runtime.Mocks):
    def __init__(self):
        self.created: list[tuple[str, str]] = []

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.created.append((args.typ, args.name))
        return f"{args.name}_id", {**args.inputs, "queue_id": "qid", "name": args.inputs.get("name", args.name)}

    def call(self, args: pulumi.runtime.MockCallArgs):
        return {}


def run(config: dict) -> tuple[Mocks, dict]:
    m = Mocks()
    pulumi.runtime.set_mocks(m, project="dt", stack="prod", preview=False)
    pulumi.runtime.set_all_config({f"dt:{k}": v if isinstance(v, str) else json.dumps(v) for k, v in config.items()})
    return m, runpy.run_path(str(PROGRAM))


def after(outputs: list, check) -> pulumi.Output:
    return pulumi.Output.all(*outputs).apply(lambda _: check())


DASHBOARD = [
    ("cloudflare:index/d1Database:D1Database", "prod-d1"),
    ("cloudflare:index/dnsRecord:DnsRecord", "prod-cname"),
    ("cloudflare:index/pagesDomain:PagesDomain", "prod-domain"),
    ("cloudflare:index/pagesProject:PagesProject", "prod-pages"),
    ("oa:cfn:CfnDashboard", "prod"),
]


@pulumi.runtime.test
def test_minimal():
    m, g = run(BASE)

    def check():
        assert sorted(m.created) == DASHBOARD
    return after([g["dash"].d1.id, g["dash"].domain.id], check)


@pulumi.runtime.test
def test_every_optional_block():
    m, g = run({
        **BASE,
        "branchAliases": [{"domain": "dev.disk.example.com", "branch": "dev"}],
        "kvName": "dt-cache",
        "devProject": "dt-dev",
        "devDomain": "dev2.disk.example.com",
        "capturesBucket": "dt-captures",
        "capturesQueue": "dt-captures-q",
    })

    def check():
        assert sorted(m.created) == sorted(DASHBOARD + [
            ("cloudflare:index/pagesDomain:PagesDomain", "prod-dev-domain"),
            ("cloudflare:index/dnsRecord:DnsRecord", "prod-dev-cname"),
            ("cloudflare:index/workersKvNamespace:WorkersKvNamespace", "prod-cache-kv"),
            ("cloudflare:index/pagesProject:PagesProject", "dev-pages"),
            ("cloudflare:index/dnsRecord:DnsRecord", "dev-cname"),
            ("cloudflare:index/pagesDomain:PagesDomain", "dev-domain"),
            ("disky:cf:CaptureTrigger", "captures"),
            ("cloudflare:index/queue:Queue", "captures-queue"),
            ("cloudflare:index/r2BucketEventNotification:R2BucketEventNotification", "captures-notification"),
        ])
    return after([g["dash"].d1.id, g["dev"].id, g["trigger"].notification.id, *[d.id for d in g["dash"].branch_domains.values()]], check)


@pytest.mark.parametrize("extra, msg", [
    ({"devProject": "dt-dev"}, "devProject and devDomain go together"),
    ({"capturesQueue": "q"}, "capturesBucket and capturesQueue go together"),
])
def test_pairs_go_together(extra, msg):
    with pytest.raises(pulumi.RunError) as e:
        run({**BASE, **extra})
    assert str(e.value) == msg
