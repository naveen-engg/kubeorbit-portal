# --------------------------------------------------------------------------- #
# 1. Azure Resource Group
# --------------------------------------------------------------------------- #
resource "azurerm_resource_group" "aks" {
  name     = var.resource_group_name
  location = var.location

  tags = {
    Environment = var.environment
    ManagedBy   = "terraform"
    Portal      = "kubeorbit"
  }
}

# --------------------------------------------------------------------------- #
# 2. Azure Kubernetes Service (AKS) Cluster
#    Configured with Azure Entra ID (AAD) + Azure RBAC for Kubernetes
# --------------------------------------------------------------------------- #
resource "azurerm_kubernetes_cluster" "aks" {
  name                      = var.cluster_name
  location                  = azurerm_resource_group.aks.location
  resource_group_name       = azurerm_resource_group.aks.name
  dns_prefix                = "${var.cluster_name}-dns"
  kubernetes_version        = var.kubernetes_version
  oidc_issuer_enabled       = true
  workload_identity_enabled = true
  local_account_disabled    = false

  default_node_pool {
    name       = "systempool"
    node_count = var.node_count
    vm_size    = var.vm_size
  }

  identity {
    type = "SystemAssigned"
  }

  # Enable Entra ID authentication & Azure RBAC for Kubernetes so the
  # Federated Managed Identity can authenticate non-interactively via kubelogin
  azure_active_directory_role_based_access_control {
    managed            = true
    azure_rbac_enabled = true
    tenant_id          = data.azurerm_client_config.current.tenant_id
  }

  dynamic "api_server_access_profile" {
    for_each = var.cloudrun_nat_static_ip != "" ? [1] : []
    content {
      authorized_ip_ranges = ["${var.cloudrun_nat_static_ip}/32"]
    }
  }

  network_profile {
    network_plugin    = "azure"
    load_balancer_sku = "standard"
  }

  tags = {
    Environment = var.environment
    ManagedBy   = "terraform"
    Portal      = "kubeorbit"
  }
}
