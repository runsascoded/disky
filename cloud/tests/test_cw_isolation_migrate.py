from importlib.util import module_from_spec, spec_from_file_location
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner
import pytest

spec = spec_from_file_location("cw_migrate", Path(__file__).resolve().parents[2] / "job/cw-isolation-migrate.py")
migrate = module_from_spec(spec)
spec.loader.exec_module(migrate)

GEN = "2026-10-10cw"
RECORD = {"bucket": migrate.SOURCE, "key": "cw-l2/scan/path-index.parquet", "generation": "42",
          "bytes": 123, "storage_class": "STANDARD", "md5": "md5", "crc32c": "crc"}


def blob(**changes: object) -> SimpleNamespace:
    return SimpleNamespace(**({"generation": 99, "size": 123, "storage_class": "STANDARD", "md5_hash": "md5", "crc32c": "crc"} | changes))


def state_records() -> list[dict]:
    return [RECORD | {"bucket": migrate.SCRATCH_SOURCE, "key": f"static-names/{GEN}/state/2026-10-13T0601/{kind}/r{i:04d}.{extension}"}
            for kind, extension in (("copen", "parquet"), ("done", "json")) for i in range(2)]


def test_preserve_only_complete_published_scratch_state() -> None:
    records = state_records()
    shuffle = RECORD | {"bucket": migrate.SCRATCH_SOURCE, "key": f"static-names/{GEN}/shuffle/part.parquet"}
    assert migrate.scratch_state([shuffle, *records], GEN, "2026-10-13T0601", 2) == records


@pytest.mark.parametrize("mutation, expected", [
    ("missing", "published scratch state is incomplete or contains unexpected objects"),
    ("extra", "published scratch state is incomplete or contains unexpected objects"),
    ("lease", "merge lease exists; drain/review it before state preservation"),
    ("bucket", "scratch records outside CW source generation"),
])
def test_scratch_plan_fails_closed(mutation: str, expected: str) -> None:
    records = state_records()
    if mutation == "missing":
        records.pop()
    elif mutation == "extra":
        records.append(records[0] | {"key": f"static-names/{GEN}/state/2026-10-13T0601/unknown"})
    elif mutation == "lease":
        records.append(records[0] | {"key": f"static-names/{GEN}/merge.lease.json"})
    else:
        records[0]["bucket"] = migrate.SOURCE
    with pytest.raises(ValueError) as raised:
        migrate.scratch_state(records, GEN, "2026-10-13T0601", 2)
    assert str(raised.value) == expected


@pytest.mark.parametrize("changes", [{"size": 124}, {"md5_hash": "wrong"}, {"crc32c": "wrong"}, {"storage_class": "NEARLINE"}, {"generation": None}])
def test_destination_verification_is_exact(changes: dict) -> None:
    with pytest.raises(ValueError, match="^destination checksum/size/class mismatch: cw-l2/scan/path-index.parquet$"):
        migrate.verify_blob(blob(**changes), RECORD)


def test_destination_checksum_matches() -> None:
    assert migrate.verify_blob(blob(), RECORD) is None


class FakeClient:
    def __init__(self, existing: object = None) -> None:
        self.existing = existing
        self.calls = []
        self.target = blob()
        self.target.rewrite = self.rewrite
        self.target.reload = self.reload

    def bucket(self, name: str) -> object:
        self.calls.append(("bucket", name))
        return SimpleNamespace(get_blob=self.get_blob, blob=lambda key, **kwargs: self.new_blob(name, key, kwargs))

    def get_blob(self, key: str) -> object:
        self.calls.append(("get_blob", key))
        return self.existing

    def new_blob(self, bucket: str, key: str, kwargs: dict) -> object:
        self.calls.append(("blob", bucket, key, kwargs))
        return self.target if bucket == migrate.DESTINATION else SimpleNamespace(key=key, generation=kwargs["generation"])

    def rewrite(self, source: object, **kwargs: object) -> tuple:
        self.calls.append(("rewrite", source.key, source.generation, kwargs))
        return ("next", 60, 123) if kwargs["token"] is None else (None, 123, 123)

    def reload(self, **kwargs: object) -> None:
        self.calls.append(("reload", kwargs))


