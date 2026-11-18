# =========================================================================== #
# Azure AKS Zero-Key Workload Identity Federation & RBAC for KubeOrbit
# =========================================================================== #
# Architecture:
#   1. KubeOrbit on GCP Cloud Run requests an OIDC ID Token from the local
#      GCP Metadata Server with `audience=api://AzureADTokenExchange`.
#      The JWT contains:
#        - iss: "https://accounts.google.com"
#        - aud: "api://AzureADTokenExchange"
#        - sub: "<var.cloudrun_sa_unique_id>" (the numeric OAuth2 Client ID of kubeorbit-runner SA)
#   2. KubeOrbit calls:
#        `az login --service-principal -u <client_id> --tenant <tenant_id> --federated-token <oidc_token>`
#   3. Azure Entra ID verifies the Google signature against `azurerm_federated_identity_credential`
#      and issues an Azure AD access token for `azurerm_user_assigned_identity.kubeorbit`.
#   4. KubeOrbit calls `az aks get-credentials` and `kubelogin convert-kubeconfig -l azurecli`
#      so `kubectl` / Python K8s SDK authenticate seamlessly using Azure RBAC for Kubernetes.
# =========================================================================== #

# 1. Dedicated User-Assigned Managed Identity for KubeOrbit
resource "azurerm_user_assigned_identity" "kubeorbit" {
  name                = "id-kubeorbit-${var.cluster_name}"
  location            = azurerm_resource_group.aks.location
  resource_group_name = azurerm_resource_group.aks.name

  tags = {
    ManagedBy = "terraform"
    Portal    = "kubeorbit"
  }
}

# 2. Federated Identity Credential trusting GCP Cloud Run (`https://accounts.google.com`)
resource "azurerm_federated_identity_credential" "gcp_cloudrun" {
  name                = "gcp-cloudrun-kubeorbit-federation"
  resource_group_name = azurerm_resource_group.aks.name
  parent_id           = azurerm_user_assigned_identity.kubeorbit.id
  issuer              = "https://accounts.google.com"
  subject             = var.cloudrun_sa_unique_id
  audience            = ["api://AzureADTokenExchange"]
}

# 3. Azure Control-Plane Role Assignments on the AKS Cluster
#    3a. "Azure Kubernetes Service Cluster User Role" -> allows `az aks get-credentials` (listClusterUserCredential)
resource "azurerm_role_assignment" "aks_cluster_user" {
  scope                = azurerm_kubernetes_cluster.aks.id
  role_definition_name = "Azure Kubernetes Service Cluster User Role"
  principal_id         = azurerm_user_assigned_identity.kubeorbit.principal_id
}

#    3b. "Reader" -> allows `az aks show` and `az aks nodepool list` for KubeOrbit Control Plane & Cost views
resource "azurerm_role_assignment" "aks_control_plane_reader" {
  scope                = azurerm_kubernetes_cluster.aks.id
  role_definition_name = "Reader"
  principal_id         = azurerm_user_assigned_identity.kubeorbit.principal_id
}

# 4. Kubernetes Data-Plane Role Assignments (via Azure RBAC for Kubernetes)
#    4a. "Azure Kubernetes Service RBAC Reader" -> cluster-wide read access to nodes, pods, workloads, events, logs
resource "azurerm_role_assignment" "aks_k8s_rbac_reader" {
  scope                = azurerm_kubernetes_cluster.aks.id
  role_definition_name = "Azure Kubernetes Service RBAC Reader"
  principal_id         = azurerm_user_assigned_identity.kubeorbit.principal_id
}

#    4b. Optional "Azure Kubernetes Service RBAC Writer" -> enables KubeOrbit Operator Secret Reveal & Workload Rollback
resource "azurerm_role_assignment" "aks_k8s_rbac_writer" {
  count                = var.enable_operator_permissions ? 1 : 0
  scope                = azurerm_kubernetes_cluster.aks.id
  role_definition_name = "Azure Kubernetes Service RBAC Writer"
  principal_id         = azurerm_user_assigned_identity.kubeorbit.principal_id
}
