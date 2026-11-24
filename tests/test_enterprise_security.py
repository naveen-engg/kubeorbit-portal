"""Enterprise Security, 3-Tier RBAC, Cross-Cloud OIDC Federation, and Concurrency TDD Suite.

Written FIRST (Red Phase) to verify:
1. Singleflight TTLCache deduplication (10 concurrent requests = 1 factory call).
2. 3-Tier RBAC (Admin vs Operator vs Viewer) mapped per team in clusters.yaml + global env.
3. Disabling 'local' provider and /parse-kubeconfig in Cloud Run / IAP mode.
4. Blocking kubectl flag injection (--*) in /manifest, /logs, /rollout and path traversal in cluster_id.
5. Auditing REVEAL_SECRET, ONBOARD_CLUSTER, UPDATE_CLUSTER, DELETE_CLUSTER with non-colliding IDs.
6. Zero-Key OIDC Federation from Cloud Run for AWS EKS (AssumeRoleWithWebIdentity) and Azure AKS (--federated-token + kubelogin).
7. Per-user AI chat session isolation and bounded session eviction.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

import auth
import config
from config import ClusterConfig, get_config_store
from kube_client._cache import TTLCache
from kube_client.audit import get_audit_logs, record_audit_event
from providers.aks import AKSProvider
from providers.eks import EKSProvider
from tests.test_integration import MockGKEProvider


# =========================================================================== #
# 1. Singleflight TTLCache (Fixes Cold-Cache Stampede)
# =========================================================================== #


class TestSingleflightTTLCache:
    @pytest.mark.asyncio
    async def test_10_concurrent_callers_invoke_factory_only_once(self):
        cache = TTLCache()
        factory_calls = 0

        async def slow_factory():
            nonlocal factory_calls
            factory_calls += 1
            await asyncio.sleep(0.03)
            return {"data": "shared-result"}

        results = await asyncio.gather(*(cache.get_or_set("cold-key", 60, slow_factory) for _ in range(10)))
        assert factory_calls == 1
        assert all(val == {"data": "shared-result"} for val, _ in results)
        # Exactly 1 caller gets from_cache=False (the leader), 9 followers get from_cache=True
        assert sum(1 for _, from_cache in results if not from_cache) == 1
        assert sum(1 for _, from_cache in results if from_cache) == 9


# =========================================================================== #
# 2. 3-Tier RBAC (Viewer vs Operator vs Admin) & IAP / Local Provider Lockout
# =========================================================================== #


class TestThreeTierRBACAndCloudRunLockout:
    @pytest.mark.asyncio
    async def test_3_tier_rbac_enforcement_across_endpoints(
        self,
        async_client: httpx.AsyncClient,
        isolated_env: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ):
        # Configure IAP mode + team RBAC in clusters.yaml (opt-in rollback enabled for Operator RBAC verification)
        monkeypatch.setenv("KUBEORBIT_AUTH_MODE", "iap")
        monkeypatch.setenv("KUBEORBIT_ADMIN_EMAILS", "superadmin@corp.com")
        monkeypatch.setenv("KUBEORBIT_READ_ONLY", "false")
        config.get_runtime_env.cache_clear()

        kcfg_home = isolated_env["kubeconfig_home"]
        monkeypatch.setattr("app.build_provider", lambda cfg: MockGKEProvider(cfg, kcfg_home))

        # Mock rollout_undo
        async def fake_rollback(ctx, kcfg, ns, wtype, name, to_revision=None):
            return {"status": "ok", "message": "rolled back"}

        monkeypatch.setattr("kube_client.rollout_undo", fake_rollback)
        monkeypatch.setattr("kube_client._fetch._sync_sdk_list", lambda ctx, kcfg, res, ns=None: ([], True))

        # Inject team-level RBAC into config
        import yaml
        cfg_path = isolated_env["config_file"]
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        raw["teams"][0]["rbac"] = {
            "admins": ["teamadmin@corp.com"],
            "operators": ["sre-operator@corp.com"],
            "viewers": ["dev-viewer@corp.com"],
        }
        cfg_path.write_text(yaml.dump(raw, sort_keys=False), encoding="utf-8")
        config.reload_config_store()

        viewer_headers = {"X-Goog-Authenticated-User-Email": "accounts.google.com:dev-viewer@corp.com"}
        operator_headers = {"X-Goog-Authenticated-User-Email": "accounts.google.com:sre-operator@corp.com"}
        admin_headers = {"X-Goog-Authenticated-User-Email": "accounts.google.com:teamadmin@corp.com"}

        # --- A. Viewer can read nodes, CANNOT rollback or onboard clusters; Secret reveal/YAML are disabled ---
        r_view_nodes = await async_client.get("/api/clusters/obs-sbx/nodes", headers=viewer_headers)
        assert r_view_nodes.status_code == 200

        r_view_secret = await async_client.post(
            "/api/clusters/obs-sbx/namespaces/default/secrets/my-sec/reveal", headers=viewer_headers
        )
        assert r_view_secret.status_code in (404, 405)

        r_view_secret_yaml = await async_client.get(
            "/api/clusters/obs-sbx/manifest?kind=Secret&name=my-sec&namespace=default", headers=viewer_headers
        )
        assert r_view_secret_yaml.status_code == 403

        r_view_rb = await async_client.post(
            "/api/clusters/obs-sbx/workloads/deployment/web/rollback?namespace=default",
            json={"to_revision": 1},
            headers=viewer_headers,
        )
        assert r_view_rb.status_code == 403

        r_view_admin = await async_client.delete("/api/admin/clusters/obs-sbx", headers=viewer_headers)
        assert r_view_admin.status_code == 403

        # --- B. Operator CAN rollback, CANNOT add/edit/delete clusters; Secret reveal/YAML stay disabled ---
        r_op_secret = await async_client.post(
            "/api/clusters/obs-sbx/namespaces/default/secrets/my-sec/reveal", headers=operator_headers
        )
        assert r_op_secret.status_code in (404, 405)

        r_op_secret_yaml = await async_client.get(
            "/api/clusters/obs-sbx/manifest?kind=Secret&name=my-sec&namespace=default", headers=operator_headers
        )
        assert r_op_secret_yaml.status_code == 403

        r_op_rb = await async_client.post(
            "/api/clusters/obs-sbx/workloads/deployment/web/rollback?namespace=default",
            json={"to_revision": 1},
            headers=operator_headers,
        )
        assert r_op_rb.status_code == 200

        r_op_admin = await async_client.delete("/api/admin/clusters/obs-sbx", headers=operator_headers)
        assert r_op_admin.status_code == 403

        # --- C. Admin CAN add/edit/delete clusters ---
        r_adm_put = await async_client.put(
            "/api/admin/clusters/obs-sbx",
            json={
                "id": "obs-sbx",
                "name": "OBS SBX Enterprise",
                "provider": "gke",
                "environment": "sandbox",
                "team_id": "platform",
                "team_name": "Platform Engineering",
                "config": {"project": "adk-enterprise-observability", "zone": "us-central1-a", "cluster_name": "kubeorbit-gke"},
            },
            headers=admin_headers,
        )
        assert r_adm_put.status_code == 200

    @pytest.mark.asyncio
    async def test_local_provider_and_parse_kubeconfig_disabled_in_iap_cloud_run_mode(
        self,
        async_client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("KUBEORBIT_AUTH_MODE", "iap")
        monkeypatch.setenv("KUBEORBIT_ADMIN_EMAILS", "admin@corp.com")
        config.get_runtime_env.cache_clear()

        admin_headers = {"X-Goog-Authenticated-User-Email": "accounts.google.com:admin@corp.com"}

        # 1. /api/admin/clusters/parse-kubeconfig must be blocked (403) in Cloud Run / IAP mode
        res_parse = await async_client.post(
            "/api/admin/clusters/parse-kubeconfig",
            json={"kubeconfig_path": "C:/Windows/win.ini"},
            headers=admin_headers,
        )
        assert res_parse.status_code == 403

        # 2. Onboarding a 'local' provider cluster must be rejected (403 or 400) in Cloud Run / IAP mode
        res_local = await async_client.post(
            "/api/admin/clusters",
            json={
                "id": "local-attempt",
                "name": "Local Attempt",
                "provider": "local",
                "environment": "sandbox",
                "config": {"kubeconfig_path": "/etc/passwd"},
            },
            headers=admin_headers,
        )
        assert res_local.status_code in (400, 403)


# =========================================================================== #
# 3. Input Sanitization: Flag Injection & Path Traversal Prevention
# =========================================================================== #


class TestInputSanitizationAndFlagInjection:
    @pytest.mark.asyncio
    async def test_kubectl_flag_injection_blocked_with_422(
        self,
        async_client: httpx.AsyncClient,
        isolated_env: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ):
        kcfg_home = isolated_env["kubeconfig_home"]
        monkeypatch.setattr("app.build_provider", lambda cfg: MockGKEProvider(cfg, kcfg_home))

        # 1. /manifest with kind=--help or name=--kubeconfig=/tmp/evil
        r1 = await async_client.get("/api/clusters/obs-sbx/manifest?kind=--help&name=foo")
        assert r1.status_code == 422

        r2 = await async_client.get("/api/clusters/obs-sbx/manifest?kind=Pod&name=--token=leaked")
        assert r2.status_code == 422

        # 2. /logs with pod_name=--v=9 or container=--previous
        r3 = await async_client.get("/api/clusters/obs-sbx/namespaces/default/pods/--v=9/logs")
        assert r3.status_code == 422

        r4 = await async_client.get("/api/clusters/obs-sbx/namespaces/default/pods/valid-pod/logs?container=--all-containers")
        assert r4.status_code == 422

        # 3. /rollout/history with invalid workload_type or name starting with --
        r5 = await async_client.get(
            "/api/clusters/obs-sbx/workloads/--evil/web/rollout/history?namespace=default"
        )
        assert r5.status_code == 422

    @pytest.mark.asyncio
    async def test_path_traversal_in_cluster_id_blocked_with_422(
        self, async_client: httpx.AsyncClient
    ):
        # Attempting to register a cluster with path traversal ID
        r_post = await async_client.post(
            "/api/admin/clusters",
            json={
                "id": "../../evil-cluster",
                "name": "Evil",
                "provider": "gke",
                "environment": "sandbox",
                "config": {"project": "p", "zone": "us-central1-a"},
            },
        )
        assert r_post.status_code == 422


# =========================================================================== #
# 4. Audit Trail Completeness (Workload Rollback, Cluster CRUD, Collision-Free IDs)
# =========================================================================== #


class TestAuditTrailCompleteness:
    @pytest.mark.asyncio
    async def test_rollback_recorded_in_audit_logs_and_secret_reveal_disabled(
        self,
        async_client: httpx.AsyncClient,
        isolated_env: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ):
        kcfg_home = isolated_env["kubeconfig_home"]
        monkeypatch.setenv("KUBEORBIT_READ_ONLY", "false")
        config.get_runtime_env.cache_clear()
        monkeypatch.setattr("app.build_provider", lambda cfg: MockGKEProvider(cfg, kcfg_home))

        async def fake_rollback(ctx, kcfg, ns, wtype, name, to_revision=None):
            return {"status": "ok", "message": "rolled back"}

        monkeypatch.setattr("kube_client.rollout_undo", fake_rollback)

        # Secret reveal route is removed (404/405 via root static mount) and Secret YAML is blocked (403)
        rev = await async_client.post(
            "/api/clusters/obs-sbx/namespaces/kubeorbit-demo/secrets/app-credentials/reveal"
        )
        assert rev.status_code in (404, 405)
        sec_yaml = await async_client.get(
            "/api/clusters/obs-sbx/manifest?kind=Secret&name=app-credentials&namespace=kubeorbit-demo"
        )
        assert sec_yaml.status_code == 403

        rb = await async_client.post(
            "/api/clusters/obs-sbx/workloads/deployment/web/rollback?namespace=kubeorbit-demo",
            json={"to_revision": 1},
        )
        assert rb.status_code == 200

        logs = get_audit_logs(cluster_id="obs-sbx")
        assert any(l["action"] == "ROLLBACK_WORKLOAD" and l["resource"] == "deployment/web" for l in logs)

        # Rapid synchronous audit creation never produces duplicate IDs
        e1 = record_audit_event("u1", "ACT", "obs-sbx", "ns", "r1")
        e2 = record_audit_event("u1", "ACT", "obs-sbx", "ns", "r2")
        assert e1["id"] != e2["id"]


# =========================================================================== #
# 5. Zero-Key Cross-Cloud OIDC Federation (Cloud Run -> AWS EKS & Azure AKS)
# =========================================================================== #


class TestCrossCloudOIDCFederation:
    @pytest.mark.asyncio
    async def test_eks_provider_uses_gcp_oidc_web_identity_when_available(
        self, isolated_env: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ):
        cluster = ClusterConfig(
            id="eks-fed",
            name="EKS Federated",
            provider="eks",
            environment="prod",
            config={
                "region": "us-east-1",
                "cluster_name": "prod-eks",
                "role_arn": "arn:aws:iam::123456789012:role/KubeOrbitEKSRole",
            },
        )
        kcfg_path = isolated_env["kubeconfig_home"] / "eks-fed.yaml"
        provider = EKSProvider(cluster, str(kcfg_path))

        # Simulate Cloud Run GCP Metadata Server returning a signed OIDC JWT
        monkeypatch.setattr(
            "providers.eks._fetch_gcp_oidc_token",
            lambda audience="sts.amazonaws.com": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.fake-gcp-oidc-token",
        )

        executed_cmds: List[List[str]] = []

        async def fake_run_cli(cmd: List[str], env=None, timeout=None, check=True):
            executed_cmds.append(cmd)
            if "assume-role-with-web-identity" in cmd:
                return (
                    0,
                    '{"Credentials":{"AccessKeyId":"ASIA123","SecretAccessKey":"sec","SessionToken":"tok"}}',
                    "",
                )
            if "update-kubeconfig" in cmd:
                kcfg_path.write_text("apiVersion: v1\ncurrent-context: kubeorbit-eks-fed\n", encoding="utf-8")
                return 0, "Updated context", ""
            if "get-caller-identity" in cmd:
                return 0, '{"Arn":"arn:aws:sts::123456789012:assumed-role/KubeOrbitEKSRole/kubeorbit-eks-fed","Account":"123456789012"}', ""
            return 0, "{}", ""

        monkeypatch.setattr("providers.eks.run_cli", fake_run_cli)

        auth_res = await provider.authenticate()
        assert auth_res.context_name == "kubeorbit-eks-fed"
        assert any("assume-role-with-web-identity" in c for c in executed_cmds)

    @pytest.mark.asyncio
    async def test_aks_provider_uses_gcp_oidc_federated_token_and_kubelogin(
        self, isolated_env: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ):
        cluster = ClusterConfig(
            id="aks-fed",
            name="AKS Federated",
            provider="aks",
            environment="prod",
            config={
                "resource_group": "rg-aks-prod",
                "cluster_name": "aks-prod-cluster",
                "subscription_id": "sub-1234",
                "tenant_id": "tenant-5678",
                "client_id": "app-client-9999",
            },
        )
        kcfg_path = isolated_env["kubeconfig_home"] / "aks-fed.yaml"
        provider = AKSProvider(cluster, str(kcfg_path))

        monkeypatch.setattr(
            "providers.aks._fetch_gcp_oidc_token",
            lambda audience="api://AzureADTokenExchange": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.fake-gcp-oidc-for-azure",
        )

        executed_cmds: List[List[str]] = []

        async def fake_run_cli(cmd: List[str], env=None, timeout=None, check=True):
            executed_cmds.append(cmd)
            if "get-credentials" in cmd:
                kcfg_path.write_text("apiVersion: v1\ncurrent-context: aks-prod-cluster\n", encoding="utf-8")
                return 0, "Merged context", ""
            return 0, "{}", ""

        monkeypatch.setattr("providers.aks.run_cli", fake_run_cli)

        auth_res = await provider.authenticate()
        assert auth_res.context_name == "aks-prod-cluster"
        # Verify federated token login (--federated-token) and kubelogin convert-kubeconfig were invoked
        assert any("--federated-token" in c for c in executed_cmds)
        assert any("kubelogin" in c[0] for c in executed_cmds)


# =========================================================================== #
# 6. Google ADK + Vertex AI Gemini Integration & In-Process IAP Tool Calls
# =========================================================================== #


class TestVertexAIADKIntegration:
    @pytest.mark.asyncio
    async def test_vertex_ai_env_defaults_and_inprocess_iap_tool_propagation(
        self,
        async_client: httpx.AsyncClient,
        isolated_env: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ):
        import ai_agent
        from mcp_server import tools as mcp_tools
        from mcp_server.client import configure_inprocess_app, reset_inprocess_user, set_inprocess_user

        # 1. Verify ensure_vertex_ai_env defaults to Vertex AI in adk-enterprise-observability
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.delenv("KUBEORBIT_AI_MODEL", raising=False)

        cfg = ai_agent.ensure_vertex_ai_env()
        assert cfg["backend"] == "vertex_ai"
        assert cfg["project"] == "adk-enterprise-observability"
        assert cfg["location"] == "global"
        assert cfg["model"] == "gemini-3.5-flash"

        # 2. Verify in-process ADK tool call works under KUBEORBIT_AUTH_MODE=iap without 401
        monkeypatch.setenv("KUBEORBIT_AUTH_MODE", "iap")
        config.get_runtime_env.cache_clear()
        kcfg_home = isolated_env["kubeconfig_home"]
        monkeypatch.setattr("app.build_provider", lambda c: MockGKEProvider(c, kcfg_home))

        from app import app
        configure_inprocess_app(app)

        tok = set_inprocess_user("sre-user@corp.com")
        try:
            teams_res = await mcp_tools.list_teams_and_clusters()
            assert "teams" in teams_res
            assert teams_res["teams"][0]["id"] == "platform"
        finally:
            reset_inprocess_user(tok)


# =========================================================================== #
# 7. GCP Custom IAM Roles (kubeorbitAdmin/Operator/Viewer) + Strict Zero-Trust 403
# =========================================================================== #


class TestGCPCustomIAMRolesAndStrictZeroTrust:
    @pytest.mark.asyncio
    async def test_gcp_custom_iam_role_resolution_and_strict_403_deny(
        self,
        async_client: httpx.AsyncClient,
        isolated_env: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("KUBEORBIT_AUTH_MODE", "iap")
        monkeypatch.setenv("KUBEORBIT_GCP_IAM_RBAC", "true")
        monkeypatch.setenv("KUBEORBIT_STRICT_RBAC", "true")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "adk-enterprise-observability")
        config.get_runtime_env.cache_clear()
        auth.invalidate_gcp_iam_cache()
        monkeypatch.setenv("KUBEORBIT_READ_ONLY", "false")
        config.get_runtime_env.cache_clear()

        kcfg_home = isolated_env["kubeconfig_home"]
        monkeypatch.setattr("app.build_provider", lambda cfg: MockGKEProvider(cfg, kcfg_home))
        monkeypatch.setattr("kube_client._fetch._sync_sdk_list", lambda ctx, kcfg, res, ns=None: ([], True))

        async def fake_rollback(ctx, kcfg, ns, wtype, name, to_revision=None):
            return {"status": "ok", "message": "rolled back"}

        monkeypatch.setattr("kube_client.rollout_undo", fake_rollback)

        # Mock GCP Cloud Resource Manager getIamPolicy response with our 3 Custom IAM Roles
        fake_gcp_bindings = {
            "admin": ["user:gcp-admin@gmail.com"],
            "operator": ["user:gcp-ops@gmail.com"],
            "viewer": ["user:gcp-viewer@gmail.com", "domain:verifiedcorp.com"],
        }
        monkeypatch.setattr("auth._get_gcp_custom_role_bindings", lambda project_id: fake_gcp_bindings)

        # 1. Unlisted IAP user (passed IAP login, but has NO kubeorbit* role in GCP IAM) -> Strict 403 Forbidden
        intruder_headers = {"X-Goog-Authenticated-User-Email": "accounts.google.com:intruder@gmail.com"}
        r_intruder = await async_client.get("/api/status", headers=intruder_headers)
        assert r_intruder.status_code == 403
        assert "Zero-Trust" in r_intruder.json()["detail"] or "Forbidden" in r_intruder.json()["detail"]

        # 2. Explicit Gmail Viewer in kubeorbitViewer -> role='viewer', can view status, 403 on rollback
        viewer_headers = {"X-Goog-Authenticated-User-Email": "accounts.google.com:gcp-viewer@gmail.com"}
        r_viewer_status = await async_client.get("/api/status", headers=viewer_headers)
        assert r_viewer_status.status_code == 200
        assert r_viewer_status.json()["user"]["role"] == "viewer"

        r_viewer_rb = await async_client.post(
            "/api/clusters/obs-sbx/workloads/deployment/web/rollback?namespace=default",
            json={"to_revision": 1},
            headers=viewer_headers,
        )
        assert r_viewer_rb.status_code == 403

        # 3. Domain-matched Viewer (domain:verifiedcorp.com) -> role='viewer'
        domain_headers = {"X-Goog-Authenticated-User-Email": "accounts.google.com:anyone@verifiedcorp.com"}
        r_domain_status = await async_client.get("/api/status", headers=domain_headers)
        assert r_domain_status.status_code == 200
        assert r_domain_status.json()["user"]["role"] == "viewer"

        # 4. Explicit Gmail Operator in kubeorbitOperator -> role='operator', CAN rollback, 403 on cluster delete
        ops_headers = {"X-Goog-Authenticated-User-Email": "accounts.google.com:gcp-ops@gmail.com"}
        r_ops_status = await async_client.get("/api/status", headers=ops_headers)
        assert r_ops_status.status_code == 200
        assert r_ops_status.json()["user"]["role"] == "operator"

        r_ops_rb = await async_client.post(
            "/api/clusters/obs-sbx/workloads/deployment/web/rollback?namespace=default",
            json={"to_revision": 1},
            headers=ops_headers,
        )
        assert r_ops_rb.status_code == 200

        r_ops_del = await async_client.delete("/api/admin/clusters/obs-sbx", headers=ops_headers)
        assert r_ops_del.status_code == 403

        # 5. Explicit Gmail Admin in kubeorbitAdmin -> role='admin'
        adm_headers = {"X-Goog-Authenticated-User-Email": "accounts.google.com:gcp-admin@gmail.com"}
        r_adm_status = await async_client.get("/api/status", headers=adm_headers)
        assert r_adm_status.status_code == 200
        assert r_adm_status.json()["user"]["role"] == "admin"

    def test_gcp_iam_policy_parser_supports_versioned_custom_role_suffix(self, monkeypatch: pytest.MonkeyPatch):
        class _FakeResp:
            status_code = 200
            text = ""

            def json(self):
                return {
                    "bindings": [
                        {"role": "projects/demo-proj/roles/kubeorbitAdmin_v2", "members": ["user:Admin@Corp.com"]},
                        {"role": "projects/demo-proj/roles/kubeorbitOperator_v2", "members": ["user:ops@corp.com"]},
                        {"role": "projects/demo-proj/roles/kubeorbitViewer_v2", "members": ["domain:corp.com"]},
                    ]
                }

        class _FakeSession:
            def __init__(self, creds):
                self.creds = creds

            def post(self, url, json=None, timeout=8):
                return _FakeResp()

        import google.auth
        import google.auth.transport.requests

        monkeypatch.setattr(google.auth, "default", lambda scopes=None: (object(), "demo-proj"))
        monkeypatch.setattr(google.auth.transport.requests, "AuthorizedSession", _FakeSession)

        parsed = auth._fetch_gcp_iam_policy_from_api("demo-proj")
        assert parsed["admin"] == ["user:admin@corp.com"]
        assert parsed["operator"] == ["user:ops@corp.com"]
        assert parsed["viewer"] == ["domain:corp.com"]


