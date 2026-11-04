from base64 import b64encode
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

from click import ClickException
from google_crc32c import value as crc32c
import pytest
from click.testing import CliRunner

spec = spec_from_file_location("cw_credentials", Path(__file__).resolve().parents[2] / "job/cw-isolation-credentials.py")
credentials = module_from_spec(spec)
spec.loader.exec_module(credentials)


def record(purpose: str) -> dict:
    resource = "com.cloudflare.api.account.fixture" if purpose == "pages-token" else "com.cloudflare.edge.r2.bucket.fixture_default_oa-cw-s3-usage-index"
    return {"status": "active", "policies": [{"effect": "allow", "resources": {resource: "*"},
            "permission_groups": [{"name": name, "id": str(i)} for i, name in enumerate(credentials.PERMISSIONS[purpose])]}]}


@pytest.mark.parametrize("purpose", ["reader", "publisher", "pages-token"])
def test_exact_live_policy(purpose: str) -> None:
    assert credentials.verify_policy(record(purpose), "fixture", purpose) is None


@pytest.mark.parametrize("mutation", ["scope", "extra-policy", "extra-permission", "inactive", "deny"])
def test_reject_unreviewed_policy(mutation: str) -> None:
    actual = record("reader")
    if mutation == "scope":
        actual["policies"][0]["resources"] = {"com.cloudflare.api.account.fixture": "*"}
    elif mutation == "extra-policy":
        actual["policies"].append(actual["policies"][0])
    elif mutation == "extra-permission":
        actual["policies"][0]["permission_groups"].append({"name": "Workers R2 Storage Bucket Item Write"})
    elif mutation == "inactive":
        actual["status"] = "disabled"
    else:
        actual["policies"][0]["effect"] = "deny"
    with pytest.raises(ClickException, match="^Unexpected reader token policy/status; refusing credential delivery$"):
        credentials.verify_policy(actual, "fixture", "reader")


def payload(data: bytes) -> dict:
    return {"payload": {"data": b64encode(data).decode(), "dataCrc32c": str(crc32c(data))}}


class Http:
    def __init__(self, responses: list[tuple[int, dict]]) -> None:
        self.responses = iter(responses)
        self.calls = []

    def request(self, method: str, url: str, kwargs: dict) -> object:
        self.calls.append((method, url, kwargs))
        status, body = next(self.responses)
        return SimpleNamespace(status_code=status, json=lambda: body)

    def get(self, url: str, **kwargs) -> object:
        return self.request("GET", url, kwargs)

    def post(self, url: str, **kwargs) -> object:
        return self.request("POST", url, kwargs)


def test_equal_existing_secret_is_not_rewritten() -> None:
    http = Http([(200, payload(b"fixture-secret"))])
    sid = "cw-s3-cf-pages-token"
    assert credentials.install_secret(http, sid, b"fixture-secret") == {"secret": sid, "action": "verified-existing"}
    assert http.calls == [("GET", f"https://secretmanager.googleapis.com/v1/projects/{credentials.PROJECT}/secrets/{sid}/versions/latest:access", {"timeout": 60})]


@pytest.mark.parametrize("project", ["oa-internal-450019", "123456789"])
def test_empty_container_delivery_and_readback(project: str) -> None:
    sid = "cw-s3-cf-pages-token"
    data = b"fixture-secret"
    base = f"https://secretmanager.googleapis.com/v1/projects/{credentials.PROJECT}/secrets/{sid}"
    http = Http([(404, {}), (200, {}), (200, {"name": f"projects/{project}/secrets/{sid}/versions/1"}), (200, payload(data))])
    assert credentials.install_secret(http, sid, data) == {"secret": sid, "action": "created-and-verified", "version": "1"}
    assert http.calls == [
        ("GET", base + "/versions/latest:access", {"timeout": 60}),
        ("GET", base + "/versions", {"params": {"pageSize": 1}, "timeout": 60}),
        ("POST", base + ":addVersion", {"json": payload(data), "timeout": 60}),
        ("GET", base + "/versions/1:access", {"timeout": 60}),
    ]


@pytest.mark.parametrize("responses, message", [
    ([(200, payload(b"different"))], "Existing cw-s3-cf-pages-token value differs; refusing to replace it"),
    ([(403, {})], "Secret access failed: HTTP 403"),
    ([(404, {}), (200, {"versions": [{"state": "DISABLED"}]})], "cw-s3-cf-pages-token is not an empty readable container"),
    ([(404, {}), (404, {})], "cw-s3-cf-pages-token is not an empty readable container"),
])
def test_delivery_fails_closed(responses: list, message: str) -> None:
    http = Http(responses)
    with pytest.raises(ClickException) as raised:
        credentials.install_secret(http, "cw-s3-cf-pages-token", b"fixture-secret")
    assert str(raised.value) == message
    assert [method for method, _, _ in http.calls] == ["GET"] * len(responses)


def test_old_shared_secret_is_never_a_delivery_target() -> None:
    http = Http([])
    with pytest.raises(ClickException, match="^Secret is outside the isolation delivery allowlist$"):
        credentials.install_secret(http, "cf-pages-token", b"fixture-secret")
    assert http.calls == []


def test_corrupted_access_payload_fails() -> None:
    actual = payload(b"fixture-secret")
    actual["payload"]["dataCrc32c"] = "0"
    with pytest.raises(ClickException, match="^Secret Manager payload checksum mismatch$"):
        credentials.access_payload(actual)


