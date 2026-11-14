"""A signed `vmd` client (specs/vm-mode.md §4.3), stdlib only: what the Worker's adapter does, for checks and benches.

    from vmd_client import Vmd
    v = Vmd("http://127.0.0.1:7340", "gcs", key)
    status, body = v.post("/v1/view", {...})
"""
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class BadSignature(Exception):
    pass


class Vmd:
    def __init__(self, url: str, tenant: str, key: bytes):
        self.url, self.tenant, self.key = url.rstrip("/"), tenant, key

    def sig(self, s: str) -> str:
        return hmac.new(self.key, s.encode(), hashlib.sha256).hexdigest()

    def call(self, method: str, path: str, body: dict | None = None, ts: int | None = None) -> tuple[int, dict]:
        """`(status, JSON body)`; raises `BadSignature` when a signed reply's signature doesn't check."""
        data = json.dumps(body).encode() if body is not None else b""
        ts = str(ts if ts is not None else int(time.time()))
        h = {
            "x-disky-tenant": self.tenant,
            "x-disky-ts": ts,
            "x-disky-sig": self.sig(f"v1\n{method}\n{path}\n{self.tenant}\n{ts}\n{sha256_hex(data)}"),
            "content-type": "application/json",
        }
        rq = urllib.request.Request(self.url + path, data=data if method == "POST" else None, headers=h, method=method)
        try:
            with urllib.request.urlopen(rq, timeout=600) as r:
                status, out, rh = r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            status, out, rh = e.code, e.read(), e.headers
        got = rh.get("x-disky-sig")
        if got is not None and not hmac.compare_digest(got, self.sig(f"v1-resp\n{self.tenant}\n{ts}\n{sha256_hex(out)}")):
            raise BadSignature(f"{path}: the reply's signature doesn't check")
        return status, json.loads(out)

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        return self.call("POST", path, body)

    def get(self, path: str) -> tuple[int, dict]:
        return self.call("GET", path)


def matcher(t: str) -> dict:
    """A bench name or path term (`^`/`$` anchors) as a `QueryAst` matcher."""
    m = {"kind": "sub", "text": t.lstrip("^").rstrip("$")}
    if t.startswith("^"):
        m["start"] = True
    if t.endswith("$") and len(t) > 1:
        m["end"] = True
    return m


def ast_of(e: dict) -> dict | None:
    """A bench search expression (`{"t"|"p"|"re": …}`, `and` / `or` / `not`) as the Worker's `QueryAst`
    (`(OR of AND-groups) AND NOT (any negative)`), or None when it has no such form (a negative inside an OR, a NOT of
    an AND). A regex comes back as a `regex` matcher (vmd refuses it)."""
    (k, v), = e.items()
    if k in ("t", "p"):
        return {"alts": [[matcher(v)]], "neg": []}
    if k == "re":
        return {"alts": [[{"kind": "regex", "text": v}]], "neg": []}
    if k == "not":
        inner = ast_of(v)
        if inner is None or inner["neg"] or any(len(g) != 1 for g in inner["alts"]):
            return None
        return {"alts": [[]], "neg": [g[0] for g in inner["alts"]]}
    parts = [ast_of(x) for x in v]
    if any(p is None for p in parts):
        return None
    if k == "or":
        if any(p["neg"] for p in parts) or any(g == [] for p in parts for g in p["alts"]):
            return None
        return {"alts": [g for p in parts for g in p["alts"]], "neg": []}
    # and: the product of the parts' groups, their negatives together
    alts = [[]]
    for p in parts:
        alts = [a + g for a in alts for g in p["alts"]]
    return {"alts": alts, "neg": [n for p in parts for n in p["neg"]]}


def body_of(q: dict, pins: dict) -> dict | None:
    """A bench query as a `/v1/view` body (`flat`: the bench's answer form too; `min_ppm` as the cell rule's legacy
    form), or None when it has none: a directory-total view, or a search with no `QueryAst` form."""
    if q.get("dirs"):
        return None
    cell = q.get("cell") or {"depth": q["depth"], **({"min_ppm": q["min_ppm"]} if q.get("min_ppm") else {})}
    b = {"pins": pins, "path": q.get("path", ""), "cell": cell, "flat": True}
    for k in ("scan", "from", "to", "owners", "classes", "staged"):
        if q.get(k) is not None:
            b[k] = q[k]
    if q.get("q") is not None:
        a = ast_of(q["q"])
        if a is None:
            return None
        b["q"] = a
    return b
