from pathlib import Path
import sys

import pulumi
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gcp"))
from gcp_jobs import Adopt, Secrets


class Mocks(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.resources: dict[str, tuple[str, dict]] = {}

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources[args.name] = (args.typ, args.inputs)
        return args.name, args.inputs

    def call(self, args: pulumi.runtime.MockCallArgs):
        return {}


@pulumi.runtime.test
@pytest.mark.parametrize("grant_only, expected", [(None, ["current", "retired"]), ({"current"}, ["current"]), (set(), [])])
def test_selective_grants_keep_all_secret_containers(grant_only: set[str] | None, expected: list[str]) -> pulumi.Output:
    mocks = Mocks()
    pulumi.runtime.set_mocks(mocks, project="fixture", stack="fixture", preview=False)
    secrets = Secrets("credentials", project="fixture", secrets={"current": None, "retired": None},
                      accessor="serviceAccount:job@fixture.iam.gserviceaccount.com", accessor_email="job@fixture.iam.gserviceaccount.com",
                      adopt=Adopt(False), grant_only=grant_only)

    def check(_: object) -> None:
        assert sorted((name, typ) for name, (typ, _) in mocks.resources.items()) == sorted([
            ("credentials", "disky:gcp:Secrets"),
            ("current", "gcp:secretmanager/secret:Secret"),
            ("retired", "gcp:secretmanager/secret:Secret"),
            *[(name + "-accessor", "gcp:secretmanager/secretIamMember:SecretIamMember") for name in expected],
        ])
        assert {name: mocks.resources[name][1] for name in ("current", "retired")} == {
            name: {"project": "fixture", "secretId": name, "replication": {"auto": {}}}
            for name in ("current", "retired")
        }

    assert sorted(secrets.grants) == expected
    # Wait on the grants too: each registers after its secret (`depends_on`), so
    # waiting on the secrets alone races the grants' registration.
    return pulumi.Output.all(*[r.id for r in [*secrets.secrets.values(), *secrets.grants.values()]]).apply(check)


def test_unknown_grant_id_is_rejected() -> None:
    pulumi.runtime.set_mocks(Mocks(), project="fixture", stack="fixture", preview=False)
    with pytest.raises(ValueError, match="^grant_only must be a subset of the managed secret ids$"):
        Secrets("credentials", project="fixture", secrets={"current": None}, accessor="member", accessor_email="job@fixture.iam.gserviceaccount.com",
                adopt=Adopt(False), grant_only={"unknown"})
