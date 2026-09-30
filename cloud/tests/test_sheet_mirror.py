"""The sheet mirror (`dt_cloud.sheet_mirror`, specs/done/sheet-mirror.md).

- exporters: each named source's column contract, read from canned endpoint
  JSON (`fixtures/sheet_mirror/<deployment>.json`, keyed by request path +
  query — shaped after the Pages Functions' responses) through an injected
  getter, asserted as exact CSV text;
- `sheet-push`'s key-aware placement, against a fake worksheet: append,
  remove (one cleared row, compacted on a later unchanged run), reorder-stable,
  and a no-op run that writes nothing;
- the `sheet-mirror.yml` → `sync.sh` plan / deploy env helpers.
"""

from __future__ import annotations

import io
import json
import shlex
from pathlib import Path
from urllib.parse import urlencode

import pytest
from click.testing import CliRunner

from dt_cloud.cli import main
from dt_cloud.sheet_mirror import (
    ConfigError,
    ExportArgs,
    ExportError,
    env_lines,
    export,
    list_sources,
    load_config,
    plan_lines,
    plan_sheet,
    push,
    render_config,
    write_csv,
)
from dt_cloud.site import SiteError

FIXTURES = Path(__file__).parent / "fixtures" / "sheet_mirror"


def getter(deployment: str):
    """A `Getter` over `fixtures/sheet_mirror/<deployment>.json`; an int value
    is an HTTP error status. Records each request."""
    canned = json.loads((FIXTURES / f"{deployment}.json").read_text())
    calls: list[str] = []

    def get(path: str, params: dict | None):
        req = f"{path}?{urlencode(params, safe='/')}" if params else path
        calls.append(req)
        if req not in canned:
            raise AssertionError(f"unexpected request {req}")
        v = canned[req]
        if isinstance(v, int):
            raise SiteError(f"{req} → HTTP {v}", status=v)
        return v

    get.calls = calls
    return get


def csv_text(source: str, deployment: str, args: ExportArgs = ExportArgs()) -> list[str]:
    columns, rows = export(source, getter(deployment), args)
    buf = io.StringIO()
    write_csv(columns, rows, buf)
    return buf.getvalue().split("\n")


# --------------------------------------------------------------------------
# Exporters
# --------------------------------------------------------------------------

def test_owners_contract():
    assert csv_text("owners", "gcs") == [
        "user,bytes,standard,nearline,coldline,archive",
        "alice,5000000000000,4000000000000,1000000000000,0,0",
        "bob,1500000000000,1500000000000,0,0,0",
        "carol,1500000000000,500000000000,0,0,1000000000000",
        "dave,0,0,0,0,0",
        "",
    ]


def test_owners_explicit_date_skips_scans_json():
    get = getter("gcs")
    export("owners", get, ExportArgs(date="2026-09-27"))
    assert get.calls == ["/api/owners?date=2026-09-27"]


def test_staged_contract_sizes_each_prefix_404_is_blank():
    assert csv_text("staged", "gcs") == [
        "prefix,staged_by,staged_at,note,bytes",
        "gs://marin-us-central2/scratch/gone/,alice@example.org,2025-09-26 15:20,,",
        "gs://marin-us-central2/scratch/tmp/,alice@example.org,2025-09-26 15:20,,1048576",
        "gs://marin-us-east1/checkpoints/old-run/,bob@example.org,2025-09-27 16:20,superseded,734003200",
        "",
    ]


def test_staged_under_a_subdir_store():
    assert csv_text("staged", "cw", ExportArgs(subdir="cw")) == [
        "prefix,staged_by,staged_at,note,bytes",
        "s3://marin-us-east-02a/tmp/,dan@example.org,2025-09-16 05:21,,2000000",
        "",
    ]


def test_staged_no_open_plan_is_header_only():
    def get(path: str, params: dict | None):
        assert (path, params) == ("/api/plans/staged", None)
        return {"plan": None, "items": [], "batches": [], "runs": []}

    assert export("staged", get, ExportArgs()) == (("prefix", "staged_by", "staged_at", "note", "bytes"), [])


