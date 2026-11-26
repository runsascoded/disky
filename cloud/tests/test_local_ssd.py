"""A stage's local SSD fits its machine: GCE takes only some counts per N2 size, and refuses any other when it
schedules the job (cw's drill on an n2-highmem-32 with 2 SSDs, 2026-11-26), so a static-names profile refuses the
pair when it loads."""
import pytest

from dt_cloud.scan_profile import local_ssd_counts, local_ssd_problem
from dt_cloud.static_profile import from_mapping


def test_n2_counts_by_vcpus():
    assert [local_ssd_counts(m) for m in ("n2-highmem-4", "n2-highmem-16", "n2-highmem-32", "n2-highmem-48", "n2-standard-128")] == [
        (1, 2, 4, 8, 16, 24), (2, 4, 8, 16, 24), (4, 8, 16, 24), (8, 16, 24), (16, 24),
    ]
    assert [local_ssd_counts(m) for m in ("e2-standard-4", "c3-highmem-44")] == [None, None]


def test_problem_names_the_sizes_the_machine_takes():
    assert [local_ssd_problem(m, gb) for m, gb in (
        ("n2-highmem-16", 750), ("n2-highmem-32", 1500), ("n2-highmem-48", 3000), ("e2-standard-4", 750),
    )] == [None, None, None, None]
    assert [local_ssd_problem(m, gb) for m, gb in (("n2-highmem-32", 750), ("n2-highmem-16", 500))] == [
        "n2-highmem-32 takes 4, 8, 16, 24 local SSDs (1500, 3000, 6000, 9000 GB), not 2 (750 GB)",
        "500 GB of local SSD is not a multiple of 375",
    ]


def test_profile_refuses_a_stage_its_machine_cant_hold():
    base = {"machine": "n2-highmem-16", "ssd_gb": 750}
    assert from_mapping({**base, "machines": {"drill": "n2-highmem-32"}, "ssd_gbs": {"drill": 1500}}).ssd_for("drill") == 1500
    with pytest.raises(SystemExit) as e:
        from_mapping({**base, "machines": {"drill": "n2-highmem-32"}})
    assert str(e.value) == ("static names profile: stage drill (machines / ssd_gbs): "
                            "n2-highmem-32 takes 4, 8, 16, 24 local SSDs (1500, 3000, 6000, 9000 GB), not 2 (750 GB)")
    with pytest.raises(SystemExit) as e:
        from_mapping({"machine": "n2-highmem-48", "ssd_gb": 750})
    assert str(e.value) == ("static names profile: machine / ssd_gb: "
                            "n2-highmem-48 takes 8, 16, 24 local SSDs (3000, 6000, 9000 GB), not 2 (750 GB)")


def _spec(machine: str, *sizes: int) -> dict:
    disks = [{"newDisk": {"type": "local-ssd", "sizeGb": str(gb)}, "deviceName": f"d{i}"} for i, gb in enumerate(sizes)]
    return {"allocationPolicy": {"instances": [{"policy": {"machineType": machine, "bootDisk": {"type": "pd-balanced", "sizeGb": "100"}, "disks": disks}}]}}


def test_every_batch_submit_checks_its_local_ssd_against_its_machine(monkeypatch):
    """`batch.check_local_ssd` in `submit_job`: `vm-pack`'s n2-highmem-16 with 1 SSD (2026-11-26) is refused before the POST,
    as any submitter's; a pair GCE takes, a family not tabled, and a spec without local SSD pass."""
    from dt_cloud import batch

    assert [batch.check_local_ssd(s) for s in (
        _spec("n2-highmem-16", 750), _spec("n2-highmem-32", 750, 750), _spec("e2-standard-4", 375), _spec("n2-highmem-16"), {},
    )] == [None] * 5
    with pytest.raises(ValueError) as e:
        batch.check_local_ssd(_spec("n2-highmem-16", 375), "Batch job vm-pack-x")
    assert str(e.value) == "Batch job vm-pack-x: n2-highmem-16 takes 2, 4, 8, 16, 24 local SSDs (750, 1500, 3000, 6000, 9000 GB), not 1 (375 GB)"
    monkeypatch.setattr(batch, "check_spec", lambda spec, what: None)
    monkeypatch.setattr(batch, "session", lambda: pytest.fail("posted"))
    with pytest.raises(ValueError) as e:
        batch.submit_job(_spec("n2-highmem-32", 375, 375), "drill-1", "us-east1", "p")
    assert str(e.value) == "Batch job drill-1: n2-highmem-32 takes 4, 8, 16, 24 local SSDs (1500, 3000, 6000, 9000 GB), not 2 (750 GB)"
