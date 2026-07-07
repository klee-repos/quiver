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
  description = "e2-medium (2 vCPU, 4 GiB) runs XFCE + Chrome + the tick; ~$25-30/mo. Chrome Remote Desktop/Chrome are x86_64, which e2 is."
  type        = string
  default     = "e2-medium"
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
