output "cloudrun_service_url" {
  description = "HTTPS URL of the deployed KubeOrbit Cloud Run service"
  value       = google_cloud_run_v2_service.kubeorbit.uri
}

output "artifact_registry_repo" {
  description = "Artifact Registry Docker repository path"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.kubeorbit.repository_id}"
}

output "runner_sa_email" {
  description = "Cloud Run Service Account email (pass this as var.cloudrun_sa_email to terraform/gke)"
  value       = google_service_account.runner.email
}

output "runner_sa_unique_id" {
  description = "Cloud Run Service Account Unique Numeric OAuth2 Subject ID 'sub' (pass this as var.cloudrun_sa_unique_id to terraform/eks and terraform/aks)"
  value       = google_service_account.runner.unique_id
}

output "nat_static_ip" {
  description = "Static outbound IP of Cloud NAT (allowlist this IP/32 in GKE Master Authorized Networks, EKS Public Endpoint CIDR, and AKS API Server Authorized IP Ranges)"
  value       = google_compute_address.nat_static_ip.address
}

output "vpc_network_name" {
  description = "VPC network name used by Cloud Run Direct VPC Egress"
  value       = google_compute_network.vpc.name
}

output "custom_iam_roles" {
  description = "Custom GCP IAM Role IDs created for KubeOrbit 3-Tier RBAC"
  value = {
    admin    = google_project_iam_custom_role.kubeorbit_admin.name
    operator = google_project_iam_custom_role.kubeorbit_operator.name
    viewer   = google_project_iam_custom_role.kubeorbit_viewer.name
  }
}
