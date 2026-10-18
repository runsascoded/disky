import pytest

from dt_cloud.r2_access import calculation_body, capture_sampling, export_window, query_body, query_events

START = 1791676800000  # 2026-10-11T00:00:00Z
END = 1791763200000  # 2026-10-12T00:00:00Z


def page(ids: list[str], count: int | None = None, abr_level: int = 1, status: str = "COMPLETED", timestamp: int = START) -> dict:
    return {"success": True, "result": {
        "events": {"count": len(ids) if count is None else count, "events": [
            {"timestamp": timestamp, "$metadata": {"id": ident}, "source": {"action": "HeadObject"}} for ident in ids
        ]},
        "statistics": {"abr_level": abr_level},
        "run": {"status": status},
    }}


BODY = query_body("jc-taxes", "2026-10-11T00:00:00Z", "2026-10-12T00:00:00Z", limit=1)


def calculation(aggregates: list[tuple[int, float]], abr_level: int = 1, status: str = "COMPLETED") -> dict:
    return {"success": True, "result": {
        "run": {"status": status},
        "statistics": {"abr_level": abr_level},
        "calculations": [{"alias": "events", "aggregates": [
            {"count": count, "sampleInterval": interval} for count, interval in aggregates
        ]}],
    }}


def test_export_drains_cursor_pages_and_reports_only_observation_coverage():
    calls = []
    responses = iter([page(["a"]), page(["b"]), page([])])

    def fetch(body: dict) -> dict:
        calls.append(body)
        return next(responses)

    body = query_body("jc-taxes", "2026-10-11T00:00:00Z", "2026-10-12T00:00:00Z", limit=1)
    result = export_window(body, fetch=fetch, max_pages=3)
    assert calls == [body, {**body, "offset": "a", "offsetDirection": "next"}, {**body, "offset": "b", "offsetDirection": "next"}]
    assert result["summary"] == {
        "pages": 3, "events": 2, "query_exhausted": True,
        "query_abr_levels": [1], "capture_sampling": "unknown", "provider_delivery": "best_effort",
        "actions": {"HeadObject": 2},
    }
    assert result["pages"] == [page(["a"]), page(["b"]), page([])]


@pytest.mark.parametrize("response,message", [
    (page(["a"], abr_level=2), "sampled query cannot be exported as an exact event set"),
    (page(["a"], status="STARTED"), "query is not COMPLETED"),
])
def test_export_refuses_partial_or_sampled_queries(response: dict, message: str):
    with pytest.raises(RuntimeError, match=f"^{message}$"):
        export_window(BODY, fetch=lambda body: response)


def test_export_refuses_page_cap_without_claiming_exhaustion():
    with pytest.raises(RuntimeError, match="^max_pages reached before cursor exhaustion$"):
        export_window(BODY, fetch=lambda body: page(["a"]), max_pages=1)


def test_export_refuses_stalled_cursor():
    with pytest.raises(RuntimeError, match="^pagination cursor did not advance$"):
        export_window(BODY, fetch=lambda body: page(["a"]))


def test_export_ends_at_a_short_page_of_events_already_drained():
    """A twice-delivered last event: the cursor at its first copy returns only the second (marin-na 2026-10-17T16)."""
    body = query_body("jc-taxes", "2026-10-11T00:00:00Z", "2026-10-12T00:00:00Z", limit=2)
    responses = iter([page(["a", "b"]), page(["b"])])
    result = export_window(body, fetch=lambda b: next(responses), max_pages=3)
    assert result["summary"] == {
        "pages": 2, "events": 2, "query_exhausted": True,
        "query_abr_levels": [1], "capture_sampling": "unknown", "provider_delivery": "best_effort",
        "actions": {"HeadObject": 2},
    }


def test_export_refuses_a_full_page_repeating_its_cursor():
    body = query_body("jc-taxes", "2026-10-11T00:00:00Z", "2026-10-12T00:00:00Z", limit=2)
    with pytest.raises(RuntimeError, match="^pagination cursor did not advance$"):
        export_window(body, fetch=lambda b: page(["a", "b"]), max_pages=3)


def test_export_refuses_page_count_shortfall():
    with pytest.raises(RuntimeError, match="^page event count does not match returned events$"):
        export_window(BODY, fetch=lambda body: page(["a"], count=2))


