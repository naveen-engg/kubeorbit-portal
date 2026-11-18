# --------------------------------------------------------------------------- #
# 1. Enable Required Google Cloud APIs
# --------------------------------------------------------------------------- #
locals {
  required_apis = [
    "run.googleapis.com",
    "iap.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "container.googleapis.com",
    "aiplatform.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.required_apis)
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

data "google_project" "current" {
  project_id = var.project_id
  depends_on = [google_project_service.apis]
}

# --------------------------------------------------------------------------- #
# 2. Artifact Registry Repository for KubeOrbit Container Image
# --------------------------------------------------------------------------- #
resource "google_artifact_registry_repository" "kubeorbit" {
  location      = var.region
  repository_id = "${var.service_name}-repo"
  description   = "Container registry for KubeOrbit enterprise multi-cloud portal"
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]
}

# --------------------------------------------------------------------------- #
# 3. VPC Network, Subnet & Cloud NAT with Static Egress IP
#    Allows Private GKE peering + deterministic IP allowlisting on EKS/AKS/GKE
# --------------------------------------------------------------------------- #
resource "google_compute_network" "vpc" {
  name                    = "${var.service_name}-vpc"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "cloudrun_subnet" {
  name                     = "${var.service_name}-cloudrun-subnet"
  ip_cidr_range            = "10.24.0.0/24"
  region                   = var.region
  network                  = google_compute_network.vpc.id
  private_ip_google_access = true
}

resource "google_compute_router" "router" {
  name    = "${var.service_name}-router"
  region  = var.region
  network = google_compute_network.vpc.id
}

resource "google_compute_address" "nat_static_ip" {
  name         = "${var.service_name}-nat-static-ip"
  region       = var.region
  address_type = "EXTERNAL"
  depends_on   = [google_project_service.apis]
}

resource "google_compute_router_nat" "nat" {
  name                                = "${var.service_name}-nat"
  router                              = google_compute_router.router.name
  region                              = var.region
  nat_ip_allocate_option              = "MANUAL_ONLY"
  nat_ips                             = [google_compute_address.nat_static_ip.self_link]
  source_subnetwork_ip_ranges_to_nat  = "LIST_OF_SUBNETWORKS"
  enable_dynamic_port_allocation      = true
  min_ports_per_vm                    = 4096
  max_ports_per_vm                    = 32768
  enable_endpoint_independent_mapping = false
  tcp_time_wait_timeout_sec           = 30

  subnetwork {
    name                    = google_compute_subnetwork.cloudrun_subnet.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }
}

# --------------------------------------------------------------------------- #
# 4. Secret Manager for Initial clusters.yaml
# --------------------------------------------------------------------------- #
resource "google_secret_manager_secret" "clusters_config" {
  secret_id = "${var.service_name}-clusters-yaml"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "clusters_config_initial" {
  secret      = google_secret_manager_secret.clusters_config.id
  secret_data = var.initial_clusters_yaml

  lifecycle {
    ignore_changes = [secret_data]
  }
}

# --------------------------------------------------------------------------- #
# 5. Cloud Run v2 Service with Direct VPC Egress & IAP Auth
# --------------------------------------------------------------------------- #
resource "google_cloud_run_v2_service" "kubeorbit" {
  provider             = google-beta
  name                 = var.service_name
  location             = var.region
  ingress              = "INGRESS_TRAFFIC_ALL"
  launch_stage         = "BETA"
  iap_enabled          = true
  deletion_protection  = false

  template {
    service_account                  = google_service_account.runner.email
    max_instance_request_concurrency = 40
    timeout                          = "120s"

    scaling {
      min_instance_count = var.min_instance_count
      max_instance_count = var.max_instance_count
    }

    # Direct VPC Egress routes private RFC 1918 traffic over VPC and internet traffic via Cloud NAT static IP
    vpc_access {
      network_interfaces {
        network    = google_compute_network.vpc.name
        subnetwork = google_compute_subnetwork.cloudrun_subnet.name
      }
      egress = "ALL_TRAFFIC"
    }

    volumes {
      name = "clusters-config-seed"
      secret {
        secret = google_secret_manager_secret.clusters_config.secret_id
        items {
          version = "latest"
          path    = "clusters.yaml"
        }
      }
    }

    containers {
      image = var.container_image

      # Copy seed config from read-only Secret Manager mount into writable /tmp/clusters.yaml
      # using 'cat >' (avoids gVisor Secret Manager FUSE inode replacement error in GNU cp).
      command = ["sh", "-c"]
      args = [
        "if [ ! -f /tmp/clusters.yaml ]; then cat /etc/kubeorbit-seed/clusters.yaml > /tmp/clusters.yaml; fi && exec uvicorn app:app --host 0.0.0.0 --port $${PORT:-8080}"
      ]

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
        cpu_idle = false
      }

      env {
        name  = "KUBEORBIT_AUTH_MODE"
        value = "iap"
      }
      env {
        name  = "KUBEORBIT_GCP_IAM_RBAC"
        value = "true"
      }
      env {
        name  = "KUBEORBIT_STRICT_RBAC"
        value = "true"
      }
      env {
        name  = "KUBEORBIT_DISABLE_LOCAL_PROVIDER"
        value = "true"
      }
      env {
        name  = "KUBEORBIT_CONFIG_PATH"
        value = "/tmp/clusters.yaml"
      }
      env {
        name  = "USE_GCP_SECRET_MANAGER"
        value = "true"
      }
      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "GCP_SECRET_NAME"
        value = google_secret_manager_secret.clusters_config.secret_id
      }
      env {
        name  = "KUBEORBIT_ADMIN_EMAILS"
        value = var.admin_emails
      }
      env {
        name  = "KUBEORBIT_OPERATOR_EMAILS"
        value = var.operator_emails
      }
      env {
        name  = "KUBEORBIT_VIEWER_EMAILS"
        value = var.viewer_emails
      }
      env {
        name  = "KUBEORBIT_IAP_AUDIENCE"
        value = var.iap_audience
      }
      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "TRUE"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.ai_location
      }
      env {
        name  = "KUBEORBIT_AI_MODEL"
        value = var.ai_model
      }

      volume_mounts {
        name       = "clusters-config-seed"
        mount_path = "/etc/kubeorbit-seed"
      }

      startup_probe {
        http_get {
          path = "/api/health"
          port = 8080
        }
        initial_delay_seconds = 3
        period_seconds        = 5
        failure_threshold     = 12
      }

      liveness_probe {
        http_get {
          path = "/api/health"
          port = 8080
        }
        period_seconds = 30
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_version.clusters_config_initial,
    google_secret_manager_secret_iam_member.runner_secret_access,
    google_compute_router_nat.nat,
  ]
}
