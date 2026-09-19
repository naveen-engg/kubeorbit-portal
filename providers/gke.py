"""
GKE (Google Kubernetes Engine) provider adapter.

Auth model:
  - Local dev: relies on `gcloud auth login` / `gcloud auth application-default
    login` having already been run by the developer. Optionally impersonates
    a service account (`config.impersonate_service_account` or the
    GOOGLE_IMPERSONATE_SERVICE_ACCOUNT env var) for least-privilege access.
  - kubeconfig context comes from `gcloud container clusters get-credentials`,
    written into this cluster's isolated kubeconfig via the KUBECONFIG env var
    (gcloud has no --kubeconfig flag for this subcommand).
  - Pod-level auth in-cluster is GKE Workload Identity, detected (not
    configured) from the cluster's `workloadIdentityConfig.workloadPool`.

Expected config.* keys (see config/clusters.example.yaml):
  project (required), and either region or zone (required, mutually exclusive).
  cluster_name (optional, defaults to the cluster's `name`),
  impersonate_service_account (optional).
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List

from kube_client import CLIError, kubeconfig_path_for_cluster, read_current_context, run_cli
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
from providers.pricing import fetch_gcp_compute_hourly_price

logger = logging.getLogger("kubeorbit.providers.gke")


@register_provider("gke")
class GKEProvider(BaseKubeProvider):
    def _location(self) -> tuple[str, str]:
        zone = self.cluster.config.get("zone")
        region = self.cluster.config.get("region")
        if not zone and not region:
            raise ProviderConfigError(
                f"cluster '{self.cluster.id}' (gke) needs either config.zone or config.region"
            )
        return ("--zone", zone) if zone else ("--region", region)

    def _cluster_name(self) -> str:
        return self.cluster.config.get("cluster_name", self.cluster.name)

    def _project(self) -> str:
        return self.require("project")

    def _impersonation_target(self) -> str | None:
        return self.cluster.config.get("impersonate_service_account") or os.environ.get(
            "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT"
        )

    async def authenticate(self) -> ClusterAuthResult:
        loc_flag, loc_val = self._location()
        project = self._project()
        cluster_name = self._cluster_name()
        impersonate = self._impersonation_target()

        cmd = [
            "gcloud", "container", "clusters", "get-credentials", cluster_name,
            "--project", project, loc_flag, loc_val,
        ]
        if impersonate:
            cmd += ["--impersonate-service-account", impersonate]

        env = {"KUBECONFIG": str(self.kubeconfig_path)}
        try:
            await run_cli(cmd, env=env)
        except CLIError as exc:
            raise ProviderAuthError(
                f"gcloud could not fetch credentials for GKE cluster '{cluster_name}': {exc.stderr or exc}"
            ) from exc

        context_name = read_current_context(self.kubeconfig_path)
        identity = await self._current_identity(impersonate)
        return ClusterAuthResult(
            context_name=context_name,
            kubeconfig_path=str(self.kubeconfig_path),
            authenticated_at=now(),
            identity=identity,
        )

    async def _current_identity(self, impersonate: str | None) -> ProviderIdentity:
        try:
            _, stdout, _ = await run_cli(
                ["gcloud", "config", "get-value", "account", "--format=value"], check=False
            )
            account = stdout.strip() or None
        except CLIError:
            account = None
        return ProviderIdentity(
            provider="gke",
            account=account,
            project_or_subscription=self._project(),
            impersonated_as=impersonate,
        )

    async def get_cluster_metadata(self) -> ClusterMeta:
        loc_flag, loc_val = self._location()
        project = self._project()
        cluster_name = self._cluster_name()

        rc, stdout, stderr = await run_cli(
            [
                "gcloud", "container", "clusters", "describe", cluster_name,
                "--project", project, loc_flag, loc_val, "--format=json",
            ],
            check=False,
        )
        if rc != 0:
            logger.warning("gke describe failed for %s: %s", cluster_name, stderr)
            return ClusterMeta(
                provider="gke", cluster_id=self.cluster.id, name=cluster_name,
                kubernetes_version=None, location=loc_val, status=None,
                degraded=True, error=stderr.strip()[:400],
            )

        data = json.loads(stdout)
        node_pools = [
            NodePoolInfo(
                name=np.get("name"),
                node_count=np.get("initialNodeCount", 0),
                machine_type=(np.get("config") or {}).get("machineType"),
                zones=np.get("locations", []) or [],
                autoscaling=(np.get("autoscaling") or {}).get("enabled", False),
                min_nodes=(np.get("autoscaling") or {}).get("minNodeCount"),
                max_nodes=(np.get("autoscaling") or {}).get("maxNodeCount"),
            )
            for np in data.get("nodePools", []) or []
        ]

        workload_pool = (data.get("workloadIdentityConfig") or {}).get("workloadPool")
        wi = self.get_workload_identity_info()
        wi["enabled"] = bool(workload_pool)
        wi["detail"] = f"Workload Pool: {workload_pool}" if workload_pool else "Not configured for this cluster."

        return ClusterMeta(
            provider="gke",
            cluster_id=self.cluster.id,
            name=cluster_name,
            kubernetes_version=data.get("currentMasterVersion"),
            location=data.get("location", loc_val),
            status=data.get("status"),
            node_pools=node_pools,
            console_url=self.get_console_url(),
            logs_url=self.get_logs_url(),
            workload_identity=wi,
            extra={
                "release_channel": (data.get("releaseChannel") or {}).get("channel"),
                "private_cluster": bool((data.get("privateClusterConfig") or {}).get("enablePrivateNodes")),
            },
        )

    async def get_cost_estimate(self, node_pools: List[NodePoolInfo]) -> CostEstimate:
        try:
            _, token, _ = await run_cli(["gcloud", "auth", "print-access-token"], check=False)
            token = token.strip()
        except CLIError as exc:
            return CostEstimate(provider="gke", error=f"Could not obtain a gcloud access token: {exc}")
        if not token:
            return CostEstimate(provider="gke", error="Could not obtain a gcloud access token for Cloud Billing Catalog API.")

        pools: list[NodePoolCost] = []
        total = 0.0
        any_priced = False
        for pool in node_pools:
            # Billing Catalog SKUs are region-scoped; strip a zone suffix
            # (e.g. "us-central1-a" -> "us-central1") if this pool's zones
            # are zonal rather than regional.
            zone_or_region = (pool.zones or [self._location()[1]])[0]
            region = zone_or_region.rsplit("-", 1)[0] if re.match(r".+-[a-z]$", zone_or_region) else zone_or_region

            price = None
            note = None
            if pool.machine_type:
                try:
                    price = await fetch_gcp_compute_hourly_price(region, pool.machine_type, token)
                except Exception as exc:
                    note = f"Pricing lookup failed: {exc}"
            if price is None and note is None:
                note = (
                    f"Machine type '{pool.machine_type}' isn't priced -- only standard/highmem/highcpu "
                    f"predefined shapes in the e2/n1/n2/n2d families are supported. Shared-core sizes "
                    f"(e2-micro/small/medium) are intentionally excluded: their burstable vCPU allocation "
                    f"doesn't fit the vCPU x rate + RAM x rate formula used here, so guessing a price would "
                    f"likely be wrong."
                )

            monthly = price * pool.node_count * _AVG_HOURS_PER_MONTH if price is not None else None
            if price is not None:
                any_priced = True
                total += monthly
            pools.append(NodePoolCost(
                name=pool.name, machine_type=pool.machine_type, node_count=pool.node_count,
                hourly_price_usd=price, monthly_estimate_usd=monthly, priced=price is not None, note=note,
            ))

        return CostEstimate(
            provider="gke", pools=pools,
            monthly_total_usd=round(total, 2) if any_priced else None,
            error=None if any_priced else "No node pools could be priced (see per-pool notes).",
        )

    def get_console_url(self) -> str:
        loc_flag, loc_val = self._location()
        return (
            f"https://console.cloud.google.com/kubernetes/clusters/details/"
            f"{loc_val}/{self._cluster_name()}/details?project={self._project()}"
        )

    def get_logs_url(self) -> str:
        # Fully derivable from project + cluster name already required to
        # register the cluster -- no extra config field needed, unlike
        # EKS/AKS below.
        import urllib.parse

        query = f'resource.type="k8s_container"\nresource.labels.cluster_name="{self._cluster_name()}"'
        return (
            f"https://console.cloud.google.com/logs/query;query={urllib.parse.quote(query)}"
            f"?project={self._project()}"
        )

    def get_workload_identity_info(self) -> Dict[str, Any]:
        return {
            "mechanism": "GKE Workload Identity",
            "docs_url": "https://cloud.google.com/kubernetes-engine/docs/how-to/workload-identity",
            "description": (
                "Maps a Kubernetes ServiceAccount to a Google service account so pods "
                "authenticate to GCP APIs without node-level credentials."
            ),
        }
