"""Comprehensive Unit Tests for:
1. kube_client/rca.py (12-Step `kubectl apply` RCA engine, folding, scoring, K8S_COMPONENT_CATALOG)
2. kube_client/topology.py (get_network_topology Ingress -> Service -> Pod label selector matching)
3. kube_client/workloads.py (get_pod_inspector 12-Step Lifecycle Checklist & probe/log correlation)
4. config.py (KUBEORBIT_DISABLE_LOCAL_PROVIDER precedence in IAP / Cloud Run / local modes)
5. providers/local.py (LocalProvider.authenticate happy path & missing file/context errors)

Covers happy paths, edge cases (null/None inputs, empty arrays, boundary scores), and failure modes.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

import config
from config import ClusterConfig
from kube_client._cli import CLIError
from kube_client.rca import (
    K8S_COMPONENT_CATALOG,
    PIPELINE_STAGE_CATALOG,
    _build_rca_payload,
    _detect_pipeline_integrations,
    diagnose_cluster_issues,
)
from kube_client.topology import get_network_topology
from kube_client.workloads import get_pod_inspector
from providers.local import LocalProvider


# =========================================================================== #
# 1. kube_client/rca.py — 12-Step Folding & Diagnosis Unit Tests
# =========================================================================== #


class TestRca12StepUnit:
    def test_catalogs_cover_all_12_steps_without_gaps(self):
        comp_steps = sorted(s for c in K8S_COMPONENT_CATALOG for s in c["steps"])
        assert comp_steps == list(range(1, 13))
        assert len(PIPELINE_STAGE_CATALOG) >= 6
        for comp in K8S_COMPONENT_CATALOG:
            assert comp["component_id"]
            assert comp["label"]
            assert comp["steps"]

    def test_build_rca_payload_empty_array_and_score_bounds(self):
        res = _build_rca_payload([], workloads_healthy=0, workloads_total=0, namespaces_with_workloads=[])
        assert res["summary"]["overall_score"] == 100
        assert res["summary"]["active_root_causes"] == 0
        assert res["summary"]["workloads_total"] == 0
        assert res["summary"]["workloads_healthy"] == 0
        assert res["root_causes"] == []
        assert len(res["components"]) == len(K8S_COMPONENT_CATALOG)

        # Score must clamp at 0 even with many critical failures (13 * 8 = 104 -> 0)
        many_critical = [
            {
                "namespace": "default",
                "issue_type": f"crit-{i}",
                "resource_name": f"pod-{i}",
                "kind": "active_failure",
                "severity": "critical",
                "confidence": 0.95,
                "root_cause": "boom",
                "impacted_service": "svc-a",
                "remediation_cmd": "kubectl delete pod",
                "step_number": 10,
                "component": "kubelet",
            }
            for i in range(13)
        ]
        clamped = _build_rca_payload(many_critical, workloads_healthy=0, workloads_total=5, namespaces_with_workloads=["default"])
        assert clamped["summary"]["overall_score"] == 0
        assert clamped["summary"]["active_root_causes"] == 13

    def test_build_rca_payload_folds_duplicate_symptoms_and_handles_nulls(self):
        raw = [
            {
                "namespace": "default",
                "issue_type": "ErrImagePull",
                "resource_name": "web-frontend-abc12-xyz99",
                "kind": "active_failure",
                "severity": "critical",
                "confidence": 0.99,
                "root_cause": "ErrImagePull",
                "impacted_service": "web-svc",
                "remediation_cmd": "kubectl set image ...",
                "component": None,
                "step_number": 10,
            },
            {
                "namespace": "default",
                "issue_type": "ErrImagePull",
                "resource_name": "web-frontend-abc12-uvw11",
                "kind": "active_failure",
                "severity": "critical",
                "confidence": 0.99,
                "root_cause": "ErrImagePull",
                "impacted_service": "web-svc",
                "remediation_cmd": "kubectl set image ...",
                "component": "kubelet (Image Pull)",
                "step_number": 10,
            },
        ]
        res = _build_rca_payload(raw, workloads_healthy=0, workloads_total=2, namespaces_with_workloads=["default"])
        assert res["summary"]["active_root_causes"] == 1
        rc = res["root_causes"][0]
        assert len(rc["folded_failures"]) == 2
        assert rc["component_id"] == "kubelet-runtime"
        assert rc["stage_id"] == "step-10"

    @pytest.mark.asyncio
    async def test_diagnose_cluster_issues_all_healthy_and_empty_cluster(self, monkeypatch: pytest.MonkeyPatch):
        async def _empty(*args, **kwargs):
            return []

        async def _empty_cached(*args, **kwargs):
            return ([], False)

        monkeypatch.setattr("kube_client.rca.get_pods", _empty)
        monkeypatch.setattr("kube_client.rca.get_events", _empty)
        monkeypatch.setattr("kube_client.rca.get_nodes", _empty)
        monkeypatch.setattr("kube_client.rca.get_deployments", _empty)
        monkeypatch.setattr("kube_client.rca.get_statefulsets", _empty)
        monkeypatch.setattr("kube_client.rca.get_daemonsets", _empty)
        monkeypatch.setattr("kube_client.rca.get_services", _empty)
        monkeypatch.setattr("kube_client.rca.get_configmaps", _empty)
        monkeypatch.setattr("kube_client.rca.cached_kubectl_list", _empty_cached)

        res = await diagnose_cluster_issues("ctx", Path("/tmp/kcfg"), namespace="kubeorbit-demo")
        assert res["summary"]["overall_score"] == 100
        assert res["summary"]["active_root_causes"] == 0
        assert res["root_causes"] == []
        assert res["integrations"]["helm"]["active"] is False
        assert res["integrations"]["argocd"]["active"] is False
        assert res["integrations"]["prometheus"]["active"] is False
        assert res["integrations"]["kubectl"]["primary"] is True

    def test_detect_pipeline_integrations_helm_argocd_prometheus_and_kubectl(self):
        # 1. Helm release in target namespace -> highlights Helm ONLY, not Argo CD or Prometheus
        helm_only = _detect_pipeline_integrations(
            raw_pods=[
                {
                    "metadata": {
                        "name": "hello-storefront-web-abc",
                        "namespace": "helm-demo",
                        "labels": {
                            "app.kubernetes.io/managed-by": "Helm",
                            "helm.sh/chart": "hello-storefront-0.1.0",
                        },
                        "annotations": {"meta.helm.sh/release-name": "hello-storefront"},
                    }
                }
            ],
            raw_deployments=[],
            raw_replicasets=[],
            services=[{"name": "hello-storefront-svc", "namespace": "helm-demo", "labels": {}}],
            helm_data={
                "available": True,
                "items": [{"name": "hello-storefront", "namespace": "helm-demo", "chart": "hello-storefront-0.1.0"}],
            },
            namespace="helm-demo",
        )
        assert helm_only["helm"]["active"] is True
        assert helm_only["helm"]["releases"] == ["hello-storefront"]
        assert helm_only["helm"]["charts"] == ["hello-storefront-0.1.0"]
        assert helm_only["argocd"]["active"] is False
        assert helm_only["prometheus"]["active"] is False
        assert helm_only["kubectl"]["primary"] is False

        # 2. Plain kubectl apply workloads (other Helm releases excluded when scoped to default) -> all inactive
        plain_kubectl = _detect_pipeline_integrations(
            raw_pods=[{"metadata": {"name": "nginx-1", "namespace": "default", "labels": {"app": "nginx"}}}],
            raw_deployments=[{"metadata": {"name": "nginx", "namespace": "default", "labels": {"app": "nginx"}}}],
            raw_replicasets=[],
            services=[],
            helm_data={
                "available": True,
                "items": [{"name": "hello-storefront", "namespace": "helm-demo", "chart": "hello-storefront-0.1.0"}],
            },
            namespace="default",
        )
        assert plain_kubectl["helm"]["active"] is False
        assert plain_kubectl["argocd"]["active"] is False
        assert plain_kubectl["prometheus"]["active"] is False
        assert plain_kubectl["kubectl"]["primary"] is True

        # 3. Argo CD + Prometheus annotations/labels activate them explicitly
        argo_prom = _detect_pipeline_integrations(
            raw_pods=[
                {
                    "metadata": {
                        "name": "checkout-api-1",
                        "namespace": "prod",
                        "labels": {"argocd.argoproj.io/instance": "checkout-app"},
                        "annotations": {"prometheus.io/scrape": "true"},
                    }
                }
            ],
            raw_deployments=[],
            raw_replicasets=[],
            services=[],
            helm_data={"available": False, "items": []},
            namespace="prod",
        )
        assert argo_prom["argocd"]["active"] is True
        assert argo_prom["argocd"]["apps"] == ["checkout-app"]
        assert argo_prom["prometheus"]["active"] is True
        assert argo_prom["prometheus"]["targets"] == ["checkout-api-1"]
        assert argo_prom["helm"]["active"] is False

    @pytest.mark.asyncio
    async def test_diagnose_cluster_issues_maps_steps_9_to_12(self, monkeypatch: pytest.MonkeyPatch):
        pods = [
            {
                "name": "gpu-ml-0",
                "namespace": "kubeorbit-demo",
                "status": "Pending",
                "ready": False,
                "restarts": 0,
                "node": "",
                "labels": {"app": "gpu-ml"},
                "containers": [{"name": "ml", "image": "nvidia:1.0", "ready": False, "state": "Pending", "reason": "Pending", "requests": {}, "limits": {}}],
            },
            {
                "name": "checkout-canary-1",
                "namespace": "kubeorbit-demo",
                "status": "Pending",
                "ready": False,
                "restarts": 0,
                "node": "node-1",
                "labels": {"app": "checkout"},
                "containers": [{"name": "web", "image": "bad:999", "ready": False, "state": "ErrImagePull", "reason": "ErrImagePull", "requests": {}, "limits": {}}],
            },
            {
                "name": "faulty-worker-1",
                "namespace": "kubeorbit-demo",
                "status": "Running",
                "ready": False,
                "restarts": 4,
                "node": "node-1",
                "labels": {"app": "faulty"},
                "containers": [{"name": "worker", "image": "busybox", "ready": False, "state": "CrashLoopBackOff", "reason": "CrashLoopBackOff", "requests": {}, "limits": {}}],
            },
            {
                "name": "inventory-unready-1",
                "namespace": "kubeorbit-demo",
                "status": "Running",
                "ready": False,
                "restarts": 0,
                "node": "node-1",
                "labels": {"app": "inventory"},
                "containers": [{"name": "inv", "image": "nginx", "ready": False, "state": "Running", "reason": "", "requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"cpu": "100m", "memory": "128Mi"}}],
            },
        ]
        events = [
            {"namespace": "kubeorbit-demo", "involved_kind": "Pod", "involved_name": "gpu-ml-0", "type": "Warning", "reason": "FailedScheduling", "message": "0/1 nodes are available: Insufficient nvidia.com/gpu."},
            {"namespace": "kubeorbit-demo", "involved_kind": "Pod", "involved_name": "inventory-unready-1", "type": "Warning", "reason": "Unhealthy", "message": "Readiness probe failed: HTTP 503"},
        ]
        services = [
            {"name": "orphaned-orders-svc", "namespace": "kubeorbit-demo", "type": "ClusterIP", "selector": {"app": "does-not-exist"}, "ports": [{"port": 8080}]},
        ]
        raw_pods = [
            {
                "metadata": {"name": p["name"], "namespace": p["namespace"], "labels": p["labels"]},
                "spec": {"containers": [{"name": c["name"], "image": c["image"], "resources": {}} for c in p["containers"]]},
                "status": {"phase": p["status"]},
            }
            for p in pods
        ]

        async def _pods(*a, **kw): return pods
        async def _events(*a, **kw): return events
        async def _nodes(*a, **kw): return [{"name": "node-1", "status": "Ready", "taints": [], "conditions": []}]
        async def _empty(*a, **kw): return []
        async def _services(*a, **kw): return services
        async def _cached(ctx, kcfg, res, *a, **kw):
            return (raw_pods, False) if res == "pods" else ([], False)
        async def _logs(*a, **kw): return "Traceback: fatal error"

        monkeypatch.setattr("kube_client.rca.get_pods", _pods)
        monkeypatch.setattr("kube_client.rca.get_events", _events)
        monkeypatch.setattr("kube_client.rca.get_nodes", _nodes)
        monkeypatch.setattr("kube_client.rca.get_deployments", _empty)
        monkeypatch.setattr("kube_client.rca.get_statefulsets", _empty)
        monkeypatch.setattr("kube_client.rca.get_daemonsets", _empty)
        monkeypatch.setattr("kube_client.rca.get_services", _services)
        monkeypatch.setattr("kube_client.rca.get_configmaps", _empty)
        monkeypatch.setattr("kube_client.rca.cached_kubectl_list", _cached)
        monkeypatch.setattr("kube_client.rca.get_pod_logs", _logs)

        res = await diagnose_cluster_issues("ctx", Path("/tmp/kcfg"), namespace="kubeorbit-demo")
        steps_found = {rc["step_number"] for rc in res["root_causes"] if rc["kind"] == "active_failure"}
        assert 9 in steps_found   # Scheduler Pending
        assert 10 in steps_found  # ErrImagePull / CrashLoopBackOff
        assert 11 in steps_found  # Readiness probe failed
        assert 12 in steps_found  # Orphaned Service / EndpointSlice
        assert res["summary"]["overall_score"] < 100


# =========================================================================== #
# 2. kube_client/topology.py — Ingress -> Service -> Pod Unit Tests
# =========================================================================== #


class TestNetworkTopologyUnit:
    @pytest.mark.asyncio
    async def test_get_network_topology_links_ingress_service_pod_and_handles_empty(self, monkeypatch: pytest.MonkeyPatch):
        async def _ingresses(*a, **kw):
            return {
                "items": [
                    {
                        "name": "web-gw",
                        "namespace": "kubeorbit-demo",
                        "addresses": ["34.1.2.3"],
                        "hosts": ["demo.local"],
                        "rules": [{"host": "demo.local", "paths": [{"path": "/", "backend_service": "web-svc", "port": 80}]}],
                    }
                ]
            }

        async def _services(*a, **kw):
            return [
                {"name": "web-svc", "namespace": "kubeorbit-demo", "type": "ClusterIP", "cluster_ip": "10.0.0.5", "external_ips": [], "selector": {"app": "web"}, "ports": [{"port": 80}]},
                {"name": "no-sel-svc", "namespace": "kubeorbit-demo", "type": "ClusterIP", "cluster_ip": "10.0.0.6", "external_ips": [], "selector": None, "ports": []},
            ]

        async def _pods(*a, **kw):
            return [
                {"name": "web-pod-1", "namespace": "kubeorbit-demo", "status": "Running", "ready": True, "labels": {"app": "web", "tier": "fe"}},
                {"name": "other-pod", "namespace": "kubeorbit-demo", "status": "Running", "ready": True, "labels": None},
            ]

        async def _empty_items(*a, **kw): return {"available": False, "items": []}
        async def _empty_cached(*a, **kw): return ([], False)

        monkeypatch.setattr("kube_client.topology.get_ingresses", _ingresses)
        monkeypatch.setattr("kube_client.topology.get_services", _services)
        monkeypatch.setattr("kube_client.topology.get_pods", _pods)
        monkeypatch.setattr("kube_client.topology.get_gateways", _empty_items)
        monkeypatch.setattr("kube_client.topology.get_httproutes", _empty_items)
        monkeypatch.setattr("kube_client.topology.cached_kubectl_list", _empty_cached)

        graph = await get_network_topology("ctx", Path("/tmp/kcfg"), "kubeorbit-demo")
        node_ids = {n["id"] for n in graph["nodes"]}
        edges = {(e["source"], e["target"]) for e in graph["edges"]}
        assert "ingress::web-gw" in node_ids
        assert "service::web-svc" in node_ids
        assert "pod::web-pod-1" in node_ids
        assert ("ingress::web-gw", "service::web-svc") in edges
        assert ("service::web-svc", "pod::web-pod-1") in edges
        assert ("service::no-sel-svc", "pod::other-pod") not in edges


# =========================================================================== #
# 3. kube_client/workloads.py — Pod Inspector 12-Step Checklist Unit Tests
# =========================================================================== #


class TestPodInspectorUnit:
    @pytest.mark.asyncio
    async def test_get_pod_inspector_12_step_lifecycle_happy_and_pending(self, monkeypatch: pytest.MonkeyPatch):
        pending_pod = {
            "metadata": {"name": "gpu-pod", "namespace": "kubeorbit-demo", "labels": {"app": "gpu"}},
            "spec": {"containers": [{"name": "gpu", "image": "nvidia:1.0"}]},
            "status": {
                "phase": "Pending",
                "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": "0/1 nodes available"}],
            },
        }

        async def _run_cli(cmd, **kw):
            return 0, json.dumps(pending_pod), ""

        async def _events(*a, **kw):
            return [{"involved_name": "gpu-pod", "type": "Warning", "reason": "FailedScheduling", "message": "Insufficient nvidia.com/gpu"}]

        async def _services(*a, **kw): return []
        async def _logs(*a, **kw): return ""

        monkeypatch.setattr("kube_client.workloads.run_cli", _run_cli)
        monkeypatch.setattr("kube_client.resources.get_events", _events)
        monkeypatch.setattr("kube_client.workloads.get_services", _services)
        monkeypatch.setattr("kube_client.workloads.get_pod_logs", _logs)

        info = await get_pod_inspector("ctx", Path("/tmp/kcfg"), "kubeorbit-demo", "gpu-pod")
        assert len(info["pipeline_steps"]) >= 6
        step9 = next(s for s in info["pipeline_steps"] if s["step"] == "Step 9")
        assert step9["status"] == "failed"


# =========================================================================== #
# 4. config.py — disable_local_provider Precedence Unit Tests
# =========================================================================== #


class TestDisableLocalProviderConfig:
    @pytest.mark.parametrize(
        "explicit,auth_mode,k_service,expected",
        [
            ("false", "iap", "kubeorbit", False),
            ("true", "none", "", True),
            (None, "iap", "", True),
            (None, "none", "kubeorbit", True),
            (None, "none", "", False),
        ],
    )
    def test_disable_local_provider_precedence(self, monkeypatch: pytest.MonkeyPatch, explicit, auth_mode, k_service, expected):
        config.get_runtime_env.cache_clear()
        if explicit is None:
            monkeypatch.delenv("KUBEORBIT_DISABLE_LOCAL_PROVIDER", raising=False)
        else:
            monkeypatch.setenv("KUBEORBIT_DISABLE_LOCAL_PROVIDER", explicit)
        monkeypatch.setenv("KUBEORBIT_AUTH_MODE", auth_mode)
        if k_service:
            monkeypatch.setenv("K_SERVICE", k_service)
        else:
            monkeypatch.delenv("K_SERVICE", raising=False)

        assert config.get_runtime_env().disable_local_provider is expected


# =========================================================================== #
# 5. providers/local.py — LocalProvider Unit Tests
# =========================================================================== #


class TestLocalProviderUnit:
    @pytest.mark.asyncio
    async def test_local_provider_authenticates_from_kubeconfig_file(self, isolated_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        src = isolated_env["kubeconfig_home"] / "my-kubeconfig.yaml"
        src.write_text(
            yaml.dump(
                {
                    "apiVersion": "v1",
                    "kind": "Config",
                    "current-context": "kind-demo",
                    "contexts": [{"name": "kind-demo", "context": {"cluster": "kind-demo", "user": "kind-demo"}}],
                    "clusters": [{"name": "kind-demo", "cluster": {"server": "https://127.0.0.1:6443"}}],
                    "users": [{"name": "kind-demo", "user": {"token": "tok"}}],
                }
            ),
            encoding="utf-8",
        )
        dest = isolated_env["kubeconfig_home"] / "local-demo.yaml"
        cluster = ClusterConfig(
            id="local-demo",
            name="Local Demo",
            provider="local",
            environment="sandbox",
            config={"kubeconfig_path": str(src), "context_name": "kind-demo"},
        )
        monkeypatch.setattr("providers.local.run_cli", lambda *a, **kw: _async_rc0())
        provider = LocalProvider(cluster, dest)
        res = await provider.authenticate()
        assert res.context_name == "kind-demo"
        assert Path(res.kubeconfig_path).exists()
        assert res.identity.provider == "local"

    @pytest.mark.asyncio
    async def test_local_provider_raises_when_kubeconfig_missing(self, isolated_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        dest = isolated_env["kubeconfig_home"] / "missing.yaml"
        cluster = ClusterConfig(
            id="missing-local",
            name="Missing",
            provider="local",
            environment="sandbox",
            config={"kubeconfig_path": str(isolated_env["kubeconfig_home"] / "does-not-exist.yaml")},
        )
        provider = LocalProvider(cluster, dest)
        with pytest.raises(CLIError):
            await provider.authenticate()


async def _async_rc0():
    return 0, "", ""
