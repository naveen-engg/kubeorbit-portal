"""Unit, RCA Step 12, HTTP API, and MCP tests for the Static NetworkPolicy Simulator."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

from kube_client.netpol import evaluate_network_policy_reachability, find_netpol_isolated_services
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


def _pod(
    name: str,
    ns: str,
    labels: Dict[str, str],
    ip: str = "10.0.0.10",
    ports: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    return {
        "metadata": {"name": name, "namespace": ns, "labels": labels},
        "spec": {"containers": [{"name": "app", "ports": ports or [{"name": "http", "containerPort": 8080, "protocol": "TCP"}]}]},
        "status": {"phase": "Running", "podIP": ip},
    }


class TestNetworkPolicyEvaluator:
    def test_default_allow_when_no_policies_select_pods(self):
        src = _pod("web-1", "frontend", {"app": "web"}, ip="10.1.0.5")
        dst = _pod("api-1", "backend", {"app": "api"}, ip="10.1.1.9")
        res = evaluate_network_policy_reachability(
            source_pod=src,
            dest_pod=dst,
            source_netpols=[],
            dest_netpols=[],
            port=8080,
            protocol="TCP",
        )
        assert res["allowed"] is True
        assert res["verdict"] == "ALLOWED"
        assert res["egress_mode"] == "default-allow"
        assert res["ingress_mode"] == "default-allow"

    def test_default_deny_ingress_blocks_source(self):
        src = _pod("web-1", "frontend", {"app": "web"})
        dst = _pod("api-1", "backend", {"app": "api"})
        deny_all_ingress = {
            "metadata": {"name": "deny-all-ingress", "namespace": "backend"},
            "spec": {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": []},
        }
        res = evaluate_network_policy_reachability(
            source_pod=src,
            dest_pod=dst,
            source_netpols=[],
            dest_netpols=[deny_all_ingress],
            port=8080,
        )
        assert res["allowed"] is False
        assert res["verdict"] == "BLOCKED_INGRESS"
        assert res["selecting_ingress_policies"] == ["deny-all-ingress"]
        assert "deny-all-ingress" in res["suggested_fix_hint"]

    def test_default_deny_egress_blocks_outbound(self):
        src = _pod("web-1", "frontend", {"app": "web"})
        dst = _pod("api-1", "backend", {"app": "api"})
        deny_egress = {
            "metadata": {"name": "deny-egress", "namespace": "frontend"},
            "spec": {"podSelector": {"matchLabels": {"app": "web"}}, "policyTypes": ["Egress"], "egress": []},
        }
        res = evaluate_network_policy_reachability(
            source_pod=src,
            dest_pod=dst,
            source_netpols=[deny_egress],
            dest_netpols=[],
            port=8080,
        )
        assert res["allowed"] is False
        assert res["verdict"] == "BLOCKED_EGRESS"
        assert res["egress_allowed"] is False
        assert res["ingress_allowed"] is True

    def test_and_vs_or_peer_semantics_and_match_expressions(self):
        src = _pod("web-1", "frontend", {"app": "web", "tier": "edge"})
        dst = _pod("api-1", "backend", {"app": "api"})
        ns_labels = {
            "frontend": {"kubernetes.io/metadata.name": "frontend", "env": "prod"},
            "backend": {"kubernetes.io/metadata.name": "backend", "env": "prod"},
        }

        # Single peer containing BOTH namespaceSelector and podSelector -> AND required
        pol_and = {
            "metadata": {"name": "strict-and", "namespace": "backend"},
            "spec": {
                "podSelector": {"matchLabels": {"app": "api"}},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [
                            {
                                "namespaceSelector": {"matchLabels": {"env": "prod"}},
                                "podSelector": {"matchLabels": {"app": "wrong-app"}},
                            }
                        ],
                        "ports": [{"protocol": "TCP", "port": 8080}],
                    }
                ],
            },
        }
        blocked = evaluate_network_policy_reachability(
            source_pod=src,
            dest_pod=dst,
            source_netpols=[],
            dest_netpols=[pol_and],
            namespaces_labels=ns_labels,
            port=8080,
        )
        assert blocked["allowed"] is False

        # Separate peers with matchExpressions (In, NotIn, Exists) -> OR semantics
        pol_or = {
            "metadata": {"name": "expr-or", "namespace": "backend"},
            "spec": {
                "podSelector": {"matchLabels": {"app": "api"}},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [
                            {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "other"}}},
                            {
                                "namespaceSelector": {
                                    "matchExpressions": [
                                        {"key": "env", "operator": "In", "values": ["prod", "staging"]},
                                        {"key": "quarantined", "operator": "NotIn", "values": ["true"]},
                                    ]
                                },
                                "podSelector": {
                                    "matchExpressions": [{"key": "tier", "operator": "Exists", "values": []}]
                                },
                            },
                        ]
                    }
                ],
            },
        }
        allowed = evaluate_network_policy_reachability(
            source_pod=src,
            dest_pod=dst,
            source_netpols=[],
            dest_netpols=[pol_or],
            namespaces_labels=ns_labels,
            port=8080,
        )
        assert allowed["allowed"] is True
        assert allowed["allowing_ingress_policies"] == ["expr-or"]

    def test_ipblock_cidr_except_named_port_and_endport(self):
        src_allowed_ip = _pod("client-ok", "ext", {"role": "client"}, ip="10.20.1.5")
        src_except_ip = _pod("client-ex", "ext", {"role": "client"}, ip="10.20.1.99")
        dst = _pod(
            "api-1",
            "backend",
            {"app": "api"},
            ip="10.30.0.5",
            ports=[{"name": "http", "containerPort": 8080, "protocol": "TCP"}],
        )

        # Policy allows CIDR 10.20.0.0/16 except 10.20.1.96/28 on named port "http", and range 9000..9010
        pol = {
            "metadata": {"name": "cidr-and-ports", "namespace": "backend"},
            "spec": {
                "podSelector": {"matchLabels": {"app": "api"}},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [{"ipBlock": {"cidr": "10.20.0.0/16", "except": ["10.20.1.96/28"]}}],
                        "ports": [
                            {"protocol": "TCP", "port": "http"},
                            {"protocol": "TCP", "port": 9000, "endPort": 9010},
                        ],
                    }
                ],
            },
        }

        ok_named = evaluate_network_policy_reachability(
            source_pod=src_allowed_ip, dest_pod=dst, source_netpols=[], dest_netpols=[pol], port=8080
        )
        assert ok_named["allowed"] is True

        ok_range = evaluate_network_policy_reachability(
            source_pod=src_allowed_ip, dest_pod=dst, source_netpols=[], dest_netpols=[pol], port=9005
        )
        assert ok_range["allowed"] is True

        denied_except = evaluate_network_policy_reachability(
            source_pod=src_except_ip, dest_pod=dst, source_netpols=[], dest_netpols=[pol], port=8080
        )
        assert denied_except["allowed"] is False


class TestNetpolIntegrationAndMCP:
    @pytest.mark.asyncio
    async def test_service_simulation_step12_rca_http_and_mcp(
        self,
        async_client: httpx.AsyncClient,
        isolated_env: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ):
        kcfg_home = isolated_env["kubeconfig_home"]
        monkeypatch.setattr("app.build_provider", lambda cfg: _StubProvider(cfg, kcfg_home))
        monkeypatch.setenv("KUBEORBIT_READ_ONLY", "true")
        import config
        config.get_runtime_env.cache_clear()

        src_pod = _pod("web-0", "apps", {"app": "web"}, ip="10.2.0.10")
        dst_pod = _pod("db-0", "apps", {"app": "postgres"}, ip="10.2.0.20", ports=[{"name": "pg", "containerPort": 5432, "protocol": "TCP"}])
        pg_svc = {
            "metadata": {"name": "postgres-svc", "namespace": "apps"},
            "spec": {"selector": {"app": "postgres"}, "ports": [{"name": "pg", "port": 5432, "targetPort": "pg", "protocol": "TCP"}]},
        }
        deny_pg = {
            "metadata": {"name": "lockdown-postgres", "namespace": "apps"},
            "spec": {"podSelector": {"matchLabels": {"app": "postgres"}}, "policyTypes": ["Ingress"], "ingress": []},
        }
        ns_obj = {"metadata": {"name": "apps", "labels": {"kubernetes.io/metadata.name": "apps"}}}

        async def _fake_list(context, kubeconfig_path, resource, namespace=None, ttl=15, **kwargs):
            if resource == "pods":
                return [src_pod, dst_pod], True
            if resource == "namespaces":
                return [ns_obj], True
            if resource == "services":
                return [pg_svc], True
            if resource == "networkpolicies":
                return [deny_pg], True
            return [], True

        monkeypatch.setattr("kube_client.netpol.cached_kubectl_list", _fake_list)

        # 1. Verify Step 12 isolated-service detection helper directly
        iso = find_netpol_isolated_services([src_pod, dst_pod], [pg_svc], {"apps": [deny_pg]})
        assert len(iso) == 1
        assert iso[0]["service"] == "postgres-svc"
        assert iso[0]["selecting_policies"] == ["lockdown-postgres"]

        # 2. HTTP GET endpoint validation (400 when target omitted, 404 on missing pod, 200 blocked simulation under read-only)
        r_bad = await async_client.get(
            "/api/clusters/obs-sbx/networkpolicy/simulate",
            params={"source_namespace": "apps", "source_pod": "web-0", "dest_namespace": "apps"},
        )
        assert r_bad.status_code == 400

        r_missing = await async_client.get(
            "/api/clusters/obs-sbx/networkpolicy/simulate",
            params={"source_namespace": "apps", "source_pod": "does-not-exist", "dest_namespace": "apps", "dest_service": "postgres-svc"},
        )
        assert r_missing.status_code == 404

        r_sim = await async_client.get(
            "/api/clusters/obs-sbx/networkpolicy/simulate",
            params={"source_namespace": "apps", "source_pod": "web-0", "dest_namespace": "apps", "dest_service": "postgres-svc"},
        )
        assert r_sim.status_code == 200
        payload = r_sim.json()
        assert payload["allowed"] is False
        assert payload["verdict"] == "BLOCKED_INGRESS"
        assert payload["evaluated_port"] == 5432
        assert payload["destination"]["service"] == "postgres-svc"
        assert payload["destination"]["pod"] == "db-0"

        # 3. MCP tool execution over in-process client
        from app import app
        from mcp_server.client import configure_inprocess_app
        from mcp_server import tools as mcp_tools

        configure_inprocess_app(app)
        mcp_res = await mcp_tools.simulate_network_policy(
            cluster_id="obs-sbx",
            source_namespace="apps",
            source_pod="web-0",
            dest_namespace="apps",
            dest_service="postgres-svc",
        )
        assert mcp_res["allowed"] is False
        assert mcp_res["verdict"] == "BLOCKED_INGRESS"

    @pytest.mark.asyncio
    async def test_rca_step12_emits_isolated_service_finding(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        from kube_client.rca import diagnose_cluster_issues

        dst_pod = _pod("db-0", "apps", {"app": "postgres"}, ip="10.2.0.20")
        deny_pg = {
            "metadata": {"name": "lockdown-postgres", "namespace": "apps"},
            "spec": {"podSelector": {"matchLabels": {"app": "postgres"}}, "policyTypes": ["Ingress"], "ingress": []},
        }

        async def _empty(*args, **kwargs):
            return []

        async def _pods(*args, **kwargs):
            return [{"name": "db-0", "namespace": "apps", "status": "Running", "healthy": True, "ready": True, "restarts": 0}]

        async def _svcs(*args, **kwargs):
            return [{"name": "postgres-svc", "namespace": "apps", "selector": {"app": "postgres"}, "ports": []}]

        async def _deploys(*args, **kwargs):
            return [{"name": "postgres", "namespace": "apps", "replicas": 2, "ready": 2, "healthy": True}]

        async def _cached(context, kubeconfig, resource, namespace=None, ttl=15, **kwargs):
            if resource == "pods":
                return [dst_pod], True
            if resource == "networkpolicies":
                return [deny_pg], True
            return [], True

        monkeypatch.setattr("kube_client.rca.get_pods", _pods)
        monkeypatch.setattr("kube_client.rca.get_events", _empty)
        monkeypatch.setattr("kube_client.rca.get_nodes", _empty)
        monkeypatch.setattr("kube_client.rca.get_deployments", _deploys)
        monkeypatch.setattr("kube_client.rca.get_statefulsets", _empty)
        monkeypatch.setattr("kube_client.rca.get_daemonsets", _empty)
        monkeypatch.setattr("kube_client.rca.get_services", _svcs)
        monkeypatch.setattr("kube_client.rca.get_configmaps", _empty)
        monkeypatch.setattr("kube_client.rca._safe_get_helm_releases", _empty)
        monkeypatch.setattr("kube_client.rca.cached_kubectl_list", _cached)

        res = await diagnose_cluster_issues("ctx", tmp_path / "k.yaml", namespace="apps")
        issue_types = {ri["issue_type"] for ri in res["raw_issues"]}
        assert "Service Isolated by Default-Deny NetworkPolicy" in issue_types
        assert any("Service Isolated by Default-Deny NetworkPolicy" in rc["title"] for rc in res["root_causes"])
        assert res["step_counts"][12] >= 1