def test_plan_does_not_copy_missing_object() -> None:
    client = FakeClient()
    assert migrate.copy_one(client, RECORD, False) == {"key": RECORD["key"], "source_generation": "42", "action": "missing"}
    assert client.calls == [("bucket", migrate.DESTINATION), ("get_blob", RECORD["key"])]


def test_scratch_plan_only_targets_its_separate_bucket() -> None:
    client = FakeClient()
    record = state_records()[0]
    assert migrate.copy_one(client, record, False, migrate.SCRATCH_DESTINATION) == {
        "key": record["key"], "source_generation": "42", "action": "missing",
    }
    assert client.calls == [("bucket", migrate.SCRATCH_DESTINATION), ("get_blob", record["key"])]


@pytest.mark.parametrize("source, destination", [
    (migrate.SOURCE, migrate.SCRATCH_DESTINATION),
    (migrate.SCRATCH_SOURCE, migrate.DESTINATION),
    (migrate.SOURCE, "unreviewed"),
])
def test_crossed_copy_pairs_are_rejected(source: str, destination: str) -> None:
    client = FakeClient()
    with pytest.raises(ValueError, match="^unreviewed source/destination pair$"):
        migrate.copy_one(client, RECORD | {"bucket": source}, True, destination)
    assert client.calls == []


@pytest.mark.parametrize("apply", [False, True])
def test_existing_matching_destination_is_never_rewritten(apply: bool) -> None:
    client = FakeClient(blob())
    assert migrate.copy_one(client, RECORD, apply) == {"key": RECORD["key"], "source_generation": "42", "destination_generation": "99", "action": "verified"}
    assert client.calls == [("bucket", migrate.DESTINATION), ("get_blob", RECORD["key"])]


def test_conflicting_destination_is_never_overwritten() -> None:
    client = FakeClient(blob(crc32c="wrong"))
    with pytest.raises(ValueError, match="^destination checksum/size/class mismatch: cw-l2/scan/path-index.parquet$"):
        migrate.copy_one(client, RECORD, True)
    assert client.calls == [("bucket", migrate.DESTINATION), ("get_blob", RECORD["key"])]


def test_rewrite_pins_source_requires_absent_destination_and_follows_tokens() -> None:
    client = FakeClient()
    assert migrate.copy_one(client, RECORD, True) == {"key": RECORD["key"], "source_generation": "42", "destination_generation": "99", "action": "copied"}
    assert client.calls == [
        ("bucket", migrate.DESTINATION), ("get_blob", RECORD["key"]),
        ("bucket", migrate.SOURCE), ("blob", migrate.SOURCE, RECORD["key"], {"generation": 42}),
        ("blob", migrate.DESTINATION, RECORD["key"], {}),
        ("rewrite", RECORD["key"], 42, {"token": None, "if_generation_match": 0, "if_source_generation_match": 42}),
        ("rewrite", RECORD["key"], 42, {"token": "next", "if_generation_match": 0, "if_source_generation_match": 42}),
        ("reload", {"if_generation_match": 99}),
    ]


def test_control_documents_are_deferred() -> None:
    assert [migrate.controls_key(key, GEN) for key in [
        f"static-names/{GEN}/scans.json", f"static-names/{GEN}/deltas/scan/scans.json",
        f"static-names/{GEN}/manifests/scan.m001.json", f"static-names/{GEN}/base.parquet",
        f"static-names/{GEN}/catalog/meta.json", "snapshots/cw/scan/meta.json",
        "static-names/another/scans.json",
    ]] == [True, True, True, False, False, False, False]


