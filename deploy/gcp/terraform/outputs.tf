output "instance_name" {
  value = google_compute_instance.quiver.name
}

output "zone" {
  value = var.zone
}

output "external_ip" {
  description = "Ephemeral external IP — EGRESS only (no inbound firewall opens it)"
  value       = google_compute_instance.quiver.network_interface[0].access_config[0].nat_ip
}

output "ssh_via_iap" {
  description = "Admin shell with no public inbound (IAP-tunneled SSH)"
  value       = "gcloud compute ssh ${google_compute_instance.quiver.name} --zone ${var.zone} --tunnel-through-iap"
}

output "service_account" {
  value = google_service_account.quiver.email
}
