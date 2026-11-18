# --------------------------------------------------------------------------- #
# 1. Enable Required GCP APIs
# --------------------------------------------------------------------------- #
resource "google_project_service" "gke_apis" {
  for_each = toset([
    "container.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
  ])
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# --------------------------------------------------------------------------- #
# 2. Dedicated VPC & Subnet for GKE
# --------------------------------------------------------------------------- #
resource "google_compute_network" "gke_vpc" {
  name                    = "${var.cluster_name}-vpc"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.gke_apis]
}

resource "google_compute_subnetwork" "gke_subnet" {
  name                     = "${var.cluster_name}-subnet"
  ip_cidr_range            = "10.30.0.0/20"
  region                   = var.region
  network                  = google_compute_network.gke_vpc.id
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "pods-range"
    ip_cidr_range = "10.32.0.0/14"
  }

  secondary_ip_range {
    range_name    = "services-range"
    ip_cidr_range = "10.36.0.0/20"
  }
}

# --------------------------------------------------------------------------- #
# 3. GKE Cluster & Managed Node Pool
# --------------------------------------------------------------------------- #
resource "google_container_cluster" "primary" {
  name                     = var.cluster_name
  location                 = var.location
  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = false

  network    = google_compute_network.gke_vpc.id
  subnetwork = google_compute_subnetwork.gke_subnet.id

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods-range"
    services_secondary_range_name = "services-range"
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  release_channel {
    channel = "REGULAR"
  }

  dynamic "master_authorized_networks_config" {
    for_each = var.cloudrun_nat_static_ip != "" ? [1] : []
    content {
      cidr_blocks {
        cidr_block   = "${var.cloudrun_nat_static_ip}/32"
        display_name = "kubeorbit-cloudrun-nat"
      }
    }
  }

  resource_labels = {
    environment = var.environment
    managed_by  = "terraform"
    portal      = "kubeorbit"
  }

  depends_on = [google_project_service.gke_apis]
}

resource "google_container_node_pool" "primary_nodes" {
  name       = "${var.cluster_name}-pool"
  location   = var.location
  cluster    = google_container_cluster.primary.name
  node_count = var.node_count

  node_config {
    machine_type = var.machine_type
    disk_size_gb = var.disk_size_gb
    disk_type    = "pd-balanced"

    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
    ]

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    labels = {
      environment = var.environment
    }
  }
}
