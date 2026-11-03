from pathlib import Path
import sys

import pulumi

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gcp"))
from gcp_jobs import Adopt, DeploymentImages


class Mocks(pulumi.runtime.Mocks):
    def __init__(self):
        self.resources = {}

    def new_resource(self, args):
        self.resources[args.name] = (args.typ, args.inputs)
        return args.name, args.inputs

    def call(self, args):
        return {}


@pulumi.runtime.test
def test_a_deployments_image_repo_is_named_cleaned_up_and_granted():
    """`gcs`: repo `gcs-usage` (Docker, labelled `deployment=gcs`), `:latest` and the newest 10 builds kept, the rest
    deleted after 14 days; the job account reads, the build account writes; the image is `…/gcs-usage/scan`."""
    mocks = Mocks()
    pulumi.runtime.set_mocks(mocks, project="test", stack="gcs", preview=False)
    images = DeploymentImages("gcs", project="proj", region="us-central1", readers=["serviceAccount:job@proj"],
                              writers=["serviceAccount:build@proj"], adopt=Adopt(False))

    def check(image):
        typ, repo = mocks.resources["gcs-usage-images"]
        assert (typ, repo["repositoryId"], repo["format"], repo["labels"], repo["cleanupPolicyDryRun"]) == (
            "gcp:artifactregistry/repository:Repository", "gcs-usage", "DOCKER", {"component": "images", "deployment": "gcs"}, False)
        assert repo["cleanupPolicies"] == [
            {"id": "keep-deployment-tags", "action": "KEEP", "condition": {"tagState": "TAGGED", "tagPrefixes": ["latest"]}},
            {"id": "keep-recent", "action": "KEEP", "mostRecentVersions": {"keepCount": 10}},
            {"id": "delete-old", "action": "DELETE", "condition": {"tagState": "ANY", "olderThan": "1209600s"}},
        ]
        grants = {n: (i["role"], i["member"]) for n, (t, i) in mocks.resources.items() if t.endswith("RepositoryIamMember")}
        assert grants == {"gcs-usage-images-reader-0": ("roles/artifactregistry.reader", "serviceAccount:job@proj"),
                          "gcs-usage-images-writer-0": ("roles/artifactregistry.writer", "serviceAccount:build@proj")}
        assert image == "us-central1-docker.pkg.dev/proj/gcs-usage/scan"

    return images.image.apply(check)
