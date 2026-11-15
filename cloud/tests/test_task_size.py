import pytest

from dt_cloud import static_merge, task_size as T


@pytest.fixture
def host(monkeypatch):
    """A Fargate-like view: 8 CPUs in the affinity, 31 GiB of physical pages, whatever the task's size."""
    monkeypatch.setattr(T.os, "sched_getaffinity", lambda _: set(range(8)), raising=False)
    monkeypatch.setattr(T.os, "sysconf", lambda k: {"SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": 31 * 2**30 // 4096}[k])


def test_env_overrides_the_host(host) -> None:
    assert [T.task_cpus({}), T.task_cpus({"DT_NPROC": " 2\n"}), T.task_ram({}), T.task_ram({"DT_MEM": "16GiB"}),
            T.task_ram({"DT_MEM": "4GB"})] == [8, 2, 31 * 2**30, 16 * 2**30, 4 * 10**9]


@pytest.mark.parametrize("env, msg", [
    ({"DT_NPROC": "0"}, "$DT_NPROC='0': not a positive integer"),
    ({"DT_NPROC": "2.5"}, "$DT_NPROC='2.5': not a positive integer"),
    ({"DT_MEM": "lots"}, "$DT_MEM='lots': not a memory size (e.g. 16GiB)"),
    ({"DT_MEM": "0GB"}, "$DT_MEM='0GB': not a memory size (e.g. 16GiB)"),
])
def test_bad_values_raise(host, env, msg) -> None:
    with pytest.raises(ValueError) as e:
        T.task_cpus(env) if "DT_NPROC" in env else T.task_ram(env)
    assert str(e.value) == msg


def test_merge_plans_for_the_task_not_the_host(host, monkeypatch) -> None:
    """m3 2026-11-13: a 16 GiB, 2-vCPU task's carry planned for the host and set the drill's DuckDB to 23GB."""
    tiers = ["shards", "catalog", "drill", "anchors"]
    assert static_merge.tier_resources(tiers, *static_merge._machine(), concurrent=False)["drill"] == {"mem": "23GB", "threads": 8}
    monkeypatch.setenv("DT_MEM", "16GiB")
    monkeypatch.setenv("DT_NPROC", "2")
    assert static_merge.tier_resources(tiers, *static_merge._machine(), concurrent=False)["drill"] == {"mem": "12GB", "threads": 2}