def test_staged_non_404_error_propagates():
    def get(path: str, params: dict | None):
        if path == "/api/subtree":
            raise SiteError("503", status=503)
        return json.loads((FIXTURES / "gcs.json").read_text())[path]

    with pytest.raises(SiteError, match="503"):
        export("staged", get, ExportArgs())


def test_runs_gcs_sweep_joins_d1_by_run_dir():
    assert csv_text("runs", "gcs", ExportArgs(executor="sweep")) == [
        "run,mode,by,started,state,deleted_bytes",
        "gcs-sweep-dry-20260928-010000,dry,alice@example.org,2026-09-28 01:00,SUCCEEDED,2100000000",
        "gcs-sweep-real-20260928-030000,real,carol@example.org,2026-09-28 03:00,RUNNING,",
        "",
    ]


def test_runs_cw_plan_sweep_joins_d1_by_job_id():
    assert csv_text("runs", "cw", ExportArgs(executor="plan-sweep")) == [
        "run,mode,by,started,state,deleted_bytes",
        "cw-sweep-dry-20260927-220000,dry,dan@example.org,2026-09-27 22:00,SUCCEEDED,5000000000000",
        "cw-sweep-real-20260928-040000,real,,2026-09-28 04:00,QUEUED,",
        "",
    ]


def test_runs_requires_executor():
    with pytest.raises(ExportError, match="needs -e/--executor"):
        export("runs", getter("gcs"), ExportArgs())


def test_runs_unconfigured_bridge_raises():
    def get(path: str, params: dict | None):
        return {"jobs": [], "configured": False}

    with pytest.raises(ExportError, match="isn't configured"):
        export("runs", get, ExportArgs(executor="sweep"))


def test_unknown_source():
    with pytest.raises(ExportError, match="unknown source 'nope'"):
        export("nope", getter("gcs"), ExportArgs())


def test_list_sources():
    assert list_sources() == [
        "owners: user, bytes, standard, nearline, coldline, archive\n"
        "    GET /api/owners?date=<scan>: per-user owned bytes + storage-class split (/users)",
        "staged: prefix, staged_by, staged_at, note, bytes\n"
        "    GET /api/plans/staged (+ /api/subtree per prefix for bytes): the shared open deletion plan (/staged)",
        "runs: run, mode, by, started, state, deleted_bytes\n"
        "    GET /api/<executor>/jobs joined to /api/plans/staged runs: recent deletion runs (needs -e)",
    ]


def test_export_list_cli():
    r = CliRunner().invoke(main, ["export", "--list"])
    assert r.exit_code == 0, r.output
    assert r.output == "\n".join(list_sources()) + "\n"


# --------------------------------------------------------------------------
# sheet-push: key-aware placement against a fake worksheet
# --------------------------------------------------------------------------

class FakeWorksheet:
    """A grid with gspread's `get_all_values` (trailing blanks trimmed, as the
    API returns) and `update_cells`; records every write batch."""

    title = "Storage by user"

    def __init__(self, grid: list[list[str]] | None = None):
        self.grid = [list(r) for r in grid or []]
        self.writes: list[list[tuple[int, int, str]]] = []

    def get_all_values(self) -> list[list[str]]:
        rows = [list(r) for r in self.grid]
        while rows and not any(rows[-1]):
            rows.pop()
        width = max((i + 1 for r in rows for i, v in enumerate(r) if v), default=0)
        return [(r + [""] * width)[:width] for r in rows]

    def update_cells(self, cells: list[tuple[int, int, str]], value_input_option: str = "RAW") -> None:
        assert value_input_option == "RAW"
        self.writes.append(list(cells))
        for r, c, v in cells:
            while len(self.grid) < r:
                self.grid.append([])
            row = self.grid[r - 1]
            while len(row) < c:
                row.append("")
            row[c - 1] = v

    def values(self) -> list[list[str]]:
        return self.get_all_values()


HEADER = ["user", "bytes"]
T0 = "2026-09-28 01:05 UTC"
T1 = "2026-09-28 02:05 UTC"
T2 = "2026-09-28 03:05 UTC"
FOOT = "⟳ Auto-synced"


