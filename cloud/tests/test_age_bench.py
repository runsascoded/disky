import pytest

from dt_cloud.age_bench import capture


def test_capture_records_first_and_repeat_timings_and_exact_answer_parity():
    cases = [{'date': '2026-11-07T1944', 'path': 'b', 'store': 'primary'}]
    answer = {'records': [{'dt': 1, 'b': 7, 'o': 2}], 'plan': {'tier': '1h'}}
    values = iter([(200, answer, seconds, '', '') for seconds in (.4, .2, .1)])
    baseline = capture(cases, lambda _: next(values), 2)
    assert baseline == {'version': 1, 'changed': [], 'pass': True, 'cases': [{
        'query': cases[0], 'answer': answer, 'statuses': [200, 200, 200], 'first_ms': 400.0,
        'repeat_ms': [200.0, 100.0], 'repeat_median_ms': 150.0, 'stable': True,
    }]}
    assert capture(cases, lambda _: (200, answer, .1, '', ''), 1, baseline)['pass'] is True


def test_changed_or_unstable_answers_fail():
    cases = [{'date': '2026-11-07T1944'}]
    baseline = capture(cases, lambda _: (200, {'records': [7]}, .1, '', ''), 1)
    changed = capture(cases, lambda _: (200, {'records': [8]}, .1, '', ''), 1, baseline)
    assert (changed['pass'], changed['changed']) == (False, [0])
    values = iter([(200, {'records': [7]}, .1, '', ''), (503, {'error': 'retry'}, .2, '', '')])
    unstable = capture(cases, lambda _: next(values), 1, baseline)
    assert (unstable['pass'], unstable['changed'], unstable['cases'][0]['stable'], unstable['cases'][0]['statuses']) == (False, [], False, [200, 503])


def test_empty_or_different_cases_refuse():
    with pytest.raises(ValueError, match='^age benchmark needs at least one case$'):
        capture([], lambda _: None, 1)
    with pytest.raises(ValueError, match='^age benchmark cases differ from baseline$'):
        capture([{'date': 'other'}], lambda _: None, 1, {'cases': [{'query': {'date': 'original'}}]})
    with pytest.raises(ValueError, match='^age benchmark baseline did not pass$'):
        capture([{'date': 'same'}], lambda _: None, 1, {'cases': [{'query': {'date': 'same'}}], 'pass': False})


def test_cli_does_not_hide_cold_failures_with_automatic_retries(tmp_path, monkeypatch):
    import json

    from click.testing import CliRunner
    from dt_cloud import deploy, diff_check
    from dt_cloud.age_bench import age_bench_cmd

    query = {'date': '2026-11-07T1944'}
    cases = tmp_path / 'cases.json'
    cases.write_text(json.dumps([query]))
    out = tmp_path / 'out.json'
    calls = []
    monkeypatch.setattr(deploy, 'site_token', lambda _: 'fixture-read-token')

    def fetch(host, query, token, **kwargs):
        calls.append((host, query, token, kwargs))
        return 503, 'unavailable', .1, '', ''

    monkeypatch.setattr(diff_check, 'fetch', fetch)
    result = CliRunner().invoke(age_bench_cmd, ['-u', 'https://fixture', '-n', '1', '-o', str(out), str(cases)])
    assert result.exit_code == 1
    assert calls == [('https://fixture', query, 'fixture-read-token', {'retries': 1, 'route': '/api/age-pyramid'})] * 2
    assert (json.loads(out.read_text())['pass'], json.loads(out.read_text())['cases'][0]['statuses']) == (False, [503, 503])
