# Quiver headless trader — persistent-box infrastructure (decision D1/D4).
# A single hardened t4g.small running the systemd timer; secrets in SSM (free),
# CloudWatch metric-filter alarms -> SNS, and EC2 auto-recovery on host failure.
# Secret VALUES are created out-of-band (see DEPLOY.md) so they never enter tfstate.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_ami" "ubuntu_arm" {
  most_recent = true
  owners      = ["099720109477"] # Canonical
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*"]
  }
  filter {
    name   = "architecture"
    values = ["arm64"]
  }
}

# --- IAM: least-privilege instance role (SSM read on the prefix + CloudWatch logs)
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "quiver" {
  name               = "${var.name}-role"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "perms" {
  statement {
    sid       = "SsmReadPrefix"
    actions   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
    resources = ["arn:aws:ssm:${var.aws_region}:*:parameter${var.ssm_prefix}/*"]
  }
  statement {
    sid       = "CloudWatchLogs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams", "cloudwatch:PutMetricData"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "quiver" {
  name   = "${var.name}-policy"
  role   = aws_iam_role.quiver.id
  policy = data.aws_iam_policy_document.perms.json
}

resource "aws_iam_instance_profile" "quiver" {
  name = "${var.name}-profile"
  role = aws_iam_role.quiver.name
}

# --- SNS alerts (email) ---
resource "aws_sns_topic" "alerts" {
  name = "${var.name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# --- CloudWatch logs + metric-filter alarms (AUTH_ERROR / halt / KILL / plan error)
resource "aws_cloudwatch_log_group" "quiver" {
  name              = "/quiver/tick"
  retention_in_days = 30
}

locals {
  log_patterns = {
    auth_error = "AUTH_ERROR"
    daily_halt = "write_kill"
    plan_error = "\"error\""
  }
}

resource "aws_cloudwatch_log_metric_filter" "f" {
  for_each       = local.log_patterns
  name           = "${var.name}-${each.key}"
  log_group_name = aws_cloudwatch_log_group.quiver.name
  pattern        = each.value
  metric_transformation {
    name      = "${var.name}_${each.key}"
    namespace = "Quiver"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "f" {
  for_each            = local.log_patterns
  alarm_name          = "${var.name}-${each.key}"
  namespace           = "Quiver"
  metric_name         = "${var.name}_${each.key}"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# --- the box ---
resource "aws_instance" "quiver" {
  ami                  = data.aws_ami.ubuntu_arm.id
  instance_type        = var.instance_type
  iam_instance_profile = aws_iam_instance_profile.quiver.name
  key_name             = var.key_name

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }
  root_block_device {
    volume_size = 20
    volume_type = "gp3"
    encrypted   = true
  }
  user_data = <<-EOF
    #!/usr/bin/env bash
    set -euo pipefail
    export AWS_REGION=${var.aws_region} SSM_PREFIX=${var.ssm_prefix} QUIVER_REPO_URL=${var.repo_url}
    git clone "${var.repo_url}" /opt/quiver 2>/dev/null || true
    bash /opt/quiver/deploy/setup.sh
  EOF
  tags      = { Name = var.name }
}

# --- EC2 auto-recovery on a system-status-check failure (decision D4) ---
resource "aws_cloudwatch_metric_alarm" "recover" {
  alarm_name          = "${var.name}-system-recover"
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed_System"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  dimensions          = { InstanceId = aws_instance.quiver.id }
  # Reboots-in-place onto healthy hardware, preserving the EBS-resident ledger.
  alarm_actions = ["arn:aws:automate:${var.aws_region}:ec2:recover", aws_sns_topic.alerts.arn]
}
