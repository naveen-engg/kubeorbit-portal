variable "project_id" {
  description = "GCP Project ID where KubeOrbit Cloud Run service is deployed (e.g. adk-enterprise-observability)"
  type        = string
}

variable "region" {
  description = "GCP Region for Cloud Run, Artifact Registry, and Cloud NAT"
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Name of the Cloud Run service"
  type        = string
  default     = "kubeorbit"
}

variable "container_image" {
  description = "Full container image URI for KubeOrbit (e.g. us-central1-docker.pkg.dev/<PROJECT>/kubeorbit-repo/kubeorbit:latest)"
  type        = string
}

# --------------------------------------------------------------------------- #
# GCP Custom IAM Roles (kubeorbitAdmin, kubeorbitOperator, kubeorbitViewer)
# --------------------------------------------------------------------------- #
variable "admin_members" {
  description = "List of IAM principals granted projects/<PROJECT>/roles/kubeorbitAdmin + IAP access (e.g. ['user:naveen@gmail.com'])"
  type        = list(string)
  default     = []
}

variable "operator_members" {
  description = "List of IAM principals granted projects/<PROJECT>/roles/kubeorbitOperator + IAP access (e.g. ['user:sre@gmail.com'])"
  type        = list(string)
  default     = []
}

variable "viewer_members" {
  description = "List of IAM principals granted projects/<PROJECT>/roles/kubeorbitViewer + IAP access (e.g. ['user:dev@gmail.com'])"
  type        = list(string)
  default     = []
}

variable "viewer_domain" {
  description = "Optional verified Google Workspace / Cloud Identity domain to grant kubeorbitViewer + IAP access to all domain users (leave empty if using @gmail.com accounts)"
  type        = string
  default     = ""
}

variable "admin_emails" {
  description = "Optional comma-separated fallback list of Admin user emails (KUBEORBIT_ADMIN_EMAILS)"
  type        = string
  default     = ""
}

variable "operator_emails" {
  description = "Optional comma-separated fallback list of Operator user emails (KUBEORBIT_OPERATOR_EMAILS)"
  type        = string
  default     = ""
}

variable "viewer_emails" {
  description = "Optional comma-separated fallback list of Viewer user emails (KUBEORBIT_VIEWER_EMAILS)"
  type        = string
  default     = ""
}

variable "iap_authorized_members" {
  description = "Additional IAM principals allowed to access KubeOrbit through Identity-Aware Proxy (merged automatically with admin_members, operator_members, and viewer_members)"
  type        = list(string)
  default     = []
}

variable "iap_audience" {
  description = "Optional expected JWT audience string for cryptographic X-Goog-IAP-JWT-Assertion verification"
  type        = string
  default     = ""
}

variable "iap_oauth_client_id" {
  description = "Custom OAuth 2.0 Web Client ID (Required by Google Cloud IAP when authenticating @gmail.com / @zohomail.com or external accounts outside a Google Workspace domain)"
  type        = string
  default     = ""
}

variable "iap_oauth_client_secret" {
  description = "Custom OAuth 2.0 Web Client Secret paired with iap_oauth_client_id"
  type        = string
  sensitive   = true
  default     = ""
}

variable "ai_model" {
  description = "Vertex AI Gemini model ID used by the Google ADK agent in KubeOrbit (KUBEORBIT_AI_MODEL)"
  type        = string
  default     = "gemini-3.5-flash"
}

variable "ai_location" {
  description = "Vertex AI location for Gemini models (GOOGLE_CLOUD_LOCATION)"
  type        = string
  default     = "global"
}

variable "initial_clusters_yaml" {
  description = "Initial contents of clusters.yaml stored in Secret Manager"
  type        = string
  default     = <<-EOT
    settings:
      kube_cache_ttl_seconds: 30
      auth_cache_ttl_seconds: 1800
      cluster_meta_cache_ttl_seconds: 120
      excluded_namespace_patterns:
        - kube-*
        - gke-*
        - gmp-*
        - config-management-*
    teams:
      - id: platform
        name: Platform Engineering
        environments:
          - id: sandbox
            clusters:
              - id: kubeorbit-gke
                name: kubeorbit-gke
                provider: gke
                environment: sandbox
                description: GKE Sandbox Cluster (Zero-Key IAM Impersonation)
                config:
                  project: adk-enterprise-observability
                  zone: us-central1-a
                  cluster_name: kubeorbit-gke
                  impersonate_service_account: kubeorbit-gke-reader@adk-enterprise-observability.iam.gserviceaccount.com
  EOT
}

variable "min_instance_count" {
  description = "Minimum warm Cloud Run instances"
  type        = number
  default     = 1
}

variable "max_instance_count" {
  description = "Maximum Cloud Run instances"
  type        = number
  default     = 5
}

variable "custom_role_id_suffix" {
  description = "Optional suffix for custom IAM role IDs (e.g. '_v2') if previous role IDs are still in GCP's deletion hold."
  type        = string
  default     = ""
}
