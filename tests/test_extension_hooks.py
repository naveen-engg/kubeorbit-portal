"""Tests for minimal strictly read-only OSS extension hooks & RbacAuthorizationV1Api SDK list."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest

import config
import extensions
from mcp_server import schemas, server as mcp_server
from kube_client import _cli, _sdk, normalize


@pytest.fixture(autouse=True)
def _clean_hooks():
    extensions.reset_extensions_for_testing()
    mcp_server.reset_extension_tools_for_testing()
    yield
    extensions.reset_extensions_for_testing()
    mcp_server.reset_extension_tools_for_testing()


class TestExtensionHooksAndRbacSdk:
    def test_load_extensions_noop_when_missing_or_disabled(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KUBEORBIT_EXTENSION_MODULE", "missing_enterprise_pkg.plugin")
        assert extensions.load_extensions(None) is False
        assert extensions.is_enterprise_loaded() is False

        monkeypatch.setenv("KUBEORBIT_ENTERPRISE_ENABLED", "false")
        assert extensions.load_extensions(None) is False

    @pytest.mark.asyncio
    async def test_status_exposes_enterprise_ui_modules_filtered_by_role(
        self,
        async_client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        extensions.register_ui_module("ent-rbac", "RBAC", "/enterprise-static/rbac_explorer.js", min_role="viewer")
        extensions.register_ui_module("ent-admin", "Admin Only", "/enterprise-static/admin.js", min_role="admin")
        extensions._loaded_modules.add("enterprise.plugin")

        monkeypatch.setenv("KUBEORBIT_AUTH_MODE", "iap")
        monkeypatch.setenv("KUBEORBIT_VIEWER_EMAILS", "auditor@corp.com")
        monkeypatch.setenv("KUBEORBIT_ADMIN_EMAILS", "admin@corp.com")
        config.get_runtime_env.cache_clear()

        r_viewer = await async_client.get(
            "/api/status",
            headers={"X-Goog-Authenticated-User-Email": "accounts.google.com:auditor@corp.com"},
        )
        assert r_viewer.status_code == 200
        v_ent = r_viewer.json()["enterprise"]
        assert v_ent["enabled"] is True
        assert [m["id"] for m in v_ent["ui_modules"]] == ["ent-rbac"]

        r_admin = await async_client.get(
            "/api/status",
            headers={"X-Goog-Authenticated-User-Email": "accounts.google.com:admin@corp.com"},
        )
        assert r_admin.status_code == 200
        assert [m["id"] for m in r_admin.json()["enterprise"]["ui_modules"]] == ["ent-rbac", "ent-admin"]

    def test_sdk_lists_rbac_resources_and_maps_403_to_resource_forbidden(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import kubernetes.client
        from kubernetes.client.rest import ApiException

        calls: list[tuple[str, str | None]] = []

        class _FakeRbacApi:
            def __init__(self, api_client=None):
                pass

            def list_cluster_role(self, _request_timeout=8):
                calls.append(("clusterroles", None))
                return {"items": [{"metadata": {"name": "view"}}]}

            def list_cluster_role_binding(self, _request_timeout=8):
                calls.append(("clusterrolebindings", None))
                return {"items": [{"metadata": {"name": "view-binding"}}]}

            def list_namespaced_role(self, namespace, _request_timeout=8):
                calls.append(("roles", namespace))
                return {"items": [{"metadata": {"name": "pod-reader", "namespace": namespace}}]}

            def list_role_for_all_namespaces(self, _request_timeout=8):
                calls.append(("roles", None))
                return {"items": []}

            def list_namespaced_role_binding(self, namespace, _request_timeout=8):
                calls.append(("rolebindings", namespace))
                raise ApiException(status=403, reason="Forbidden")

            def list_role_binding_for_all_namespaces(self, _request_timeout=8):
                calls.append(("rolebindings", None))
                return {"items": []}

        class _FakeClient:
            def sanitize_for_serialization(self, obj):
                return obj

        monkeypatch.setattr(_sdk, "_get_or_create_sdk_client", lambda ctx, kc: _FakeClient())
        monkeypatch.setattr(kubernetes.client, "RbacAuthorizationV1Api", _FakeRbacApi)

        items_cr, ok_cr = _sdk._sync_sdk_list("ctx", tmp_path / "k.yaml", "clusterroles")
        assert ok_cr is True and items_cr == [{"metadata": {"name": "view"}}]

        items_crb, ok_crb = _sdk._sync_sdk_list("ctx", tmp_path / "k.yaml", "clusterrolebindings")
        assert ok_crb is True and items_crb == [{"metadata": {"name": "view-binding"}}]

        items_r, ok_r = _sdk._sync_sdk_list("ctx", tmp_path / "k.yaml", "roles", "demo")
        assert ok_r is True and items_r == [{"metadata": {"name": "pod-reader", "namespace": "demo"}}]

        with pytest.raises(_cli.ResourceForbiddenError):
            _sdk._sync_sdk_list("ctx", tmp_path / "k.yaml", "rolebindings", "demo")

    def test_extension_mcp_tool_registration_and_clean_reset(self):
        async def sample_readonly_tool(cluster_id: schemas.ClusterIdParam) -> dict:
            """Sample strictly read-only extension MCP tool."""
            return {"cluster_id": cluster_id}

        spec = schemas.ToolSpec(
            title="Sample Extension Tool",
            params={"cluster_id": schemas.ClusterIdParam},
            output_model=schemas._FlexibleOutput,
        )
        assert len(mcp_server.get_mcp_tool_catalog()) == 30

        mcp_server.register_extension_tool(sample_readonly_tool, spec)
        catalog = mcp_server.get_mcp_tool_catalog()
        assert len(catalog) == 31
        added = next(e for e in catalog if e["name"] == "sample_readonly_tool")
        assert added["read_only"] is True
        assert added["destructive"] is False

        tools = asyncio.run(mcp_server.mcp.list_tools())
        assert len(tools) == 31

        mcp_server.reset_extension_tools_for_testing()
        assert len(mcp_server.get_mcp_tool_catalog()) == 30
        assert len(asyncio.run(mcp_server.mcp.list_tools())) == 30

    def test_normalize_pod_extracts_service_account(self):
        explicit = normalize._normalize_pod(
            {
                "metadata": {"name": "ci-runner-7d9f", "namespace": "payments"},
                "spec": {"serviceAccountName": "ci-bot", "containers": []},
                "status": {"phase": "Running"},
            }
        )
        assert explicit["service_account"] == "ci-bot"

        defaulted = normalize._normalize_pod(
            {
                "metadata": {"name": "anon-pod", "namespace": "default"},
                "spec": {"containers": []},
                "status": {"phase": "Running"},
            }
        )
        assert defaulted["service_account"] == "default"
