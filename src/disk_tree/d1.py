"""A tiny Cloudflare D1 HTTP client (spec ``specs/staged-delete.md``, CP4).

The laptop drainer already holds ``CLOUDFLARE_API_TOKEN`` (it deploys the Pages
site), so it reads the edge-enqueued deletion runs straight from D1 over the
REST API rather than through a bespoke authenticated Functions endpoint. The
edge Functions own the *browser* path (stage / dispatch / read); this owns the
*execution* path.

stdlib only (like ``notify/discord_api.py``); no new dependency.
"""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.request import Request, urlopen

API = "https://api.cloudflare.com/client/v4"


class D1Error(RuntimeError):
    pass


class D1Client:
    """Query a Cloudflare D1 database. ``params`` bind ``?`` placeholders."""

    def __init__(self, account_id: str, database_id: str, token: str):
        self.account_id = account_id
        self.database_id = database_id
        self.token = token

    @classmethod
    def from_env(cls, database_id: str | None = None) -> "D1Client":
        """Build from the standard Cloudflare env vars. ``database_id`` falls
        back to ``DISK_TREE_D1_DATABASE_ID``."""
        account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        token = os.environ.get("CLOUDFLARE_API_TOKEN")
        database_id = database_id or os.environ.get("DISK_TREE_D1_DATABASE_ID")
        missing = [
            name
            for name, val in [
                ("CLOUDFLARE_ACCOUNT_ID", account_id),
                ("CLOUDFLARE_API_TOKEN", token),
                ("DISK_TREE_D1_DATABASE_ID", database_id),
            ]
            if not val
        ]
        if missing:
            raise D1Error(f"missing env for D1 access: {', '.join(missing)}")
        return cls(account_id, database_id, token)  # type: ignore[arg-type]

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict]:
        """Run one statement; return its result rows (empty for writes)."""
        url = f"{API}/accounts/{self.account_id}/d1/database/{self.database_id}/query"
        body = json.dumps({"sql": sql, "params": params or []}).encode()
        req = Request(
            url,
            data=body,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req) as resp:
            payload = json.loads(resp.read().decode())
        if not payload.get("success"):
            raise D1Error(f"D1 query failed: {payload.get('errors')}")
        # `result` is a list (one entry per statement); we send one.
        result = payload["result"][0]
        return result.get("results", [])
