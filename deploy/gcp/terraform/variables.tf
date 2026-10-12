variable "project" {
  description = "GCP project ID (billing enabled)"
  type        = string
  default     = "your-gcp-project-id" # override in terraform.tfvars (gitignored)
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "GCP zone for the box"
  type        = string
  default     = "us-central1-a"
}

variable "name" {
  description = "Resource name prefix"
  type        = string
  default     = "quiver"
}

variable "machine_type" {
  description = "e2-small (2 shared vCPU, 2 GiB) runs the tick with no desktop; ~$12/mo. setup.sh adds a 4G swapfile for the nightly npm update peak."
  type        = string
  default     = "e2-small"
}

variable "repo_url" {
  description = "Public Git URL the box clones to /opt/quiver (HTTPS — repo is public, no key)"
  type        = string
  default     = "https://github.com/klee-repos/quiver.git"
}

variable "alert_email" {
  description = "Email for the Cloud Monitoring notification channel (AUTH_ERROR / halt / plan error)"
  type        = string
  default     = "ops@yourdomain.com" # override in terraform.tfvars (gitignored)
}

variable "secret_prefix" {
  description = "Secret Manager name prefix holding the out-of-band secrets"
  type        = string
  default     = "quiver-"
}