def fixture_inventory(
    directory: Path,
    record: dict,
) -> None:
    directory.mkdir()
    totals = {root: {"objects": 0, "bytes": 0} for root in [
        "cw-l2/", "snapshots/cw/", "digest/cw/", "probes/cw/", "cw-sweep/", "sweep/cw/",
        "meta-l2/", "snapshots/meta/", f"static-names/{GEN}/",
    ]}
    totals["cw-l2/"] = {"objects": 1, "bytes": record["bytes"]}
    (directory / "data.jsonl").write_text(json.dumps(record) + "\n")
    (directory / "_SUCCESS.json").write_text(json.dumps({"data_objects": 1}) + "\n")
    (directory / "summary.json").write_text(json.dumps({"static_gen": GEN, "missing_index_files": [], "data": totals}) + "\n")


def test_load_inventory_validates_approved_scope_and_totals(tmp_path: Path) -> None:
    directory = tmp_path / "inventory"
    fixture_inventory(directory, RECORD)
    assert migrate.load_inventory(directory) == ([RECORD], GEN)


@pytest.mark.parametrize("changes, message", [
    ({"bucket": "other"}, "outside approved CW source/scope"),
    ({"key": "listing/other/a"}, "outside approved CW source/scope"),
    ({"generation": "0"}, "incomplete pinned metadata"),
    ({"md5": None, "crc32c": None}, "incomplete pinned metadata"),
    ({"storage_class": "ARCHIVE"}, "unreviewed storage class"),
])
def test_unreviewed_source_is_refused(tmp_path: Path, changes: dict, message: str) -> None:
    directory = tmp_path / "inventory"
    record = RECORD | changes
    fixture_inventory(directory, record)
    with pytest.raises(ValueError, match=f"^{message}: {record['key']}$"):
        migrate.load_inventory(directory)


def test_apply_requires_explicit_drain_confirmation(tmp_path: Path) -> None:
    directory = tmp_path / "inventory"
    fixture_inventory(directory, RECORD)
    output = tmp_path / "output"
    result = CliRunner().invoke(migrate.main, ["copy", "--apply", "-i", str(directory), "-o", str(output)])
    assert result.exit_code == 1
    assert result.output == "Error: --apply requires --writers-drained and a fresh drained inventory\n"
    assert output.exists() is False


def test_control_input_pins_must_match_exact_current_copy_revision() -> None:
    record = RECORD | {"md5": "AQID"}
    scan = {"src": RECORD["key"], "generation": 42, "size": 123, "md5": "010203", "crc32c": "crc", "id": "scan", "ts": 1, "version": 2}
    control = {"key": "control", "document": {"bucket": migrate.SOURCE, "scans": [scan]}}
    assert migrate.audit_scan_pins([control], [record]) == []
    changed = {"key": "control", "document": {"bucket": migrate.SOURCE, "scans": [scan | {"generation": 41}]}}
    assert migrate.audit_scan_pins([changed], [record]) == [{
        "control": "control", "field": "/scans/0", "source": RECORD["key"],
        "error": "source pin differs from current inventory",
        "control_pin": {"generation": 41, "size": 123, "md5": "010203", "crc32c": "crc"},
        "inventory_pin": {"generation": 42, "size": 123, "md5": "010203", "crc32c": "crc"},
    }]


def test_missing_control_dependency_fails_closed() -> None:
    control = {"key": "control", "document": {"bucket": migrate.SOURCE, "scans": [{"src": "outside"}]}}
    assert migrate.audit_scan_pins([control], []) == [{"control": "control", "field": "/scans/0", "source": "outside", "error": "source outside inventory"}]


def test_wrong_control_bucket_is_not_silently_rebound() -> None:
    control = {"key": "control", "document": {"bucket": "another", "scans": []}}
    assert migrate.audit_scan_pins([control], []) == [{"control": "control", "field": "/bucket", "error": "unexpected bucket"}]


