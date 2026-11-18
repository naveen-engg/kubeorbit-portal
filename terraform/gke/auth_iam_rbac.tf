# =========================================================================== #
# GKE Authentication & Authorization for KubeOrbit (IAM Impersonation + RBAC)
# =========================================================================== #
# Architecture:
#   Cloud Run SA (var.cloudrun_sa_email)
#     --[roles/iam.serviceAccountTokenCreator]-->
#       Target GKE SA (kubeorbit-gke-reader@<PROJECT>.iam.gserviceaccount.com)
#         --[roles/container.clusterViewer + K8s ClusterRoleBinding]-->
#           GKE Control Plane & Kubernetes API Server
# =========================================================================== #

# 1. Target GKE Reader Service Account (impersonated by KubeOrbit)
resource "google_service_account" "gke_reader" {
  account_id   = "${var.cluster_name}-reader"
  display_name = "KubeOrbit Reader SA for GKE (${var.cluster_name})"
  description  = "Impersonated by KubeOrbit Cloud Run service to inspect GKE cluster ${var.cluster_name}"
  depends_on   = [google_project_service.gke_apis]
}

# 2. Grant GCP Control-Plane Viewer role so `gcloud container clusters describe`
#    and `gcloud container clusters get-credentials` succeed
resource "google_project_iam_member" "gke_cluster_viewer" {
  project = var.project_id
  role    = "roles/container.clusterViewer"
  member  = "serviceAccount:${google_service_account.gke_reader.email}"
}

# 3. Allow KubeOrbit's Cloud Run SA (when set) and local admin user to impersonate this GKE Reader SA
resource "google_service_account_iam_member" "allow_cloudrun_impersonation" {
  count              = var.cloudrun_sa_email != "" ? 1 : 0
  service_account_id = google_service_account.gke_reader.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = startswith(var.cloudrun_sa_email, "user:") || startswith(var.cloudrun_sa_email, "serviceAccount:") ? var.cloudrun_sa_email : "serviceAccount:${var.cloudrun_sa_email}"
}

# 4. Kubernetes RBAC: ClusterRole & ClusterRoleBinding inside the GKE Cluster
#    Grants read access across all KubeOrbit views + optional Operator actions
resource "kubernetes_cluster_role" "kubeorbit_access" {
  metadata {
    name = "kubeorbit-cluster-access"
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "kubeorbit"
    }
  }

  # Core K8s resources (Nodes, Namespaces, Pods, Logs, Events, Services, ConfigMaps, PVCs, SAs)
  rule {
    api_groups = [""]
    resources = [
      "nodes",
      "namespaces",
      "pods",
      "pods/log",
      "services",
      "endpoints",
      "configmaps",
      "persistentvolumes",
      "persistentvolumeclaims",
      "serviceaccounts",
      "events",
      "resourcequotas",
      "limitranges",
    ]
    verbs = ["get", "list", "watch"]
  }

  # Workloads, Networking, Storage, Metrics, Gateway API, CRDs
  rule {
    api_groups = [
      "apps",
      "batch",
      "networking.k8s.io",
      "storage.k8s.io",
      "policy",
      "autoscaling",
      "metrics.k8s.io",
      "events.k8s.io",
      "gateway.networking.k8s.io",
      "apiextensions.k8s.io",
    ]
    resources = ["*"]
    verbs     = ["get", "list", "watch"]
  }

  # Operator capabilities: Secret Reveal (read secrets) & Workload Rollback (patch deployments/statefulsets/daemonsets)
  dynamic "rule" {
    for_each = var.enable_operator_permissions ? [1] : []
    content {
      api_groups = [""]
      resources  = ["secrets"]
      verbs      = ["get", "list"]
    }
  }

  dynamic "rule" {
    for_each = var.enable_operator_permissions ? [1] : []
    content {
      api_groups = ["apps"]
      resources  = ["deployments", "statefulsets", "daemonsets", "replicasets"]
      verbs      = ["get", "list", "watch", "patch", "update"]
    }
  }

  depends_on = [google_container_node_pool.primary_nodes]
}

resource "kubernetes_cluster_role_binding" "kubeorbit_binding" {
  metadata {
    name = "kubeorbit-cluster-access-binding"
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role.kubeorbit_access.metadata[0].name
  }

  subject {
    kind      = "User"
    name      = google_service_account.gke_reader.email
    api_group = "rbac.authorization.k8s.io"
  }
}
