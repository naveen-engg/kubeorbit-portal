variable "project_id" {
  description = "GCP Project ID where the GKE cluster is hosted (e.g. adk-enterprise-observability)"
  type        = string
}

variable "region" {
  description = "GCP Region for VPC and Subnetwork"
  type        = string
  default     = "us-central1"
}

variable "location" {
  description = "GKE Cluster zone or region (e.g. us-central1-a for zonal or us-central1 for regional)"
  type        = string
  default     = "us-central1-a"
}

variable "cluster_name" {
  description = "Name of the GKE cluster"
  type        = string
  default     = "kubeorbit-gke"
}

variable "environment" {
  description = "KubeOrbit environment tier (sandbox | nonprod | prod)"
  type        = string
  default     = "sandbox"
}

variable "cloudrun_sa_email" {
  description = "Email of KubeOrbit's Cloud Run runtime Service Account (from terraform/cloud-run output runner_sa_email). Granted roles/iam.serviceAccountTokenCreator on the GKE reader SA."
  type        = string
}

variable "cloudrun_nat_static_ip" {
  description = "Optional static outbound IP from Cloud Run's Cloud NAT to allowlist in Master Authorized Networks (e.g. '34.123.45.67')"
  type        = string
  default     = ""
}

variable "machine_type" {
  description = "Compute Engine machine type for GKE worker nodes"
  type        = string
  default     = "e2-standard-2"
}

variable "node_count" {
  description = "Number of worker nodes in the primary node pool"
  type        = number
  default     = 1
}

variable "disk_size_gb" {
  description = "Boot disk size in GB per node"
  type        = number
  default     = 30
}

variable "enable_operator_permissions" {
  description = "When true, grants Kubernetes RBAC permissions for KubeOrbit Operator features (Secret Reveal & Workload Rollback) in addition to Viewer read-only resources."
  type        = bool
  default     = true
}