def test_control_remap_changes_only_bucket_and_input_generation() -> None:
    document = {"bucket": migrate.SOURCE, "layouts": ["cw-l2/{id}/index/{gen}/path-index.parquet"],
                "derived_from": "old-cw-generation", "hex_runs": "16,8",
                "scans": [{"id": "scan", "src": RECORD["key"], "generation": 42, "size": 123, "md5": "010203", "crc32c": "crc", "ts": 1, "version": 2}]}
    assert migrate.remap_control(document, {RECORD["key"]: "99"}) == {
        "bucket": migrate.DESTINATION, "layouts": ["cw-l2/{id}/index/{gen}/path-index.parquet"],
        "derived_from": "old-cw-generation", "hex_runs": "16,8",
        "scans": [{"id": "scan", "src": RECORD["key"], "generation": 99, "size": 123, "md5": "010203", "crc32c": "crc", "ts": 1, "version": 2}],
    }
    assert document["bucket"] == migrate.SOURCE
    assert document["scans"][0]["generation"] == 42


def test_manifest_document_is_unchanged() -> None:
    document = {"gen": GEN, "date": "scan", "scans": ["scan"], "runs": [{"key": "deltas/scan", "level": 0}]}
    assert migrate.remap_control(document, {}) == {"gen": GEN, "date": "scan", "scans": ["scan"], "runs": [{"key": "deltas/scan", "level": 0}]}


def test_unknown_embedded_reference_is_not_silently_left_on_source() -> None:
    document = {"bucket": migrate.SOURCE, "scans": [], "nested": {"uri": f"gs://{migrate.SOURCE}/unknown"}}
    with pytest.raises(ValueError, match="^unreviewed embedded source-bucket reference$"):
        migrate.remap_control(document, {})


@pytest.mark.parametrize("generation", ["0", "-1", "not-a-pin"])
def test_invalid_destination_pin_cannot_be_published(generation: str) -> None:
    with pytest.raises(ValueError, match="^invalid destination generation$"):
        migrate.remap_control({"bucket": migrate.SOURCE, "scans": [{"src": "key"}]}, {"key": generation})


def fixture_journal(
    directory: Path,
    inventory: Path,
    record: dict,
) -> None:
    directory.mkdir()
    (directory / "objects.jsonl").write_text(json.dumps(record) + "\n")
    (directory / "_SUCCESS.json").write_text(json.dumps({"payloads_checked": 1}) + "\n")
    (directory / "summary.json").write_text(json.dumps({"destination": migrate.DESTINATION, "inventory_sha256": sha256((inventory / "data.jsonl").read_bytes()).hexdigest()}) + "\n")


