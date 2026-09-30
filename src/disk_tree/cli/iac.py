"""`disk-tree iac` — generate executor deployment config from buckets.yml (spec
``specs/staged-delete.md``, CP8).

- ``iac r2-bindings`` — the ``wrangler.toml`` R2 bindings that activate the edge
  CFN executor (CP7) for the configured R2 buckets.
- ``iac config`` — the ``CfnDashboard`` Pulumi component config (``iac/``).
"""
from __future__ import annotations

import json

from click import echo, option

from disk_tree.cli.base import cli


@cli.group("iac")
def iac():
    """Infrastructure-as-code helpers: derive deployment config from buckets.yml."""


@iac.command("r2-bindings")
@option("-c", "--config", "config_path", default=None, help="buckets.yml path (default: <DISK_TREE_ROOT>/buckets.yml)")
def r2_bindings(config_path: str | None):
    """Emit the `[[r2_buckets]]` wrangler.toml blocks binding each configured R2
    bucket for the edge CFN executor (CP7)."""
    from disk_tree.cli.sync import load_config
    from disk_tree.iac import r2_bindings_toml

    echo(r2_bindings_toml(load_config(config_path)), nl=False)


@iac.command("config")
@option("-c", "--config", "config_path", default=None, help="buckets.yml path (default: <DISK_TREE_ROOT>/buckets.yml)")
@option("-p", "--project", default="disk-tree", help="CfnDashboard project name")
def config_cmd(config_path: str | None, project: str):
    """Emit the `CfnDashboard` Pulumi component config (JSON) for this deployment."""
    from disk_tree.cli.sync import load_config
    from disk_tree.iac import dashboard_config

    echo(json.dumps(dashboard_config(load_config(config_path), project=project), indent=2))


@iac.command("aws-batch")
@option("-c", "--config", "config_path", default=None, help="buckets.yml path (default: <DISK_TREE_ROOT>/buckets.yml)")
@option("-p", "--project", default="disk-tree", help="Batch resource name prefix")
@option("-r", "--region", default="us-east-1", help="AWS region")
def aws_batch(config_path: str | None, project: str, region: str):
    """Emit the Terraform tfvars for the AWS Batch delete executor (`iac/aws/`)."""
    from disk_tree.cli.sync import load_config
    from disk_tree.iac import aws_batch_tfvars

    echo(aws_batch_tfvars(load_config(config_path), region=region, project=project), nl=False)
