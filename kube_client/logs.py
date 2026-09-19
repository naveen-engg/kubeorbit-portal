"""Pod log tailing and raw resource YAML dumps."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

import kubernetes.client

from ._cache import _kubectl_semaphore
from ._cli import run_cli
from ._sdk import _get_or_create_sdk_client

logger = logging.getLogger("kubeorbit.kube_client")


async def get_pod_logs(
    context: str,
    kubeconfig_path: Path,
    namespace: str,
    pod_name: str,
    container: Optional[str] = None,
    tail_lines: int = 200,
) -> str:
    def _fetch_sdk_logs():
        try:
            client = _get_or_create_sdk_client(context, kubeconfig_path)
            core = kubernetes.client.CoreV1Api(api_client=client)
            kwargs = {"name": pod_name, "namespace": namespace, "tail_lines": tail_lines}
            if container:
                kwargs["container"] = container
            return core.read_namespaced_pod_log(**kwargs)
        except Exception as e:
            logger.debug("SDK get_pod_logs failed (%s), falling back to CLI", e)
            return None

    sdk_logs = await asyncio.to_thread(_fetch_sdk_logs)
    if sdk_logs is not None:
        return sdk_logs if sdk_logs.strip() else "No logs returned."

    cmd = [
        "kubectl", "--kubeconfig", str(kubeconfig_path), "--context", context,
        "logs", "-n", namespace, pod_name, f"--tail={tail_lines}"
    ]
    if container:
        cmd += ["-c", container]
    async with _kubectl_semaphore:
        _, stdout, stderr = await run_cli(cmd, check=False)
    return stdout if stdout.strip() else (stderr or "No logs returned.")


async def get_resource_yaml(
    context: str,
    kubeconfig_path: Path,
    namespace: Optional[str],
    kind: str,
    name: str,
) -> str:
    cmd = [
        "kubectl", "--kubeconfig", str(kubeconfig_path), "--context", context,
        "get", kind, name, "-o", "yaml"
    ]
    if namespace:
        cmd += ["-n", namespace]
    async with _kubectl_semaphore:
        _, stdout, stderr = await run_cli(cmd, check=False)
    return stdout if stdout.strip() else (stderr or "# Resource not found.")
