from pathlib import Path
import sys

import pulumi
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gcp"))
from gcp_jobs import Adopt, JobAccount, SiteDispatcher

SITE = "site@fixture.iam.gserviceaccount.com"
JOB = "job@fixture.iam.gserviceaccount.com"


class Mocks(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.resources: dict[str, tuple[str, dict]] = {}

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources[args.name] = (args.typ, args.inputs)
        return args.name, {**args.inputs, "email": args.inputs.get("accountId", "") + "@fixture.iam.gserviceaccount.com",
                           "name": f"projects/fixture/serviceAccounts/{args.inputs.get('accountId', '')}@fixture.iam.gserviceaccount.com"}

    def call(self, args: pulumi.runtime.MockCallArgs):
        return {}


def accounts() -> tuple[JobAccount, JobAccount]:
    site = JobAccount("site", project="fixture", account_id="site", display_name="site", roles=[], acts_as_self=False, adopt=Adopt(False))
    job = JobAccount("job", project="fixture", account_id="job", display_name="job", roles=[], acts_as_self=False, adopt=Adopt(False))
    return site, job


def grants(mocks: Mocks) -> dict[str, tuple[str, dict]]:
    return {n: r for n, r in mocks.resources.items() if n.startswith("dispatch") and r[0] != "disky:gcp:SiteDispatcher"}


JOBS = ("gcp:projects/iAMMember:IAMMember", {"project": "fixture", "role": "roles/batch.jobsEditor", "member": f"serviceAccount:{SITE}"})
ACT = ("gcp:serviceaccount/iAMMember:IAMMember", {"serviceAccountId": f"projects/fixture/serviceAccounts/{JOB}",
                                                 "role": "roles/iam.serviceAccountUser", "member": f"serviceAccount:{SITE}"})
REAPPLY = ("gcp:storage/bucketIAMMember:BucketIAMMember", {
    "bucket": "data", "role": "roles/storage.objectViewer", "member": f"serviceAccount:{SITE}",
    "condition": {"title": "digest-reapply-runs", "description": "Only /admin/slack's digest re-apply output and rewrite plans",
                  "expression": 'resource.name.startsWith("projects/_/buckets/data/objects/digest/reapply/")'},
})
CREATE = ("gcp:storage/bucketIAMMember:BucketIAMMember", {"bucket": "data", "role": "roles/storage.objectCreator", "member": f"serviceAccount:{SITE}"})
READ = ("gcp:storage/bucketIAMMember:BucketIAMMember", {"bucket": "data", "role": "roles/storage.objectViewer", "member": f"serviceAccount:{SITE}"})


@pulumi.runtime.test
@pytest.mark.parametrize("flags, expected", [
    ({}, {}),
    ({"reapply": True}, {"dispatch-batch-jobsEditor": JOBS, "dispatch-act-job": ACT, "dispatch-reapply-reader": REAPPLY}),
    ({"sweeps": True}, {"dispatch-batch-jobsEditor": JOBS, "dispatch-act-job": ACT, "dispatch-sweep-creator": CREATE}),
    ({"sweeps": True, "sweep_reads": True, "reapply": True}, {
        "dispatch-batch-jobsEditor": JOBS, "dispatch-act-job": ACT, "dispatch-reapply-reader": REAPPLY,
        "dispatch-sweep-creator": CREATE, "dispatch-sweep-reader": READ,
    }),
])
def test_each_capability_grants_exactly_its_roles(flags: dict[str, bool], expected: dict) -> pulumi.Output:
    mocks = Mocks()
    pulumi.runtime.set_mocks(mocks, project="fixture", stack="fixture", preview=False)
    site, job = accounts()
    d = SiteDispatcher("dispatch", project="fixture", account=site, job=job, data_bucket="data", **flags)

    def check(_: object) -> None:
        assert grants(mocks) == expected

    return pulumi.Output.all(*[r.id for r in [site.account, job.account, *d.grants.values()]]).apply(check)


def test_sweep_reads_needs_sweeps() -> None:
    pulumi.runtime.set_mocks(Mocks(), project="fixture", stack="fixture", preview=False)
    site, job = accounts()
    with pytest.raises(ValueError, match="^sweep_reads needs sweeps$"):
        SiteDispatcher("dispatch", project="fixture", account=site, job=job, data_bucket="data", sweep_reads=True)


def test_unknown_alias_key_is_rejected() -> None:
    pulumi.runtime.set_mocks(Mocks(), project="fixture", stack="fixture", preview=False)
    site, job = accounts()
    with pytest.raises(ValueError, match=r"^unknown SiteDispatcher alias keys: \['jobsEditor'\]$"):
        SiteDispatcher("dispatch", project="fixture", account=site, job=job, data_bucket="data", reapply=True, aliases={"jobsEditor": []})
