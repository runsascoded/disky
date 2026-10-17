"""Bounded R2 access-log diagnostics, not a complete export or ingest watermark.

Provider responses are retained verbatim: query status, sampling and counts must
remain visible before a future collector can assert a window was drained.
"""

import json
import re
from collections.abc import Callable
from collections import Counter
from datetime import datetime
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def _millis(value: str) -> int:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("query time must include a timezone")
    return int(instant.timestamp() * 1000)


def _window(start: str, end: str) -> tuple[int, int]:
    first, last = _millis(start), _millis(end)
    if first >= last:
        raise ValueError("query start must precede end")
    return first, last


def timeframe(first: int, last: int) -> dict:
    """The provider's ``timeframe`` is exclusive at BOTH ends (measured
    2026-10-17: an event at t is returned for (t-1, t+1), not for (t, t+1000)
    or (t-1000, t)); ``from - 1`` makes the query exactly ``[first, last)``."""
    return {"from": first - 1, "to": last}


def window_of(body: dict) -> tuple[int, int]:
    """The half-open ``[first, last)`` a ``timeframe(...)`` body covers."""
    return body["timeframe"]["from"] + 1, body["timeframe"]["to"]


def _parameters(bucket: str) -> dict:
    return {
        "datasets": ["r2"],
        "filters": [{"key": "bucket", "operation": "eq", "type": "string", "value": bucket}],
        "filterCombination": "and",
    }


def query_body(
    bucket: str,
    start: str,
    end: str,
    *,
    limit: int = 100,
) -> dict:
    if not 1 <= limit <= 2000:
        raise ValueError("limit must be between 1 and 2000")
    first, last = _window(start, end)
    return {
        "queryId": "disky-r2-pilot",
        "timeframe": timeframe(first, last),
        "dry": True,
        "view": "events",
        "limit": limit,
        "parameters": _parameters(bucket),
    }


def calculation_body(
    bucket: str,
    start: str,
    end: str,
) -> dict:
    """Weighted event count per action over ``[start, end)``: the only place
    ingestion sampling is visible (rows carry weights; event rows do not)."""
    first, last = _window(start, end)
    return {
        "queryId": "disky-r2-sampling",
        "timeframe": timeframe(first, last),
        "dry": True,
        "view": "calculations",
        "parameters": {
            **_parameters(bucket),
            "calculations": [{"operator": "count", "alias": "events"}],
            "groupBys": [{"type": "string", "value": "action"}],
        },
    }


def capture_sampling(
    response: dict,
    observed: int,
) -> dict:
    """Classify a drained window against its calculation.

    ``count`` sums per-row weights and ``sampleInterval`` is their mean, so a
    value > 1 means the provider stored fewer rows than requests (measured: a
    LIST burst hour drained 169 rows, count 178). ``unsampled`` requires every
    weight to be 1 AND the weighted count to equal the drained events; anything
    else (e.g. a delivery between drain and calculation) is ``inconsistent``.
    Per-bucket ``series`` values are coarse estimates and are ignored.
    """
    result = response["result"]
    if result["run"]["status"] != "COMPLETED":
        raise RuntimeError("calculation is not COMPLETED")
    statistics = result.get("statistics") or result["run"].get("statistics", {})
    if statistics.get("abr_level", 1) not in (None, 1):
        raise RuntimeError("calculation was sampled at query time")
    calculations = result["calculations"]
    if len(calculations) != 1:
        raise RuntimeError("expected exactly one calculation")
    estimated = 0.0
    interval = 1.0
    for aggregate in calculations[0]["aggregates"]:
        estimated += aggregate["count"]
        interval = max(interval, aggregate["sampleInterval"])
    estimated_events = round(estimated)
    if interval > 1:
        status = "sampled"
    elif estimated_events == observed:
        status = "unsampled"
    else:
        status = "inconsistent"
    return {"capture_sampling": status, "estimated_events": estimated_events, "max_sample_interval": interval}


def _post(
    url: str,
    body: dict,
    token: str,
) -> dict:
    request = Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            return json.load(response)
    except HTTPError as exc:
        # The JSON error envelope goes through the same explicit failure path
        # as a 200 response with success=false. Never log request headers.
        return json.load(exc)


def query_events(
    account: str,
    token: str,
    body: dict,
    *,
    post: Callable[[str, dict, str], dict] = _post,
) -> dict:
    if re.fullmatch(r"[0-9a-fA-F]{32}", account) is None:
        raise ValueError("account must be a 32-character hexadecimal Cloudflare account ID")
    if not token:
        raise ValueError("Cloudflare token must not be empty")
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/workers/observability/telemetry/query"
    response = post(url, body, token)
    if response.get("success") is not True:
        messages = "; ".join(error.get("message", "unspecified error") for error in response.get("errors", []))
        raise RuntimeError(f"Cloudflare query failed: {messages or 'missing success status'}")
    return response


def export_window(
    body: dict,
    *,
    fetch: Callable[[dict], dict],
    max_pages: int = 20,
) -> dict:
    """Drain a bounded query window, retaining raw private pages for replay.

    Exhaustion describes the query, not completeness of provider delivery or
    absence of ingestion-time sampling. No persistent watermark is advanced.
    """
    if max_pages < 1:
        raise ValueError("max_pages must be positive")
    pages = []
    seen: set[str] = set()
    cursors: set[str] = set()
    actions: Counter[str] = Counter()
    first, last = window_of(body)
    current = dict(body)
    for _ in range(max_pages):
        response = fetch(current)
        result = response["result"]
        if result["run"]["status"] != "COMPLETED":
            raise RuntimeError("query is not COMPLETED")
        statistics = result.get("statistics") or result["run"].get("statistics", {})
        if statistics.get("abr_level", 1) not in (None, 1):
            raise RuntimeError("sampled query cannot be exported as an exact event set")
        events = result["events"]["events"]
        count = result["events"]["count"]
        if not isinstance(count, int) or count < 0:
            raise RuntimeError("invalid reported event count")
        # Live limit=1 pagination reports count=1 on each nonempty page,
        # not the total for the whole window. Never treat it as a fleet count.
        if count != len(events):
            raise RuntimeError("page event count does not match returned events")
        pages.append(response)
        if not events:
            return {
                "query": body,
                "pages": pages,
                "summary": {
                    "pages": len(pages),
                    "events": len(seen),
                    "query_exhausted": True,
                    "query_abr_levels": [1],
                    "capture_sampling": "unknown",
                    "provider_delivery": "best_effort",
                    "actions": dict(sorted(actions.items())),
                },
            }
        for event in events:
            ident = event["$metadata"]["id"]
            if not isinstance(ident, str) or not ident:
                raise RuntimeError("event has no valid pagination identity")
            if not first <= event["timestamp"] < last:
                raise RuntimeError("event outside the requested window")
            if ident not in seen:
                seen.add(ident)
                action = event["source"].get("action", "unknown")
                actions[action] += 1
        cursor = events[-1]["$metadata"]["id"]
        if cursor in cursors:
            raise RuntimeError("pagination cursor did not advance")
        cursors.add(cursor)
        current = {**body, "offset": cursor, "offsetDirection": "next"}
    raise RuntimeError("max_pages reached before cursor exhaustion")
