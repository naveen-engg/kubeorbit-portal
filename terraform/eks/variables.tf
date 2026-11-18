variable "aws_region" {
  description = "AWS Region where the EKS cluster will be provisioned (e.g. us-east-1)"
  type        = string
  default     = "us-east-1"
}

variable "cluster_name" {
  description = "Name of the Amazon EKS cluster"
  type        = string
  default     = "kubeorbit-eks"
}

variable "kubernetes_version" {
  description = "Kubernetes control-plane version for EKS"
  type        = string
  default     = "1.30"
}

variable "environment" {
  description = "KubeOrbit environment tier (sandbox | nonprod | prod)"
  type        = string
  default     = "sandbox"
}

variable "cloudrun_sa_unique_id" {
  description = "Unique Numeric OAuth2 Subject ID ('sub') of the GCP Cloud Run Service Account (from terraform/cloud-run output runner_sa_unique_id). Used in the AWS IAM Role trust policy for accounts.google.com."
  type        = string
}

variable "cloudrun_nat_static_ip" {
  description = "Optional static outbound IP from Cloud Run's Cloud NAT. If left empty, public endpoint access allows 0.0.0.0/0; if set, you can restrict public_access_cidrs."
  type        = string
  default     = ""
}

variable "node_instance_types" {
  description = "EC2 instance types for the EKS managed node group"
  type        = list(string)
  default     = ["t3.medium"]
}

variable "desired_size" {
  description = "Desired number of worker nodes"
  type        = number
  default     = 2
}

variable "min_size" {
  description = "Minimum number of worker nodes"
  type        = number
  default     = 1
}

variable "max_size" {
  description = "Maximum number of worker nodes"
  type        = number
  default     = 3
}

variable "enable_operator_permissions" {
  description = "When true, grants Kubernetes RBAC permissions for KubeOrbit Operator features (Secret Reveal & Workload Rollback) in addition to AmazonEKSViewPolicy."
  type        = bool
  default     = true
}
