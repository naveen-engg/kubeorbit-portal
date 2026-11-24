# --------------------------------------------------------------------------- #
# 1. Dedicated KubeOrbit Cloud Run Service Account (Zero-Key OIDC Identity)
# --------------------------------------------------------------------------- #
resource "google_service_account" "runner" {
  account_id   = "${var.service_name}-runner"
  display_name = "KubeOrbit Cloud Run Runtime SA (Multi-Cloud OIDC Identity)"
  description  = "Runtime identity for KubeOrbit on Cloud Run. Mints OIDC ID tokens for AWS EKS and Azure AKS and impersonates GKE reader SAs."
  depends_on   = [google_project_service.apis]
}

# Allow the Cloud Run SA to read and write versions of clusters.yaml in Secret Manager
resource "google_secret_manager_secret_iam_member" "runner_secret_access" {
  secret_id = google_secret_manager_secret.clusters_config.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runner.email}"
}

resource "google_secret_manager_secret_iam_member" "runner_secret_version_manager" {
  secret_id = google_secret_manager_secret.clusters_config.id
  role      = "roles/secretmanager.secretVersionManager"
  member    = "serviceAccount:${google_service_account.runner.email}"
}

# Allow the Cloud Run SA to invoke Vertex AI Gemini models for AI Diagnostics & Chat
resource "google_project_iam_member" "runner_vertex_ai" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.runner.email}"
}

# Allow the Cloud Run SA to write structured audit logs to GCP Cloud Logging
resource "google_project_iam_member" "runner_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.runner.email}"
}

# Allow the Cloud Run SA to inspect project IAM bindings (resourcemanager.projects.getIamPolicy)
# so KubeOrbit can resolve kubeorbitAdmin / kubeorbitOperator / kubeorbitViewer roles per user.
resource "google_project_iam_member" "runner_iam_reviewer" {
  project = var.project_id
  role    = "roles/iam.securityReviewer"
  member  = "serviceAccount:${google_service_account.runner.email}"
}

# --------------------------------------------------------------------------- #
# 2. KubeOrbit 3-Tier Custom GCP IAM Roles
#    (kubeorbitAdmin, kubeorbitOperator, kubeorbitViewer)
# --------------------------------------------------------------------------- #
resource "google_project_iam_custom_role" "kubeorbit_admin" {
  project     = var.project_id
  role_id     = "kubeorbitAdmin${var.custom_role_id_suffix}"
  title       = "KubeOrbit Portal Administrator"
  description = "Full KubeOrbit administrative access: cluster onboarding/editing/deletion, secret reveal, workload rollback, and read-only views."
  permissions = ["resourcemanager.projects.get"]
  depends_on  = [google_project_service.apis]
}

resource "google_project_iam_custom_role" "kubeorbit_operator" {
  project     = var.project_id
  role_id     = "kubeorbitOperator${var.custom_role_id_suffix}"
  title       = "KubeOrbit Portal Operator"
  description = "KubeOrbit SRE/Operator access: workload rollback, secret reveal, AI diagnostics, and read-only views (no cluster CRUD)."
  permissions = ["resourcemanager.projects.get"]
  depends_on  = [google_project_service.apis]
}

resource "google_project_iam_custom_role" "kubeorbit_viewer" {
  project     = var.project_id
  role_id     = "kubeorbitViewer${var.custom_role_id_suffix}"
  title       = "KubeOrbit Portal Viewer"
  description = "KubeOrbit read-only access: view cluster topology, pods, logs, YAML, cost, and AI diagnostics."
  permissions = ["resourcemanager.projects.get"]
  depends_on  = [google_project_service.apis]
}

locals {
  effective_viewer_members = distinct(concat(
    var.viewer_members,
    var.viewer_domain != "" ? ["domain:${var.viewer_domain}"] : []
  ))

  all_authorized_iap_members = distinct(concat(
    var.admin_members,
    var.operator_members,
    local.effective_viewer_members,
    var.iap_authorized_members
  ))
}

resource "google_project_iam_binding" "kubeorbit_admins" {
  count   = length(var.admin_members) > 0 ? 1 : 0
  project = var.project_id
  role    = google_project_iam_custom_role.kubeorbit_admin.name
  members = var.admin_members
}

resource "google_project_iam_binding" "kubeorbit_operators" {
  count   = length(var.operator_members) > 0 ? 1 : 0
  project = var.project_id
  role    = google_project_iam_custom_role.kubeorbit_operator.name
  members = var.operator_members
}

resource "google_project_iam_binding" "kubeorbit_viewers" {
  count   = length(local.effective_viewer_members) > 0 ? 1 : 0
  project = var.project_id
  role    = google_project_iam_custom_role.kubeorbit_viewer.name
  members = local.effective_viewer_members
}

# --------------------------------------------------------------------------- #
# 3. Identity-Aware Proxy (IAP) Service Agent & Auto-Synchronized Invokers
# --------------------------------------------------------------------------- #
resource "google_project_service_identity" "iap_sa" {
  provider   = google-beta
  project    = var.project_id
  service    = "iap.googleapis.com"
  depends_on = [google_project_service.apis]
}

# Allow Google IAP Service Agent to invoke the Cloud Run service
resource "google_cloud_run_v2_service_iam_member" "iap_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_service.kubeorbit.location
  name     = google_cloud_run_v2_service.kubeorbit.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-iap.iam.gserviceaccount.com"

  depends_on = [google_project_service_identity.iap_sa]
}

# Automatically grant Cloud Run Invoker & IAP Web App User to all Admin, Operator, and Viewer principals
resource "google_cloud_run_v2_service_iam_member" "authorized_users_invoker" {
  for_each = toset(local.all_authorized_iap_members)
  project  = var.project_id
  location = google_cloud_run_v2_service.kubeorbit.location
  name     = google_cloud_run_v2_service.kubeorbit.name
  role     = "roles/run.invoker"
  member   = each.value
}

resource "google_project_iam_member" "authorized_users_iap_accessor" {
  for_each = toset(local.all_authorized_iap_members)
  project  = var.project_id
  role     = "roles/iap.httpsResourceAccessor"
  member   = each.value
}

# Resource-level IAP Web Cloud Run Service IAM binding (required by Direct Cloud Run IAP)
resource "google_iap_web_cloud_run_service_iam_member" "authorized_users_cloudrun_iap" {
  for_each               = toset(local.all_authorized_iap_members)
  project                = var.project_id
  location               = google_cloud_run_v2_service.kubeorbit.location
  cloud_run_service_name = google_cloud_run_v2_service.kubeorbit.name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = each.value
}

# Custom OAuth 2.0 Client Settings for Cloud Run IAP
# Required when authenticating external Google accounts (@zohomail.com, @gmail.com)
# because Google's default managed IAP OAuth client only permits users inside a Workspace DIRECTORY_CUSTOMER_ID.
resource "google_iap_settings" "cloudrun_oauth" {
  count = var.iap_oauth_client_id != "" && var.iap_oauth_client_secret != "" ? 1 : 0
  name  = "projects/${data.google_project.current.number}/iap_web/cloud_run-${var.region}/services/${google_cloud_run_v2_service.kubeorbit.name}"

  access_settings {
    oauth_settings {
      client_id     = var.iap_oauth_client_id
      client_secret = var.iap_oauth_client_secret
    }
  }
}