def test_successful_copy_journal_gives_only_verified_destination_pins(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory"
    journal = tmp_path / "journal"
    fixture_inventory(inventory, RECORD)
    fixture_journal(journal, inventory, {"key": RECORD["key"], "source_generation": "42", "destination_generation": "99", "action": "copied"})
    assert migrate.verified_copies(journal, inventory, [RECORD], GEN) == {RECORD["key"]: "99"}


@pytest.mark.parametrize("changes, message", [
    ({"action": "missing"}, "payload not verified: cw-l2/scan/path-index.parquet"),
    ({"source_generation": "41"}, "copy journal is incomplete"),
    ({"destination_generation": "0"}, "payload not verified: cw-l2/scan/path-index.parquet"),
])
def test_incomplete_or_mismatched_copy_journal_is_refused(tmp_path: Path, changes: dict, message: str) -> None:
    inventory = tmp_path / "inventory"
    journal = tmp_path / "journal"
    fixture_inventory(inventory, RECORD)
    fixture_journal(journal, inventory, {"key": RECORD["key"], "source_generation": "42", "destination_generation": "99", "action": "copied"} | changes)
    with pytest.raises(ValueError, match=f"^{message}$"):
        migrate.verified_copies(journal, inventory, [RECORD], GEN)


@pytest.mark.parametrize("apply", [False, True])
def test_control_publisher_uses_verified_pins_and_absent_destination_precondition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, apply: bool) -> None:
    from google.cloud import storage

    inventory = tmp_path / "inventory"
    journal = tmp_path / "journal"
    controls = tmp_path / "controls.json"
    output = tmp_path / "output"
    record = RECORD | {"md5": "AQID"}
    document = {"bucket": migrate.SOURCE, "scans": [{"src": record["key"], "generation": 42, "size": 123, "md5": "010203", "crc32c": "crc"}]}
    raw = (json.dumps(document) + "\n").encode()
    control_key = f"static-names/{GEN}/scans.json"
    control_record = {"bucket": migrate.SOURCE, "key": control_key, "generation": "43", "bytes": len(raw), "md5": "control-md5", "crc32c": "control-crc", "storage_class": "STANDARD"}
    fixture_inventory(inventory, record)
    (inventory / "data.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [record, control_record]))
    summary = json.loads((inventory / "summary.json").read_text())
    summary["data"][f"static-names/{GEN}/"] = {"objects": 1, "bytes": len(raw)}
    (inventory / "summary.json").write_text(json.dumps(summary) + "\n")
    (inventory / "_SUCCESS.json").write_text(json.dumps({"data_objects": 2}) + "\n")
    fixture_journal(journal, inventory, {"key": record["key"], "source_generation": "42", "destination_generation": "99", "action": "copied"})
    controls.write_text(json.dumps([{"key": control_key, "generation": "43", "sha256": sha256(raw).hexdigest(), "document": document}]) + "\n")
    calls = []
    published = SimpleNamespace(generation=101)

    def download_source(**kwargs: object) -> bytes:
        calls.append(("download-source", kwargs))
        return raw

    def upload(payload: bytes, **kwargs: object) -> None:
        calls.append(("upload", json.loads(payload), kwargs))
        published.payload = payload
        published.size = len(payload)

    def reload(**kwargs: object) -> None:
        calls.append(("reload-target", kwargs))

    def download_target(**kwargs: object) -> bytes:
        calls.append(("download-target", kwargs))
        return published.payload

    published.upload_from_string = upload
    published.reload = reload
    published.download_as_bytes = download_target

    def destination_get(key: str) -> object:
        calls.append(("head", key))
        return blob(md5_hash="AQID") if key == record["key"] else None

    def source_blob(key: str, **kwargs: object) -> object:
        calls.append(("source", key, kwargs))
        return SimpleNamespace(download_as_bytes=download_source)

    def destination_blob(key: str) -> object:
        calls.append(("target", key))
        return published

    destination = SimpleNamespace(get_blob=destination_get, blob=destination_blob)
    def list_payloads(name: str) -> list[object]:
        calls.append(("list", name))
        return [blob(name=record["key"], md5_hash="AQID")]

    client = SimpleNamespace(bucket=lambda name: destination if name == migrate.DESTINATION else SimpleNamespace(blob=source_blob), list_blobs=list_payloads)
    monkeypatch.setattr(storage, "Client", lambda **kwargs: client)
    args = ["publish-controls", "-c", str(controls), "-i", str(inventory), "-j", str(journal), "-o", str(output)]
    if apply:
        args += ["--apply", "--writers-drained"]
    result = CliRunner().invoke(migrate.main, args)
    assert result.exit_code == 0, result.exception
    assert json.loads(result.output) == {"controls": 1, "apply": apply, "ready_for_reader_checks": apply, "ready_for_cutover": False}
    expected_document = {"bucket": migrate.DESTINATION, "scans": [{"src": record["key"], "generation": 99, "size": 123, "md5": "010203", "crc32c": "crc"}]}
    assert json.loads((output / "control-0001.json").read_text()) == expected_document
    assert calls == [
        ("list", migrate.DESTINATION),
        ("source", control_key, {"generation": 43}),
        ("download-source", {"if_generation_match": 43}),
        ("head", control_key),
        *([
            ("target", control_key),
            ("upload", expected_document, {"content_type": "application/json", "if_generation_match": 0}),
            ("reload-target", {"if_generation_match": 101}),
            ("download-target", {"if_generation_match": 101}),
        ] if apply else []),
    ]
