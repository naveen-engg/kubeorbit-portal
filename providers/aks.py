"""
AKS (Azure Kubernetes Service) provider adapter.

Auth model:
  1. Cloud Run / GCP Workload Identity Federation (Zero Keys):
     - Queries the local GCP Metadata Server for a signed Google OIDC ID token
       (`audience=api://AzureADTokenExchange`).
     - Authenticates to Microsoft Entra ID via Federated Credentials:
       `az login --service-principal -u <client_id> --tenant <tenant_id> --federated-token <oidc_token>`
     - Runs `kubelogin convert-kubeconfig -l azurecli` so Entra ID RBAC clusters
       mint non-interactive tokens automatically.
  2. Service Principal with Secret (Fallback):
     - Uses `{prefix}AZURE_CLIENT_ID`, `{prefix}AZURE_CLIENT_SECRET`, and
       `{prefix}AZURE_TENANT_ID` from environment / Secret Manager.
  3. Local dev:
     - Relies on interactive `az login` session.

Expected config.* keys:
  resource_group (required). cluster_name (optional, defaults to `name`).
  subscription_id (optional). tenant_id (optional, for Federated/SP auth).
  client_id (optional, Managed Identity / App Registration Client ID for Federated auth).
  service_principal_env_prefix (optional).
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
import urllib.request
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


def _fetch_gcp_oidc_token(audience: str = "api://AzureADTokenExchange") -> Optional[str]:
    """Fetch a signed Google OIDC ID token when running on GCP Cloud Run / GCE / GKE,
    or from KUBEORBIT_GCP_OIDC_TOKEN_FILE if mounted. Returns None when not on GCP."""
    token_file = os.environ.get("KUBEORBIT_GCP_OIDC_TOKEN_FILE")
    if token_file and os.path.exists(token_file):
        try:
            with open(token_file, "r", encoding="utf-8") as fh:
                return fh.read().strip() or None
        except OSError:
            pass

    if not (os.environ.get("K_SERVICE") or os.environ.get("KUBEORBIT_USE_GCP_METADATA_OIDC", "").lower() == "true"):
        return None

    encoded_aud = urllib.parse.quote(audience, safe="")
    url = (
        f"http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/"
        f"default/identity?audience={encoded_aud}&format=full"
    )
    req = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
    try:
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            if resp.status == 200:
                return resp.read().decode("utf-8").strip() or None
    except Exception as exc:
        logger.debug("GCP Metadata OIDC token fetch for Azure skipped/failed: %s", exc)
    return None


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

    def _client_id(self) -> Optional[str]:
        prefix = self._sp_prefix() or ""
        return self.cluster.config.get("client_id") or (os.environ.get(f"{prefix}AZURE_CLIENT_ID") if prefix else None)

    def _tenant_id(self) -> Optional[str]:
        prefix = self._sp_prefix() or ""
        return self.cluster.config.get("tenant_id") or (os.environ.get(f"{prefix}AZURE_TENANT_ID") if prefix else None)

    async def _ensure_login(self) -> bool:
        """Returns True if non-interactive Federated or Service Principal login was performed."""
        prefix = self._sp_prefix()
        client_id = self._client_id()
        tenant_id = self._tenant_id()

        if not prefix and not (client_id and tenant_id):
            return False  # assume `az login` was already run interactively for local dev

        cache_key = f"sp-login:{self.cluster.id}:{prefix or client_id}"

        async def factory():
            oidc_token = _fetch_gcp_oidc_token("api://AzureADTokenExchange")
            if client_id and tenant_id and oidc_token:
                logger.info(
                    "Exchanging GCP Cloud Run OIDC token for Azure Entra ID Client '%s' (cluster=%s)",
                    client_id, self.cluster.id,
                )
                try:
                    await run_cli(
                        [
                            "az", "login", "--service-principal",
                            "-u", client_id, "--tenant", tenant_id,
                            "--federated-token", oidc_token,
                        ]
                    )
                    return True
                except CLIError as exc:
                    raise ProviderAuthError(
                        f"az login --federated-token failed for cluster '{self.cluster.id}': {exc.stderr or exc}"
                    ) from exc

            # Fallback to Secret Manager / Environment Service Principal secret
            eff_prefix = prefix or ""
            c_id = client_id or os.environ.get(f"{eff_prefix}AZURE_CLIENT_ID")
            c_secret = self.cluster.config.get("client_secret") or os.environ.get(f"{eff_prefix}AZURE_CLIENT_SECRET")
            t_id = tenant_id or os.environ.get(f"{eff_prefix}AZURE_TENANT_ID")
            missing = [
                name for name, val in (
                    (f"{eff_prefix}AZURE_CLIENT_ID", c_id),
                    (f"{eff_prefix}AZURE_CLIENT_SECRET", c_secret),
                    (f"{eff_prefix}AZURE_TENANT_ID", t_id),
                )
                if not val
            ]
            if missing:
                raise ProviderConfigError(
                    f"cluster '{self.cluster.id}' (aks) needs either Cloud Run OIDC Federation "
                    f"(client_id + tenant_id) or env vars {missing} for its service principal"
                )
            try:
                await run_cli(
                    [
                        "az", "login", "--service-principal",
                        "-u", c_id, "-p", c_secret, "--tenant", t_id,
                    ]
                )
            except CLIError as exc:
                raise ProviderAuthError(
                    f"az login --service-principal failed for cluster '{self.cluster.id}': {exc.stderr or exc}"
                ) from exc
            return True

        await _login_cache.get_or_set(cache_key, _SP_LOGIN_TTL_SECONDS, factory)
        return True

    async def authenticate(self) -> ClusterAuthResult:
        used_sp_or_federation = await self._ensure_login()
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

        # Convert kubeconfig to use non-interactive azurecli token via kubelogin when running with SP/Federated auth
        if used_sp_or_federation:
            try:
                await run_cli(
                    ["kubelogin", "convert-kubeconfig", "-l", "azurecli", "--kubeconfig", str(self.kubeconfig_path)],
                    check=False,
                )
            except Exception as exc:
                logger.debug("kubelogin convert-kubeconfig skipped: %s", exc)

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
            account=(account.get("user") or {}).get("name") or self._client_id(),
            project_or_subscription=account.get("id") or self._subscription(),
            impersonated_as=self._client_id() or self._sp_prefix(),
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
            price = None
            note = None
            if pool.machine_type:
                try:
                    price = await fetch_azure_vm_hourly_price(meta.location, pool.machine_type)
                except Exception as exc:
                    note = f"Pricing lookup failed: {exc}"
            if price is None and note is None:
                note = f"No public pay-as-you-go Linux price found for '{pool.machine_type}' in {meta.location}."

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
            error=None if any_priced else "No agent pools could be priced.",
        )

    def get_console_url(self) -> str:
        sub = self._subscription() or "<subscription>"
        return (
            f"https://portal.azure.com/#@/resource/subscriptions/{sub}/resourceGroups/"
            f"{self._resource_group()}/providers/Microsoft.ContainerService/managedClusters/"
            f"{self._cluster_name()}/overview"
        )

    def get_logs_url(self) -> Optional[str]:
        workspace_id = self.cluster.config.get("log_analytics_workspace_id")
        if not workspace_id:
            return None
        return f"https://portal.azure.com/#blade/Microsoft_OperationsManagementSuite_Workspace/LogsBlade/resourceId/{workspace_id.lstrip('/')}"

    def get_workload_identity_info(self) -> Dict[str, Any]:
        return {
            "mechanism": "Azure Workload Identity (Entra ID)",
            "docs_url": "https://learn.microsoft.com/en-us/azure/aks/workload-identity-overview",
            "description": (
                "Federates a Kubernetes ServiceAccount with an Azure Managed Identity or "
                "Entra Application via the cluster's OIDC issuer."
            ),
        }
