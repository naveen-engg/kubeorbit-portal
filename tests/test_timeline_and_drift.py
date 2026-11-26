"""Unit, integration, RBAC, and MCP tests for Workload Change Timeline & Cross-Cluster Drift Diff."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest
import yaml

import config
from kube_client import CLIError
from kube_client.drift import compute_workload_drift
from kube_client.timeline import build_workload_timeline, get_workload_timeline
from providers.base import BaseKubeProvider, ClusterAuthResult, ClusterMeta, ProviderIdentity


class _StubProvider(BaseKubeProvider):
    def __init__(self, cluster, kubeconfig_home: Path):
        self._kcfg = kubeconfig_home / f"{cluster.id}.yaml"
        super().__init__(cluster, self._kcfg)

    async def authenticate(self, user=None) -> ClusterAuthResult:
        self._kcfg.write_text(f"apiVersion: v1\ncurrent-context: ctx-{self.cluster.id}\n", encoding="utf-8")
        return ClusterAuthResult(
            context_name=f"ctx-{self.cluster.id}",
            kubeconfig_path=str(self._kcfg),
            authenticated_at="2026-11-26T00:00:00Z",
            identity=ProviderIdentity(
                provider=self.cluster.provider,
                account="tester@example.com",
                project_or_subscription="test-proj",
            ),
        )

    async def get_cluster_metadata(self) -> ClusterMeta:
        return ClusterMeta(
            provider=self.cluster.provider,
            cluster_id=self.cluster.id,
            name=self.cluster.name,
            kubernetes_version="1.32.2",
            location="us-east-1",
            status="RUNNING",
            node_pools=[],
        )

    def get_console_url(self) -> str | None:
        return None

    def get_workload_identity_info(self) -> Dict[str, Any]:
        return {"enabled": True}


def _deployment(
    name: str,
    ns: str,
    *,
    replicas: int = 2,
    image: str = "ghcr.io/org/api:v1.0.0",
    cpu_req: str = "100m",
    mem_lim: str = "256Mi",
    db_pass_val: str = "staging-secret-123",
    probe_path: str = "/healthz",
) -> Dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "namespace": ns,
            "labels": {"app": name, "app.kubernetes.io/instance": "checkout-rel"},
            "annotations": {"meta.helm.sh/release-name": "checkout-rel"},
        },
        "spec": {
            "replicas": replicas,
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {
                    "containers": [
                        {
                            "name": "api",
                            "image": image,
                            "resources": {
                                "requests": {"cpu": cpu_req, "memory": "128Mi"},
                                "limits": {"cpu": "500m", "memory": mem_lim},
                            },
                            "env": [
                                {"name": "LOG_LEVEL", "value": "info"},
                                {"name": "DB_PASSWORD", "value": db_pass_val},
                                {
                                    "name": "JWT_KEY",
                                    "valueFrom": {"secretKeyRef": {"name": "api-secret", "key": "jwt"}},
                                },
                            ],
                            "envFrom": [{"configMapRef": {"name": "api-cfg"}}],
                            "readinessProbe": {
                                "httpGet": {"path": probe_path, "port": 8080},
                                "initialDelaySeconds": 5,
                                "periodSeconds": 10,
                            },
                        }
                    ]
                },
            },
        },
    }


class TestTimelineAndDriftUnit:
    def test_timeline_merges_chronologically_and_omits_secret_values(self):
        deploy = _deployment("checkout-api", "prod")
        rs_1 = {
            "metadata": {
                "name": "checkout-api-rs1",
                "creationTimestamp": "2026-11-26T10:00:00Z",
                "annotations": {"deployment.kubernetes.io/revision": "1"},
                "ownerReferences": [{"kind": "Deployment", "name": "checkout-api"}],
            },
            "spec": {"replicas": 0, "template": {"spec": {"containers": [{"name": "api", "image": "api:v1.0.0"}]}}},
            "status": {"readyReplicas": 0},
        }
        rs_2 = {
            "metadata": {
                "name": "checkout-api-rs2",
                "creationTimestamp": "2026-11-26T12:00:00Z",
                "annotations": {
                    "deployment.kubernetes.io/revision": "2",
                    "kubernetes.io/change-cause": "upgrade to v1.1.0",
                },
                "ownerReferences": [{"kind": "Deployment", "name": "checkout-api"}],
            },
            "spec": {"replicas": 2, "template": {"spec": {"containers": [{"name": "api", "image": "api:v1.1.0"}]}}},
            "status": {"readyReplicas": 2},
        }
        helm_hist = {
            "release": "checkout-rel",
            "items": [
                {
                    "revision": 4,
                    "updated": "2026-11-26T12:05:00Z",
                    "status": "deployed",
                    "chart": "checkout-1.4.0",
                    "app_version": "1.1.0",
                    "description": "Upgrade complete",
                    "is_current": True,
                }
            ],
        }
        configmaps = [{"metadata": {"name": "api-cfg", "resourceVersion": "901", "creationTimestamp": "2026-11-26T11:30:00Z"}, "data": {"PORT": "8080"}}]
        secrets_meta = [{"name": "api-secret", "resource_version": "999", "created_at": "2026-11-26T11:45:00Z", "data_plaintext": "SUPER_LEAKED_VALUE"}]
        events = [
            {
                "namespace": "prod",
                "involved_kind": "Pod",
                "involved_name": "checkout-api-rs2-abcde",
                "type": "Warning",
                "reason": "OOMKilled",
                "message": "Container api exceeded 256Mi limit",
                "last_seen": "2026-11-26T12:15:00Z",
                "count": 1,
            },
            {
                "namespace": "prod",
                "involved_kind": "Pod",
                "involved_name": "unrelated-worker-xyz",
                "type": "Warning",
                "reason": "BackOff",
                "message": "unrelated crash",
                "last_seen": "2026-11-26T12:20:00Z",
                "count": 1,
            },
        ]

        tl = build_workload_timeline(
            workload_type="deployment",
            name="checkout-api",
            namespace="prod",
            workload_obj=deploy,
            replicasets=[rs_1, rs_2],
            helm_history=helm_hist,
            configmaps=configmaps,
            secrets_meta=secrets_meta,
            events=events,
            limit=20,
        )

        assert tl["workload_type"] == "Deployment"
        assert tl["count"] == 6
        assert tl["summary"]["warnings"] == 1
        assert tl["items"][0]["kind"] == "event"
        assert "OOMKilled" in tl["items"][0]["title"]
        assert tl["items"][1]["kind"] == "helm"
        rollouts = [i for i in tl["items"] if i["kind"] == "rollout"]
        assert "api=api:v1.0.0 -> api=api:v1.1.0" in rollouts[0]["detail"]
        dumped = json.dumps(tl)
        assert "SUPER_LEAKED_VALUE" not in dumped
        assert "unrelated-worker" not in dumped

    def test_drift_diff_detects_all_categories_and_masks_secrets(self):
        src = _deployment(
            "checkout-api",
            "staging",
            replicas=2,
            image="ghcr.io/org/api:v1.2.0",
            cpu_req="100m",
            mem_lim="256Mi",
            db_pass_val="staging-secret-123",
            probe_path="/health",
        )
        dst = _deployment(
            "checkout-api",
            "prod",
            replicas=5,
            image="ghcr.io/org/api:v1.1.0",
            cpu_req="250m",
            mem_lim="512Mi",
            db_pass_val="prod-ultra-secret-999",
            probe_path="/healthz",
        )
        src_cm = [{"metadata": {"name": "api-cfg"}, "data": {"FEATURE_X": "true", "STAGING_FLAG": "1"}}]
        dst_cm = [{"metadata": {"name": "api-cfg"}, "data": {"FEATURE_X": "false"}}]
        src_np = []
        dst_np = [{"metadata": {"name": "prod-strict"}, "spec": {"podSelector": {"matchLabels": {"app": "checkout-api"}}, "policyTypes": ["Ingress"]}}]

        diff = compute_workload_drift(
            source_label="staging",
            target_label="prod",
            workload_type="Deployment",
            name="checkout-api",
            source_obj=src,
            target_obj=dst,
            source_configmaps=src_cm,
            target_configmaps=dst_cm,
            source_netpols=src_np,
            target_netpols=dst_np,
        )

        assert diff["has_drift"] is True
        assert diff["masked_server_side"] is True
        categories = {s["category"] for s in diff["sections"]}
        assert {"replicas", "image", "resources", "env", "probes", "configmap", "networkpolicy"}.issubset(categories)

        # Ensure sensitive literal values are hashed and never exposed in plaintext
        raw_json = json.dumps(diff)
        assert "staging-secret-123" not in raw_json
        assert "prod-ultra-secret-999" not in raw_json
        assert "sha256:" in raw_json

        missing = compute_workload_drift(
            source_label="staging",
            target_label="prod",
            workload_type="Deployment",
            name="checkout-api",
            source_obj=src,
            target_obj=None,
        )
        assert missing["has_drift"] is True
        assert missing["severity"] == "critical"
        assert missing["sections"][0]["status"] == "only_in_source"

        same = compute_workload_drift(
            source_label="staging",
            target_label="prod",
            workload_type="Deployment",
            name="checkout-api",
            source_obj=src,
            target_obj=src,
            source_configmaps=src_cm,
            target_configmaps=src_cm,
            source_netpols=src_np,
            target_netpols=src_np,
        )
        assert same["has_drift"] is False
        assert same["drift_count"] == 0


class TestTimelineAndDriftHTTPAndMCP:
    @pytest.mark.asyncio
    async def test_http_endpoints_rbac_read_only_and_mcp(
        self,
        async_client: httpx.AsyncClient,
        isolated_env: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ):
        kcfg_home = isolated_env["kubeconfig_home"]
        monkeypatch.setattr("app.build_provider", lambda cfg: _StubProvider(cfg, kcfg_home))
        monkeypatch.setenv("KUBEORBIT_READ_ONLY", "true")
        monkeypatch.setenv("KUBEORBIT_AUTH_MODE", "iap")
        monkeypatch.setenv("KUBEORBIT_STRICT_RBAC", "true")
        config.get_runtime_env.cache_clear()

        # Two separate teams: Alice only has viewer on "platform" (obs-sbx), NOT "payments" (eks-prod-1); Bob has both
        cfg_file = isolated_env["config_file"]
        two_team_cfg = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        prod_cluster = two_team_cfg["teams"][0]["environments"].pop(1)["clusters"][0]
        two_team_cfg["teams"][0]["rbac"] = {"viewers": ["alice@corp.com", "bob@corp.com"]}
        two_team_cfg["teams"].append({
            "id": "payments",
            "name": "Payments",
            "rbac": {"viewers": ["bob@corp.com"]},
            "environments": [{"id": "prod", "clusters": [prod_cluster]}],
        })
        cfg_file.write_text(yaml.dump(two_team_cfg, sort_keys=False), encoding="utf-8")
        config.reload_config_store()

        src_dep = _deployment("checkout-api", "apps", replicas=2, image="api:v2")
        dst_dep = _deployment("checkout-api", "apps", replicas=4, image="api:v1")

        async def _fake_list(context, kubeconfig_path, resource, namespace=None, ttl=15, **kwargs):
            is_prod = "eks-prod-1" in str(context)
            if resource == "deployments":
                return [dst_dep if is_prod else src_dep], True
            if resource == "replicasets":
                return [], True
            if resource == "configmaps":
                return [], True
            if resource == "secrets":
                return [], True
            if resource == "networkpolicies":
                return [], True
            if resource == "events":
                return [], True
            return [], True

        monkeypatch.setattr("kube_client.timeline.cached_kubectl_list", _fake_list)
        monkeypatch.setattr("kube_client.drift.cached_kubectl_list", _fake_list)
        monkeypatch.setattr("kube_client.resources.cached_kubectl_list", _fake_list)

        async def _fake_rollout(*args, **kwargs):
            return {"revisions": [{"revision": 2, "change_cause": "bump image", "image": "api:v2"}]}

        monkeypatch.setattr("kube_client.timeline.get_rollout_history", _fake_rollout)

        alice_headers = {"X-Goog-Authenticated-User-Email": "accounts.google.com:alice@corp.com"}
        bob_headers = {"X-Goog-Authenticated-User-Email": "accounts.google.com:bob@corp.com"}

        # 1. Timeline HTTP GET works for authorized viewer in strict read-only mode
        r_tl = await async_client.get(
            "/api/clusters/obs-sbx/workloads/Deployment/checkout-api/timeline",
            params={"namespace": "apps"},
            headers=alice_headers,
        )
        assert r_tl.status_code == 200
        assert r_tl.json()["count"] >= 1

        # 2. 404 when workload missing
        r_404 = await async_client.get(
            "/api/clusters/obs-sbx/workloads/Deployment/missing-app/timeline",
            params={"namespace": "apps"},
            headers=alice_headers,
        )
        assert r_404.status_code == 404

        # 3. Cross-cluster Drift Diff blocks Alice (lacks permission on target team "payments") with HTTP 403
        r_forbidden = await async_client.get(
            "/api/fleet/drift-diff",
            params={
                "source_cluster_id": "obs-sbx",
                "target_cluster_id": "eks-prod-1",
                "name": "checkout-api",
                "source_namespace": "apps",
            },
            headers=alice_headers,
        )
        assert r_forbidden.status_code == 403

        # 4. Bob (viewer on both teams) succeeds
        r_diff = await async_client.get(
            "/api/fleet/drift-diff",
            params={
                "source_cluster_id": "obs-sbx",
                "target_cluster_id": "eks-prod-1",
                "name": "checkout-api",
                "source_namespace": "apps",
            },
            headers=bob_headers,
        )
        assert r_diff.status_code == 200
        diff_data = r_diff.json()
        assert diff_data["has_drift"] is True
        assert diff_data["masked_server_side"] is True
        assert any(s["category"] == "image" for s in diff_data["sections"])

        # 5. Execute both MCP tools in-process as Bob
        from app import app
        from mcp_server.client import configure_inprocess_app, reset_inprocess_user, set_inprocess_user
        from mcp_server import tools as mcp_tools

        configure_inprocess_app(app)
        tok = set_inprocess_user("bob@corp.com")
        try:
            mcp_tl = await mcp_tools.get_workload_timeline("obs-sbx", "apps", "deployment", "checkout-api")
            assert mcp_tl["cluster_id"] == "obs-sbx"
            mcp_drift = await mcp_tools.diff_workload_across_clusters("obs-sbx", "eks-prod-1", "checkout-api", "apps")
            assert mcp_drift["has_drift"] is True
        finally:
            reset_inprocess_user(tok)

    @pytest.mark.asyncio
    async def test_timeline_survives_strict_readonly_secret_forbidden(
        self,
        isolated_env: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ):
        dep = _deployment("checkout-api", "prod")
        rs_1 = {
            "metadata": {
                "name": "checkout-api-rs1",
                "creationTimestamp": "2026-11-26T10:00:00Z",
                "annotations": {"deployment.kubernetes.io/revision": "1"},
                "ownerReferences": [{"kind": "Deployment", "name": "checkout-api"}],
            },
            "spec": {"template": {"spec": {"containers": [{"name": "api", "image": "api:v1.0.0"}]}}},
        }
        cfg = {"metadata": {"name": "api-cfg", "resourceVersion": "42", "creationTimestamp": "2026-11-26T10:01:00Z"}, "data": {"PORT": "8080"}}

        async def _fake_list(context, kubeconfig_path, resource, namespace=None, ttl=15, **kwargs):
            if resource == "deployments":
                return [dep], True
            if resource == "replicasets":
                return [rs_1], True
            if resource == "configmaps":
                return [cfg], True
            return [], True

        async def _forbidden_secrets(*args, **kwargs):
            raise CLIError("kubectl get secrets failed: User 'ko-ro' cannot list resource 'secrets' in API group '' (403 Forbidden)")

        async def _fake_events(*args, **kwargs):
            return {"items": []}

        async def _fake_rollout(*args, **kwargs):
            return {"revisions": []}

        monkeypatch.setattr("kube_client.timeline.cached_kubectl_list", _fake_list)
        monkeypatch.setattr("kube_client.timeline.get_secrets_metadata", _forbidden_secrets)
        monkeypatch.setattr("kube_client.timeline.get_extended_events", _fake_events)
        monkeypatch.setattr("kube_client.timeline.get_rollout_history", _fake_rollout)

        res = await get_workload_timeline(
            "ctx-obs-sbx",
            isolated_env["kubeconfig_home"] / "obs-sbx.yaml",
            workload_type="Deployment",
            name="checkout-api",
            namespace="prod",
        )
        assert res["summary"]["rollouts"] == 1
        assert res["summary"]["config_refs"] == 2
        secret_items = [i for i in res["items"] if i["resource_kind"] == "Secret"]
        assert len(secret_items) == 1
        assert secret_items[0]["resource_name"] == "api-secret"
        assert secret_items[0]["metadata"]["masked_server_side"] is True
        assert secret_items[0]["metadata"]["rbac_restricted"] is True
