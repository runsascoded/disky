#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["click", "boto3", "google-auth", "google-cloud-storage", "google-crc32c", "requests"]
# ///
"""Verify CW isolation credentials and deliver job secrets without exposing values."""

from base64 import b64decode, b64encode
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from subprocess import run
from tempfile import mkdtemp
import tomllib
from uuid import uuid4

from click import Choice, ClickException, Path as ClickPath, argument, group, option
from google_crc32c import value as crc32c

ROOT = Path(__file__).resolve().parents[1]
BUCKET = "oa-cw-s3-usage-index"
PROJECT = "oa-internal-450019"
SECRETS = {
    "cw-s3-cf-pages-token": "isolation_pages_token",
    "cw-s3-isolated-r2-access-key-id": "isolation_r2_publisher_key_id",
    "cw-s3-isolated-r2-secret-access-key": "isolation_r2_publisher_secret",
}
PERMISSIONS = {
    "reader": ["Workers R2 Storage Bucket Item Read"],
    "publisher": ["Workers R2 Storage Bucket Item Read", "Workers R2 Storage Bucket Item Write"],
    "pages-token": ["D1 Read", "D1 Write", "Pages Read", "Pages Write"],
}


def load_outputs() -> dict:
    result = run(
        [str(ROOT / "infra/cf/run"), "stack", "output", "--stack", "cw-s3", "--json", "--show-secrets"],
        capture_output=True, check=True, text=True, cwd=ROOT,
    )
    outputs = json.loads(result.stdout)
    required = [*SECRETS.values(), "isolation_r2_reader_key_id", "isolation_r2_reader_secret", "isolation_pages_token_id"]
    if any(not isinstance(outputs.get(key), str) or not outputs[key].strip() for key in required):
        raise ClickException("Missing isolation outputs; apply the additive CF phase first")
    return outputs


def cf_get(
    client: object,
    path: str,
    token: str,
) -> object:
    response = client.get("https://api.cloudflare.com/client/v4/" + path,
                          headers={"Authorization": "Bearer " + token}, timeout=60)
    if response.status_code != 200:
        raise ClickException(f"Cloudflare verification failed: HTTP {response.status_code}")
    body = response.json()
    if body.get("success") is not True:
        raise ClickException("Cloudflare verification returned an unsuccessful envelope")
    return body["result"]


def verify_policy(
    record: dict,
    account: str,
    purpose: str,
) -> None:
    scope = {f"com.cloudflare.api.account.{account}": "*"} if purpose == "pages-token" else {
        f"com.cloudflare.edge.r2.bucket.{account}_default_{BUCKET}": "*",
    }
    policies = [{
        "effect": policy["effect"], "resources": policy["resources"],
        "permissions": sorted(group["name"] for group in policy["permission_groups"]),
    } for policy in record["policies"]]
    expected = [{"effect": "allow", "resources": scope, "permissions": PERMISSIONS[purpose]}]
    if record.get("status") != "active" or policies != expected:
        raise ClickException(f"Unexpected {purpose} token policy/status; refusing credential delivery")


