# Quiver headless trader — GCP (Compute Engine) infrastructure.
# Mirrors the AWS stack: one hardened e2-medium running the systemd timer; secrets in
# Secret Manager; a desktop + Chrome + Chrome Remote Desktop for the on-server Robinhood
# OAuth; Cloud Logging metrics -> Monitoring alert policies -> email; no public inbound
# (admin via IAP-tunneled SSH; CRD dials out to Google's relay). Secret VALUES are created
# out-of-band (bootstrap-gcp.sh) so they never enter tfstate.

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
  }
}

provider "google" {
  project = var.project
  region  = var.region
  zone    = var.zone
}

data "google_compute_image" "ubuntu" {
  family  = "ubuntu-2404-lts-amd64"
  project = "ubuntu-os-cloud"
}

# --- Dedicated least-privilege service account for the box ---
resource "google_service_account" "quiver" {
  account_id   = "${var.name}-box"
  display_name = "Quiver headless trader box"
}

locals {
  # Secrets the box reads (created out-of-band by bootstrap-gcp.sh). Per-secret accessor
  # grants = least privilege (the SA can read ONLY these, not every secret in the project).
  # The OPTIONAL secrets quiver-CLAUDE_CODE_OAUTH_TOKEN / quiver-NOTIFY_ALERTS_TO are NOT
  # listed here on purpose: they're usually unset (Claude auth is the on-box `claude login`;
  # alerts fall back to NOTIFY_TO). To drive Claude auth from Secret Manager instead, create
  # that secret AND add its name here, then re-apply — for_each requires the secret to exist.
  secrets = [
    "DEEPSEEK_API_KEY", "RH_ACCOUNT_NUMBER", "RESEND_API_KEY",
    "NOTIFY_TO", "RESEND_FROM", "STRATEGY_YAML",
  ]
}

resource "google_secret_manager_secret_iam_member" "accessor" {
  for_each  = toset(local.secrets)
  secret_id = "${var.secret_prefix}${each.key}"
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.quiver.email}"
}

# Ops Agent needs to write logs + metrics.
resource "google_project_iam_member" "log_writer" {
  project = var.project
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.quiver.email}"
}

resource "google_project_iam_member" "metric_writer" {
  project = var.project
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.quiver.email}"
}

# --- Network: custom VPC (default-deny ingress) + IAP-only SSH (no public inbound) ---
resource "google_compute_network" "quiver" {
  name                    = "${var.name}-net"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "quiver" {
  name          = "${var.name}-subnet"
  ip_cidr_range = "10.10.0.0/24"
  region        = var.region
  network       = google_compute_network.quiver.id
}

# Only inbound rule: SSH from Google's IAP range (admin via `gcloud ssh --tunnel-through-iap`).
# CRD is outbound-only, so nothing else is reachable from the internet.
resource "google_compute_firewall" "iap_ssh" {
  name      = "${var.name}-allow-iap-ssh"
  network   = google_compute_network.quiver.id
  direction = "INGRESS"
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
  source_ranges = ["35.235.240.0/20"] # IAP TCP-forwarding range
  target_service_accounts = [google_service_account.quiver.email]
}

# --- the box ---
resource "google_compute_instance" "quiver" {
  name         = var.name
  machine_type = var.machine_type
  zone         = var.zone

  boot_disk {
    initialize_params {
      image = data.google_compute_image.ubuntu.self_link
      size  = 20
      type  = "pd-balanced"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.quiver.id
    access_config {} # ephemeral external IP for EGRESS only (no inbound firewall opens it)
  }

  service_account {
    email  = google_service_account.quiver.email
    scopes = ["cloud-platform"] # actual perms gated by the IAM roles above
  }

  # GCE auto-restarts on crash + live-migrates on host maintenance — the auto-recovery analog.
  scheduling {
    automatic_restart   = true
    on_host_maintenance = "MIGRATE"
    preemptible         = false
  }

  metadata = {
    enable-oslogin = "TRUE" # IAP SSH via OS Login (no project-wide SSH keys)
    startup-script = <<-EOT
      #!/bin/bash
      set -uo pipefail
      exec >>/var/log/quiver-startup.log 2>&1
      echo "=== quiver startup $(date -u) ==="
      # Re-run-safe: the heavy provision runs once; later boots just ensure the timer is up.
      if [ -f /opt/quiver/.gcp-provisioned ]; then
        systemctl start quiver.timer 2>/dev/null || true
        exit 0
      fi
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -y && apt-get install -y git curl jq
      git clone ${var.repo_url} /opt/quiver || git -C /opt/quiver pull --ff-only || true
      if bash /opt/quiver/deploy/gcp/setup.sh; then
        bash /opt/quiver/deploy/setup-desktop.sh || echo "desktop layer had issues; trading stack is up"
        touch /opt/quiver/.gcp-provisioned
      else
        echo "setup.sh FAILED — leaving unprovisioned so the next boot re-runs it"
      fi
    EOT
  }

  tags = ["quiver"]

  # The box reads its secrets + writes logs/metrics the instant it boots, so the IAM grants
  # MUST exist first. depends_on removes the create-order race; the fetch() retry in setup.sh
  # covers the residual eventual-consistency propagation lag.
  depends_on = [
    google_secret_manager_secret_iam_member.accessor,
    google_project_iam_member.log_writer,
    google_project_iam_member.metric_writer,
  ]
}

# --- Cloud Logging metrics + Monitoring alerts (mirror the CloudWatch metric filters) ---
# The Ops Agent ships /var/log/quiver/tick.log to the log "quiver_tick" (see setup.sh).
# NOTE: these filters are validated/tuned against the real log structure on first boot;
# the Resend pager (run_tick.py / in-tick MCP) is the PRIMARY, cloud-agnostic alert path.
locals {
  log_patterns = {
    auth_error = "AUTH_ERROR"
    daily_halt = "write_kill"
    tick_error = "stopped"
  }
}

resource "google_logging_metric" "f" {
  for_each = local.log_patterns
  name     = "${var.name}_${each.key}"
  filter   = "logName=\"projects/${var.project}/logs/quiver_tick\" AND \"${each.value}\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_monitoring_notification_channel" "email" {
  display_name = "${var.name}-email"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
}

resource "google_monitoring_alert_policy" "f" {
  for_each     = local.log_patterns
  display_name = "${var.name}-${each.key}"
  combiner     = "OR"
  conditions {
    display_name = "${each.key} in tick log"
    condition_threshold {
      filter          = "resource.type=\"gce_instance\" AND metric.type=\"logging.googleapis.com/user/${var.name}_${each.key}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      trigger {
        count = 1
      }
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_COUNT"
      }
    }
  }
  notification_channels = [google_monitoring_notification_channel.email.id]
  depends_on            = [google_logging_metric.f]
}