def table(*rows: tuple[str, str]) -> list[list[str]]:
    return [HEADER, *(list(r) for r in rows)]


def seeded() -> FakeWorksheet:
    ws = FakeWorksheet()
    push(ws, table(("alice", "5"), ("bob", "3"), ("carol", "1")), T0, key="user", disclaimer=FOOT)
    assert ws.values() == [
        ["user", "bytes"],
        ["alice", "5"],
        ["bob", "3"],
        ["carol", "1"],
        ["", ""],
        [f"{FOOT}; last change {T0}", ""],
    ]
    ws.writes.clear()
    return ws


def test_noop_run_writes_nothing():
    ws = seeded()
    plan = push(ws, table(("alice", "5.0"), ("bob", "3"), ("carol", "1")), T1, key="user", disclaimer=FOOT)
    assert (plan.cells, plan.data_changed, plan.compacted) == ([], False, False)
    assert ws.writes == []


def test_reorder_stable_value_change_is_one_cell():
    ws = seeded()
    # the source re-sorts (bob grew past alice): the tab keeps its order
    plan = push(ws, table(("bob", "9"), ("alice", "5"), ("carol", "1")), T1, key="user", disclaimer=FOOT)
    assert plan.data_changed
    assert ws.writes == [[(3, 2, "9"), (6, 1, f"{FOOT}; last change {T1}")]]
    assert ws.values() == [
        ["user", "bytes"],
        ["alice", "5"],
        ["bob", "9"],
        ["carol", "1"],
        ["", ""],
        [f"{FOOT}; last change {T1}", ""],
    ]


def test_positional_without_key_rewrites_every_moved_row():
    ws = seeded()
    push(ws, table(("bob", "9"), ("alice", "5"), ("carol", "1")), T1, disclaimer=FOOT)
    assert ws.writes == [[(2, 1, "bob"), (2, 2, "9"), (3, 1, "alice"), (3, 2, "5"), (6, 1, f"{FOOT}; last change {T1}")]]


def test_append_new_key_at_end():
    ws = seeded()
    # a new key sorted into the middle of the source lands after the last row
    push(ws, table(("alice", "5"), ("aaron", "4"), ("bob", "3"), ("carol", "1")), T1, key="user", disclaimer=FOOT)
    assert ws.writes == [[
        (5, 1, "aaron"), (5, 2, "4"),
        (6, 1, ""),
        (7, 1, f"{FOOT}; last change {T1}"),
    ]]
    assert ws.values() == [
        ["user", "bytes"],
        ["alice", "5"],
        ["bob", "3"],
        ["carol", "1"],
        ["aaron", "4"],
        ["", ""],
        [f"{FOOT}; last change {T1}", ""],
    ]


def test_remove_clears_one_row_then_compacts_on_unchanged_run():
    ws = seeded()
    plan = push(ws, table(("alice", "5"), ("carol", "1")), T1, key="user", disclaimer=FOOT)
    assert (plan.data_changed, plan.compacted, plan.holes) == (True, False, 1)
    assert ws.writes == [[(3, 1, ""), (3, 2, ""), (6, 1, f"{FOOT}; last change {T1}")]]
    assert ws.values() == [
        ["user", "bytes"],
        ["alice", "5"],
        ["", ""],
        ["carol", "1"],
        ["", ""],
        [f"{FOOT}; last change {T1}", ""],
    ]

    # the hole is held while data keeps moving elsewhere …
    ws.writes.clear()
    plan = push(ws, table(("alice", "6"), ("carol", "1")), T2, key="user", disclaimer=FOOT)
    assert (plan.data_changed, plan.compacted, plan.holes) == (True, False, 1)
    assert ws.writes == [[(2, 2, "6"), (6, 1, f"{FOOT}; last change {T2}")]]

    # … and compacted by the first run whose data is otherwise unchanged; the
    # "last change" stamp stays put (layout, not data)
    ws.writes.clear()
    plan = push(ws, table(("alice", "6"), ("carol", "1")), "2026-09-28 04:05 UTC", key="user", disclaimer=FOOT)
    assert (plan.data_changed, plan.compacted, plan.holes) == (False, True, 0)
    assert ws.values() == [
        ["user", "bytes"],
        ["alice", "6"],
        ["carol", "1"],
        ["", ""],
        [f"{FOOT}; last change {T2}", ""],
    ]

    # and then it's a no-op
    ws.writes.clear()
    push(ws, table(("alice", "6"), ("carol", "1")), "2026-09-28 05:05 UTC", key="user", disclaimer=FOOT)
    assert ws.writes == []