def verify_credentials(outputs: dict) -> dict:
    from boto3 import client as s3_client
    from botocore.config import Config
    from requests import Session

    account = (os.environ.get("CLOUDFLARE_ACCOUNT_ID") or os.environ.get("OA_CF_ACCT", "")).strip()
    iac = os.environ.get("CF_IAC_TOKEN", "").strip()
    if not account or not iac:
        raise ClickException("Need the existing account ID and CF_IAC_TOKEN; values are never printed")
    report = {"policies": {}, "r2": {}, "pages": False, "d1": False}
    with Session() as http:
        for purpose in PERMISSIONS:
            key = "isolation_pages_token_id" if purpose == "pages-token" else f"isolation_r2_{purpose}_key_id"
            record = cf_get(http, f"accounts/{account}/tokens/{outputs[key]}", iac)
            verify_policy(record, account, purpose)
            report["policies"][purpose] = "verified"
        page_token = outputs["isolation_pages_token"]
        cf_get(http, f"accounts/{account}/pages/projects/oa-cw-s3-usage", page_token)
        report["pages"] = True
        config = tomllib.loads((ROOT / "site/wrangler.toml").read_text())
        database = config["d1_databases"][0]["database_id"]
        cf_get(http, f"accounts/{account}/d1/database/{database}", page_token)
        report["d1"] = True
    for purpose in ("reader", "publisher"):
        s3 = s3_client("s3", endpoint_url=f"https://{account}.r2.cloudflarestorage.com", region_name="auto",
                       aws_access_key_id=outputs[f"isolation_r2_{purpose}_key_id"],
                       aws_secret_access_key=outputs[f"isolation_r2_{purpose}_secret"],
                       config=Config(retries={"max_attempts": 3}, connect_timeout=15, read_timeout=60))
        try:
            objects = s3.list_objects_v2(Bucket=BUCKET, MaxKeys=1).get("Contents", [])
            if not objects:
                raise ClickException("CW serving bucket is unexpectedly empty")
            response = s3.get_object(Bucket=BUCKET, Key=objects[0]["Key"], Range="bytes=0-63")
            try:
                content = response["Body"].read(64)
                if not content:
                    raise ClickException("CW serving range read returned no bytes")
            finally:
                response["Body"].close()
            report["r2"][purpose] = "list-and-range-read-verified"
        finally:
            s3.close()
    return report


def canary(outputs: dict) -> dict:
    """Retain a tiny unique audit object; never overwrite or delete serving data."""
    from boto3 import client as s3_client

    account = (os.environ.get("CLOUDFLARE_ACCOUNT_ID") or os.environ.get("OA_CF_ACCT", "")).strip()
    key = f"probes/cw-isolation/{uuid4()}.json"
    content = b'{"purpose":"cw-isolation-credential-check","version":1}\n'
    clients = [s3_client("s3", endpoint_url=f"https://{account}.r2.cloudflarestorage.com", region_name="auto",
                         aws_access_key_id=outputs[f"isolation_r2_{purpose}_key_id"],
                         aws_secret_access_key=outputs[f"isolation_r2_{purpose}_secret"])
               for purpose in ("publisher", "reader")]
    try:
        clients[0].put_object(Bucket=BUCKET, Key=key, Body=content, ContentType="application/json", IfNoneMatch="*")
        response = clients[1].get_object(Bucket=BUCKET, Key=key)
        try:
            if response["Body"].read(len(content) + 1) != content:
                raise ClickException("Publisher/reader canary mismatch")
        finally:
            response["Body"].close()
    finally:
        for client in clients:
            client.close()
    return {"bucket": BUCKET, "key": key, "write-and-reader-read": "verified", "retained": True}


def access_payload(response: dict) -> bytes:
    payload = response["payload"]
    data = b64decode(payload["data"], validate=True)
    if int(payload["dataCrc32c"]) != crc32c(data):
        raise ClickException("Secret Manager payload checksum mismatch")
    return data


def public_plot(
    response: object,
    name: str,
) -> bytes | None:
    if response.status_code == 404:
        return None
    if response.status_code != 200 or response.headers.get("Content-Type", "").split(";", 1)[0] != "image/png":
        raise ClickException(f"Existing digest plot fetch failed: {name}, HTTP {response.status_code}")
    content = response.content
    if len(content) > 25 * 1024 * 1024 or not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ClickException(f"Existing digest plot is not a bounded PNG: {name}")
    return content


def comparable_site_json(
    body: object,
    environment: str,
) -> object:
    """Only the intentionally different dev root label is normalized."""
    if not isinstance(body, dict) or body.get("path") != "" or "tree" not in body:
        return body
    labels = {"preview": "Marin CoreWeave (dev)", "production": "Marin CoreWeave"}
    if body["tree"]["n"] != labels[environment]:
        raise ClickException("Unexpected site root label")
    result = deepcopy(body)
    result["tree"]["n"] = "<CW root label>"
    return result


