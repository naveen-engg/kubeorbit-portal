output "gke_cluster_name" {
  description = "GKE Cluster Name (enter in KubeOrbit UI -> Register Cluster -> GKE -> GKE Cluster Name)"
  value       = google_container_cluster.primary.name
}

output "gke_project_id" {
  description = "GCP Project ID (enter in KubeOrbit UI -> Register Cluster -> GKE -> Project ID)"
  value       = var.project_id
}

output "gke_location" {
  description = "GKE Zone or Region (enter in KubeOrbit UI -> Register Cluster -> GKE -> Region / Zone)"
  value       = google_container_cluster.primary.location
}

output "gke_impersonate_service_account" {
  description = "Target GKE Service Account Email (enter in KubeOrbit UI -> Register Cluster -> GKE -> Impersonate Service Account)"
  value       = google_service_account.gke_reader.email
}

output "kubeorbit_ui_registration_summary" {
  description = "Copy-paste values for registering this GKE cluster in the KubeOrbit UI"
  value = {
    provider                    = "gke"
    environment                 = var.environment
    project                     = var.project_id
    zone_or_region              = google_container_cluster.primary.location
    cluster_name                = google_container_cluster.primary.name
    impersonate_service_account = google_service_account.gke_reader.email
  }
}