def test_remove_last_row_keeps_footer_in_place():
    ws = seeded()
    push(ws, table(("alice", "5"), ("bob", "3")), T1, key="user", disclaimer=FOOT)
    assert ws.writes == [[(4, 1, ""), (4, 2, ""), (6, 1, f"{FOOT}; last change {T1}")]]
    ws.writes.clear()
    # the trailing hole is still recognised as a hole (not the separator)
    plan = push(ws, table(("alice", "5"), ("bob", "3")), T2, key="user", disclaimer=FOOT)
    assert (plan.data_changed, plan.compacted) == (False, True)
    assert ws.values() == [
        ["user", "bytes"],
        ["alice", "5"],
        ["bob", "3"],
        ["", ""],
        [f"{FOOT}; last change {T1}", ""],
    ]


def test_remove_and_add_in_one_run():
    ws = seeded()
    push(ws, table(("alice", "5"), ("carol", "1"), ("dave", "2")), T1, key="user", disclaimer=FOOT)
    assert ws.values() == [
        ["user", "bytes"],
        ["alice", "5"],
        ["", ""],
        ["carol", "1"],
        ["dave", "2"],
        ["", ""],
        [f"{FOOT}; last change {T1}", ""],
    ]


def test_key_aware_without_footer():
    ws = FakeWorksheet(table(("alice", "5"), ("bob", "3")))
    push(ws, table(("bob", "3"), ("carol", "1")), T1, key="user")
    assert ws.values() == [["user", "bytes"], ["", ""], ["bob", "3"], ["carol", "1"]]


def test_header_without_key_falls_back_to_source_order():
    ws = FakeWorksheet([["who", "size"], ["x", "1"]])
    push(ws, table(("bob", "3"), ("alice", "5")), T1, key="user")
    assert ws.values() == [["user", "bytes"], ["bob", "3"], ["alice", "5"]]


def test_duplicate_or_empty_key_raises():
    with pytest.raises(ValueError, match="duplicate 'user' 'bob'"):
        plan_sheet([], table(("bob", "3"), ("bob", "4")), T0, key="user")
    with pytest.raises(ValueError, match="empty 'user'"):
        plan_sheet([], table(("", "3")), T0, key="user")
    with pytest.raises(ValueError, match="key column 'nope' not in header"):
        plan_sheet([], table(("bob", "3")), T0, key="nope")


def test_header_only_source_clears_rows():
    ws = seeded()
    push(ws, [HEADER], T1, key="user", disclaimer=FOOT)
    assert ws.values() == [
        ["user", "bytes"],
        ["", ""],
        ["", ""],
        ["", ""],
        ["", ""],
        [f"{FOOT}; last change {T1}", ""],
    ]


def test_sheet_push_dry_run_cli(tmp_path: Path):
    p = tmp_path / "t.csv"
    p.write_text("user,bytes\nbob,3\nbob,4\n")
    r = CliRunner().invoke(main, ["sheet-push", "-n", "-k", "user", "-w", "tab", "SHEET", str(p)])
    assert r.exit_code == 1
    assert isinstance(r.exception, ValueError)
    assert str(r.exception) == "duplicate 'user' 'bob'"


# --------------------------------------------------------------------------
# sheet-mirror.yml → sync.sh plan / deploy env
# --------------------------------------------------------------------------

