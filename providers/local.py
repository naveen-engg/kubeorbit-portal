"""
Local Kubeconfig provider adapter.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

from config import get_runtime_env
from kube_client import read_current_context, run_cli
from providers.base import (
    BaseKubeProvider,
    ClusterAuthResult,
    ClusterMeta,
    NodePoolInfo,
    ProviderAuthError,
    ProviderIdentity,
    now,
    register_provider,
)

logger = logging.getLogger("kubeorbit.providers.local")

MAX_KUBECONFIG_BYTES = 262_144
SAFE_EXEC_COMMANDS = frozenset({"gke-gcloud-auth-plugin", "aws", "kubelogin", "gcloud"})
_BLOCKED_METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata", "metadata.goog", "instance-data"})
_BLOCKED_METADATA_IPS = frozenset({"169.254.169.254", "100.100.100.200", "fd00:ec2::254"})


def _is_cloud_run_or_iap() -> bool:
    """Return True when running in Cloud Run or IAP enterprise mode."""
    return bool(os.environ.get("K_SERVICE")) or os.environ.get("KUBEORBIT_AUTH_MODE", "none").lower() == "iap"


def _expand_user_path(raw: str) -> Path:
    """Expand leading `~` using `Path.home()` so path checks and `Path.home()` always agree."""
    if raw == "~":
        return Path.home()
    if raw.startswith("~/") or raw.startswith("~\\"):
        return Path.home() / raw[2:]
    return Path(raw).expanduser()


def validate_safe_kubeconfig_path(raw_path: str, *, require_exists: bool = True) -> Path:
    """Resolve and jail a kubeconfig path inside allowed directories.

    Raises PermissionError before existence checks when the path leaves allowed roots.
    """
    cleaned = (raw_path or "").strip() or "~/.kube/config"
    if "\x00" in cleaned:
        raise ValueError("Kubeconfig path contains null bytes.")
    resolved = _expand_user_path(cleaned).resolve()

    allowed_bases: List[Path] = [get_runtime_env().kubeconfig_home.resolve()]
    if not _is_cloud_run_or_iap():
        allowed_bases.append((Path.home() / ".kube").resolve())
        extra_dirs = os.environ.get("KUBEORBIT_ALLOWED_KUBECONFIG_DIRS", "")
        for item in extra_dirs.split(","):
            trimmed = item.strip()
            if trimmed:
                allowed_bases.append(_expand_user_path(trimmed).resolve())

    if not any(resolved.is_relative_to(base) for base in allowed_bases):
        raise PermissionError(f"Kubeconfig path '{raw_path}' is outside allowed kubeconfig directories.")

    if not require_exists and not resolved.exists():
        return resolved

    if not resolved.exists():
        raise FileNotFoundError(f"Kubeconfig file not found at: {raw_path}")
    if not resolved.is_file():
        raise ValueError(f"Kubeconfig path is not a regular file: {raw_path}")
    if resolved.stat().st_size > MAX_KUBECONFIG_BYTES:
        raise ValueError(f"Kubeconfig file exceeds {MAX_KUBECONFIG_BYTES} byte limit.")
    return resolved


def _validate_server_url(server: str, strict: bool) -> None:
    """Validate cluster server URL against SSRF targets and unsafe schemes."""
    parsed = urlparse(server.strip())
    scheme = (parsed.scheme or "").lower()
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not hostname:
        raise ValueError("Cluster server URL must include a hostname.")
    if hostname in _BLOCKED_METADATA_HOSTS:
        raise ValueError(f"Cluster server host '{hostname}' is blocked.")

    is_loopback = hostname == "localhost"
    try:
        ip = ipaddress.ip_address(hostname)
        is_loopback = ip.is_loopback
        if ip.is_link_local or ip.is_unspecified or ip.is_multicast or str(ip) in _BLOCKED_METADATA_IPS:
            raise ValueError(f"Cluster server IP '{hostname}' is blocked (metadata/link-local/unspecified).")
        if strict and is_loopback:
            raise ValueError("Loopback cluster server addresses are disallowed in uploaded or Cloud Run kubeconfigs.")
    except ValueError as exc:
        if "is blocked" in str(exc) or "disallowed" in str(exc):
            raise
        if strict and hostname == "localhost":
            raise ValueError("Localhost cluster server addresses are disallowed in uploaded or Cloud Run kubeconfigs.")

    if strict:
        if scheme != "https":
            raise ValueError("Cluster server URL must use https:// in uploaded or Cloud Run/IAP kubeconfigs.")
    elif scheme not in ("https", "http") or (scheme == "http" and not is_loopback):
        raise ValueError("Cluster server URL must use https:// (or http:// for loopback only).")


def _validate_exec_command(cmd_val: Any, field_name: str, strict: bool) -> None:
    """Block arbitrary command execution via kubeconfig exec plugins or auth-provider cmd-path."""
    if strict:
        raise ValueError(f"Kubeconfig '{field_name}' is disallowed in uploaded or Cloud Run/IAP kubeconfigs.")
    if not isinstance(cmd_val, str) or not cmd_val.strip():
        raise ValueError(f"Kubeconfig '{field_name}' must be a non-empty string.")
    if ".." in cmd_val or any(ch in cmd_val for ch in (";", "&", "|", "$", "`", "\n", "\r")):
        raise ValueError(f"Kubeconfig '{field_name}' contains unsafe characters.")
    norm = cmd_val.strip().replace("\\", "/").split("/")[-1].lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if norm.endswith(suffix):
            norm = norm[: -len(suffix)]
    if norm not in SAFE_EXEC_COMMANDS:
        raise ValueError(
            f"Kubeconfig '{field_name}' binary '{norm}' is not in the allowlist {sorted(SAFE_EXEC_COMMANDS)}."
        )


def validate_safe_kubeconfig_dict(cfg: Any, *, is_uploaded: bool = False) -> None:
    """Validate parsed kubeconfig YAML against RCE, SSRF, and host file read vectors."""
    if not isinstance(cfg, dict):
        raise ValueError("Kubeconfig must be a YAML mapping.")
    strict = is_uploaded or _is_cloud_run_or_iap()

    for entry in cfg.get("clusters") or []:
        if not isinstance(entry, dict):
            raise ValueError("Each entry in kubeconfig 'clusters' must be a mapping.")
        cl = entry.get("cluster") or {}
        if not isinstance(cl, dict):
            raise ValueError("Kubeconfig 'cluster' entry must be a mapping.")
        if strict and cl.get("certificate-authority"):
            raise ValueError("File-based 'certificate-authority' is disallowed; embed 'certificate-authority-data' instead.")
        if cl.get("server"):
            if not isinstance(cl["server"], str):
                raise ValueError("Kubeconfig 'cluster.server' must be a string URL.")
            _validate_server_url(cl["server"], strict=strict)

    for entry in cfg.get("users") or []:
        if not isinstance(entry, dict):
            raise ValueError("Each entry in kubeconfig 'users' must be a mapping.")
        usr = entry.get("user") or {}
        if not isinstance(usr, dict):
            raise ValueError("Kubeconfig 'user' entry must be a mapping.")
        if strict:
            for file_key in ("client-certificate", "client-key", "tokenFile"):
                if usr.get(file_key):
                    raise ValueError(f"File-based '{file_key}' is disallowed; embed inline data or token instead.")
        if usr.get("exec"):
            exec_cfg = usr["exec"]
            cmd_val = exec_cfg.get("command") if isinstance(exec_cfg, dict) else None
            _validate_exec_command(cmd_val, "user.exec", strict=strict)
        ap = usr.get("auth-provider")
        if isinstance(ap, dict):
            ap_cfg = ap.get("config")
            if isinstance(ap_cfg, dict) and ap_cfg.get("cmd-path"):
                _validate_exec_command(ap_cfg["cmd-path"], "user.auth-provider.config.cmd-path", strict=strict)


def _rewrite_minikube_path(raw_val: str, minikube_dir: str) -> str:
    val = raw_val.replace("\\", "/")
    if ".minikube" in val:
        idx = val.find(".minikube")
        return minikube_dir + "/" + val[idx + len(".minikube"):].lstrip("/")
    return val


@register_provider("local")
class LocalProvider(BaseKubeProvider):
    async def authenticate(self, user=None) -> ClusterAuthResult:
        import yaml

        src_raw = self.cluster.config.get("kubeconfig_path", "~/.kube/config")
        Path(self.kubeconfig_path).parent.mkdir(parents=True, exist_ok=True)

        resolved_src: Path | None = None
        try:
            resolved_src = validate_safe_kubeconfig_path(src_raw)
        except (PermissionError, ValueError) as exc:
            raise ProviderAuthError(str(exc)) from exc
        except FileNotFoundError:
            if not _is_cloud_run_or_iap():
                try:
                    resolved_src = validate_safe_kubeconfig_path("~/.kube/config")
                except (PermissionError, ValueError) as exc:
                    raise ProviderAuthError(str(exc)) from exc
                except FileNotFoundError:
                    resolved_src = None

        if resolved_src is not None:
            try:
                raw_text = resolved_src.read_text(encoding="utf-8")
                cfg = yaml.safe_load(raw_text) or {}
                validate_safe_kubeconfig_dict(cfg, is_uploaded=False)
            except (yaml.YAMLError, ValueError) as exc:
                raise ProviderAuthError(f"Unsafe or invalid kubeconfig '{src_raw}': {exc}") from exc

            in_docker = os.path.exists("/.dockerenv") or os.environ.get("KUBEORBIT_IN_DOCKER") == "true"
            minikube_dir = str(Path.home() / ".minikube").replace("\\", "/")

            for c in cfg.get("clusters") or []:
                cl = c.get("cluster") or {}
                server = cl.get("server")
                if isinstance(server, str) and in_docker and ("127.0.0.1" in server or "localhost" in server):
                    cl["server"] = server.replace("127.0.0.1", "host.docker.internal").replace("localhost", "host.docker.internal")
                    cl["insecure-skip-tls-verify"] = True
                    cl.pop("certificate-authority", None)
                if isinstance(cl.get("certificate-authority"), str):
                    cl["certificate-authority"] = _rewrite_minikube_path(cl["certificate-authority"], minikube_dir)

            for u in cfg.get("users") or []:
                usr = u.get("user") or {}
                for key in ("client-certificate", "client-key"):
                    if isinstance(usr.get(key), str):
                        usr[key] = _rewrite_minikube_path(usr[key], minikube_dir)
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
            try:
                os.chmod(self.kubeconfig_path, 0o600)
            except OSError:
                pass

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
