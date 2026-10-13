"""New dashboard publisher tokens must not span an entire R2 account."""

import json
import sys
from pathlib import Path

import pulumi

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cf"))
from cfn_dashboard import CfnDashboard, Store


class Mocks(pulumi.runtime.Mocks):
    def __init__(self):
        self.resources: dict[str, dict] = {}

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources[args.name] = args.inputs
        return f"{args.name}-id", {**args.inputs, "value": "fixture-token"}

    def call(self, args: pulumi.runtime.MockCallArgs):
        return {}


@pulumi.runtime.test
def test_publisher_token_is_scoped_to_only_its_derived_bucket():
    mocks = Mocks()
    pulumi.runtime.set_mocks(mocks, project="fixture", stack="fixture", preview=False)
    dashboard = CfnDashboard(
        "r2", account_id="fixture-account", zone_id="fixture-zone",
        store=Store(pages_project="fixture-pages", production_branch="r2-prod", domain="r2.example.test", d1_name="fixture-db",
                    r2_bucket="fixture-derived-index", r2_token_permission_group_ids=("read-id", "write-id")),
    )

    def check(_: object) -> None:
        inputs = mocks.resources["r2-r2-token"]
        policies = [dict(policy) for policy in inputs["policies"]]
        for policy in policies:
            policy["resources"] = json.loads(policy["resources"])
        assert policies == [{
            "effect": "allow", "permissionGroups": [{"id": "read-id"}, {"id": "write-id"}],
            "resources": {"com.cloudflare.edge.r2.bucket.fixture-account_default_fixture-derived-index": "*"},
        }]
    return pulumi.Output.all(dashboard.r2_token.id).apply(check)
