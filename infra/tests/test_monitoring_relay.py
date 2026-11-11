"""`MonitoringRelay`: a Pub/Sub notification channel whose topic the Monitoring agent publishes to, pushed (OIDC, as
the relay's own account) to a Cloud Run service running `dt-cloud monitoring-relay serve` with the bot token; and
`CronAlerts` taking its `.channel` (or a console channel's id), refusing none."""
import sys
from pathlib import Path

import pulumi
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gcp"))
from gcp_jobs import CronAlerts, MonitoringRelay

URI = "https://relay-abc-uc.a.run.app"
CHANNEL = "projects/p/notificationChannels/7"


class Mocks(pulumi.runtime.Mocks):
    """Outputs as GCP returns them: a topic's id is its path, a service's `uri`, a channel's `name`, a project's `number`."""

    def __init__(self):
        self.resources: dict[tuple[str, str], dict] = {}  # (name, type) → inputs: the relay's resources share its name

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources[(args.name, args.typ)] = args.inputs
        out = dict(args.inputs)
        rid = args.name
        if args.typ == "gcp:pubsub/topic:Topic":
            rid = f"projects/p/topics/{args.inputs['name']}"
        elif args.typ == "gcp:cloudrunv2/service:Service":
            out["uri"] = URI
        elif args.typ == "gcp:monitoring/notificationChannel:NotificationChannel":
            out["name"] = rid = CHANNEL
        elif args.typ == "gcp:serviceaccount/account:Account":
            out["email"] = f"{args.inputs['accountId']}@p.iam.gserviceaccount.com"
            out["name"] = f"projects/p/serviceAccounts/{out['email']}"
        elif args.typ == "gcp:projects/serviceIdentity:ServiceIdentity":
            out["member"] = "serviceAccount:service-123@gcp-sa-pubsub.iam.gserviceaccount.com"
        return rid, out

    def call(self, args: pulumi.runtime.MockCallArgs):
        assert (args.token, args.args) == ("gcp:organizations/getProject:getProject", {"projectId": "p"})
        return {"number": "123"}


def relay() -> MonitoringRelay:
    return MonitoringRelay(
        "relay", project="p", region="us-central1", image="img:latest", account_id="relay-sa",
        secret_env={"SLACK_BOT_TOKEN": "bot-token"}, env={"SLACK_ALERT_CHANNEL": "C1", "DISKY_NOTIFY": "slack"},
    )


@pulumi.runtime.test
def test_the_relay_wires_monitoring_to_the_bot():
    mocks = Mocks()
    pulumi.runtime.set_mocks(mocks, project="test", stack="test", preview=False)
    r = relay()
    sa = "serviceAccount:relay-sa@p.iam.gserviceaccount.com"

    def check(v):
        assert sorted(mocks.resources) == [
            ("relay", "disky:gcp:MonitoringRelay"),
            ("relay", "gcp:cloudrunv2/service:Service"),
            ("relay", "gcp:monitoring/notificationChannel:NotificationChannel"),
            ("relay", "gcp:pubsub/subscription:Subscription"),
            ("relay", "gcp:pubsub/topic:Topic"),
            ("relay-bot-token-accessor", "gcp:secretmanager/secretIamMember:SecretIamMember"),
            ("relay-invoker", "gcp:cloudrunv2/serviceIamMember:ServiceIamMember"),
            ("relay-monitoring-publisher", "gcp:pubsub/topicIAMMember:TopicIAMMember"),
            ("relay-pubsub-agent", "gcp:projects/serviceIdentity:ServiceIdentity"),
            ("relay-pubsub-api", "gcp:projects/service:Service"),
            ("relay-pubsub-token", "gcp:serviceaccount/iAMMember:IAMMember"),
            ("relay-sa", "gcp:serviceaccount/account:Account"),
        ]
        res = mocks.resources
        assert [
            res[("relay-invoker", "gcp:cloudrunv2/serviceIamMember:ServiceIamMember")],
            res[("relay-pubsub-token", "gcp:serviceaccount/iAMMember:IAMMember")],
            res[("relay-monitoring-publisher", "gcp:pubsub/topicIAMMember:TopicIAMMember")],
            res[("relay-bot-token-accessor", "gcp:secretmanager/secretIamMember:SecretIamMember")],
        ] == [
            {"project": "p", "location": "us-central1", "name": "relay", "role": "roles/run.invoker", "member": sa},
            {"serviceAccountId": "projects/p/serviceAccounts/relay-sa@p.iam.gserviceaccount.com", "role": "roles/iam.serviceAccountTokenCreator",
             "member": "serviceAccount:service-123@gcp-sa-pubsub.iam.gserviceaccount.com"},
            {"project": "p", "topic": "relay", "role": "roles/pubsub.publisher",
             "member": "serviceAccount:service-123@gcp-sa-monitoring-notification.iam.gserviceaccount.com"},
            {"project": "p", "secretId": "bot-token", "role": "roles/secretmanager.secretAccessor", "member": sa},
        ]
        check_inputs(v[0], res)

    return pulumi.Output.all(r.channel, r.subscription.id, r.service.uri).apply(check)


