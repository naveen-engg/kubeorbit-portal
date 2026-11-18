# =========================================================================== #
# AWS EKS Zero-Key OIDC Federation & Authorization for KubeOrbit
# =========================================================================== #
# Architecture:
#   1. KubeOrbit on GCP Cloud Run requests an OIDC ID Token from the local
#      GCP Metadata Server with `audience=sts.amazonaws.com`.
#      The JWT contains:
#        - iss: "https://accounts.google.com"
#        - aud: "sts.amazonaws.com"
#        - sub: "<var.cloudrun_sa_unique_id>" (the numeric OAuth2 Client ID of kubeorbit-runner SA)
#   2. KubeOrbit calls `aws sts assume-role-with-web-identity` for `aws_iam_role.kubeorbit_eks_role`.
#   3. AWS STS verifies the Google signature, `aud`, and `sub` and issues temporary
#      AWS credentials (ASIA...).
#   4. EKS Access Entry (`aws_eks_access_entry.kubeorbit`) maps the IAM Role ARN to
#      `AmazonEKSViewPolicy` + the `kubeorbit:operators` Kubernetes RBAC group.
# =========================================================================== #

# 1. Zero-Key Cross-Cloud IAM Role trusting Google OIDC (`accounts.google.com`)
resource "aws_iam_role" "kubeorbit_eks_role" {
  name        = "KubeOrbitEKSRole-${var.cluster_name}"
  description = "Assumed by KubeOrbit on GCP Cloud Run via Zero-Key OIDC Web Identity Federation (accounts.google.com)"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowGCPCloudRunServiceAccountOIDC"
        Effect = "Allow"
        Principal = {
          Federated = "accounts.google.com"
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "accounts.google.com:aud" = "sts.amazonaws.com"
            "accounts.google.com:sub" = var.cloudrun_sa_unique_id
          }
        }
      }
    ]
  })

  tags = {
    ManagedBy = "terraform"
    Portal    = "kubeorbit"
  }
}

# 2. IAM Policy allowing KubeOrbit to inspect EKS Control Plane & Nodegroups
resource "aws_iam_role_policy" "kubeorbit_eks_describe" {
  name = "KubeOrbitEKSControlPlaneRead"
  role = aws_iam_role.kubeorbit_eks_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EKSControlPlaneMetadataAndKubeconfig"
        Effect = "Allow"
        Action = [
          "eks:DescribeCluster",
          "eks:ListNodegroups",
          "eks:DescribeNodegroup",
          "eks:ListFargateProfiles",
          "eks:DescribeFargateProfile",
          "eks:ListAddons",
          "eks:DescribeAddon",
          "eks:ListClusters",
        ]
        Resource = "*"
      }
    ]
  })
}

# 3. EKS Access Entry mapping `aws_iam_role.kubeorbit_eks_role` into Kubernetes
resource "aws_eks_access_entry" "kubeorbit" {
  cluster_name      = aws_eks_cluster.this.name
  principal_arn     = aws_iam_role.kubeorbit_eks_role.arn
  kubernetes_groups = var.enable_operator_permissions ? ["kubeorbit:operators"] : ["kubeorbit:viewers"]
  type              = "STANDARD"
}

# Associate AWS-managed cluster-wide read-only view policy
resource "aws_eks_access_policy_association" "kubeorbit_view" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = aws_iam_role.kubeorbit_eks_role.arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSViewPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_access_entry.kubeorbit]
}

# 4. Kubernetes RBAC for KubeOrbit Operator Features (Secret Reveal & Workload Rollback)
resource "kubernetes_cluster_role" "kubeorbit_operator_extras" {
  count = var.enable_operator_permissions ? 1 : 0

  metadata {
    name = "kubeorbit-operator-extras"
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "kubeorbit"
    }
  }

  # Allow reading nodes, metrics, Gateway API, CRDs, and secrets (for audited Reveal Secret)
  rule {
    api_groups = [
      "",
      "metrics.k8s.io",
      "gateway.networking.k8s.io",
      "apiextensions.k8s.io",
    ]
    resources = ["nodes", "pods", "secrets", "gateways", "httproutes", "customresourcedefinitions"]
    verbs     = ["get", "list", "watch"]
  }

  # Allow workload rollback (`kubectl rollout undo`)
  rule {
    api_groups = ["apps"]
    resources  = ["deployments", "statefulsets", "daemonsets", "replicasets"]
    verbs      = ["get", "list", "watch", "patch", "update"]
  }

  depends_on = [aws_eks_node_group.default]
}

resource "kubernetes_cluster_role_binding" "kubeorbit_operator_extras_binding" {
  count = var.enable_operator_permissions ? 1 : 0

  metadata {
    name = "kubeorbit-operator-extras-binding"
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role.kubeorbit_operator_extras[0].metadata[0].name
  }

  subject {
    kind      = "Group"
    name      = "kubeorbit:operators"
    api_group = "rbac.authorization.k8s.io"
  }
}