def test_public_plot_bytes_and_missing_history() -> None:
    content = b"\x89PNG\r\n\x1a\nfixture"
    assert credentials.public_plot(SimpleNamespace(status_code=200, headers={"Content-Type": "image/png; charset=binary"}, content=content), "plot.png") == content
    assert credentials.public_plot(SimpleNamespace(status_code=404), "old.png") is None


@pytest.mark.parametrize("status, mime, content, expected", [
    (503, "image/png", b"", "Existing digest plot fetch failed: plot.png, HTTP 503"),
    (200, "text/html", b"", "Existing digest plot fetch failed: plot.png, HTTP 200"),
    (200, "image/png", b"html", "Existing digest plot is not a bounded PNG: plot.png"),
])
def test_invalid_plot_response_is_not_skipped(status: int, mime: str, content: bytes, expected: str) -> None:
    with pytest.raises(ClickException) as raised:
        credentials.public_plot(SimpleNamespace(status_code=status, headers={"Content-Type": mime}, content=content), "plot.png")
    assert str(raised.value) == expected


def test_preview_secret_delivery_is_stdin_only_and_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    outputs = {key: key + "-fixture-value" for key in [*credentials.SECRETS.values(), "isolation_r2_reader_key_id", "isolation_r2_reader_secret"]}
    calls = []

    def execute(args: list[str], **kwargs: object) -> object:
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=outputs["isolation_r2_reader_secret"], stderr="")

    monkeypatch.setattr(credentials, "run", execute)
    assert credentials.wrangler(outputs, ["pages", "secret", "bulk", "--env", "preview"], "fixture-stdin") == "<redacted>"
    [call] = calls
    args, kwargs = call
    assert args == ["pnpm", "--dir", str(credentials.ROOT / "infra/cf/cron-dispatch"), "exec", "wrangler", "--cwd", str(credentials.ROOT / "site"), "pages", "secret", "bulk", "--env", "preview"]
    assert kwargs["input"] == "fixture-stdin"
    assert kwargs["env"]["CLOUDFLARE_API_TOKEN"] == outputs["isolation_pages_token"]
    assert kwargs["capture_output"] is True


def test_site_comparison_only_normalizes_the_declared_root_label() -> None:
    body = {"path": "", "tree": {"n": "Marin CoreWeave (dev)", "b": 123, "c": [{"n": "data (dev)"}]}, "nodes": 2}
    assert credentials.comparable_site_json(body, "preview") == {
        "path": "", "tree": {"n": "<CW root label>", "b": 123, "c": [{"n": "data (dev)"}]}, "nodes": 2,
    }
    assert body["tree"]["n"] == "Marin CoreWeave (dev)"
    assert credentials.comparable_site_json({"other": "unchanged"}, "preview") == {"other": "unchanged"}
    with pytest.raises(ClickException, match="^Unexpected site root label$"):
        credentials.comparable_site_json(body, "production")


def test_live_verifier_compares_all_routes_without_exposing_the_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import google.auth
    import google.auth.transport.requests
    import requests
    import json

    scans = ["2026-10-13T0601", "2026-10-13T0001"]
    calls = []
    counts = {}

    class Session:
        def __init__(self, *args: object) -> None:
            self.headers = {}

        def __enter__(self) -> object:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, url: str, **kwargs: object) -> object:
            calls.append(url)
            counts[url] = counts.get(url, 0) + 1
            if url.startswith("https://secretmanager.googleapis.com/"):
                return SimpleNamespace(status_code=200, json=lambda: payload(b"fixture-viewer-grant"))
            if "/scans.json" in url:
                body = scans
            elif "/meta.json" in url:
                body = {"total_bytes": 123}
            elif "/api/subtree" in url:
                name = "Marin CoreWeave (dev)" if "dev.cw.oa.dev" in url else "Marin CoreWeave"
                body = {"path": "", "tree": {"n": name, "b": 123}, "nodes": 1}
            else:
                body = {"fixture": "same on both hosts"}
            return SimpleNamespace(status_code=200, content=json.dumps(body).encode(), json=lambda: body,
                                   headers={"x-cache": "miss" if counts[url] == 1 else "hit"})

    monkeypatch.setattr(google.auth, "default", lambda **kwargs: (object(), None))
    monkeypatch.setattr(google.auth.transport.requests, "AuthorizedSession", Session)
    monkeypatch.setattr(requests, "Session", Session)
    output = tmp_path / "evidence"
    result = CliRunner().invoke(credentials.cli, ["verify-site", "-o", str(output), "-u", "https://12345678.oa-cw-s3-usage.pages.dev", "production"])
    assert result.exit_code == 0, result.exception
    expected = {"environment": "production", "comparisons": [
        {"route": label, "result": "exact-JSON-match-except-dev-root-label"}
        for label in ["latest-meta", "oldest-meta", "tree", "history-tree", "search", "diff", "series"]
    ], "ok": True, "latest": scans[0], "repeated_tree": "exact-JSON-match", "cold_tree": "verified-cache-miss", "warm_tree": "verified-cache-hit"}
    assert json.loads(result.output) == expected
    assert json.loads((output / "_SUCCESS.json").read_text()) == expected
    assert len(calls) == 18
    assert "fixture-viewer-grant" not in result.output  # Secondary leak check after exact output verification.