def install_secret(
    client: object,
    secret_id: str,
    data: bytes,
) -> dict:
    if secret_id not in SECRETS:
        raise ClickException("Secret is outside the isolation delivery allowlist")
    base = f"https://secretmanager.googleapis.com/v1/projects/{PROJECT}/secrets/{secret_id}"
    latest = client.get(base + "/versions/latest:access", timeout=60)
    if latest.status_code == 200:
        if access_payload(latest.json()) != data:
            raise ClickException(f"Existing {secret_id} value differs; refusing to replace it")
        return {"secret": secret_id, "action": "verified-existing"}
    if latest.status_code != 404:
        raise ClickException(f"Secret access failed: HTTP {latest.status_code}")
    # A 404 must mean an empty, existing container, not a disabled/deleted version.
    versions = client.get(base + "/versions", params={"pageSize": 1}, timeout=60)
    if versions.status_code != 200 or versions.json().get("versions") or versions.json().get("nextPageToken"):
        raise ClickException(f"{secret_id} is not an empty readable container")
    added = client.post(base + ":addVersion", json={"payload": {
        "data": b64encode(data).decode(), "dataCrc32c": str(crc32c(data)),
    }}, timeout=60)
    if added.status_code != 200:
        raise ClickException(f"Secret version creation failed: HTTP {added.status_code}")
    name = added.json()["name"]
    match = re.fullmatch(rf"projects/(?:{PROJECT}|[0-9]+)/secrets/{secret_id}/versions/([1-9][0-9]*)", name)
    if match is None:
        raise ClickException("Secret Manager returned an unexpected resource identity")
    version = match[1]
    checked = client.get(base + f"/versions/{version}:access", timeout=60)
    if checked.status_code != 200 or access_payload(checked.json()) != data:
        raise ClickException("Secret version readback failed")
    return {"secret": secret_id, "action": "created-and-verified", "version": version}


@group()
def cli() -> None:
    """Only new CW isolation credentials are accepted; no live bindings are switched."""


@cli.command()
@option("-a", "--apply", is_flag=True, help="Write and retain one tiny R2 canary in the CW bucket.")
def verify(apply: bool) -> None:
    outputs = load_outputs()
    report = verify_credentials(outputs)
    if apply:
        report["canary"] = canary(outputs)
    print(json.dumps(report, indent=2))


@cli.command("install-job")
@option("-a", "--apply", is_flag=True, help="Install new secret versions after live credential verification.")
def install_job(apply: bool) -> None:
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    outputs = load_outputs()
    report = {"verification": verify_credentials(outputs), "delivery": []}
    if apply:
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"], quota_project_id=PROJECT)
        with AuthorizedSession(credentials) as http:
            report["delivery"] = [install_secret(http, name, outputs[key].encode()) for name, key in SECRETS.items()]
    else:
        report["delivery"] = [{"secret": name, "action": "planned"} for name in SECRETS]
    print(json.dumps(report, indent=2))


def wrangler(
    outputs: dict,
    args: list[str],
    stdin: str | None = None,
) -> str:
    env = {**os.environ, "CLOUDFLARE_API_TOKEN": outputs["isolation_pages_token"],
           "WRANGLER_LOG_PATH": str(ROOT / "tmp/cw-isolation-site-wrangler.log")}
    result = run(["pnpm", "--dir", str(ROOT / "infra/cf/cron-dispatch"), "exec", "wrangler", "--cwd", str(ROOT / "site"), *args],
                 env=env, input=stdin, capture_output=True, text=True, cwd=ROOT, check=False)
    diagnostic = result.stdout + result.stderr
    sensitive = [outputs[key] for key in (*SECRETS.values(), "isolation_r2_reader_key_id", "isolation_r2_reader_secret")]
    sensitive.append(os.environ.get("CF_IAC_TOKEN", "").strip())
    for value in sensitive:
        if value:
            diagnostic = diagnostic.replace(value, "<redacted>")
    if result.returncode:
        raise ClickException("CW site operation failed:\n" + diagnostic)
    return diagnostic