def test_export_accepts_documented_omitted_abr_level():
    response = page([])
    response["result"]["statistics"].pop("abr_level")
    assert export_window(BODY, fetch=lambda body: response)["summary"]["query_abr_levels"] == [1]


def test_export_cli_saves_private_archive_and_only_prints_summary(monkeypatch, tmp_path):
    import json
    from stat import S_IMODE
    from click.testing import CliRunner
    from dt_cloud.cli import main
    from dt_cloud import r2_access

    monkeypatch.setenv("CF_OBSERVABILITY_TOKEN", "dedicated-test-token")
    responses = iter([page(["a"]), page([])])
    calc = calculation([(1, 1)])
    monkeypatch.setattr(r2_access, "query_events", lambda account, token, body: calc if body["view"] == "calculations" else next(responses))
    output = tmp_path / "archive.json"
    args = ["access", "r2-export", "-a", "a" * 32, "-b", "jc-taxes",
            "-f", "2026-10-11T00:00:00Z", "-t", "2026-10-12T00:00:00Z", "-o", str(output)]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "pages": 2, "events": 1, "query_exhausted": True,
        "query_abr_levels": [1], "capture_sampling": "unsampled", "provider_delivery": "best_effort",
        "actions": {"HeadObject": 1}, "estimated_events": 1, "max_sample_interval": 1,
    }
    saved = json.loads(output.read_text())
    assert (saved["pages"], saved["calculation"]) == ([page(["a"]), page([])], calc)
    assert S_IMODE(output.stat().st_mode) == 0o600
    again = CliRunner().invoke(main, args)
    assert again.exit_code == 2
    assert again.output.splitlines() == [
        "Usage: main access r2-export [OPTIONS]",
        "Try 'main access r2-export --help' for help.",
        "",
        "Error: output already exists; use a new archive path",
    ]
    assert json.loads(output.read_text())["pages"] == [page(["a"]), page([])]


def test_query_is_scoped_bounded_and_not_persisted():
    assert query_body("jc-taxes", "2026-10-11T00:00:00Z", "2026-10-12T00:00:00Z", limit=100) == {
        "queryId": "disky-r2-pilot",
        # The provider excludes both bounds: `from - 1` makes the window [start, end).
        "timeframe": {"from": START - 1, "to": END},
        "dry": True,
        "view": "events",
        "limit": 100,
        "parameters": {
            "datasets": ["r2"],
            "filters": [{"key": "bucket", "operation": "eq", "type": "string", "value": "jc-taxes"}],
            "filterCombination": "and",
        },
    }


@pytest.mark.parametrize("limit", [0, 2001])
def test_invalid_limits_refuse(limit: int):
    with pytest.raises(ValueError, match="^limit must be between 1 and 2000$"):
        query_body("b", "2026-10-11T00:00:00Z", "2026-10-12T00:00:00Z", limit=limit)


def test_naive_times_refuse_rather_than_guessing_a_zone():
    with pytest.raises(ValueError, match="^query time must include a timezone$"):
        query_body("b", "2026-10-11", "2026-10-12")


def test_reversed_times_refuse():
    with pytest.raises(ValueError, match="^query start must precede end$"):
        query_body("b", "2026-10-12T00:00:00Z", "2026-10-11T00:00:00Z")


def test_query_preserves_provider_response_and_does_not_log_a_token():
    calls = []
    response = {"success": True, "result": {"events": {"count": 0, "events": []}}}

    def post(url: str, body: dict, token: str) -> dict:
        calls.append((url, body, token))
        return response

    body = query_body("b", "2026-10-11T00:00:00Z", "2026-10-12T00:00:00Z")
    assert query_events("a" * 32, "test-token", body, post=post) == response
    assert calls == [("https://api.cloudflare.com/client/v4/accounts/" + "a" * 32 + "/workers/observability/telemetry/query", body, "test-token")]


def test_provider_error_cannot_be_mistaken_for_no_events():
    def post(url: str, body: dict, token: str) -> dict:
        return {"success": False, "errors": [{"message": "permission denied"}]}

    with pytest.raises(RuntimeError, match="^Cloudflare query failed: permission denied$"):
        query_events("a" * 32, "test-token", {}, post=post)


