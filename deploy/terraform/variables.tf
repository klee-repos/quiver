variable "aws_region" {
  description = "AWS region (decision D1)"
  type        = string
  default     = "us-east-1"
}

variable "name" {
  description = "Resource name prefix"
  type        = string
  default     = "quiver"
}

variable "instance_type" {
  description = "t4g.small is ~$12/mo and fits node+python+claude; bump to t4g.medium if 2 GiB is tight"
  type        = string
  default     = "t4g.small"
}

variable "ssm_prefix" {
  description = "SSM Parameter Store prefix holding the secrets (created out-of-band)"
  type        = string
  default     = "/quiver"
}

variable "alert_email" {
  description = "Email subscribed to the SNS alerts topic (AUTH_ERROR / halt / host failure)"
  type        = string
}

variable "key_name" {
  description = "Existing EC2 key pair for SSH (needed for the one-time interactive MCP re-auth)"
  type        = string
}

variable "repo_url" {
  description = "Git URL the box clones to /opt/quiver"
  type        = string
}