@cli.command("install-site")
@option("-a", "--apply", is_flag=True, help="Install the CW-bucket read-only pair in the selected Pages environment")
@argument("environment", type=Choice(["preview", "production"]))
def install_site(apply: bool, environment: str) -> None:
    """Send secrets through stdin in memory, never through arguments or files."""
    outputs = load_outputs()
    report = {"verification": verify_credentials(outputs), "environment": environment, "action": "planned"}
    if apply:
        values = {"STORE_ACCESS_KEY_ID": outputs["isolation_r2_reader_key_id"],
                  "STORE_SECRET_ACCESS_KEY": outputs["isolation_r2_reader_secret"]}
        report["log"] = wrangler(outputs, ["pages", "secret", "bulk", "--project-name", "oa-cw-s3-usage", "--env", environment], json.dumps(values))
        report["action"] = "installed; redeployment and live reads required"
    print(json.dumps(report, indent=2))


@cli.command("audit-logs")
def audit_logs() -> None:
    """Report only whether new credential values leaked into local operation logs."""
    outputs = load_outputs()
    values = [outputs[key].encode() for key in (*SECRETS.values(), "isolation_r2_reader_secret")]
    logs = sorted((ROOT / "tmp").glob("cw-isolation-*.log"))
    leaked = []
    for path in logs:
        content = path.read_bytes()
        if any(value in content for value in values):
            leaked.append(path.name)
    if leaked:
        raise ClickException("Credential values found in operation logs: " + ", ".join(leaked))
    print(json.dumps({"logs_checked": len(logs), "plaintext_credential_present": False}))


@cli.command("deploy-site")
@option("-a", "--apply", is_flag=True, help="Build and deploy committed CW source without pushing")
@argument("environment", type=Choice(["preview", "production"]))
def deploy_site(apply: bool, environment: str) -> None:
    """Use the guarded site release path; a stale prebuilt directory is not a release."""
    config = tomllib.loads((ROOT / "site/wrangler.toml").read_text())
    variables = config["vars"] if environment == "production" else config["env"]["preview"]["vars"]
    if config["name"] != "oa-cw-s3-usage" or variables["DATA_BUCKET"] != "oa-cw-s3-usage-data":
        raise ClickException("Site configuration does not match the CW isolation profile")
    command = [str(ROOT / "site/deploy"), "-P", *(["--dev"] if environment == "preview" else [])]
    if not apply:
        print(json.dumps({"environment": environment, "action": "planned", "command": command}, indent=2))
        return
    run([str(ROOT / "scripts/require-clean")], cwd=ROOT, check=True)
    outputs = load_outputs()
    verify_credentials(outputs)
    result = run(command, cwd=ROOT, env={**os.environ, "CLOUDFLARE_API_TOKEN": outputs["isolation_pages_token"]},
                 capture_output=True, text=True, check=False)
    diagnostic = result.stdout + result.stderr
    for key in (*SECRETS.values(), "isolation_r2_reader_key_id", "isolation_r2_reader_secret"):
        diagnostic = diagnostic.replace(outputs[key], "<redacted>")
    if result.returncode:
        raise ClickException("CW site deployment failed:\n" + diagnostic)
    print(json.dumps({"environment": environment, "action": "deployed; live checks required", "log": diagnostic}, indent=2))


