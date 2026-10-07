"""GCS XML multi-object deletion of explicit, generation-pinned objects."""

from __future__ import annotations

import random
from base64 import b64encode
from hashlib import md5
import threading
import time
from email.utils import formatdate
from functools import partial
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree as ET
import sys
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from math import isfinite

from requests.exceptions import RequestException

from .sweep_exec import DELETE_ATTEMPTS, DELETE_BACKOFF_CAP, TRANSIENT_CODES
from .sweep_pacing import DeleteAttempt

err = partial(print, file=sys.stderr)
XML_BATCH = 1000
RETRY_ERRORS = frozenset({"InternalError", "ServiceUnavailable", "SlowDown", "RequestTimeout", "Throttling"})
_sleep = time.sleep


def retry_after_seconds(value: str | None) -> float:
    """Malformed provider headers fall back to normal exponential backoff."""
    if not value:
        return 0
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return 0
    return max(0, seconds) if isfinite(seconds) else 0


def delete_payload(blobs: list[Any]) -> bytes:
    if not 0 < len(blobs) <= XML_BATCH:
        raise ValueError("XML delete needs 1..1000 objects")
    identities = [(blob.name, int(blob.generation or 0)) for blob in blobs]
    if any(generation <= 0 for _, generation in identities):
        raise ValueError("XML delete requires a positive generation for every object")
    if len(set(identities)) != len(identities):
        raise ValueError("XML delete identities must be unique")
    root = ET.Element("Delete")
    for name, generation in identities:
        obj = ET.SubElement(root, "Object")
        ET.SubElement(obj, "Key").text = name
        ET.SubElement(obj, "VersionId").text = str(generation)
    ET.SubElement(root, "Quiet").text = "False"
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def delete_results(payload: bytes, blobs: list[Any]) -> dict[tuple[str, int], str | None]:
    """Reject replies we cannot attribute to the exact requested identities."""
    root = ET.fromstring(payload)
    for element in root.iter():
        element.tag = element.tag.rsplit("}", 1)[-1]
    if root.tag != "DeleteResult":
        raise ValueError("XML delete response is not DeleteResult")
    wanted = {(blob.name, int(blob.generation)) for blob in blobs}
    results = {}
    for element in root:
        if element.tag not in ("Deleted", "Error"):
            raise ValueError("unexpected XML delete response element")
        key = (element.findtext("Key") or "", int(element.findtext("VersionId") or 0))
        if key not in wanted or key in results:
            raise ValueError("XML delete response has an unknown or duplicate identity")
        if element.tag == "Deleted":
            decision = "delete"
        else:
            code = element.findtext("Code")
            if code in RETRY_ERRORS:
                decision = None
            elif code in ("NoSuchKey", "NoSuchVersion", "NotFound"):
                decision = "skipped_gone"
            elif code == "PreconditionFailed":
                decision = "skipped_overwritten"
            else:
                decision = "delete_failed"
                err(f"XML delete {key[0]}@{key[1]}: {code}: {element.findtext('Message')}", flush=True)
        results[key] = decision
    if set(results) != wanted:
        raise ValueError("XML delete response omitted requested identities")
    return results


class XmlDeleter:
    """One authenticated HTTP connection pool per worker; reusable across batches.

    VersionId addresses the reviewed generation, including a noncurrent one.
    Missing objects can be acknowledged as Deleted by the service: this is an
    acknowledged-delete count, not proof of which actor removed the object.
    """

    def __init__(
        self,
        credentials: Any = None,
        http: Any = None,
        before_attempt: Callable[[int], None] | None = None,
        on_attempt: Callable[[DeleteAttempt], None] | None = None,
    ) -> None:
        self.credentials = credentials
        self.http = http
        self.local = threading.local()
        self.before_attempt = before_attempt
        self.on_attempt = on_attempt

    def session(self) -> Any:
        if self.http is not None:
            return self.http
        if not hasattr(self.local, "http"):
            from google.auth.transport.requests import AuthorizedSession
            self.local.http = AuthorizedSession(self.credentials)
        return self.local.http

    def __call__(self, client: Any, bucket: Any, blobs: list[Any]) -> list[tuple[Any, str]]:
        bucket_name = bucket if isinstance(bucket, str) else bucket.name
        remaining = list(blobs)
        delete_payload(remaining)  # Validate the whole batch before any request.
        settled = {}
        for attempt in range(DELETE_ATTEMPTS):
            results = None
            if self.before_attempt is not None:
                self.before_attempt(len(remaining))
            started = time.monotonic()
            status = None
            reason = "transport"
            retry_after = 0
            try:
                payload = delete_payload(remaining)
                response = self.session().post(
                    f"https://storage.googleapis.com/{quote(bucket_name, safe='')}?delete",
                    data=payload,
                    headers={"Content-Type": "application/xml", "Content-MD5": b64encode(md5(payload, usedforsecurity=False).digest()).decode(), "Date": formatdate(usegmt=True)},
                    timeout=(15, 120),
                )
                status = response.status_code
                retry_after = retry_after_seconds(response.headers.get("Retry-After"))
                reason = f"http-{status}" if status in TRANSIENT_CODES else "incomplete-response"
                if response.status_code not in TRANSIENT_CODES:
                    try:
                        response.raise_for_status()
                    except RequestException as error:
                        error.add_note(f"XML delete response: {response.text[:2000]}")
                        raise
                    try:
                        results = delete_results(response.content, remaining)
                    except (ET.ParseError, ValueError):
                        # An incomplete/unattributable reply may follow applied
                        # deletes. Retry exact generations, never current names.
                        results = None
            except RequestException as error:
                response = getattr(error, "response", None)
                if response is not None and response.status_code not in TRANSIENT_CODES:
                    if self.on_attempt is not None:
                        self.on_attempt(DeleteAttempt(len(remaining), time.monotonic() - started, "fatal", f"http-{response.status_code}"))
                    if settled:
                        # Return prior acknowledgements to the undo journal;
                        # failed remaining identities stop executor admission.
                        err(f"XML delete retry refused: HTTP {response.status_code}; retaining {len(settled)} settled identities", flush=True)
                        break
                    raise
            if results is not None:
                reason = "xml-transient" if any(decision is None for decision in results.values()) else ""
            if self.on_attempt is not None:
                self.on_attempt(DeleteAttempt(len(remaining), time.monotonic() - started, "backpressure" if reason else "fatal" if any(decision == "delete_failed" for decision in (results or {}).values()) else "ok", reason, retry_after))
            if results is not None:
                retry = []
                for blob in remaining:
                    key = (blob.name, int(blob.generation))
                    decision = results[key]
                    if decision is None:
                        retry.append(blob)
                    else:
                        settled[key] = decision
                remaining = retry
            if not remaining:
                break
            err(f"XML delete batch: {len(remaining)} of {len(blobs)} unsettled after attempt {attempt + 1}/{DELETE_ATTEMPTS}", flush=True)
            if attempt + 1 < DELETE_ATTEMPTS:
                _sleep(max(retry_after, min(DELETE_BACKOFF_CAP, 2 ** attempt) + random.uniform(0, 1)))
        return [(blob, settled.get((blob.name, int(blob.generation)), "delete_failed")) for blob in blobs]
