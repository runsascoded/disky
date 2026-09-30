# AWS Batch delete executor (spec `specs/staged-delete.md` CP8): the large-scope
# cell the drainer submits oversized S3 runs to (`disk-tree dispatch --serve`,
# `delete.batch` config). The job container (`delete-job/`) does the recursive
# delete and writes the run's result back to D1.
#
# Fill `terraform.tfvars` with `disk-tree iac aws-batch`. Validated + applied
# where Terraform + AWS creds live, not in this repo. `terraform init && apply`.

terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5" }
  }
}

provider "aws" {
  region = var.region
}

# The job's IAM role: delete only from the configured buckets (least privilege).
data "aws_iam_policy_document" "delete" {
  statement {
    actions   = ["s3:DeleteObject", "s3:ListBucket", "s3:ListBucketVersions", "s3:DeleteObjectVersion"]
    resources = concat(
      [for b in var.s3_buckets : "arn:aws:s3:::${b}"],
      [for b in var.s3_buckets : "arn:aws:s3:::${b}/*"],
    )
  }
}

resource "aws_iam_role" "job" {
  name               = "${var.project}-batch-delete-job"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  inline_policy {
    name   = "delete"
    policy = data.aws_iam_policy_document.delete.json
  }
}

resource "aws_batch_compute_environment" "fargate" {
  compute_environment_name = "${var.project}-delete"
  type                     = "MANAGED"
  compute_resources {
    type               = "FARGATE"
    max_vcpus          = 4
    subnets            = var.subnets
    security_group_ids = var.security_group_ids
  }
}

resource "aws_batch_job_queue" "delete" {
  name     = "${var.project}-delete"
  state    = "ENABLED"
  priority = 1
  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.fargate.arn
  }
}

resource "aws_batch_job_definition" "delete" {
  name = "${var.project}-delete"
  type = "container"
  platform_capabilities = ["FARGATE"]
  container_properties = jsonencode({
    image      = var.image
    jobRoleArn = aws_iam_role.job.arn
    resourceRequirements = [
      { type = "VCPU", value = "1" },
      { type = "MEMORY", value = "2048" },
    ]
    # D1 write-back creds are the deployment's; pass as a Batch secret in prod.
    environment = [{ name = "DISK_TREE_D1_DATABASE_ID", value = var.d1_database_id }]
    networkConfiguration = { assignPublicIp = "ENABLED" }
  })
}

output "job_queue" {
  value = aws_batch_job_queue.delete.name
}
output "job_definition" {
  value = aws_batch_job_definition.delete.name
}
