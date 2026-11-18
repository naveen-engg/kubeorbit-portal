output "eks_region" {
  description = "AWS Region (enter in KubeOrbit UI -> Register Cluster -> EKS -> AWS Region)"
  value       = var.aws_region
}

output "eks_cluster_name" {
  description = "EKS Cluster Name (enter in KubeOrbit UI -> Register Cluster -> EKS -> Cluster Name)"
  value       = aws_eks_cluster.this.name
}

output "eks_role_arn" {
  description = "IAM Role ARN for Zero-Key OIDC Federation (enter in KubeOrbit UI -> Register Cluster -> EKS -> IAM Role ARN)"
  value       = aws_iam_role.kubeorbit_eks_role.arn
}

output "kubeorbit_ui_registration_summary" {
  description = "Copy-paste values for registering this EKS cluster in the KubeOrbit UI"
  value = {
    provider     = "eks"
    environment  = var.environment
    region       = var.aws_region
    cluster_name = aws_eks_cluster.this.name
    role_arn     = aws_iam_role.kubeorbit_eks_role.arn
  }
}