def check_inputs(channel: str, res: dict[tuple[str, str], dict]):
    assert channel == CHANNEL
    sub = res[("relay", "gcp:pubsub/subscription:Subscription")]
    assert (sub["topic"], sub["pushConfig"], sub["ackDeadlineSeconds"], sub["messageRetentionDuration"], sub["retryPolicy"]) == (
        "projects/p/topics/relay",
        {"pushEndpoint": URI, "oidcToken": {"serviceAccountEmail": "relay-sa@p.iam.gserviceaccount.com", "audience": URI}},
        60, "86400s", {"minimumBackoff": "10s", "maximumBackoff": "600s"},
    )
    svc = res[("relay", "gcp:cloudrunv2/service:Service")]
    template = svc["template"]
    assert (svc["ingress"], svc["deletionProtection"], template["serviceAccount"], template["scaling"]) == (
        "INGRESS_TRAFFIC_ALL", False, "relay-sa@p.iam.gserviceaccount.com", {"minInstanceCount": 0, "maxInstanceCount": 1},
    )
    channel_labels = res[("relay", "gcp:monitoring/notificationChannel:NotificationChannel")]["labels"]
    topic_labels = res[("relay", "gcp:pubsub/topic:Topic")]["labels"]
    assert template["containers"] == [{
        "image": "img:latest",
        "commands": ["dt-cloud", "monitoring-relay", "serve", "-c", "slack"],
        "resources": {"limits": {"cpu": "1000m", "memory": "1Gi"}, "cpuIdle": True, "startupCpuBoost": True},
        "envs": [
            {"name": "SLACK_ALERT_CHANNEL", "value": "C1"},
            {"name": "DISKY_NOTIFY", "value": "slack"},
            {"name": "SLACK_BOT_TOKEN", "valueSource": {"secretKeyRef": {"secret": "bot-token", "version": "latest"}}},
        ],
    }]
    assert channel_labels == {"topic": "projects/p/topics/relay"}
    assert topic_labels == {"component": "alert-relay"}


@pulumi.runtime.test
def test_cron_alerts_take_the_relays_channel_beside_a_console_one():
    mocks = Mocks()
    pulumi.runtime.set_mocks(mocks, project="test", stack="test", preview=False)
    r = relay()
    a = CronAlerts("daily", project="p", job="daily-submit", trigger="daily", channels=["projects/p/notificationChannels/1", r.channel])
    return pulumi.Output.all(a.failed.notification_channels, a.unstarted.notification_channels).apply(
        lambda v: _eq(v, [["projects/p/notificationChannels/1", CHANNEL]] * 2),
    )


def _eq(got, want):
    assert got == want


def test_cron_alerts_refuse_no_channels():
    pulumi.runtime.set_mocks(Mocks(), project="test", stack="test", preview=False)
    with pytest.raises(ValueError) as e:
        CronAlerts("daily", project="p", job="daily-submit", trigger="daily", channels=[])
    assert str(e.value) == "CronAlerts daily: no notification channels (policies nobody is told of)"
