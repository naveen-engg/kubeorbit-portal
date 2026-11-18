"""
Local Kubeconfig provider adapter.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
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

                # Only rewrite 127.0.0.1 -> host.docker.internal when actually inside a Docker container
                in_docker = os.path.exists("/.dockerenv") or os.environ.get("KUBEORBIT_IN_DOCKER") == "true"
                minikube_dir = str(Path.home() / ".minikube").replace("\\", "/")

                for c in cfg.get("clusters", []):
                    cl = c.get("cluster", {})
                    if "server" in cl and isinstance(cl["server"], str):
                        if in_docker and ("127.0.0.1" in cl["server"] or "localhost" in cl["server"]):
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
                    if os.name != "nt":
                        exec_cfg = usr.get("exec")
                        if isinstance(exec_cfg, dict) and isinstance(exec_cfg.get("command"), str):
                            cmd_str = exec_cfg["command"].replace("\\", "/")
                            if cmd_str.lower().endswith(".exe") or ":" in cmd_str:
                                exec_cfg["command"] = cmd_str.split("/")[-1].removesuffix(".exe").removesuffix(".EXE")
                            if "gke-gcloud-auth-plugin" in exec_cfg["command"]:
                                adc_file = os.environ.get(
                                    "GOOGLE_APPLICATION_CREDENTIALS",
                                    str(Path.home() / ".config" / "gcloud" / "application_default_credentials.json"),
                                )
                                if os.path.exists(adc_file):
                                    args = list(exec_cfg.get("args") or [])
                                    if "--use_application_default_credentials" not in args:
                                        args.append("--use_application_default_credentials")
                                    exec_cfg["args"] = args
                        ap_cfg = (usr.get("auth-provider") or {}).get("config")
                        if isinstance(ap_cfg, dict) and isinstance(ap_cfg.get("cmd-path"), str):
                            cp_str = ap_cfg["cmd-path"].replace("\\", "/")
                            if cp_str.lower().endswith(".exe") or ":" in cp_str:
                                ap_cfg["cmd-path"] = cp_str.split("/")[-1].removesuffix(".exe").removesuffix(".EXE")

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
            node_pools=[
                NodePoolInfo(
                    name="default-pool",
                    node_count=node_count,
                    machine_type="local-host",
                    zones=["local"],
                    autoscaling=False,
                )
            ],
            console_url=None,
            workload_identity=self.get_workload_identity_info(),
        )
