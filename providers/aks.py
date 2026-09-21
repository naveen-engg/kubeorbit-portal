"""
AKS (Azure Kubernetes Service) provider adapter.

Auth model:
  - Local dev: relies on `az login` / `az account set` having already been
    run interactively.
  - Per-environment/team service principal auth: if `service_principal_env_prefix`
    is set in config, `az login --service-principal` is performed using
    `{prefix}AZURE_CLIENT_ID` / `{prefix}AZURE_CLIENT_SECRET` /
    `{prefix}AZURE_TENANT_ID` env vars (never hardcoded), letting different
    teams/environments use different service principals without code changes.
    Login state is cached briefly to avoid re-authenticating on every request.
  - kubeconfig context comes from `az aks get-credentials`, written directly
    to this cluster's isolated kubeconfig via `--file`. Azure AD/Entra
    RBAC-enabled clusters transparently prompt through `kubelogin` if it is
    installed (see README) -- no extra code needed here.
  - Pod-level auth in-cluster is Azure Workload Identity, detected from the
    cluster's security profile + OIDC issuer being enabled.

Expected config.* keys (see config/clusters.example.yaml):
  resource_group (required). cluster_name (optional, defaults to `name`).
  subscription_id (optional). service_principal_env_prefix (optional).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from kube_client import CLIError, TTLCache, read_current_context, run_cli
from providers.base import (
    _AVG_HOURS_PER_MONTH,
    BaseKubeProvider,
    ClusterAuthResult,
    ClusterMeta,
    CostEstimate,
    NodePoolCost,
    NodePoolInfo,
    ProviderAuthError,
    ProviderConfigError,
    ProviderIdentity,
    now,
    register_provider,
)
from providers.pricing import fetch_azure_vm_hourly_price

logger = logging.getLogger("kubeorbit.providers.aks")

_login_cache = TTLCache()
_SP_LOGIN_TTL_SECONDS = 1800


@register_provider("aks")
class AKSProvider(BaseKubeProvider):
    def _resource_group(self) -> str:
        return self.require("resource_group")

    def _cluster_name(self) -> str:
        return self.cluster.config.get("cluster_name", self.cluster.name)

    def _subscription(self) -> Optional[str]:
        return self.cluster.config.get("subscription_id")

    def _sp_prefix(self) -> Optional[str]:
        return self.cluster.config.get("service_principal_env_prefix")

    async def _ensure_login(self) -> None:
        prefix = self._sp_prefix()
        if not prefix:
            return  # assume `az login` was already run interactively for local dev

        async def factory():
            import os

            client_id = os.environ.get(f"{prefix}AZURE_CLIENT_ID")
            client_secret = os.environ.get(f"{prefix}AZURE_CLIENT_SECRET")
            tenant_id = os.environ.get(f"{prefix}AZURE_TENANT_ID")
            missing = [
                name for name, val in (
                    (f"{prefix}AZURE_CLIENT_ID", client_id),
                    (f"{prefix}AZURE_CLIENT_SECRET", client_secret),
                    (f"{prefix}AZURE_TENANT_ID", tenant_id),
                )
                if not val
            ]
            if missing:
                raise ProviderConfigError(
                    f"cluster '{self.cluster.id}' (aks) needs env vars {missing} for its service principal"
                )
            try:
                await run_cli(
                    [
                        "az", "login", "--service-principal",
                        "-u", client_id, "-p", client_secret, "--tenant", tenant_id,
                    ]
                )
            except CLIError as exc:
                raise ProviderAuthError(
                    f"az login --service-principal failed for cluster '{self.cluster.id}': {exc.stderr or exc}"
                ) from exc
            return True

        await _login_cache.get_or_set(f"sp-login:{prefix}", _SP_LOGIN_TTL_SECONDS, factory)

    async def authenticate(self) -> ClusterAuthResult:
        await self._ensure_login()
        rg = self._resource_group()
        cluster_name = self._cluster_name()
        subscription = self._subscription()

        cmd = [
            "az", "aks", "get-credentials",
            "--resource-group", rg, "--name", cluster_name,
            "--file", str(self.kubeconfig_path), "--overwrite-existing",
        ]
        if subscription:
            cmd += ["--subscription", subscription]

        try:
            await run_cli(cmd)
        except CLIError as exc:
            raise ProviderAuthError(
                f"az aks get-credentials failed for '{cluster_name}': {exc.stderr or exc}"
            ) from exc

        context_name = read_current_context(self.kubeconfig_path)
        identity = await self._current_identity()
        return ClusterAuthResult(
            context_name=context_name,
            kubeconfig_path=str(self.kubeconfig_path),
            authenticated_at=now(),
            identity=identity,
        )

    async def _current_identity(self) -> ProviderIdentity:
        try:
            _, stdout, _ = await run_cli(["az", "account", "show", "--output", "json"], check=False)
            account = json.loads(stdout) if stdout.strip() else {}
        except CLIError:
            account = {}
        return ProviderIdentity(
            provider="aks",
            account=(account.get("user") or {}).get("name"),
            project_or_subscription=account.get("id") or self._subscription(),
            impersonated_as=self._sp_prefix(),
        )

    async def get_cluster_metadata(self) -> ClusterMeta:
        rg = self._resource_group()
        cluster_name = self._cluster_name()
        subscription = self._subscription()

        cmd = ["az", "aks", "show", "--resource-group", rg, "--name", cluster_name, "--output", "json"]
        if subscription:
            cmd += ["--subscription", subscription]

        rc, stdout, stderr = await run_cli(cmd, check=False)
        if rc != 0:
            logger.warning("az aks show failed for %s: %s", cluster_name, stderr)
            return ClusterMeta(
                provider="aks", cluster_id=self.cluster.id, name=cluster_name,
                kubernetes_version=None, location=None, status=None,
                degraded=True, error=stderr.strip()[:400],
            )

        data = json.loads(stdout)
        node_pools = [
            NodePoolInfo(
                name=pool.get("name"),
                node_count=pool.get("count", 0),
                machine_type=pool.get("vmSize"),
                zones=pool.get("availabilityZones", []) or [],
                autoscaling=pool.get("enableAutoScaling", False),
                min_nodes=pool.get("minCount"),
                max_nodes=pool.get("maxCount"),
            )
            for pool in data.get("agentPoolProfiles", []) or []
        ]

        security_profile = data.get("securityProfile") or {}
        wi_enabled = bool((security_profile.get("workloadIdentity") or {}).get("enabled"))
        oidc_enabled = bool((data.get("oidcIssuerProfile") or {}).get("enabled"))
        wi = self.get_workload_identity_info()
        wi["enabled"] = wi_enabled and oidc_enabled
        wi["detail"] = (
            "Workload Identity + OIDC issuer enabled" if (wi_enabled and oidc_enabled)
            else "Not fully configured on this cluster."
        )

        network_profile = data.get("networkProfile") or {}
        api_access = data.get("apiServerAccessProfile") or {}
        addon_profiles = data.get("addonProfiles") or {}

        # az aks show only surfaces a VNet name when a custom VNet was used
        # (vnetSubnetID on a node pool) -- clusters on the AKS-managed
        # default VNet have no such ID, which isn't an error, just means
        # there's no customer-owned VNet to name.
        vnet_subnet_id = next(
            (p.get("vnetSubnetID") for p in data.get("agentPoolProfiles", []) or [] if p.get("vnetSubnetID")),
            None,
        )
        vnet_match = re.search(r"/virtualNetworks/([^/]+)", vnet_subnet_id) if vnet_subnet_id else None
        private_cluster = bool(api_access.get("enablePrivateCluster"))
        aad_rbac = bool(data.get("aadProfile"))

        network = {
            "api_server_endpoint": data.get("fqdn") or data.get("privateFqdn"),
            "vpc_id": vnet_match.group(1) if vnet_match else "Managed by AKS (default VNet)",
            "pod_cidr": network_profile.get("podCidr"),
            "service_cidr": network_profile.get("serviceCidr"),
            "cni": network_profile.get("networkPlugin"),
            "network_policy": network_profile.get("networkPolicy") or "None",
            "private_cluster": private_cluster,
        }

        return ClusterMeta(
            provider="aks",
            cluster_id=self.cluster.id,
            name=cluster_name,
            kubernetes_version=data.get("kubernetesVersion") or data.get("currentKubernetesVersion"),
            location=data.get("location"),
            status=data.get("provisioningState"),
            node_pools=node_pools,
            console_url=self.get_console_url(),
            logs_url=self.get_logs_url(),
            workload_identity=wi,
            network=network,
            extra={
                "aad_rbac_enabled": aad_rbac,
                "private_cluster": private_cluster,
                "features": [
                    {"label": "Azure AD RBAC", "enabled": aad_rbac},
                    {"label": "Private Cluster", "enabled": private_cluster},
                    {"label": "Workload Identity", "enabled": wi_enabled},
                    {"label": "OIDC Issuer", "enabled": oidc_enabled},
                    {"label": "Azure Policy", "enabled": bool((addon_profiles.get("azurepolicy") or {}).get("enabled"))},
                    {"label": "Key Vault Secrets Provider", "enabled": bool((addon_profiles.get("azureKeyvaultSecretsProvider") or {}).get("enabled"))},
                ],
            },
        )

    async def get_cost_estimate(self, node_pools: List[NodePoolInfo]) -> CostEstimate:
        meta = await self.get_cluster_metadata()
        if not meta.location:
            return CostEstimate(provider="aks", error="Could not determine cluster region for pricing lookup.")

        pools: List[NodePoolCost] = []
        total = 0.0
        any_priced = False
        for pool in node_pools:
            price, note = None, None
            if pool.machine_type:
                try:
                    price = await fetch_azure_vm_hourly_price(meta.location, pool.machine_type)
                except Exception as exc:
                    note = f"Pricing lookup failed: {exc}"
            if price is None and note is None:
                note = f"No public retail price found for '{pool.machine_type}' in '{meta.location}'."

            monthly = price * pool.node_count * _AVG_HOURS_PER_MONTH if price is not None else None
            if price is not None:
                any_priced = True
                total += monthly
            pools.append(NodePoolCost(
                name=pool.name, machine_type=pool.machine_type, node_count=pool.node_count,
                hourly_price_usd=price, monthly_estimate_usd=monthly, priced=price is not None, note=note,
            ))

        return CostEstimate(
            provider="aks", pools=pools,
            monthly_total_usd=round(total, 2) if any_priced else None,
            error=None if any_priced else "No node pools could be priced (see per-pool notes).",
        )

    def get_console_url(self) -> str:
        subscription = self._subscription() or "-"
        rg = self._resource_group()
        cluster_name = self._cluster_name()
        return (
            f"https://portal.azure.com/#resource/subscriptions/{subscription}/resourceGroups/"
            f"{rg}/providers/Microsoft.ContainerService/managedClusters/{cluster_name}/overview"
        )

    def get_logs_url(self) -> Optional[str]:
        # Unlike GKE/EKS, an AKS cluster's Log Analytics workspace is a
        # separate Azure resource with no derivable naming convention -- a
        # cluster may have none, or one arbitrary workspace. Requires
        # config.log_analytics_workspace_id (full ARM resource ID) to be set
        # at registration time; no default to fall back to.
        workspace_id = self.cluster.config.get("log_analytics_workspace_id")
        if not workspace_id:
            return None
        return f"https://portal.azure.com/#@/resource{workspace_id}/logs"

    def get_workload_identity_info(self) -> Dict[str, Any]:
        return {
            "mechanism": "Azure AD Workload Identity",
            "docs_url": "https://learn.microsoft.com/azure/aks/workload-identity-overview",
            "description": (
                "Federates a Kubernetes ServiceAccount with a Microsoft Entra ID app "
                "via an OIDC issuer, so pods obtain Azure AD tokens without node identity."
            ),
        }
