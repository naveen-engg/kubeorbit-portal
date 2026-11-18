output "aks_resource_group" {
  description = "Azure Resource Group (enter in KubeOrbit UI -> Register Cluster -> AKS -> Resource Group)"
  value       = azurerm_resource_group.aks.name
}

output "aks_cluster_name" {
  description = "AKS Cluster Name (enter in KubeOrbit UI -> Register Cluster -> AKS -> Cluster Name)"
  value       = azurerm_kubernetes_cluster.aks.name
}

output "aks_subscription_id" {
  description = "Azure Subscription ID (enter in KubeOrbit UI -> Register Cluster -> AKS -> Subscription ID)"
  value       = data.azurerm_client_config.current.subscription_id
}

output "aks_tenant_id" {
  description = "Azure Entra Tenant ID (enter in KubeOrbit UI -> Register Cluster -> AKS -> Entra Tenant ID)"
  value       = data.azurerm_client_config.current.tenant_id
}

output "aks_federated_client_id" {
  description = "Federated Managed Identity Client ID (enter in KubeOrbit UI -> Register Cluster -> AKS -> Federated Managed Identity / App Client ID)"
  value       = azurerm_user_assigned_identity.kubeorbit.client_id
}

output "kubeorbit_ui_registration_summary" {
  description = "Copy-paste values for registering this AKS cluster in the KubeOrbit UI"
  value = {
    provider        = "aks"
    environment     = var.environment
    resource_group  = azurerm_resource_group.aks.name
    cluster_name    = azurerm_kubernetes_cluster.aks.name
    subscription_id = data.azurerm_client_config.current.subscription_id
    tenant_id       = data.azurerm_client_config.current.tenant_id
    client_id       = azurerm_user_assigned_identity.kubeorbit.client_id
  }
}
