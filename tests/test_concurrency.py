"""High-Concurrency / 10-User Traffic Simulation Tests.

Simulates 10 concurrent active users interacting with KubeOrbit simultaneously:
1. 10 concurrent users polling `/api/clusters/{id}/namespaces/{ns}/resources` on a cold cache.
2. 10 concurrent users switching clusters, viewing logs, and fetching YAML manifests under `_kubectl_semaphore`.
3. 10 concurrent admin requests mutating `clusters.yaml` via `POST` and `PUT /api/admin/clusters`.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

from tests.test_integration import MockGKEProvider


class TestTenConcurrentUsersSimulation:
    @pytest.mark.asyncio
    async def test_10_concurrent_users_ColdCache_NamespaceResources_and_Logs(
        self,
        async_client: httpx.AsyncClient,
        isolated_env: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Simulate 10 concurrent users hitting Namespace Resources, Pod Logs, and YAML Manifests."""
        kcfg_home = isolated_env["kubeconfig_home"]
        auth_invocations = 0
        sdk_invocations = 0
        cli_invocations = 0

        class CountingMockProvider(MockGKEProvider):
            async def authenticate(self):
                nonlocal auth_invocations
                auth_invocations += 1
                await asyncio.sleep(0.02)  # simulate 20ms cloud CLI latency
                return await super().authenticate()

        monkeypatch.setattr("app.build_provider", lambda cfg: CountingMockProvider(cfg, kcfg_home))

        def slow_sdk_list(
            context: str, kubeconfig_path: Path, resource: str, namespace: Optional[str] = None
        ) -> Tuple[Optional[List[Dict[str, Any]]], bool]:
            nonlocal sdk_invocations
            sdk_invocations += 1
            if resource == "pods":
                return [
                    {
                        "metadata": {"name": "api-backend-1", "namespace": "kubeorbit-demo"},
                        "spec": {"nodeName": "node-1", "containers": [{"name": "api", "resources": {"limits": {"cpu": "100m", "memory": "128Mi"}}}]},
                        "status": {"phase": "Running", "containerStatuses": [{"name": "api", "ready": True, "restartCount": 0}]},
                    }
                ], True
            return [], True

        monkeypatch.setattr("kube_client._fetch._sync_sdk_list", slow_sdk_list)

        async def slow_run_cli(cmd: List[str], env=None, timeout=None, check=True):
            nonlocal cli_invocations
            cli_invocations += 1
            await asyncio.sleep(0.015)  # simulate 15ms subprocess execution
            if "top" in cmd:
                if "-n" in cmd:
                    return 0, "api-backend-1 15m 32Mi\n", ""
                return 0, "kubeorbit-demo api-backend-1 15m 32Mi\n", ""
            if "logs" in cmd:
                return 0, "2026-11-06T12:00:00Z request handled\n", ""
            return 0, "apiVersion: v1\nkind: Pod\n", ""

        monkeypatch.setattr("kube_client._cli.run_cli", slow_run_cli)
        monkeypatch.setattr("kube_client._fetch.run_cli", slow_run_cli)
        monkeypatch.setattr("kube_client.resources.run_cli", slow_run_cli)
        monkeypatch.setattr("kube_client.logs.run_cli", slow_run_cli)

        async def simulate_single_user(user_idx: int):
            r_ns = await async_client.get("/api/clusters/obs-sbx/namespaces/kubeorbit-demo/resources")
            r_logs = await async_client.get(
                "/api/clusters/obs-sbx/namespaces/kubeorbit-demo/pods/api-backend-1/logs?tail_lines=50"
            )
            r_yaml = await async_client.get(
                "/api/clusters/obs-sbx/manifest?kind=Pod&name=api-backend-1&namespace=kubeorbit-demo"
            )
            return r_ns.status_code, r_logs.status_code, r_yaml.status_code, r_ns.json()

        results = await asyncio.gather(*(simulate_single_user(i) for i in range(10)))

        for status_ns, status_logs, status_yaml, body in results:
            assert status_ns == 200
            assert status_logs == 200
            assert status_yaml == 200
            assert body["pods"][0]["resources"]["cpu_pct"] == 15

        # Subsequent wave of 10 users hits warm TTL cache -> 0 new auth calls
        auth_before_warm = auth_invocations
        warm_results = await asyncio.gather(
            *(async_client.get("/api/clusters/obs-sbx/namespaces/kubeorbit-demo/resources") for _ in range(10))
        )
        assert all(r.status_code == 200 for r in warm_results)
        assert auth_invocations == auth_before_warm

    @pytest.mark.asyncio
    async def test_10_concurrent_cluster_edits_and_reads(self, async_client: httpx.AsyncClient):
        """Simulate 10 concurrent users reading and editing cluster display names."""

        async def user_edit_and_list(idx: int):
            put_res = await async_client.put(
                "/api/admin/clusters/obs-sbx",
                json={
                    "id": "obs-sbx",
                    "name": f"OBS SBX Edited-{idx}",
                    "provider": "gke",
                    "environment": "sandbox",
                    "team_id": "platform",
                    "team_name": "Platform Engineering",
                    "description": f"Concurrent edit {idx}",
                    "config": {
                        "project": "adk-enterprise-observability",
                        "zone": "us-central1-a",
                        "cluster_name": "kubeorbit-gke",
                    },
                },
            )
            list_res = await async_client.get("/api/admin/clusters")
            return put_res.status_code, list_res.status_code, list_res.json()

        outcomes = await asyncio.gather(*(user_edit_and_list(i) for i in range(10)))
        for put_code, list_code, payload in outcomes:
            assert put_code == 200
            assert list_code == 200
            # Verify obs-sbx is never duplicated even after 10 concurrent PUTs
            obs_entries = [c for c in payload["clusters"] if c["id"] == "obs-sbx"]
            assert len(obs_entries) == 1
