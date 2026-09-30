variable "project" {
  type        = string
  default     = "disk-tree"
  description = "Resource name prefix (matches `disk-tree iac aws-batch -p`)."
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "s3_buckets" {
  type        = list(string)
  description = "Buckets the delete job's IAM role may delete from (from `disk-tree iac aws-batch`)."
}

variable "image" {
  type        = string
  description = "ECR image URI of the delete-job container (built from delete-job/)."
}

variable "d1_database_id" {
  type        = string
  description = "D1 database the job writes run results back to."
}

variable "subnets" {
  type        = list(string)
  description = "Subnets for the Fargate compute environment."
}

variable "security_group_ids" {
  type        = list(string)
  description = "Security groups for the Fargate compute environment."
}