def test_account_identifier_cannot_inject_a_different_endpoint():
    with pytest.raises(ValueError, match="^account must be a 32-character hexadecimal Cloudflare account ID$"):
        query_events("../other", "test-token", {})


def test_empty_token_refuses_before_any_request():
    with pytest.raises(ValueError, match="^Cloudflare token must not be empty$"):
        query_events("a" * 32, "", {})


def test_missing_provider_success_is_an_error():
    def post(url: str, body: dict, token: str) -> dict:
        return {"result": {"events": {"count": 0, "events": []}}}

    with pytest.raises(RuntimeError, match="^Cloudflare query failed: missing success status$"):
        query_events("a" * 32, "test-token", {}, post=post)


def test_cli_prefers_dedicated_token_and_preserves_json(monkeypatch, tmp_path):
    import json
    from click.testing import CliRunner
    from dt_cloud.cli import main
    from dt_cloud import r2_access

    monkeypatch.setenv("CF_OBSERVABILITY_TOKEN", "dedicated-test-token")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "other-test-token")
    calls = []
    response = {"success": True, "result": {"events": {"count": 3, "events": []}, "run": {"status": "STARTED"}}}

    def query(account: str, token: str, body: dict) -> dict:
        calls.append((account, token, body))
        return response

    monkeypatch.setattr(r2_access, "query_events", query)
    output = tmp_path / "response.json"
    result = CliRunner().invoke(main, [
        "access", "r2-query", "-a", "a" * 32, "-b", "jc-taxes",
        "-f", "2026-10-11T00:00:00Z", "-t", "2026-10-12T00:00:00Z", "-o", str(output),
    ])
    assert result.exit_code == 0
    assert result.output == f"saved bounded provider response to {output}; no ingest watermark advanced\n"
    assert json.loads(output.read_text()) == response
    assert calls == [("a" * 32, "dedicated-test-token", query_body("jc-taxes", "2026-10-11T00:00:00Z", "2026-10-12T00:00:00Z"))]


def test_export_refuses_events_outside_the_half_open_window():
    with pytest.raises(RuntimeError, match="^event outside the requested window$"):
        export_window(BODY, fetch=lambda body: page(["a"], timestamp=END))
    assert export_window(BODY, fetch=lambda body: page([] if "offset" in body else ["a"], timestamp=END - 1))["summary"]["events"] == 1


def test_calculation_body_is_a_weighted_count_by_action_over_the_same_window():
    assert calculation_body("jc-taxes", "2026-10-11T00:00:00Z", "2026-10-12T00:00:00Z") == {
        "queryId": "disky-r2-sampling",
        "timeframe": {"from": START - 1, "to": END},
        "dry": True,
        "view": "calculations",
        "parameters": {
            "datasets": ["r2"],
            "filters": [{"key": "bucket", "operation": "eq", "type": "string", "value": "jc-taxes"}],
            "filterCombination": "and",
            "calculations": [{"operator": "count", "alias": "events"}],
            "groupBys": [{"type": "string", "value": "action"}],
        },
    }


@pytest.mark.parametrize("aggregates,observed,expected", [
    ([(4, 1), (2, 1)], 6, {"capture_sampling": "unsampled", "estimated_events": 6, "max_sample_interval": 1}),
    # Measured 2026-10-14 12:30–13:30Z: 169 drained LIST rows, weighted count 178.
    ([(178, 1.0532544378698225)], 169, {"capture_sampling": "sampled", "estimated_events": 178, "max_sample_interval": 1.0532544378698225}),
    ([(5, 1)], 4, {"capture_sampling": "inconsistent", "estimated_events": 5, "max_sample_interval": 1}),
    ([], 0, {"capture_sampling": "unsampled", "estimated_events": 0, "max_sample_interval": 1}),
])
def test_capture_sampling_classifies_windows(aggregates: list, observed: int, expected: dict):
    assert capture_sampling(calculation(aggregates), observed) == expected


@pytest.mark.parametrize("response,message", [
    (calculation([(10, 10)], abr_level=10), "calculation was sampled at query time"),
    (calculation([], status="RUNNING"), "calculation is not COMPLETED"),
])
def test_capture_sampling_refuses_query_sampled_or_incomplete(response: dict, message: str):
    with pytest.raises(RuntimeError, match=f"^{message}$"):
        capture_sampling(response, 0)
