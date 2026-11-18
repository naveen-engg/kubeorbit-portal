variable "subscription_id" {
  description = "Azure Subscription ID where the AKS cluster and Managed Identity will be created"
  type        = string
  default     = ""
}

variable "location" {
  description = "Azure Region for the Resource Group and AKS Cluster (e.g. eastus, centralus)"
  type        = string
  default     = "eastus"
}

variable "resource_group_name" {
  description = "Name of the Azure Resource Group"
  type        = string
  default     = "rg-kubeorbit-aks"
}

variable "cluster_name" {
  description = "Name of the Azure Kubernetes Service (AKS) cluster"
  type        = string
  default     = "kubeorbit-aks"
}

variable "kubernetes_version" {
  description = "Kubernetes control-plane version for AKS (leave empty for Azure default stable)"
  type        = string
  default     = null
}

variable "environment" {
  description = "KubeOrbit environment tier (sandbox | nonprod | prod)"
  type        = string
  default     = "sandbox"
}

variable "cloudrun_sa_unique_id" {
  description = "Unique Numeric OAuth2 Subject ID ('sub') of the GCP Cloud Run Service Account (from terraform/cloud-run output runner_sa_unique_id). Used in the Azure Entra ID Federated Identity Credential."
  type        = string
}

variable "cloudrun_nat_static_ip" {
  description = "Optional static outbound IP from Cloud Run's Cloud NAT to allowlist in AKS API Server Authorized IP Ranges"
  type        = string
  default     = ""
}

variable "vm_size" {
  description = "Azure VM size for the default AKS node pool"
  type        = string
  default     = "Standard_B2s_v2"
}

variable "node_count" {
  description = "Number of worker nodes in the default node pool"
  type        = number
  default     = 1
}

variable "enable_operator_permissions" {
  description = "When true, assigns 'Azure Kubernetes Service RBAC Writer' in addition to 'Azure Kubernetes Service RBAC Reader' so KubeOrbit Operator actions (Secret Reveal & Workload Rollback) are authorized."
  type        = bool
  default     = true
}
