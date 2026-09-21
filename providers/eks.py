"""
EKS (Elastic Kubernetes Service) provider adapter.

Auth model:
  - Local dev: relies on `aws configure` / SSO login having already been run.
    An optional `aws_profile` config key selects a named profile.
  - kubeconfig context comes from `aws eks update-kubeconfig`, written
    directly to this cluster's isolated kubeconfig via `--kubeconfig`.
  - Per-environment/team role assumption uses two distinct mechanisms that
    both matter and are easy to conflate:
      1. Data-plane (kubectl) auth: `update-kubeconfig --role-arn <arn>`
         bakes the role into the kubeconfig's exec plugin, so every kubectl
         call transparently authenticates as that role. This is the
         AWS-recommended approach and needs no manual STS call.
      2. Control-plane (AWS API) calls made *by this adapter itself* --
         describe-cluster, list-nodegroups -- are not covered by the
         kubeconfig exec plugin, so we explicitly perform `aws sts
         assume-role` and inject the resulting temporary credentials as
         environment variables before those calls.
  - Pod-level auth in-cluster is IRSA, detected via the cluster's OIDC issuer.

Expected config.* keys (see config/clusters.example.yaml):
  region (required). cluster_name (optional, defaults to `name`).
  role_arn (optional, per-environment/team STS role). aws_profile (optional).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

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
    ProviderIdentity,
    now,
    register_provider,
)
from providers.pricing import fetch_aws_ec2_hourly_price

logger = logging.getLogger("kubeorbit.providers.eks")

_sts_cache = TTLCache()
_STS_SESSION_TTL_SECONDS = 3000  # refresh a bit before AWS's default 1h expiry


@register_provider("eks")
class EKSProvider(BaseKubeProvider):
    def _region(self) -> str:
        return self.require("region")

    def _cluster_name(self) -> str:
        return self.cluster.config.get("cluster_name", self.cluster.name)

    def _role_arn(self) -> Optional[str]:
        return self.cluster.config.get("role_arn")

    def _base_env(self) -> Dict[str, str]:
        profile = self.cluster.config.get("aws_profile")
        return {"AWS_PROFILE": profile} if profile else {}

    async def _sts_env(self) -> Dict[str, str]:
        """Temporary credentials for management-API calls this adapter makes
        directly (describe-cluster, list-nodegroups). No-op if no role_arn is
        configured for this cluster."""
        role_arn = self._role_arn()
        if not role_arn:
            return self._base_env()

        async def factory():
            cmd = [
                "aws", "sts", "assume-role", "--role-arn", role_arn,
                "--role-session-name", f"kubeorbit-{self.cluster.id}",
                "--output", "json",
            ]
            try:
                _, stdout, _ = await run_cli(cmd, env=self._base_env())
            except CLIError as exc:
                raise ProviderAuthError(
                    f"could not assume role '{role_arn}' for cluster '{self.cluster.id}': {exc.stderr or exc}"
                ) from exc
            creds = json.loads(stdout)["Credentials"]
            return {
                "AWS_ACCESS_KEY_ID": creds["AccessKeyId"],
                "AWS_SECRET_ACCESS_KEY": creds["SecretAccessKey"],
                "AWS_SESSION_TOKEN": creds["SessionToken"],
            }

        env, _ = await _sts_cache.get_or_set(f"sts:{role_arn}", _STS_SESSION_TTL_SECONDS, factory)
        return env

    async def authenticate(self) -> ClusterAuthResult:
        region = self._region()
        cluster_name = self._cluster_name()
        alias = f"kubeorbit-{self.cluster.id}"

        cmd = [
            "aws", "eks", "update-kubeconfig",
            "--name", cluster_name, "--region", region,
            "--kubeconfig", str(self.kubeconfig_path), "--alias", alias,
        ]
        role_arn = self._role_arn()
        if role_arn:
            cmd += ["--role-arn", role_arn]

        try:
            await run_cli(cmd, env=self._base_env())
        except CLIError as exc:
            raise ProviderAuthError(
                f"aws eks update-kubeconfig failed for '{cluster_name}': {exc.stderr or exc}"
            ) from exc

        context_name = read_current_context(self.kubeconfig_path)
        identity = await self._current_identity(role_arn)
        return ClusterAuthResult(
            context_name=context_name,
            kubeconfig_path=str(self.kubeconfig_path),
            authenticated_at=now(),
            identity=identity,
        )

    async def _current_identity(self, role_arn: Optional[str]) -> ProviderIdentity:
        try:
            _, stdout, _ = await run_cli(
                ["aws", "sts", "get-caller-identity", "--output", "json"], env=self._base_env(), check=False
            )
            caller = json.loads(stdout) if stdout.strip() else {}
        except CLIError:
            caller = {}
        return ProviderIdentity(
            provider="eks",
            account=caller.get("Arn"),
            project_or_subscription=caller.get("Account"),
            impersonated_as=role_arn,
        )

    async def get_cluster_metadata(self) -> ClusterMeta:
        region = self._region()
        cluster_name = self._cluster_name()
        env = await self._sts_env()

        rc, stdout, stderr = await run_cli(
            ["aws", "eks", "describe-cluster", "--name", cluster_name, "--region", region, "--output", "json"],
            env=env, check=False,
        )
        if rc != 0:
            logger.warning("eks describe-cluster failed for %s: %s", cluster_name, stderr)
            return ClusterMeta(
                provider="eks", cluster_id=self.cluster.id, name=cluster_name,
                kubernetes_version=None, location=region, status=None,
                degraded=True, error=stderr.strip()[:400],
            )

        cluster_data = json.loads(stdout)["cluster"]
        node_pools = await self._describe_nodegroups(region, cluster_name, env)

        oidc_issuer = ((cluster_data.get("identity") or {}).get("oidc") or {}).get("issuer")
        wi = self.get_workload_identity_info()
        wi["enabled"] = bool(oidc_issuer)
        wi["detail"] = f"OIDC issuer: {oidc_issuer}" if oidc_issuer else "No OIDC provider associated with this cluster."

        vpc_config = cluster_data.get("resourcesVpcConfig") or {}
        net_config = cluster_data.get("kubernetesNetworkConfig") or {}
        encryption_config = cluster_data.get("encryptionConfig") or []
        enabled_log_types = [
            t for entry in (cluster_data.get("logging") or {}).get("clusterLogging") or []
            if entry.get("enabled") for t in entry.get("types", [])
        ]
        public_access = bool(vpc_config.get("endpointPublicAccess"))
        private_access = bool(vpc_config.get("endpointPrivateAccess"))

        network = {
            "api_server_endpoint": cluster_data.get("endpoint"),
            "vpc_id": vpc_config.get("vpcId"),
            # AWS VPC CNI hands pods routable VPC IPs directly -- there's no
            # separate pod CIDR the way GKE/AKS overlay networking has one.
            "pod_cidr": None,
            "service_cidr": net_config.get("serviceIpv4Cidr") or net_config.get("serviceIpv6Cidr"),
            "cni": "Amazon VPC CNI (default)",
            "network_policy": None,
            "private_cluster": private_access and not public_access,
        }

        return ClusterMeta(
            provider="eks",
            cluster_id=self.cluster.id,
            name=cluster_name,
            kubernetes_version=cluster_data.get("version"),
            location=region,
            status=cluster_data.get("status"),
            node_pools=node_pools,
            console_url=self.get_console_url(),
            logs_url=self.get_logs_url(),
            workload_identity=wi,
            network=network,
            extra={
                "platform_version": cluster_data.get("platformVersion"),
                "features": [
                    {"label": "IAM Roles for Service Accounts (IRSA)", "enabled": bool(oidc_issuer)},
                    {"label": "Private Endpoint Access", "enabled": private_access},
                    {"label": "Public Endpoint Access", "enabled": public_access},
                    {"label": "Secrets Encryption (KMS)", "enabled": bool(encryption_config)},
                    {"label": "Control Plane Logging", "enabled": bool(enabled_log_types)},
                ],
            },
        )

    async def _describe_nodegroups(self, region: str, cluster_name: str, env: Dict[str, str]):
        try:
            _, stdout, _ = await run_cli(
                ["aws", "eks", "list-nodegroups", "--cluster-name", cluster_name, "--region", region, "--output", "json"],
                env=env,
            )
        except CLIError as exc:
            logger.warning("eks list-nodegroups failed for %s: %s", cluster_name, exc)
            return []

        names = json.loads(stdout).get("nodegroups", []) if stdout.strip() else []
        raw_pools = []
        all_subnet_ids: set = set()
        for ng_name in names:
            try:
                _, ng_stdout, _ = await run_cli(
                    [
                        "aws", "eks", "describe-nodegroup", "--cluster-name", cluster_name,
                        "--nodegroup-name", ng_name, "--region", region, "--output", "json",
                    ],
                    env=env,
                )
            except CLIError as exc:
                logger.warning("eks describe-nodegroup failed for %s/%s: %s", cluster_name, ng_name, exc)
                continue
            ng = json.loads(ng_stdout)["nodegroup"]
            raw_pools.append((ng_name, ng))
            all_subnet_ids.update(ng.get("subnets", []) or [])

        subnet_to_az = await self._resolve_subnet_azs(region, sorted(all_subnet_ids), env)

        pools = []
        for ng_name, ng in raw_pools:
            scaling = ng.get("scalingConfig", {}) or {}
            subnet_ids = ng.get("subnets", []) or []
            zones = sorted({subnet_to_az[s] for s in subnet_ids if s in subnet_to_az}) or subnet_ids
            pools.append(
                NodePoolInfo(
                    name=ng_name,
                    node_count=scaling.get("desiredSize", 0),
                    machine_type=", ".join(ng.get("instanceTypes", []) or []),
                    zones=zones,
                    autoscaling=scaling.get("minSize") != scaling.get("maxSize"),
                    min_nodes=scaling.get("minSize"),
                    max_nodes=scaling.get("maxSize"),
                )
            )
        return pools

    async def _resolve_subnet_azs(self, region: str, subnet_ids: list, env: Dict[str, str]) -> Dict[str, str]:
        """describe-nodegroup only returns subnet IDs, not availability zones,
        so resolve them with one batched ec2:DescribeSubnets call. Best-effort:
        if the caller lacks ec2:DescribeSubnets, node pools still render, just
        with subnet IDs in place of AZ names."""
        if not subnet_ids:
            return {}
        try:
            _, stdout, _ = await run_cli(
                ["aws", "ec2", "describe-subnets", "--subnet-ids", *subnet_ids, "--region", region, "--output", "json"],
                env=env,
            )
        except CLIError as exc:
            logger.info("ec2:DescribeSubnets unavailable (showing subnet IDs instead of AZs): %s", exc)
            return {}
        subnets = json.loads(stdout).get("Subnets", []) if stdout.strip() else []
        return {s["SubnetId"]: s["AvailabilityZone"] for s in subnets if "SubnetId" in s and "AvailabilityZone" in s}

    async def get_cost_estimate(self, node_pools: list) -> CostEstimate:
        region = self._region()
        env = await self._sts_env()

        pools: list[NodePoolCost] = []
        total = 0.0
        any_priced = False
        for pool in node_pools:
            # A node group can mix instance types; price the first listed one
            # and say so, rather than silently averaging/guessing.
            instance_type = (pool.machine_type or "").split(",")[0].strip()
            mixed_note = " (mixed instance types configured; priced the first)" if "," in (pool.machine_type or "") else ""

            price, note = None, None
            if instance_type:
                try:
                    price = await fetch_aws_ec2_hourly_price(region, instance_type, env)
                except Exception as exc:
                    note = f"Pricing lookup failed: {exc}"
            if price is None and note is None:
                note = f"No on-demand price found for '{instance_type}' in '{region}'."
            elif price is not None and mixed_note:
                note = instance_type + mixed_note

            monthly = price * pool.node_count * _AVG_HOURS_PER_MONTH if price is not None else None
            if price is not None:
                any_priced = True
                total += monthly
            pools.append(NodePoolCost(
                name=pool.name, machine_type=pool.machine_type, node_count=pool.node_count,
                hourly_price_usd=price, monthly_estimate_usd=monthly, priced=price is not None, note=note,
            ))

        return CostEstimate(
            provider="eks", pools=pools,
            monthly_total_usd=round(total, 2) if any_priced else None,
            error=None if any_priced else "No node pools could be priced (see per-pool notes; requires `aws` CLI and pricing:GetProducts permission).",
        )

    def get_console_url(self) -> str:
        region = self._region()
        return f"https://{region}.console.aws.amazon.com/eks/home?region={region}#/clusters/{self._cluster_name()}"

    def get_logs_url(self) -> str:
        import urllib.parse

        region = self._region()
        # EKS's own control-plane log group name is predictable
        # (/aws/eks/<cluster>/cluster), but real setups often ship extra
        # groups (e.g. Fluent Bit to a custom group) -- config.cloudwatch_log_group
        # lets the cluster registration override the default.
        log_group = self.cluster.config.get("cloudwatch_log_group") or f"/aws/eks/{self._cluster_name()}/cluster"
        encoded = urllib.parse.quote(urllib.parse.quote(log_group, safe=""), safe="")
        return f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}#logsV2:log-groups/log-group/{encoded}"

    def get_workload_identity_info(self) -> Dict[str, Any]:
        return {
            "mechanism": "IAM Roles for Service Accounts (IRSA)",
            "docs_url": "https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html",
            "description": (
                "Associates an IAM role with a Kubernetes ServiceAccount via an OIDC "
                "identity provider, so pods assume the role instead of node IAM credentials."
            ),
        }
