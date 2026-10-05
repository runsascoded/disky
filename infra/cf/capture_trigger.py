"""`CaptureTrigger`: a finished capture landing in R2 → a queue → the trigger Worker.

A capture is complete when its marker object (`_SUCCESS.json`, written last)
lands under `prefix` in `bucket`. R2 publishes that event to this queue; the
`capture-trigger` Worker (deployed by wrangler, which attaches it as the queue's
consumer) submits the ingest (`BatchIngest`). Laptops then need only R2 write.

`moved_from_root=True` aliases the queue and notification to the same-named
resources at the stack root, so a stack that declared them inline adopts the
component with no replacements.
"""
from __future__ import annotations

import pulumi
import pulumi_cloudflare as cloudflare


class CaptureTrigger(pulumi.ComponentResource):
    queue: cloudflare.Queue
    notification: cloudflare.R2BucketEventNotification

    def __init__(
        self,
        name: str,
        account_id: str,
        bucket: str,
        queue_name: str,
        prefix: str = "captures/",
        suffix: str = "_SUCCESS.json",
        moved_from_root: bool = False,
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__("disky:cf:CaptureTrigger", name, None, opts)

        def o(old: str) -> pulumi.ResourceOptions:
            aliases = [pulumi.Alias(name=old, parent=pulumi.ROOT_STACK_RESOURCE)] if moved_from_root else None
            return pulumi.ResourceOptions(parent=self, aliases=aliases)

        self.queue = cloudflare.Queue(f"{name}-queue", account_id=account_id, queue_name=queue_name, opts=o("captures-queue"))
        self.notification = cloudflare.R2BucketEventNotification(
            f"{name}-notification",
            account_id=account_id,
            bucket_name=bucket,
            queue_id=self.queue.queue_id,
            rules=[cloudflare.R2BucketEventNotificationRuleArgs(
                actions=["PutObject", "CompleteMultipartUpload", "CopyObject"],
                prefix=prefix,
                suffix=suffix,
                description="a finished capture → ingest",
            )],
            opts=o("captures-notification"),
        )
        self.register_outputs({"queue": self.queue.queue_name})
