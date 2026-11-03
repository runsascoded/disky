#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["click"]
# ///
"""A bounded Batch smoke test, using the real CW account and new targets only."""

from datetime import datetime, timezone
import json
from pathlib import Path
from subprocess import run
from uuid import uuid4

from click import command, option

PROJECT = "oa-internal-450019"
REGION = "us-central1"
ROOT = Path(__file__).resolve().parents[1]
IMAGE = "us-central1-docker.pkg.dev/oa-internal-450019/cw-s3-usage/scan:latest"
SCRIPT = '''import json, os
from uuid import uuid4
from boto3 import client as s3_client
from google.cloud.storage import Client
from requests import Session
from dt_cloud.static_profile import load_profile

profile = load_profile({"STATIC_NAMES_PROFILE": "/app/job/static-names.json"})
assert (profile.bucket, profile.scratch, profile.r2_secrets["key_id"], profile.r2_secrets["secret"]) == (
    "oa-cw-s3-usage-data", "oa-cw-s3-usage-scratch", "cw-s3-isolated-r2-access-key-id", "cw-s3-isolated-r2-secret-access-key",
)
print("CW-ISOLATION-SMOKE profile verified", flush=True)
key = "probes/cw/isolation-smoke/" + str(uuid4()) + ".json"
content = b'{"purpose":"cw-isolation-batch-smoke","version":1}\\n'
storage = Client(project="oa-internal-450019")
for bucket in (profile.bucket, profile.scratch):
    blob = storage.bucket(bucket).blob(key)
    blob.upload_from_string(content, content_type="application/json", if_generation_match=0)
    assert blob.download_as_bytes(if_generation_match=int(blob.generation)) == content
    print("CW-ISOLATION-SMOKE GCS " + bucket + " verified", flush=True)
account = "74981a43be0de7712369306c7b19133d"
with Session() as http:
    http.headers["Authorization"] = "Bearer " + os.environ["CLOUDFLARE_API_TOKEN"].strip()
    for suffix in ("pages/projects/oa-cw-s3-usage-icons", "d1/database/7f1e1326-b879-4ecd-8621-846621c24f36"):
        response = http.get("https://api.cloudflare.com/client/v4/accounts/" + account + "/" + suffix, timeout=60)
        assert response.status_code == 200 and response.json()["success"] is True
print("CW-ISOLATION-SMOKE Pages/D1 verified", flush=True)
s3 = s3_client("s3", endpoint_url=os.environ["R2_ENDPOINT"].strip(), region_name="auto",
               aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"].strip(), aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"].strip())
try:
    s3.put_object(Bucket="oa-cw-s3-usage-index", Key=key, Body=content, ContentType="application/json", IfNoneMatch="*")
    response = s3.get_object(Bucket="oa-cw-s3-usage-index", Key=key)
    try:
        assert response["Body"].read(len(content) + 1) == content
    finally:
        response["Body"].close()
finally:
    s3.close()
print("CW-ISOLATION-SMOKE R2 publisher verified", flush=True)
print("CW-ISOLATION-SMOKE DONE " + key, flush=True)
'''


def job_spec() -> dict:
    secrets = {
        "CLOUDFLARE_API_TOKEN": "cw-s3-cf-pages-token", "R2_ENDPOINT": "cw-s3-r2-endpoint",
        "R2_ACCESS_KEY_ID": "cw-s3-isolated-r2-access-key-id", "R2_SECRET_ACCESS_KEY": "cw-s3-isolated-r2-secret-access-key",
    }
    return {
        "taskGroups": [{"taskCount": 1, "taskSpec": {
            "runnables": [{"container": {"imageUri": IMAGE, "entrypoint": "python3", "commands": ["-c", SCRIPT]}}],
            "computeResource": {"cpuMilli": 1000, "memoryMib": 3000}, "maxRetryCount": 0, "maxRunDuration": "600s",
            "environment": {"secretVariables": {key: f"projects/{PROJECT}/secrets/{sid}/versions/latest" for key, sid in secrets.items()}},
        }}],
        "allocationPolicy": {
            "instances": [{"policy": {"machineType": "e2-standard-2", "bootDisk": {"type": "pd-balanced", "sizeGb": "30"}}}],
            "serviceAccount": {"email": f"cw-s3-job@{PROJECT}.iam.gserviceaccount.com"},
            "location": {"allowedLocations": [f"regions/{REGION}"]},
            "labels": {"app": "disky", "deployment": "cw-s3", "component": "isolation-smoke"},
        },
        "labels": {"app": "disky", "deployment": "cw-s3", "component": "isolation-smoke"},
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
    }


@command()
@option("-a", "--apply", is_flag=True, help="Submit the smoke test; canaries are retained, no source writes or deletions")
def main(apply: bool) -> None:
    spec = job_spec()
    if not apply:
        print(json.dumps(spec, indent=2))
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    job_id = f"cw-isolation-smoke-{stamp}-{uuid4().hex[:6]}"
    with (ROOT / "tmp" / (job_id + ".json")).open("x") as stream:
        stream.write(json.dumps(spec, indent=2) + "\n")
    run(["gcloud", "batch", "jobs", "submit", job_id, "--project", PROJECT, "--location", REGION, "--config", "-"],
        input=json.dumps(spec), text=True, check=True)
    print(json.dumps({"job": job_id, "region": REGION, "image": IMAGE}))


if __name__ == "__main__":
    main()
