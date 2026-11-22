"""Integration Tests for the 12-Step `kubectl apply` RCA, Pod Inspector,
Network Topology, and Local Kubeconfig Upload/Onboarding end-to-end flows.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest
import yaml

from app import app
from providers.base import BaseKubeProvider, ClusterAuthResult, ClusterMeta, ProviderIdentity


class _StubProvider(BaseKubeProvider):
    def __init__(self, cluster, kubeconfig_home: Path):
        self._kcfg = kubeconfig_home / f"{cluster.id}.yaml"
        super().__init__(cluster, self._kcfg)

    async def authenticate(self, user=None) -> ClusterAuthResult:
        self._kcfg.write_text("apiVersion: v1\ncurrent-context: stub-ctx\n", encoding="utf-8")
        return ClusterAuthResult(
            context_name="stub-ctx",
            kubeconfig_path=str(self._kcfg),
            authenticated_at="2026-11-22T00:00:00Z",
            identity=ProviderIdentity(
                provider=self.cluster.provider,
                account="tester@example.com",
                project_or_subscription="adk-enterprise-observability",
            ),
        )

    async def get_cluster_metadata(self) -> ClusterMeta:
        return ClusterMeta(
            provider=self.cluster.provider,
            cluster_id=self.cluster.id,
            name=self.cluster.name,
            kubernetes_version="1.36.4",
            location="us-central1-a",
            status="RUNNING",
            node_pools=[],
        )

    def get_console_url(self) -> str | None:
        return None

    def get_workload_identity_info(self) -> Dict[str, Any]:
        return {"enabled": True}


@pytest.fixture
def mocked_12step_env(monkeypatch: pytest.MonkeyPatch, isolated_env: Dict[str, Any]):
    kcfg_home = isolated_env["kubeconfig_home"]
    monkeypatch.setattr("app.build_provider", lambda cfg: _StubProvider(cfg, kcfg_home))

    sample_pods = [
        {
            "metadata": {
                "name": "faulty-worker-abc",
                "namespace": "kubeorbit-demo",
                "labels": {"app": "faulty-worker"},
                "ownerReferences": [{"kind": "ReplicaSet", "name": "faulty-worker-rs", "controller": True}],
            },
            "spec": {"nodeName": "node-1", "containers": [{"name": "worker", "image": "busybox", "resources": {}}]},
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {
                        "name": "worker",
                        "ready": False,
                        "restartCount": 5,
                        "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "Back-off 40s restarting"}},
                    }
                ],
            },
        },
        {
            "metadata": {
                "name": "web-frontend-123",
                "namespace": "kubeorbit-demo",
                "labels": {"app": "web-frontend"},
            },
            "spec": {"nodeName": "node-1", "containers": [{"name": "web", "image": "nginx:1.25", "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"cpu": "100m", "memory": "128Mi"}}}]},
            "status": {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [{"name": "web", "ready": True, "restartCount": 0, "state": {"running": {}}}],
            },
        },
    ]
    sample_services = [
        {
            "metadata": {"name": "web-frontend-svc", "namespace": "kubeorbit-demo"},
            "spec": {"type": "ClusterIP", "clusterIP": "10.36.0.10", "selector": {"app": "web-frontend"}, "ports": [{"port": 80, "targetPort": 80}]},
        },
        {
            "metadata": {"name": "orphaned-orders-svc", "namespace": "kubeorbit-demo"},
            "spec": {"type": "ClusterIP", "clusterIP": "10.36.0.99", "selector": {"app": "missing-app"}, "ports": [{"port": 8080}]},
        },
    ]
    sample_ingresses = [
        {
            "metadata": {"name": "kubeorbit-gateway", "namespace": "kubeorbit-demo"},
            "spec": {
                "rules": [
                    {
                        "host": "demo.kubeorbit.local",
                        "http": {"paths": [{"path": "/", "backend": {"service": {"name": "web-frontend-svc", "port": {"number": 80}}}}]},
                    }
                ]
            },
        }
    ]
    sample_nodes = [
        {
            "metadata": {"name": "node-1", "labels": {}},
            "spec": {},
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "addresses": [{"type": "InternalIP", "address": "10.0.0.1"}],
                "capacity": {"cpu": "2", "memory": "4096Mi", "pods": "110"},
                "nodeInfo": {"kubeletVersion": "v1.36.4"},
            },
        }
    ]

    resource_map = {
        "pods": sample_pods,
        "services": sample_services,
        "ingresses": sample_ingresses,
        "ingresses.networking.k8s.io": sample_ingresses,
        "nodes": sample_nodes,
        "events": [],
        "deployments": [],
        "statefulsets": [],
        "daemonsets": [],
        "configmaps": [],
        "replicasets": [],
        "gateways": [],
        "httproutes": [],
        "networkpolicies": [],
    }

    async def _fake_list(ctx, kcfg, resource, namespace="", ttl=15, optional=False):
        items = resource_map.get(resource, [])
        if namespace:
            items = [i for i in items if (i.get("metadata") or {}).get("namespace", "") == namespace or resource == "nodes"]
        return items, False

    async def _fake_run_cli(cmd, **kw):
        if "get" in cmd and "pod" in cmd:
            return 0, json.dumps(sample_pods[0]), ""
        if "logs" in cmd:
            return 0, "Crash error line", ""
        return 0, "{}", ""

    monkeypatch.setattr("kube_client._fetch.cached_kubectl_list", _fake_list)
    monkeypatch.setattr("kube_client.resources.cached_kubectl_list", _fake_list)
    monkeypatch.setattr("kube_client.rca.cached_kubectl_list", _fake_list)
    monkeypatch.setattr("kube_client.topology.cached_kubectl_list", _fake_list)
    monkeypatch.setattr("kube_client.workloads.run_cli", _fake_run_cli)
    monkeypatch.setattr("kube_client.logs.run_cli", _fake_run_cli)
    monkeypatch.setattr("kube_client.resources.run_cli", _fake_run_cli)
    return isolated_env


@pytest.mark.asyncio
async def test_12_step_rca_pod_inspector_and_network_topology_end_to_end(mocked_12step_env):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. 12-Step RCA endpoint
        rca_resp = await client.get("/api/clusters/obs-sbx/diagnostics/rca", params={"namespace": "kubeorbit-demo"})
        assert rca_resp.status_code == 200
        rca = rca_resp.json()
        assert len(rca["components"]) == 8
        assert len(rca["pipeline_stages"]) >= 6
        steps = {rc["step_number"] for rc in rca["root_causes"]}
        assert 10 in steps  # faulty-worker CrashLoopBackOff
        assert 12 in steps  # orphaned-orders-svc EndpointSlice

        # 2. Pod Inspector 12-Step Lifecycle Checklist
        insp_resp = await client.get("/api/clusters/obs-sbx/namespaces/kubeorbit-demo/pods/faulty-worker-abc/details")
        assert insp_resp.status_code == 200
        insp = insp_resp.json()
        assert len(insp["pipeline_steps"]) >= 6
        assert any(s["step"] == "Step 10c/d" and s["status"] == "failed" for s in insp["pipeline_steps"])

        # 3. Network Topology Ingress -> Service -> Pod
        topo_resp = await client.get("/api/clusters/obs-sbx/topology/network", params={"namespace": "kubeorbit-demo"})
        assert topo_resp.status_code == 200
        topo = topo_resp.json()
        edges = {(e["source"], e["target"]) for e in topo["edges"]}
        assert ("ingress::kubeorbit-gateway", "service::web-frontend-svc") in edges
        assert ("service::web-frontend-svc", "pod::web-frontend-123") in edges


@pytest.mark.asyncio
async def test_local_kubeconfig_upload_parse_and_onboard_end_to_end(monkeypatch: pytest.MonkeyPatch, isolated_env):
    import config

    config.get_runtime_env.cache_clear()
    monkeypatch.setenv("KUBEORBIT_DISABLE_LOCAL_PROVIDER", "false")
    monkeypatch.setenv("KUBEORBIT_AUTH_MODE", "none")

    kcfg_yaml = yaml.dump(
        {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "docker-desktop",
            "contexts": [{"name": "docker-desktop", "context": {"cluster": "docker-desktop", "user": "docker-desktop"}}],
            "clusters": [{"name": "docker-desktop", "cluster": {"server": "https://34.120.10.5:6443"}}],
            "users": [{"name": "docker-desktop", "user": {"token": "abc"}}],
        }
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Upload + parse kubeconfig content
        parse_resp = await client.post(
            "/api/admin/clusters/parse-kubeconfig",
            json={"kubeconfig_path": "my-local.yaml", "kubeconfig_content": kcfg_yaml, "filename": "my-local.yaml"},
        )
        assert parse_resp.status_code == 200
        parsed = parse_resp.json()
        assert parsed["current_context"] == "docker-desktop"
        assert parsed["contexts"][0]["name"] == "docker-desktop"
        assert Path(parsed["kubeconfig_path"]).exists()

        # 2. Onboard the local cluster using the returned path
        add_resp = await client.post(
            "/api/admin/clusters",
            json={
                "id": "local-desktop",
                "name": "Local Desktop",
                "provider": "local",
                "environment": "sandbox",
                "config": {"kubeconfig_path": parsed["kubeconfig_path"], "context_name": "docker-desktop"},
            },
        )
        assert add_resp.status_code == 200
        assert add_resp.json()["status"] == "ok"

        # 3. Invalid YAML upload returns 400
        bad_resp = await client.post(
            "/api/admin/clusters/parse-kubeconfig",
            json={"kubeconfig_path": "bad.yaml", "kubeconfig_content": "just-a-string", "filename": "bad.yaml"},
        )
        assert bad_resp.status_code == 400

        # 4. Path outside jail returns 403; missing file inside jail returns 404
        outside_resp = await client.post(
            "/api/admin/clusters/parse-kubeconfig",
            json={"kubeconfig_path": "/nonexistent/path/kubeconfig.yaml"},
        )
        assert outside_resp.status_code == 403

        missing_resp = await client.post(
            "/api/admin/clusters/parse-kubeconfig",
            json={"kubeconfig_path": str(isolated_env["kubeconfig_home"] / "missing-kubeconfig.yaml")},
        )
        assert missing_resp.status_code == 404

        # 5. Disabled local provider returns 403
        config.get_runtime_env.cache_clear()
        monkeypatch.setenv("KUBEORBIT_DISABLE_LOCAL_PROVIDER", "true")
        blocked_resp = await client.post(
            "/api/admin/clusters/parse-kubeconfig",
            json={"kubeconfig_path": "~/.kube/config"},
        )
        assert blocked_resp.status_code == 403
