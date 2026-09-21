"""
Local Kubeconfig provider adapter.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict

from kube_client import read_current_context, run_cli
from providers.base import (
    BaseKubeProvider,
    ClusterAuthResult,
    ClusterMeta,
    NodePoolInfo,
    ProviderIdentity,
    now,
    register_provider,
)

logger = logging.getLogger("kubeorbit.providers.local")


@register_provider("local")
class LocalProvider(BaseKubeProvider):
    async def authenticate(self, user=None) -> ClusterAuthResult:
        src_raw = self.cluster.config.get("kubeconfig_path", "~/.kube/config")
        src_path = os.path.expanduser(src_raw)
        
        # Ensure parent directory for isolated kubeconfig exists
        Path(self.kubeconfig_path).parent.mkdir(parents=True, exist_ok=True)

        if os.path.exists(src_path):
            import yaml
            try:
                with open(src_path, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}

                is_linux = sys.platform.startswith("linux") or os.name != "nt"
                minikube_dir = str(Path.home() / ".minikube").replace("\\", "/")

                for c in cfg.get("clusters", []):
                    cl = c.get("cluster", {})
                    if "server" in cl and isinstance(cl["server"], str):
                        # Inside Docker containers, map localhost to host.docker.internal
                        if is_linux and ("127.0.0.1" in cl["server"] or "localhost" in cl["server"]):
                            cl["server"] = cl["server"].replace("127.0.0.1", "host.docker.internal").replace("localhost", "host.docker.internal")
                            cl["insecure-skip-tls-verify"] = True
                            cl.pop("certificate-authority", None)

                    if "certificate-authority" in cl and isinstance(cl["certificate-authority"], str):
                        ca_val = cl["certificate-authority"].replace("\\", "/")
                        if ".minikube" in ca_val:
                            idx = ca_val.find(".minikube")
                            cl["certificate-authority"] = minikube_dir + "/" + ca_val[idx + len(".minikube"):].lstrip("/")
                        else:
                            cl["certificate-authority"] = ca_val

                for u in cfg.get("users", []):
                    usr = u.get("user", {})
                    for key in ("client-certificate", "client-key"):
                        if key in usr and isinstance(usr[key], str):
                            val = usr[key].replace("\\", "/")
                            if ".minikube" in val:
                                idx = val.find(".minikube")
                                usr[key] = minikube_dir + "/" + val[idx + len(".minikube"):].lstrip("/")
                            else:
                                usr[key] = val

                with open(self.kubeconfig_path, "w", encoding="utf-8") as f:
                    yaml.dump(cfg, f)
            except Exception as exc:
                logger.warning("Could not parse/adapt kubeconfig %s: %s, falling back to copy", src_path, exc)
                shutil.copyfile(src_path, self.kubeconfig_path)
        else:
            fallback = Path.home() / ".kube" / "config"
            if fallback.exists():
                shutil.copyfile(str(fallback), self.kubeconfig_path)

        ctx = self.cluster.config.get("context_name") or read_current_context(self.kubeconfig_path)
        if ctx:
            await run_cli(
                ["kubectl", "config", "use-context", ctx],
                env={"KUBECONFIG": str(self.kubeconfig_path)},
                check=False,
            )
        identity = await self._current_identity(ctx)
        return ClusterAuthResult(
            context_name=ctx,
            kubeconfig_path=str(self.kubeconfig_path),
            authenticated_at=now(),
            identity=identity,
        )

    async def _current_identity(self, ctx: str | None) -> ProviderIdentity:
        return ProviderIdentity(
            provider="local",
            account="local-user",
            project_or_subscription=ctx or "local-context",
            impersonated_as=None,
        )

    def get_console_url(self) -> str | None:
        return None

    def get_workload_identity_info(self) -> Dict[str, Any]:
        return {
            "mechanism": "Local Kubeconfig",
            "docs_url": "https://kubernetes.io/docs/concepts/configuration/organize-cluster-access-kubeconfig/",
            "description": "Standard local kubeconfig context authentication for Minikube, Kind, or Docker Desktop.",
        }

    async def get_cluster_metadata(self) -> ClusterMeta:
        env = {"KUBECONFIG": str(self.kubeconfig_path)}
        k8s_version = None
        node_count = 1

        try:
            rc, stdout, _ = await run_cli(
                ["kubectl", "version", "--output=json"], env=env, check=False
            )
            if rc == 0 and stdout:
                vdata = json.loads(stdout)
                k8s_version = (vdata.get("serverVersion") or {}).get("gitVersion")
        except Exception:
            pass

        try:
            rc, stdout, _ = await run_cli(
                ["kubectl", "get", "nodes", "--output=json"], env=env, check=False
            )
            if rc == 0 and stdout:
                ndata = json.loads(stdout)
                nodes = ndata.get("items", [])
                if nodes:
                    node_count = len(nodes)
        except Exception:
            pass

        return ClusterMeta(
            provider="local",
            cluster_id=self.cluster.id,
            name=self.cluster.name,
            kubernetes_version=k8s_version or "v1.30.0",
            location="local",
            status="RUNNING",
            node_pools=[NodePoolInfo(name="local-pool", node_count=node_count)],
            console_url=self.get_console_url(),
            workload_identity=self.get_workload_identity_info(),
            network={
                "api_server_endpoint": None,
                "vpc_id": None,
                "pod_cidr": None,
                "service_cidr": None,
                "cni": None,
                "network_policy": None,
                "private_cluster": False,
            },
            extra={"features": []},
        )
