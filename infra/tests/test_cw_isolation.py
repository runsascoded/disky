"""CW isolation preparation registers only its eight new resources."""

from pathlib import Path
import sys

import pulumi

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gcp"))
from cw_isolation import prepare_credentials, prepare_dispatcher, prepare_isolation


class Mocks(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.resources: dict[str, dict] = {}
        self.types: dict[str, str] = {}

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources[args.name] = args.inputs
        self.types[args.name] = args.typ
        return f"{args.name}-id", args.inputs

    def call(self, args: pulumi.runtime.MockCallArgs):
        return {}


@pulumi.runtime.test
def test_additive_cw_foundation() -> pulumi.Output:
    mocks = Mocks()
    pulumi.runtime.set_mocks(mocks, project="fixture", stack="cw-s3", preview=False)
    member = "serviceAccount:cw-s3-job@fixture.iam.gserviceaccount.com"
    resources = prepare_isolation("fixture", member)

    def check(_: object) -> None:
        assert mocks.types == {
            "cw-isolation-data": "gcp:storage/bucket:Bucket",
            "cw-isolation-data-job": "gcp:storage/bucketIAMMember:BucketIAMMember",
            "cw-isolation-scratch": "gcp:storage/bucket:Bucket",
            "cw-isolation-scratch-job": "gcp:storage/bucketIAMMember:BucketIAMMember",
            "cw-isolation-images": "gcp:artifactregistry/repository:Repository",
            "cw-isolation-images-job": "gcp:artifactregistry/repositoryIamMember:RepositoryIamMember",
            "cw-isolation-cf-token": "gcp:secretmanager/secret:Secret",
            "cw-isolation-cf-token-job": "gcp:secretmanager/secretIamMember:SecretIamMember",
        }
        labels = {"app": "disky", "deployment": "cw-s3"}
        for component, seconds, rules in (
            ("data", 604800, []),
            ("scratch", 0, [{"action": {"type": "Delete"}, "condition": {"age": 7}}]),
        ):
            bucket = "oa-cw-s3-usage-" + component
            assert mocks.resources[f"cw-isolation-{component}"] == {
                "project": "fixture", "name": bucket, "location": "US-EAST1", "storageClass": "STANDARD",
                "uniformBucketLevelAccess": True, "publicAccessPrevention": "enforced", "forceDestroy": False,
                "labels": {"app": "disky", "deployment": "cw", "component": component}, "softDeletePolicy": {"retentionDurationSeconds": seconds},
                "lifecycleRules": rules,
            }
            assert mocks.resources[f"cw-isolation-{component}-job"] == {
                "bucket": bucket, "role": "roles/storage.objectAdmin", "member": member,
            }
        assert mocks.resources["cw-isolation-images"] == {
            "project": "fixture", "location": "us-central1", "repositoryId": "cw-s3-usage", "format": "DOCKER",
            "description": "CW deployment images; no GCS image tags", "labels": {**labels, "component": "images"},
        }
        assert mocks.resources["cw-isolation-images-job"] == {
            "project": "fixture", "location": "us-central1", "repository": "cw-s3-usage",
            "role": "roles/artifactregistry.reader", "member": member,
        }
        assert mocks.resources["cw-isolation-cf-token"] == {
            "project": "fixture", "secretId": "cw-s3-cf-pages-token", "replication": {"auto": {}},
            "labels": {**labels, "component": "secret"},
        }
        assert mocks.resources["cw-isolation-cf-token-job"] == {
            "project": "fixture", "secretId": "cw-s3-cf-pages-token",
            "role": "roles/secretmanager.secretAccessor", "member": member,
        }

    return pulumi.Output.all(*[resource.id for resource in resources]).apply(check)


@pulumi.runtime.test
def test_separate_publisher_secret_containers() -> pulumi.Output:
    mocks = Mocks()
    pulumi.runtime.set_mocks(mocks, project="fixture", stack="cw-s3", preview=False)
    member = "serviceAccount:cw-s3-job@fixture.iam.gserviceaccount.com"
    resources = prepare_credentials("fixture", member)

    def check(_: object) -> None:
        expected = {}
        types = {}
        for suffix in ("access-key-id", "secret-access-key"):
            name = f"cw-isolation-r2-{suffix}"
            types[name] = "gcp:secretmanager/secret:Secret"
            types[name + "-job"] = "gcp:secretmanager/secretIamMember:SecretIamMember"
            expected[name] = {
                "project": "fixture", "secretId": f"cw-s3-isolated-r2-{suffix}",
                "replication": {"auto": {}},
                "labels": {"app": "disky", "deployment": "cw-s3", "component": "secret"},
            }
            expected[name + "-job"] = {
                "project": "fixture", "secretId": f"cw-s3-isolated-r2-{suffix}",
                "role": "roles/secretmanager.secretAccessor", "member": member,
            }
        assert mocks.types == types
        assert mocks.resources == expected

    return pulumi.Output.all(*[resource.id for resource in resources]).apply(check)


@pulumi.runtime.test
def test_dispatcher_gets_read_create_without_delete() -> pulumi.Output:
    mocks = Mocks()
    pulumi.runtime.set_mocks(mocks, project="fixture", stack="cw-s3", preview=False)
    member = "serviceAccount:cw-s3-dispatch@fixture.iam.gserviceaccount.com"
    resources = prepare_dispatcher(member)

    def check(_: object) -> None:
        assert mocks.types == {
            f"cw-isolation-data-dispatch-{purpose}": "gcp:storage/bucketIAMMember:BucketIAMMember"
            for purpose in ("reader", "creator")
        }
        assert mocks.resources == {
            f"cw-isolation-data-dispatch-{purpose}": {
                "bucket": "oa-cw-s3-usage-data", "role": f"roles/storage.object{role}", "member": member,
            } for purpose, role in (("reader", "Viewer"), ("creator", "Creator"))
        }

    return pulumi.Output.all(*[resource.id for resource in resources]).apply(check)
