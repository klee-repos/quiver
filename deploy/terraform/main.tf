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

# x86_64 (NOT arm64): Chrome Remote Desktop's Linux host package and Google Chrome are
# amd64-only, and the on-server browser OAuth is the whole point of this box. So we run an
# x86_64 Ubuntu + a t3-class instance rather than the cheaper t4g/arm64.
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
  filter {
    name   = "architecture"
    values = ["x86_64"]
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

# SSM Session Manager — operator shell access with NO inbound port (the box runs no
# sshd-facing rule). The only human admin path; Chrome Remote Desktop (the desktop) is
# likewise outbound-only, so the box needs zero inbound security-group rules.
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.quiver.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
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

# Dedicated security group: ZERO inbound (Chrome Remote Desktop dials OUT to Google's relay;
# operator shell is SSM Session Manager, also outbound) + all egress. This ENFORCES the
# "no inbound on a live-broker box" posture in IaC rather than inheriting the account's
# shared, mutable default security group.
resource "aws_security_group" "quiver" {
  name        = "${var.name}-sg"
  description = "Quiver box — no inbound; egress only"
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = var.name }
}

# --- the box ---
resource "aws_instance" "quiver" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  iam_instance_profile   = aws_iam_instance_profile.quiver.name
  key_name               = var.key_name
  vpc_security_group_ids = [aws_security_group.quiver.id]

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
    # Bootstrap just enough to read the read-only deploy key from SSM and clone the
    # PRIVATE repo over SSH (GitHub host key pinned, not trust-on-first-use); the
    # idempotent setup.sh installs everything else. Fails closed: a missing key or a
    # failed clone aborts provisioning rather than leaving a half-built box.
    apt-get update -y && apt-get install -y git curl unzip
    command -v aws >/dev/null || { curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscliv2.zip ; unzip -q -o /tmp/awscliv2.zip -d /tmp ; /tmp/aws/install --update ; }
    install -d -m 700 /root/.ssh
    aws ssm get-parameter --region "$AWS_REGION" --name "$SSM_PREFIX/GITHUB_DEPLOY_KEY" --with-decryption --query Parameter.Value --output text > /root/.ssh/id_ed25519
    chmod 600 /root/.ssh/id_ed25519
    echo 'github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl' > /root/.ssh/known_hosts
    git clone "$QUIVER_REPO_URL" /opt/quiver
    bash /opt/quiver/deploy/setup.sh
    # Desktop + Chrome Remote Desktop layer for the on-server Robinhood/Claude OAuth.
    # Best-effort: a desktop hiccup must NOT undo the (already-provisioned) trading stack.
    bash /opt/quiver/deploy/setup-desktop.sh || echo "setup-desktop.sh had issues; trading stack is up, finish CRD via SSM"
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
