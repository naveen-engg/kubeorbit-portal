"""
EKS (Elastic Kubernetes Service) provider adapter.

Auth model:
  1. Cloud Run / GCP Workload Identity Federation (Zero Keys):
     - Queries the local GCP Metadata Server (`metadata.google.internal`) for a
       signed Google OIDC ID token (`audience=sts.amazonaws.com` or custom
       `config.oidc_audience`).
     - Calls `aws sts assume-role-with-web-identity --role-arn <role_arn>` to
       obtain short-lived AWS STS credentials without any static AWS access keys.
  2. Local dev / AWS Native (ECS Fargate or AWS SSO/Profile):
     - Relies on `aws configure` / `aws sso login` / ECS Task Role credentials,
       and assumes `role_arn` via `aws sts assume-role`.

Expected config.* keys (see config/clusters.example.yaml):
  region (required). cluster_name (optional, defaults to `name`).
  role_arn (optional, per-environment/team IAM role ARN).
  aws_profile (optional). oidc_audience (optional, defaults to `sts.amazonaws.com`).
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
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
_STS_SESSION_TTL_SECONDS = 2700  # refresh before 1h expiry


def _fetch_gcp_oidc_token(audience: str = "sts.amazonaws.com") -> Optional[str]:
    """Fetch a signed Google OIDC ID token when running on GCP Cloud Run / GCE / GKE,
    or from KUBEORBIT_GCP_OIDC_TOKEN_FILE if mounted. Returns None when not on GCP."""
    token_file = os.environ.get("KUBEORBIT_GCP_OIDC_TOKEN_FILE")
    if token_file and os.path.exists(token_file):
        try:
            with open(token_file, "r", encoding="utf-8") as fh:
                return fh.read().strip() or None
        except OSError:
            pass

    # Only probe metadata.google.internal if on Cloud Run (K_SERVICE) or explicitly requested
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
        logger.debug("GCP Metadata OIDC token fetch skipped/failed: %s", exc)
    return None


@register_provider("eks")
class EKSProvider(BaseKubeProvider):
    def _region(self) -> str:
        return self.require("region")

    def _cluster_name(self) -> str:
        return self.cluster.config.get("cluster_name", self.cluster.name)

    def _role_arn(self) -> Optional[str]:
        return self.cluster.config.get("role_arn")

    def _oidc_audience(self) -> str:
        return self.cluster.config.get("oidc_audience") or "sts.amazonaws.com"

    def _base_env(self) -> Dict[str, str]:
        profile = self.cluster.config.get("aws_profile")
        return {"AWS_PROFILE": profile} if profile else {}

    async def _sts_env(self) -> Dict[str, str]:
        """Temporary credentials for AWS CLI / EKS calls.
        Automatically uses OIDC `assume-role-with-web-identity` on Cloud Run when a GCP OIDC
        token is available, or falls back to `aws sts assume-role`."""
        role_arn = self._role_arn()
        if not role_arn:
            return self._base_env()

        async def factory():
            oidc_token = _fetch_gcp_oidc_token(self._oidc_audience())
            if oidc_token:
                logger.info(
                    "Exchanging GCP Cloud Run OIDC token for AWS role '%s' (cluster=%s)",
                    role_arn, self.cluster.id,
                )
                cmd = [
                    "aws", "sts", "assume-role-with-web-identity",
                    "--role-arn", role_arn,
                    "--role-session-name", f"kubeorbit-{self.cluster.id}",
                    "--web-identity-token", oidc_token,
                    "--output", "json",
                ]
            else:
                cmd = [
                    "aws", "sts", "assume-role",
                    "--role-arn", role_arn,
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
                "_USED_WEB_IDENTITY": "true" if oidc_token else "false",
            }

        env_raw, _ = await _sts_cache.get_or_set(f"sts:{self.cluster.id}:{role_arn}", _STS_SESSION_TTL_SECONDS, factory)
        return {k: v for k, v in env_raw.items() if not k.startswith("_")}

    async def authenticate(self) -> ClusterAuthResult:
        region = self._region()
        cluster_name = self._cluster_name()
        alias = f"kubeorbit-{self.cluster.id}"
        role_arn = self._role_arn()

        # Check if we are using Cloud Run OIDC Web Identity exchange
        oidc_token = _fetch_gcp_oidc_token(self._oidc_audience()) if role_arn else None
        cli_env = await self._sts_env() if oidc_token else self._base_env()

        cmd = [
            "aws", "eks", "update-kubeconfig",
            "--name", cluster_name, "--region", region,
            "--kubeconfig", str(self.kubeconfig_path), "--alias", alias,
        ]
        # When NOT using web-identity pre-assumed env vars, bake --role-arn into update-kubeconfig
        if role_arn and not oidc_token:
            cmd += ["--role-arn", role_arn]

        try:
            await run_cli(cmd, env=cli_env)
        except CLIError as exc:
            raise ProviderAuthError(
                f"aws eks update-kubeconfig failed for '{cluster_name}': {exc.stderr or exc}"
            ) from exc

        context_name = read_current_context(self.kubeconfig_path)
        identity = await self._current_identity(role_arn, cli_env)
        return ClusterAuthResult(
            context_name=context_name,
            kubeconfig_path=str(self.kubeconfig_path),
            authenticated_at=now(),
            identity=identity,
        )

    async def _current_identity(self, role_arn: Optional[str], env: Optional[Dict[str, str]] = None) -> ProviderIdentity:
        try:
            _, stdout, _ = await run_cli(
                ["aws", "sts", "get-caller-identity", "--output", "json"],
                env=env if env is not None else self._base_env(),
                check=False,
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
            zones = sorted({subnet_to_az[sid] for sid in subnet_ids if sid in subnet_to_az}) or subnet_ids
            instance_types = ng.get("instanceTypes") or []
            pools.append(
                NodePoolInfo(
                    name=ng_name,
                    node_count=scaling.get("desiredSize", 0),
                    machine_type=instance_types[0] if instance_types else None,
                    zones=zones,
                    autoscaling=True,
                    min_nodes=scaling.get("minSize"),
                    max_nodes=scaling.get("maxSize"),
                )
            )
        return pools

    async def _resolve_subnet_azs(self, region: str, subnet_ids: list, env: Dict[str, str]) -> Dict[str, str]:
        if not subnet_ids:
            return {}
        try:
            _, stdout, _ = await run_cli(
                [
                    "aws", "ec2", "describe-subnets", "--region", region,
                    "--subnet-ids", *subnet_ids, "--output", "json",
                ],
                env=env, check=False,
            )
            data = json.loads(stdout) if stdout.strip() else {}
            return {
                s["SubnetId"]: s["AvailabilityZone"]
                for s in data.get("Subnets", [])
                if "SubnetId" in s and "AvailabilityZone" in s
            }
        except Exception:
            return {}

    async def get_cost_estimate(self, node_pools: list[NodePoolInfo]) -> CostEstimate:
        region = self._region()
        pools: list[NodePoolCost] = []
        total = 0.0
        any_priced = False
        for pool in node_pools:
            price = None
            note = None
            if pool.machine_type:
                try:
                    price = await fetch_aws_ec2_hourly_price(region, pool.machine_type)
                except Exception as exc:
                    note = f"Pricing lookup failed: {exc}"
            if price is None and note is None:
                note = f"No public on-demand Linux price found for '{pool.machine_type}' in {region}."

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
            error=None if any_priced else "No nodegroups could be priced.",
        )

    def get_console_url(self) -> str:
        region = self._region()
        return f"https://{region}.console.aws.amazon.com/eks/home?region={region}#/clusters/{self._cluster_name()}"

    def get_logs_url(self) -> Optional[str]:
        region = self._region()
        log_group = self.cluster.config.get("cloudwatch_log_group") or f"/aws/eks/{self._cluster_name()}/cluster"
        encoded = log_group.replace("/", "$252F")
        return (
            f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
            f"#logsV2:log-groups/log-group/{encoded}"
        )

    def get_workload_identity_info(self) -> Dict[str, Any]:
        return {
            "mechanism": "IAM Roles for Service Accounts (IRSA)",
            "docs_url": "https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html",
            "description": (
                "Associates an AWS IAM role with a Kubernetes ServiceAccount via the cluster's "
                "OIDC provider and the eks.amazonaws.com/role-arn annotation."
            ),
        }
