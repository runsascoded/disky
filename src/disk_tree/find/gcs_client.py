"""A `google-cloud-storage` client whose requests name disky in their User-Agent (`disky/<component> gcloud-python/…`),
so a bucket's usage logs (and `dt-cloud access requesters`, whose `client` is the agent's first token) attribute
disky's own requests exactly: GCS bills every request to the bucket's project and has no per-request label."""

from __future__ import annotations

from typing import Any


def user_agent(component: str) -> str:
    return f"disky/{component}"


def storage_client(component: str, **kwargs: Any):
    from google.api_core.client_info import ClientInfo
    from google.cloud import storage

    return storage.Client(client_info=ClientInfo(user_agent=user_agent(component)), **kwargs)
