"""
External process execution (kubectl/helm/gcloud/aws/az).

Centralizing this means every CLI invocation -- from kube_client itself and
from the provider auth adapters (gcloud/aws/az) -- gets the same timeout
handling, non-zero-exit logging, and binary-missing detection without each
call site re-implementing subprocess plumbing.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from typing import Dict, List, Optional, Tuple

from config import get_config_store

logger = logging.getLogger("kubeorbit.kube_client")


class CLIError(Exception):
    """A subprocess (kubectl/helm/gcloud/aws/az) exited non-zero or timed out."""

    def __init__(self, cmd: List[str], returncode: Optional[int], stderr: str):
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr.strip()
        display_cmd = " ".join(cmd[:2])
        super().__init__(f"'{display_cmd}...' failed (exit {returncode}): {self.stderr[:500]}")


class BinaryNotFoundError(CLIError):
    """The requested CLI binary (kubectl/helm/gcloud/aws/az) is not on PATH."""


class ResourceTypeUnavailableError(CLIError):
    """The cluster doesn't have this API resource registered (e.g. Gateway API CRDs)."""


def _sync_subprocess_exec(
    exec_cmd: List[str], full_env: Optional[Dict[str, str]], timeout: int
) -> Tuple[int, bytes, bytes]:
    import subprocess

    proc = subprocess.run(
        exec_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=full_env,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


async def run_cli(
    cmd: List[str],
    env: Optional[Dict[str, str]] = None,
    timeout: Optional[int] = None,
    check: bool = True,
) -> Tuple[int, str, str]:
    """Run an external CLI command asynchronously and return (rc, stdout, stderr).

    Centralizing this means every kubectl/helm/gcloud/aws/az invocation gets
    the same timeout handling, non-zero-exit logging, and binary-missing
    detection without each provider re-implementing subprocess plumbing.
    """
    binary = cmd[0]
    resolved = shutil.which(binary)
    if resolved is None:
        raise BinaryNotFoundError(cmd, None, f"'{binary}' is not installed or not on PATH")

    exec_cmd = [resolved, *cmd[1:]]

    timeout = timeout or get_config_store().settings.cli_timeout_seconds
    full_env = None
    if env:
        import os

        full_env = {**os.environ, **env}

    logger.debug("Running CLI command: %s", " ".join(exec_cmd))
    started = time.monotonic()
    try:
        returncode, stdout_b, stderr_b = await asyncio.to_thread(
            _sync_subprocess_exec, exec_cmd, full_env, timeout
        )
    except Exception as exc:
        if "Timeout" in type(exc).__name__:
            logger.error("CLI command timed out after %ss: %s", timeout, " ".join(cmd))
            raise CLIError(cmd, None, f"command timed out after {timeout}s") from None
        raise

    elapsed_ms = int((time.monotonic() - started) * 1000)
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")

    if returncode != 0:
        logger.warning(
            "CLI command failed (exit=%s, %sms): %s :: %s",
            returncode,
            elapsed_ms,
            " ".join(cmd),
            stderr.strip()[:300],
        )
        if check:
            if "doesn't have a resource type" in stderr or "the server could not find the requested resource" in stderr:
                raise ResourceTypeUnavailableError(cmd, returncode, stderr)
            raise CLIError(cmd, returncode, stderr)
    else:
        logger.debug("CLI command ok (%sms): %s", elapsed_ms, " ".join(cmd))

    return returncode, stdout, stderr
