"""Integration Tests for KubeOrbit FastAPI application.

Demonstrates end-to-end data flow, component interaction (FastAPI routes ->
ConfigStore -> Auth -> Providers -> kube_client -> Audit), mocked external
CLI/SDK calls, and error propagation across all major endpoints.
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

import kube_client
from providers.base import (
    BaseKubeProvider,
    ClusterAuthResult,
    ClusterMeta,
    NodePoolInfo,
    ProviderAuthError,
    ProviderConfigError,
    ProviderIdentity,
)


class MockGKEProvider(BaseKubeProvider):
    """Deterministic in-memory provider mock so integration tests never invoke gcloud."""

    def __init__(self, cluster, kubeconfig_home: Path):
        self._kcfg = kubeconfig_home / f"{cluster.id}.yaml"
        super().__init__(cluster, self._kcfg)

    async def authenticate(self) -> ClusterAuthResult:
        self._kcfg.write_text("apiVersion: v1\ncurrent-context: mock-gke-ctx\n", encoding="utf-8")
        return ClusterAuthResult(
            context_name="mock-gke-ctx",
            kubeconfig_path=str(self._kcfg),
            authenticated_at="2026-11-06T12:00:00Z",
            identity=ProviderIdentity(
                provider=self.cluster.provider,
                account="operator@example.com",
                project_or_subscription="adk-enterprise-observability",
            ),
        )

    async def get_cluster_metadata(self) -> ClusterMeta:
        return ClusterMeta(
            provider=self.cluster.provider,
            cluster_id=self.cluster.id,
            name=self.cluster.name,
            kubernetes_version="1.36.4-gke.2046003",
            location="us-central1-a",
            status="RUNNING",
            node_pools=[
                NodePoolInfo(
                    name="default-pool",
                    machine_type="e2-medium",
                    node_count=1,
                    min_nodes=1,
                    max_nodes=3,
                    autoscaling=True,
                    zones=["us-central1-a"],
                )
            ],
        )

    def get_console_url(self) -> str:
        return "https://console.cloud.google.com/kubernetes/list"

    def get_workload_identity_info(self) -> Dict[str, Any]:
        return {"enabled": True, "workload_pool": "adk-enterprise-observability.svc.id.goog"}


@pytest.fixture
def mock_cluster_backend(monkeypatch: pytest.MonkeyPatch, isolated_env: Dict[str, Any]):
    """Mock provider creation, Kubernetes SDK list calls, and CLI execution."""
    kcfg_home = isolated_env["kubeconfig_home"]

    monkeypatch.setattr("app.build_provider", lambda cluster_cfg: MockGKEProvider(cluster_cfg, kcfg_home))

    sample_resources: Dict[str, List[Dict[str, Any]]] = {
        "nodes": [
            {
                "metadata": {
                    "name": "gke-kubeorbit-default-node-1",
                    "labels": {
                        "cloud.google.com/gke-nodepool": "default-pool",
                        "topology.kubernetes.io/zone": "us-central1-a",
                        "node.kubernetes.io/instance-type": "e2-medium",
                    },
                    "creationTimestamp": "2026-11-06T10:00:00Z",
                },
                "spec": {},
                "status": {
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "addresses": [{"type": "InternalIP", "address": "10.128.0.2"}],
                    "capacity": {"cpu": "2", "memory": "4096000Ki", "pods": "110"},
                    "nodeInfo": {"kubeletVersion": "v1.36.4", "osImage": "Container-Optimized OS"},
                },
            }
        ],
        "namespaces": [
            {"metadata": {"name": "kube-system", "creationTimestamp": "2026-11-06T10:00:00Z"}, "status": {"phase": "Active"}},
            {"metadata": {"name": "kubeorbit-demo", "creationTimestamp": "2026-11-06T10:05:00Z"}, "status": {"phase": "Active"}},
        ],
        "pods": [
            {
                "metadata": {
                    "name": "web-frontend-abc",
                    "namespace": "kubeorbit-demo",
                    "creationTimestamp": "2026-11-06T10:10:00Z",
                    "labels": {"app": "web-frontend", "app.kubernetes.io/part-of": "storefront"},
                    "ownerReferences": [{"kind": "ReplicaSet", "name": "web-frontend-rs", "controller": True}],
                },
                "spec": {
                    "nodeName": "gke-kubeorbit-default-node-1",
                    "containers": [
                        {
                            "name": "nginx",
                            "image": "nginx:1.27-alpine",
                            "resources": {
                                "requests": {"cpu": "20m", "memory": "32Mi"},
                                "limits": {"cpu": "100m", "memory": "128Mi"},
                            },
                            "livenessProbe": {"httpGet": {"path": "/", "port": 80}},
                            "readinessProbe": {"httpGet": {"path": "/", "port": 80}},
                        }
                    ],
                },
                "status": {
                    "phase": "Running",
                    "podIP": "10.4.0.12",
                    "qosClass": "Burstable",
                    "containerStatuses": [
                        {"name": "nginx", "image": "nginx:1.27-alpine", "ready": True, "restartCount": 0, "state": {"running": {}}}
                    ],
                },
            }
        ],
        "deployments": [
            {
                "metadata": {
                    "name": "web-frontend",
                    "namespace": "kubeorbit-demo",
                    "labels": {"app": "web-frontend", "app.kubernetes.io/part-of": "storefront"},
                },
                "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "web-frontend"}}},
                "status": {"readyReplicas": 1, "availableReplicas": 1, "updatedReplicas": 1},
            }
        ],
        "services": [
            {
                "metadata": {"name": "web-frontend-svc", "namespace": "kubeorbit-demo"},
                "spec": {
                    "type": "ClusterIP",
                    "clusterIP": "10.8.1.25",
                    "selector": {"app": "web-frontend"},
                    "ports": [{"port": 80, "targetPort": 80, "protocol": "TCP"}],
                },
                "status": {},
            }
        ],
        "configmaps": [
            {
                "metadata": {"name": "app-config", "namespace": "kubeorbit-demo"},
                "data": {"ENV": "sandbox", "LOG_LEVEL": "info"},
            }
        ],
        "secrets": [
            {
                "metadata": {"name": "app-credentials", "namespace": "kubeorbit-demo"},
                "type": "Opaque",
                "data": {"API_TOKEN": base64.b64encode(b"top-secret-token-123").decode("ascii")},
            }
        ],
    }

    def fake_sync_sdk_list(
        context: str, kubeconfig_path: Path, resource: str, namespace: Optional[str] = None
    ) -> Tuple[Optional[List[Dict[str, Any]]], bool]:
        items = sample_resources.get(resource, [])
        if namespace:
            items = [i for i in items if i.get("metadata", {}).get("namespace", namespace) == namespace]
        return items, True

    monkeypatch.setattr("kube_client._fetch._sync_sdk_list", fake_sync_sdk_list)

    async def fake_run_cli(cmd: List[str], env=None, timeout=None, check=True):
        cmd_str = " ".join(cmd)
        if "top pods" in cmd_str:
            if "-n" in cmd:
                return 0, "web-frontend-abc 5m 16Mi\n", ""
            return 0, "kubeorbit-demo web-frontend-abc 5m 16Mi\n", ""
        if "helm" in cmd[0]:
            return 0, "[]", ""
        if "rollout" in cmd and "undo" in cmd:
            return 0, "deployment.apps/web-frontend rolled back\n", ""
        if "get" in cmd and "secret" in cmd:
            import json
            return 0, json.dumps(sample_resources["secrets"][0]), ""
        if "logs" in cmd:
            return 0, "2026-11-06T12:00:00Z INFO server started on :80\n", ""
        if "-o" in cmd and "yaml" in cmd:
            return 0, "apiVersion: v1\nkind: Pod\nmetadata:\n  name: web-frontend-abc\n", ""
        return 0, "{}", ""

    monkeypatch.setattr("kube_client._cli.run_cli", fake_run_cli)
    monkeypatch.setattr("kube_client._fetch.run_cli", fake_run_cli)
    monkeypatch.setattr("kube_client.resources.run_cli", fake_run_cli)
    monkeypatch.setattr("kube_client.logs.run_cli", fake_run_cli)
    return sample_resources


# =========================================================================== #
# 1. End-to-End Cluster Onboarding, Editing, Switching & Deletion Flow
# =========================================================================== #


class TestClusterLifecycleIntegration:
    @pytest.mark.asyncio
    async def test_full_cluster_crud_and_edit_display_name(
        self, async_client: httpx.AsyncClient, mock_cluster_backend
    ):
        # 1. Health & Status
        h_res = await async_client.get("/api/health")
        assert h_res.status_code == 200
        assert h_res.json() == {"status": "ok"}

        st_res = await async_client.get("/api/status")
        assert st_res.status_code == 200
        assert st_res.json()["cluster_count"] == 2

        # 2. List clusters (verify `config` dict is returned for the Edit modal)
        list_res = await async_client.get("/api/admin/clusters")
        assert list_res.status_code == 200
        clusters = list_res.json()["clusters"]
        assert len(clusters) == 2
        assert clusters[0]["id"] == "obs-sbx"
        assert clusters[0]["config"]["cluster_name"] == "kubeorbit-gke"

        # 3. Onboard a new cluster
        new_payload = {
            "id": "local-kind-dev",
            "name": "gke_adk-enterprise-observability_us-central1-a_kubeorbit-gke",
            "provider": "local",
            "environment": "sandbox",
            "team_id": "platform",
            "team_name": "Platform Engineering",
            "config": {"kubeconfig_path": "~/.kube/config", "context_name": "kind-dev"},
        }
        post_res = await async_client.post("/api/admin/clusters", json=new_payload)
        assert post_res.status_code == 200

        # 4. Edit the newly added cluster (change display name + environment to nonprod)
        edit_payload = {
            **new_payload,
            "name": "Local GKE Context",
            "environment": "nonprod",
            "description": "Renamed friendly display name",
        }
        put_res = await async_client.put("/api/admin/clusters/local-kind-dev", json=edit_payload)
        assert put_res.status_code == 200

        list_after_edit = (await async_client.get("/api/admin/clusters")).json()["clusters"]
        edited = next(c for c in list_after_edit if c["id"] == "local-kind-dev")
        assert edited["name"] == "Local GKE Context"
        assert edited["environment"] == "nonprod"
        assert edited["description"] == "Renamed friendly display name"
        # Ensure no duplicate ID was created when moving sandbox -> nonprod
        assert sum(1 for c in list_after_edit if c["id"] == "local-kind-dev") == 1

        # 5. Switch active cluster
        sw_res = await async_client.post("/api/clusters/obs-sbx/switch")
        assert sw_res.status_code == 200
        assert sw_res.json()["context_name"] == "mock-gke-ctx"

        # 6. Delete cluster
        del_res = await async_client.delete("/api/admin/clusters/local-kind-dev")
        assert del_res.status_code == 200
        list_after_del = (await async_client.get("/api/admin/clusters")).json()["clusters"]
        assert all(c["id"] != "local-kind-dev" for c in list_after_del)


# =========================================================================== #
# 2. Workloads, Namespace Resources, Metrics, Secret Reveal & Rollback
# =========================================================================== #


class TestWorkloadsAndResourcesIntegration:
    @pytest.mark.asyncio
    async def test_cluster_info_nodes_and_namespaces(
        self, async_client: httpx.AsyncClient, mock_cluster_backend
    ):
        info_res = await async_client.get("/api/clusters/obs-sbx/info")
        assert info_res.status_code == 200
        info = info_res.json()
        assert info["meta"]["kubernetes_version"] == "1.36.4-gke.2046003"
        assert info["node_count"] == 1

        # Default namespaces call excludes kube-system (returns list of namespace strings)
        ns_res = await async_client.get("/api/clusters/obs-sbx/namespaces")
        assert ns_res.status_code == 200
        assert ns_res.json()["items"] == ["kubeorbit-demo"]

        # include_system=true includes kube-system
        ns_all = await async_client.get("/api/clusters/obs-sbx/namespaces?include_system=true")
        assert len(ns_all.json()["items"]) == 2

    @pytest.mark.asyncio
    async def test_namespace_resources_and_secret_reveal_isolation(
        self, async_client: httpx.AsyncClient, mock_cluster_backend
    ):
        res = await async_client.get("/api/clusters/obs-sbx/namespaces/kubeorbit-demo/resources")
        assert res.status_code == 200
        data = res.json()

        # Verify pod normalization + live metrics enrichment (5m / 100m = 5%, 16Mi / 128Mi = 12%)
        assert len(data["pods"]) == 1
        pod = data["pods"][0]
        assert pod["name"] == "web-frontend-abc"
        assert pod["resources"]["used_cpu_str"] == "5m"
        assert pod["resources"]["used_mem_str"] == "16Mi"
        assert pod["resources"]["cpu_pct"] == 5
        assert pod["resources"]["mem_pct"] == 12

        # Verify secret list only returns metadata keys, never plaintext or base64 values
        assert len(data["secrets"]) == 1
        sec_meta = data["secrets"][0]
        assert sec_meta["name"] == "app-credentials"
        assert sec_meta["keys"] == ["API_TOKEN"]
        assert "top-secret-token-123" not in str(sec_meta)

        # Explicit secret reveal decodes value and sets Cache-Control: no-store
        rev_res = await async_client.post(
            "/api/clusters/obs-sbx/namespaces/kubeorbit-demo/secrets/app-credentials/reveal"
        )
        assert rev_res.status_code == 200
        assert rev_res.headers.get("cache-control") == "no-store"
        rev_json = rev_res.json()
        decoded_map = rev_json.get("values") or rev_json.get("data") or {}
        assert decoded_map["API_TOKEN"] == "top-secret-token-123"

    @pytest.mark.asyncio
    async def test_applications_topology_and_rollback_audit_flow(
        self, async_client: httpx.AsyncClient, mock_cluster_backend, monkeypatch: pytest.MonkeyPatch
    ):
        # Logical applications grouping
        apps_res = await async_client.get("/api/clusters/obs-sbx/applications?namespace=kubeorbit-demo")
        assert apps_res.status_code == 200
        apps_payload = apps_res.json()
        apps = apps_payload.get("items") or apps_payload.get("applications") or []
        assert len(apps) >= 1
        assert apps[0]["name"] == "storefront"

        # Network topology
        net_res = await async_client.get("/api/clusters/obs-sbx/topology/network?namespace=kubeorbit-demo")
        assert net_res.status_code == 200
        assert len(net_res.json()["nodes"]) >= 2

        # Rollback workload & verify audit trail entry is created
        async def fake_rollout_undo(context, kubeconfig, namespace, workload_type, name, to_revision=None):
            return {"status": "ok", "message": f"Rolled back {workload_type}/{name} to {to_revision or 'previous'}"}

        monkeypatch.setattr("kube_client.rollout_undo", fake_rollout_undo)

        rb_res = await async_client.post(
            "/api/clusters/obs-sbx/workloads/deployment/web-frontend/rollback?namespace=kubeorbit-demo",
            json={"to_revision": 2},
        )
        assert rb_res.status_code == 200

        audit_res = await async_client.get("/api/audit/logs?cluster_id=obs-sbx")
        assert audit_res.status_code == 200
        logs = audit_res.json()["items"]
        assert len(logs) == 1
        assert logs[0]["action"] == "ROLLBACK_WORKLOAD"
        assert logs[0]["resource"] == "deployment/web-frontend"


# =========================================================================== #
# 3. Error Propagation & Input Validation Boundary Tests
# =========================================================================== #


class TestErrorPropagationIntegration:
    @pytest.mark.asyncio
    async def test_unknown_cluster_returns_404(self, async_client: httpx.AsyncClient):
        res = await async_client.get("/api/clusters/unknown-cluster-999/nodes")
        assert res.status_code == 404
        assert "unknown cluster" in res.json()["detail"]

    @pytest.mark.asyncio
    async def test_invalid_k8s_namespace_or_resource_name_returns_422(
        self, async_client: httpx.AsyncClient, mock_cluster_backend
    ):
        # Uppercase or path traversal in namespace segment -> 422 Unprocessable Entity
        res1 = await async_client.get("/api/clusters/obs-sbx/namespaces/INVALID_NS/resources")
        assert res1.status_code == 422

        # Namespace exceeding 63 characters -> 422
        long_ns = "a" * 64
        res2 = await async_client.get(f"/api/clusters/obs-sbx/namespaces/{long_ns}/resources")
        assert res2.status_code == 422

    @pytest.mark.asyncio
    async def test_provider_and_cli_error_status_codes(
        self, async_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        # 1. ProviderConfigError -> 400 Bad Request
        class BadConfigProvider(BaseKubeProvider):
            async def authenticate(self):
                raise ProviderConfigError("GKE cluster is missing 'project'")

            async def get_cluster_metadata(self):
                pass

            def get_console_url(self):
                return None

            def get_workload_identity_info(self):
                return {}

        monkeypatch.setattr("app.build_provider", lambda cfg: BadConfigProvider(cfg, Path("dummy.yaml")))
        res_400 = await async_client.get("/api/clusters/obs-sbx/nodes")
        assert res_400.status_code == 400
        assert res_400.json()["error"] == "ProviderConfigError"

        # 2. ProviderAuthError -> 424 Failed Dependency
        class BadAuthProvider(BaseKubeProvider):
            async def authenticate(self):
                raise ProviderAuthError("gcloud credentials expired")

            async def get_cluster_metadata(self):
                pass

            def get_console_url(self):
                return None

            def get_workload_identity_info(self):
                return {}

        monkeypatch.setattr("app.build_provider", lambda cfg: BadAuthProvider(cfg, Path("dummy.yaml")))
        res_424 = await async_client.get("/api/clusters/obs-sbx/nodes")
        assert res_424.status_code == 424
        assert res_424.json()["error"] == "ProviderAuthError"
