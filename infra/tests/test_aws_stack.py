"""The shared AWS program (`infra/aws/stack/`) under Pulumi's mocks: a config
alone names every resource, and the image build runs `infra/aws/build-image`.

    uv run --project infra --with pytest pytest infra/tests
"""
import json
import runpy
from pathlib import Path

import pulumi

from test_capture_components import Mocks

PROGRAM = Path(__file__).resolve().parents[1] / "aws" / "stack" / "__main__.py"


@pulumi.runtime.test
def test_config_only():
    m = Mocks()
    pulumi.runtime.set_mocks(m, project="dt", stack="laptop", preview=False)
    pulumi.runtime.set_all_config({
        "dt:prefix": "dt-laptop",
        "dt:sources": json.dumps(["infra/aws/Dockerfile", "infra/aws/ingest.sh"]),
        "dt:dockerfile": "infra/aws/Dockerfile",
        "dt:vcpu": "1",
        "dt:memory_mib": "8192",
        "dt:secrets": json.dumps({"AWS_ACCESS_KEY_ID": "dt-laptop/r2-key-id"}),
    })
    g = runpy.run_path(str(PROGRAM))
    ingest = g["ingest"]

    def check(args):
        queue, job_def, user = args
        assert (queue, job_def, user) == ("dt-laptop", "dt-laptop-ingest", "dt-laptop-capture-trigger")
        cmd = next(i for t, n, i in m.created if t == "command:local:Command")
        assert cmd["create"].split(" ")[:3] == ["python3", "infra/aws/build-image", "dt-laptop-image"]
        zip_files = sorted(next(i for t, n, i in m.created if n == "ingest-build-src-zip")["source"].assets)
        assert zip_files == ["infra/aws/Dockerfile", "infra/aws/ingest.sh"]

    return pulumi.Output.all(ingest.queue.name, ingest.job_def.name, ingest.trigger_user.name).apply(check)