CONFIG = """\
site: https://usage.example.org/
token_secret: usage-sheet-token
schedule: "5 * * * *"
gcp:
  project: my-project
  service_account: job@my-project.iam.gserviceaccount.com
  job: usage-sheet-mirror
mirrors:
  - sheet: SHEET_A
    tab: Storage by user
    source: owners
    key: user
    footer: "⟳ Auto-synced hourly from {site}/users — edits are overwritten"
  - sheet: SHEET_A
    tab: Runs
    source: runs
    key: run
    executor: sweep
"""


def test_plan_lines_round_trip_through_shell_quoting():
    lines = plan_lines(load_config(CONFIG))
    assert lines == [
        "source=owners site=https://usage.example.org subdir='' sheet=SHEET_A tab='Storage by user' key=user "
        "footer='⟳ Auto-synced hourly from https://usage.example.org/users — edits are overwritten' executor='' unit=B",
        "source=runs site=https://usage.example.org subdir='' sheet=SHEET_A tab=Runs key=run footer='' executor=sweep unit=B",
    ]
    assert dict(kv.split("=", 1) for kv in shlex.split(lines[0])) == {
        "source": "owners",
        "site": "https://usage.example.org",
        "subdir": "",
        "sheet": "SHEET_A",
        "tab": "Storage by user",
        "key": "user",
        "footer": "⟳ Auto-synced hourly from https://usage.example.org/users — edits are overwritten",
        "executor": "",
        "unit": "B",
    }


def test_env_lines():
    assert env_lines(load_config(CONFIG)) == [
        "SITE=https://usage.example.org",
        "TOKEN_SECRET=usage-sheet-token",
        "SCHEDULE='5 * * * *'",
        "PROJECT=my-project",
        "REGION=us-central1",
        "SA=job@my-project.iam.gserviceaccount.com",
        "JOB=usage-sheet-mirror",
        "TRIGGER=usage-sheet-mirror-trigger",
        "IMAGE=us-central1-docker.pkg.dev/my-project/cloud-run-source-deploy/usage-sheet-mirror:latest",
    ]


def test_example_config_is_valid():
    example = Path(__file__).parents[2] / "deploy" / "sheet-mirror" / "example.yml"
    cfg = load_config(example.read_text(), env={"USAGE_SHEET_ID": "SHEET_X"})
    assert [(m.sheet, m.source, m.key, m.tab, m.unit) for m in cfg.mirrors] == [("SHEET_X", "owners", "user", "Storage by user", "TiB")]


def test_cli_plan_from_stdin():
    r = CliRunner().invoke(main, ["sheet-mirror", "plan", "-"], input=CONFIG)
    assert r.exit_code == 0, r.output
    assert r.output == "\n".join(plan_lines(load_config(CONFIG))) + "\n"


@pytest.mark.parametrize(("patch", "msg"), [
    (("key: user", "key: bytesz"), "mirrors[0]: key 'bytesz' is not a owners column"),
    (("executor: sweep", "executor: nope"), "mirrors[1]: `runs` needs `executor`"),
    (("    source: owners\n", "    source: owners\n    executor: sweep\n"), "mirrors[0]: `executor` only applies to the `runs` source"),
    (("tab: Runs", "tab: Storage by user"), "mirrors[1]: another mirror already writes sheet SHEET_A tab 'Storage by user'"),
    (("source: owners", "source: nope"), "mirrors[0]: unknown source 'nope'"),
    (("schedule:", "cron:"), "sheet-mirror.yml: unknown key(s) ['cron']"),
    (("    key: run\n", ""), "mirrors[1]: `key` is required"),
])
def test_config_errors(patch: tuple[str, str], msg: str):
    with pytest.raises(ConfigError) as e:
        load_config(CONFIG.replace(*patch, 1))
    assert str(e.value).startswith(msg)


def test_config_without_mirrors_or_gcp():
    with pytest.raises(ConfigError, match="`mirrors` must be a non-empty list"):
        load_config("site: x\ntoken_secret: t\nschedule: s\nmirrors: []\n")
    no_gcp = load_config(CONFIG.split("gcp:")[0] + "mirrors:" + CONFIG.split("mirrors:")[1])
    with pytest.raises(ConfigError, match="`gcp` .* is required to build/deploy"):
        env_lines(no_gcp)