@cli.command("verify-site")
@option("-o", "--output", type=ClickPath(path_type=Path), required=True, help="New private response evidence directory")
@option("-u", "--deployment-url", help="Exact immutable CW Pages deployment URL for cache-miss verification")
@argument("environment", type=Choice(["preview", "production"]))
def verify_site(
    output: Path,
    deployment_url: str | None,
    environment: str,
) -> None:
    """Compare authenticated history/tree/search/diff/series with the other host."""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    from requests import Session

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"], quota_project_id=PROJECT)
    with AuthorizedSession(credentials) as auth:
        response = auth.get(f"https://secretmanager.googleapis.com/v1/projects/{PROJECT}/secrets/cw-s3-job-grant/versions/latest:access", timeout=60)
        if response.status_code != 200:
            raise ClickException(f"Job viewer grant access failed: HTTP {response.status_code}")
        token = access_payload(response.json()).decode().strip()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    hosts = {"preview": "https://dev.cw.oa.dev", "production": "https://cw.oa.dev"}
    if deployment_url:
        if re.fullmatch(r"https://[a-f0-9]{8}\.oa-cw-s3-usage\.pages\.dev", deployment_url) is None:
            raise ClickException("Deployment URL is outside the immutable CW Pages allowlist")
        hosts[environment] = deployment_url
    report = {"environment": environment, "comparisons": [], "ok": False}
    fetched = {}
    headers = {}
    with Session() as http:
        http.headers.update({"Authorization": "Bearer " + token, "User-Agent": "gcs-usage-healthcheck/1.0"})

        def get(host: str, path: str, label: str) -> object:
            response = http.get(hosts[host] + path, timeout=90)
            headers[f"{host}/{label}"] = {"status": response.status_code, "x-cache": response.headers.get("x-cache"),
                                         "server-timing": response.headers.get("server-timing")}
            (output / f"{host}-{label}.json").write_bytes(response.content)
            if response.status_code != 200:
                raise ClickException(f"{host}/{label} failed: HTTP {response.status_code}; private evidence retained")
            return response.json()

        for host in hosts:
            fetched[host] = {"scans": get(host, "/data/cw/scans.json", "scans")}
        scans = fetched[environment]["scans"]
        if not isinstance(scans, list) or len(scans) < 2 or fetched["preview"]["scans"] != fetched["production"]["scans"]:
            raise ClickException("Old/new published scan histories differ or are incomplete")
        latest, previous = scans[:2]
        paths = {
            "latest-meta": f"/data/cw/{latest}/meta.json",
            "oldest-meta": f"/data/cw/{scans[-1]}/meta.json",
            "tree": f"/api/subtree?date={latest}&w=128&h=128",
            "history-tree": f"/api/subtree?date={previous}&w=128&h=128",
            "search": f"/api/subtree?date={latest}&w=128&h=128&q=snowball",
            "diff": f"/api/diff?from={previous}&to={latest}&w=128&h=128&summary=1",
            "series": "/api/series",
        }
        for label, path in paths.items():
            for host in hosts:
                fetched[host][label] = get(host, path, label)
            if comparable_site_json(fetched["preview"][label], "preview") != comparable_site_json(fetched["production"][label], "production"):
                raise ClickException(f"Old/new {label} JSON differs; review private response evidence")
            report["comparisons"].append({"route": label, "result": "exact-JSON-match-except-dev-root-label"})
        tree = fetched[environment]["tree"]
        if tree["tree"]["b"] != fetched[environment]["latest-meta"]["total_bytes"] or tree["nodes"] <= 0:
            raise ClickException("Latest tree is empty or differs from published scan total")
        warm = get(environment, paths["tree"], "tree-warm")
        if warm != tree:
            raise ClickException("Cold/warm tree JSON differs")
    report["ok"] = True
    report["latest"] = latest
    report["repeated_tree"] = "exact-JSON-match"
    if deployment_url:
        if headers[f"{environment}/tree"]["x-cache"] != "miss":
            raise ClickException("Immutable deployment tree was not a cache miss; fresh serving proof is incomplete")
        report["cold_tree"] = "verified-cache-miss"
        if headers[f"{environment}/tree-warm"]["x-cache"] != "hit":
            raise ClickException("Repeated immutable deployment tree was not a cache hit")
        report["warm_tree"] = "verified-cache-hit"
    (output / "headers.json").write_text(json.dumps(headers, indent=2) + "\n")
    (output / "_SUCCESS.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


@cli.command("deploy-icons")
@option("-a", "--apply", is_flag=True, help="Deploy only to the new CW icons project.")
@argument("kind", type=Choice(["avatars", "plots"]))
def deploy_icons(apply: bool, kind: str) -> None:
    """Copy active public plot bytes, preserving names; never modify digest state."""
    from google.cloud.storage import Client
    from requests import Session

    branch = "main" if kind == "avatars" else "cw"
    report = {"project": "oa-cw-s3-usage-icons", "branch": branch, "action": "planned"}
    if not apply:
        print(json.dumps(report, indent=2))
        return
    source_sha = run([str(ROOT / "scripts/require-clean")], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    outputs = load_outputs()
    verify_credentials(outputs)
    assets = ROOT / "job/icons"
    if kind == "plots":
        assets = Path(mkdtemp(prefix="cw-isolation-plots-", dir=ROOT / "tmp"))
        names = set()
        client = Client(project=PROJECT)
        for blob in client.list_blobs("oa-gcs-usage-dvx", prefix="digest/cw/"):
            if not blob.name.endswith(".json"):
                continue
            if blob.size > 2 * 1024 * 1024:
                raise ClickException("Digest state exceeds the bounded JSON read limit")
            state = json.loads(blob.download_as_bytes(if_generation_match=int(blob.generation)))
            name = state.get("plot_name")
            if name is not None:
                if not re.fullmatch(r"plot-[0-9a-f]{32}\.png", name):
                    raise ClickException("Unrecognized digest plot filename")
                names.add(name)
        if not names:
            raise ClickException("No active digest plots found; refusing an empty asset deployment")
        report["plots"] = []
        report["already_missing_at_source"] = []
        with Session() as http:
            for name in sorted(names):
                response = http.get("https://cw.gcs-usage-icons.pages.dev/" + name, timeout=60)
                content = public_plot(response, name)
                if content is None:
                    report["already_missing_at_source"].append(name)
                    continue
                (assets / name).write_bytes(content)
                report["plots"].append({"name": name, "sha256": sha256(content).hexdigest()})
        if not report["plots"]:
            raise ClickException("No public plots remain at source; refusing an empty deployment")
        (assets / "_headers").write_bytes((ROOT / "job/icons-cw/_headers").read_bytes())
    env = {**os.environ, "CLOUDFLARE_API_TOKEN": outputs["isolation_pages_token"],
           "WRANGLER_LOG_PATH": str(ROOT / f"tmp/cw-isolation-icons-{kind}-wrangler.log")}
    result = run(["pnpm", "--dir", str(ROOT / "infra/cf/cron-dispatch"), "exec", "wrangler", "pages", "deploy",
                  str(assets), "--project-name", report["project"], "--branch", branch, "--commit-hash", source_sha, "--commit-dirty=false"],
                 env=env, capture_output=True, text=True, cwd=ROOT, check=False)
    # Deployment logs contain no credential values, but still redact before emitting.
    diagnostic = result.stdout + result.stderr
    for value in (outputs["isolation_pages_token"], os.environ.get("CF_IAC_TOKEN", "").strip()):
        if value:
            diagnostic = diagnostic.replace(value, "<redacted>")
    if result.returncode:
        raise ClickException("CW icons deployment failed:\n" + diagnostic)
    report["action"] = "deployed"
    report["log"] = diagnostic
    if kind == "plots":
        with Session() as http:
            for plot in report["plots"]:
                content = public_plot(http.get("https://cw.oa-cw-s3-usage-icons.pages.dev/" + plot["name"], timeout=60), plot["name"])
                if content is None or sha256(content).hexdigest() != plot["sha256"]:
                    raise ClickException("New public plot readback differs from source bytes")
        report["public_readback"] = "sha256-verified"
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    cli()