# --------------------------------------------------------------------------
# `${NAME}` substitution + byte units
# --------------------------------------------------------------------------

ENV_CONFIG = CONFIG.replace("sheet: SHEET_A\n    tab: Storage by user", "sheet: ${SHEET_ID}\n    tab: Storage by user")


def test_env_substitution_in_values():
    cfg = load_config(ENV_CONFIG, env={"SHEET_ID": "real-id"})
    assert [(m.sheet, m.tab) for m in cfg.mirrors] == [("real-id", "Storage by user"), ("SHEET_A", "Runs")]


def test_env_substitution_unset_is_an_error_naming_it():
    with pytest.raises(ConfigError) as e:
        load_config(ENV_CONFIG.replace("${SHEET_ID}", "${SHEET_ID}-${OTHER}"), env={})
    assert str(e.value) == "sheet-mirror.yml: unset environment variable(s) ['OTHER', 'SHEET_ID']"


def test_env_substitution_cannot_inject_yaml():
    # Substituted into the parsed value, not the YAML text: a value that looks
    # like YAML stays one (one-line-checked) string, so it can't add keys.
    with pytest.raises(ConfigError) as e:
        load_config(ENV_CONFIG, env={"SHEET_ID": "x\n    tab: Hijack"})
    assert str(e.value) == "mirrors[0]: `sheet` must be one line"
    cfg = load_config(ENV_CONFIG, env={"SHEET_ID": "x tab: Hijack"})
    assert [(m.sheet, m.tab) for m in cfg.mirrors][0] == ("x tab: Hijack", "Storage by user")


def test_render_substitutes_and_validates():
    out = render_config(ENV_CONFIG, env={"SHEET_ID": "real-id"})
    assert "${" not in out
    assert [m.sheet for m in load_config(out, env={}).mirrors] == ["real-id", "SHEET_A"]


def test_owners_in_tib():
    assert csv_text("owners", "gcs", ExportArgs(unit="TiB")) == [
        "user,bytes (TiB),standard (TiB),nearline (TiB),coldline (TiB),archive (TiB)",
        "alice,4.55,3.64,0.91,0.0,0.0",
        "bob,1.36,1.36,0.0,0.0,0.0",
        "carol,1.36,0.45,0.0,0.0,0.91",
        "dave,0.0,0.0,0.0,0.0,0.0",
        "",
    ]


def test_staged_in_gib_keeps_blank_sizes():
    assert csv_text("staged", "gcs", ExportArgs(unit="GiB")) == [
        "prefix,staged_by,staged_at,note,bytes (GiB)",
        "gs://marin-us-central2/scratch/gone/,alice@example.org,2025-09-26 15:20,,",
        "gs://marin-us-central2/scratch/tmp/,alice@example.org,2025-09-26 15:20,,0.0",
        "gs://marin-us-east1/checkpoints/old-run/,bob@example.org,2025-09-27 16:20,superseded,0.7",
        "",
    ]


def test_unknown_unit_rejected_in_config():
    with pytest.raises(ConfigError) as e:
        load_config(CONFIG.replace("    key: user\n", "    key: user\n    unit: PB\n"), env={})
    assert str(e.value) == "mirrors[0]: unknown unit 'PB' (one of B, GiB, TiB)"


def test_cli_render_unset_var_is_a_clean_error():
    r = CliRunner(env={"SHEET_ID": None}).invoke(main, ["sheet-mirror", "render", "-"], input=ENV_CONFIG)
    assert (r.exit_code, r.output) == (1, "Error: sheet-mirror.yml: unset environment variable(s) ['SHEET_ID']\n")


def test_cli_render_substitutes():
    r = CliRunner(env={"SHEET_ID": "real-id"}).invoke(main, ["sheet-mirror", "render", "-"], input=ENV_CONFIG)
    assert r.exit_code == 0, r.output
    assert [m.sheet for m in load_config(r.output, env={}).mirrors] == ["real-id", "SHEET_A"]
