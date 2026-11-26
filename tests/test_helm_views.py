"""Unit and integration tests for Helm Releases, Version History, Diff Viewer,
Release Details, server-side secret masking, MCP catalog, and frontend wiring."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

import ai_agent
from kube_client.helm import (
    _normalize_helm_timestamp,
    compute_structured_diff,
    mask_helm_yaml,
)
from mcp_server.server import get_mcp_tool_catalog
from providers.base import BaseKubeProvider, ClusterAuthResult, ClusterMeta, ProviderIdentity


class _MockHelmProvider(BaseKubeProvider):
    def __init__(self, cluster, kubeconfig_home: Path):
        kcfg = kubeconfig_home / f"{cluster.id}.yaml"
        super().__init__(cluster, kcfg)
        self._kcfg = kcfg

    async def authenticate(self) -> ClusterAuthResult:
        self._kcfg.write_text("apiVersion: v1\ncurrent-context: mock-helm-ctx\n", encoding="utf-8")
        return ClusterAuthResult(
            context_name="mock-helm-ctx",
            kubeconfig_path=str(self._kcfg),
            authenticated_at="2026-11-24T08:00:00Z",
            identity=ProviderIdentity(provider=self.cluster.provider, account="ops@corp.com"),
        )

    async def get_cluster_metadata(self) -> ClusterMeta:
        return ClusterMeta(
            provider=self.cluster.provider,
            cluster_id=self.cluster.id,
            name=self.cluster.name,
            kubernetes_version="1.36.4",
            location="us-central1-a",
            status="RUNNING",
        )

    def get_console_url(self) -> str:
        return "https://console.cloud.google.com/kubernetes/list"

    def get_workload_identity_info(self) -> Dict[str, Any]:
        return {"enabled": True, "workload_pool": "adk-enterprise-observability.svc.id.goog"}


@pytest.fixture
def mock_helm_cluster(monkeypatch: pytest.MonkeyPatch, isolated_env: Dict[str, Any]):
    kcfg_home = isolated_env["kubeconfig_home"]
    monkeypatch.setattr("app.build_provider", lambda cfg: _MockHelmProvider(cfg, kcfg_home))

    sample_k8s = {
        "deployments": [
            {
                "metadata": {"name": "splunk-sc4s-deploy", "namespace": "splunk"},
                "spec": {"replicas": 1},
                "status": {"readyReplicas": 1},
            },
            {
                "metadata": {"name": "dynakube-operator", "namespace": "dynatrace"},
                "spec": {"replicas": 1},
                "status": {"readyReplicas": 0},
            },
        ],
        "pods": [],
        "statefulsets": [],
        "daemonsets": [],
        "jobs": [],
    }

    def fake_sync_sdk_list(
        context: str, kubeconfig_path: Path, resource: str, namespace: Optional[str] = None
    ) -> Tuple[Optional[List[Dict[str, Any]]], bool]:
        items = sample_k8s.get(resource, [])
        if namespace:
            items = [i for i in items if i.get("metadata", {}).get("namespace") == namespace]
        return items, True

    monkeypatch.setattr("kube_client._fetch._sync_sdk_list", fake_sync_sdk_list)

    releases_json = [
        {
            "name": "splunk-sc4s",
            "namespace": "splunk",
            "chart": "splunk-connect-for-syslog-3.48.0",
            "app_version": "3.48.0",
            "revision": "23",
            "status": "deployed",
            "updated": "2026-11-23 08:15:00.123456789 +0000 UTC",
        },
        {
            "name": "dynatrace-operator",
            "namespace": "dynatrace",
            "chart": "dynatrace-operator-1.10.1",
            "app_version": "1.10.1",
            "revision": "4",
            "status": "deployed",
            "updated": "2026-11-03 10:00:00.000 +0000 UTC",
        },
    ]

    history_by_release = {
        ("splunk", "splunk-sc4s"): [
            {
                "revision": 18,
                "updated": "2026-10-29 10:00:00.0 +0000 UTC",
                "status": "failed",
                "chart": "splunk-connect-for-syslog-3.38.0",
                "app_version": "3.38.0",
                "description": 'Upgrade "splunk-sc4s" failed: context deadline exceeded',
            },
            {
                "revision": 19,
                "updated": "2026-10-29 11:00:00.0 +0000 UTC",
                "status": "superseded",
                "chart": "splunk-connect-for-syslog-3.38.0",
                "app_version": "3.38.0",
                "description": "Upgrade complete",
            },
            {
                "revision": 23,
                "updated": "2026-11-23 08:15:00.0 +0000 UTC",
                "status": "deployed",
                "chart": "splunk-connect-for-syslog-3.48.0",
                "app_version": "3.48.0",
                "description": "Upgrade complete",
            },
        ],
        ("dynatrace", "dynatrace-operator"): [
            {
                "revision": 4,
                "updated": "2026-11-03 10:00:00.0 +0000 UTC",
                "status": "deployed",
                "chart": "dynatrace-operator-1.10.1",
                "app_version": "1.10.1",
                "description": "Upgrade complete",
            }
        ],
    }

    artifacts = {
        ("splunk", "splunk-sc4s", 19): {
            "values": (
                "existingSecret: splunk-existing-ref\n"
                "hec_token: super-secret-hec-stable-999\n"
                "apiToken: rotated-secret-v1\n"
                "env:\n"
                "  SC4S_SOURCE_STORE_RAWMSG: 'yes'\n"
                "  SC4S_SOURCE_TLS_OPTIONS: no-sslv2\n"
            ),
            "all_values": (
                "replicaCount: 1\n"
                "existingSecret: splunk-existing-ref\n"
                "hec_token: super-secret-hec-stable-999\n"
                "apiToken: rotated-secret-v1\n"
            ),
            "manifest": (
                "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: splunk-sc4s-deploy\n  namespace: splunk\n---\n"
                "apiVersion: v1\nkind: Secret\nmetadata:\n  name: splunk-sc4s-secret\n  namespace: splunk\n"
                "data:\n  hec: c3VwZXItc2VjcmV0LWgx\n"
            ),
            "notes": "Deployed splunk-sc4s rev 19.\n",
        },
        ("splunk", "splunk-sc4s", 23): {
            "values": (
                "existingSecret: splunk-existing-ref\n"
                "hec_token: super-secret-hec-stable-999\n"
                "apiToken: rotated-secret-v2\n"
                "env:\n"
                "  SC4S_LISTEN_DEFAULT_TLS_PORT: disabled\n"
                "  SC4S_SOURCE_STORE_RAWMSG: 'yes'\n"
            ),
            "all_values": (
                "replicaCount: 2\n"
                "existingSecret: splunk-existing-ref\n"
                "hec_token: super-secret-hec-stable-999\n"
                "apiToken: rotated-secret-v2\n"
            ),
            "manifest": (
                "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: splunk-sc4s-deploy\n  namespace: splunk\n---\n"
                "apiVersion: v1\nkind: Service\nmetadata:\n  name: splunk-sc4s-svc\n  namespace: splunk\n---\n"
                "apiVersion: v1\nkind: Secret\nmetadata:\n  name: splunk-sc4s-secret\n  namespace: splunk\n"
                "data:\n  hec: c3VwZXItc2VjcmV0LWgy\n"
            ),
            "notes": "Deployed splunk-sc4s rev 23 with Authorization Bearer abcdefghij123456!\n",
        },
        ("dynatrace", "dynatrace-operator", 4): {
            "values": "apiUrl: https://abc.live.dynatrace.com/api\n",
            "all_values": "apiUrl: https://abc.live.dynatrace.com/api\n",
            "manifest": (
                "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: dynakube-operator\n  namespace: dynatrace\n"
            ),
            "notes": "",
        },
    }

    from kube_client._cli import CLIError

    async def fake_run_cli(cmd: List[str], env=None, timeout=None, check=True):
        if cmd[:2] == ["helm", "list"]:
            return 0, json.dumps(releases_json), ""
        if cmd[:2] == ["helm", "history"]:
            rel = cmd[2]
            ns = cmd[cmd.index("-n") + 1]
            if (ns, rel) not in history_by_release:
                raise CLIError(cmd, 1, "Error: release: not found")
            return 0, json.dumps(history_by_release[(ns, rel)]), ""
        if cmd[:2] == ["helm", "get"]:
            kind = cmd[2]
            rel = cmd[3]
            ns = cmd[cmd.index("-n") + 1]
            rev = int(cmd[cmd.index("--revision") + 1]) if "--revision" in cmd else (
                23 if rel == "splunk-sc4s" else 4
            )
            entry = artifacts.get((ns, rel, rev))
            if not entry:
                raise CLIError(cmd, 1, "Error: release: not found")
            if kind == "values":
                return 0, entry["all_values" if "--all" in cmd else "values"], ""
            if kind == "manifest":
                return 0, entry["manifest"], ""
            if kind == "notes":
                return 0, entry["notes"], ""
        return 0, "{}", ""

    monkeypatch.setattr("kube_client.resources.run_cli", fake_run_cli)
    monkeypatch.setattr("kube_client._cli.run_cli", fake_run_cli)


class TestHelmUnitHelpers:
    def test_normalize_helm_timestamp(self):
        assert _normalize_helm_timestamp("2026-11-23 14:22:05.123456789 +0000 UTC") == "2026-11-23T14:22:05Z"
        assert _normalize_helm_timestamp("2026-11-23 09:22:05.12 -0500 EST") == "2026-11-23T14:22:05Z"
        assert _normalize_helm_timestamp("2026-11-23T14:22:05Z") == "2026-11-23T14:22:05Z"
        assert _normalize_helm_timestamp("") == ""
        assert _normalize_helm_timestamp("not-a-timestamp") == "not-a-timestamp"

    def test_secret_masking_values_and_manifests(self):
        values_yaml = (
            "existingSecret: my-existing-secret\n"
            "secretName: app-secret-ref\n"
            "usePassword: true\n"
            "enableAuth: false\n"
            "imagePullSecrets:\n"
            "  - name: regcred\n"
            "dbPassword: my-raw-password-42\n"
            "apiToken: dt0c01.FAKE.TOKEN99999\n"
            "dbUrl: postgres://admin:s3cretP@ss@db.internal:5432/app\n"
            "secrets:\n"
            "  enabled: true\n"
            "  customKey: ultra-secret-custom\n"
            "env:\n"
            "  - name: SPLUNK_HEC_TOKEN\n"
            "    value: hec-raw-token-123\n"
            "  - name: LOG_LEVEL\n"
            "    value: info\n"
        )
        masked = mask_helm_yaml(values_yaml, is_manifest=False)

        # Benign references and non-secret config preserved
        assert "my-existing-secret" in masked
        assert "app-secret-ref" in masked
        assert "regcred" in masked
        assert "LOG_LEVEL" in masked
        assert "info" in masked

        # Plaintext secrets are gone and replaced with fp fingerprints
        for leaked in (
            "my-raw-password-42",
            "dt0c01.FAKE.TOKEN99999",
            "s3cretP@ss",
            "ultra-secret-custom",
            "hec-raw-token-123",
        ):
            assert leaked not in masked
        assert "***MASKED*** (fp:" in masked

        # Same secret produces identical fingerprint; rotated secret changes fingerprint
        m1 = mask_helm_yaml("password: same-secret\n")
        m2 = mask_helm_yaml("password: same-secret\n")
        m3 = mask_helm_yaml("password: rotated-secret\n")
        assert m1 == m2
        assert m1 != m3

        # K8s Secret manifest data, stringData, and last-applied-configuration annotation
        raw_secret = base64.b64encode(b"raw-api-key-xyz").decode("ascii")
        manifest_yaml = (
            "apiVersion: v1\nkind: Secret\nmetadata:\n  name: app-sec\n"
            "  annotations:\n    kubectl.kubernetes.io/last-applied-configuration: '{\"data\":{\"key\":\"secret\"}}'\n"
            f"data:\n  token: {raw_secret}\nstringData:\n  plain: inline-secret\n"
        )
        masked_manifest = mask_helm_yaml(manifest_yaml, is_manifest=True)
        assert raw_secret not in masked_manifest
        assert "inline-secret" not in masked_manifest
        assert '{"data"' not in masked_manifest
        assert "***MASKED*** (fp:" in masked_manifest

        # Malformed YAML still falls back safely to regex masking
        bad_yaml = "auth: [\npassword: broken-yaml-secret-val\n"
        bad_masked = mask_helm_yaml(bad_yaml)
        assert "broken-yaml-secret-val" not in bad_masked
        assert "***MASKED*** (fp:" in bad_masked

    def test_compute_structured_diff_and_collapsed_blocks(self):
        old_lines = [f"line_{i}: unchanged" for i in range(1, 21)]
        new_lines = list(old_lines)
        new_lines[10] = "line_11: updated"
        diff = compute_structured_diff("\n".join(old_lines), "\n".join(new_lines), context=3)

        assert diff["has_changes"] is True
        assert diff["added_lines"] == 1
        assert diff["removed_lines"] == 1
        assert diff["blocks"][0]["type"] == "collapsed"
        assert diff["blocks"][0]["count"] == 7
        assert len(diff["blocks"][0]["lines"]) == 7

        hunk = diff["blocks"][1]
        assert hunk["type"] == "hunk"
        kinds = [ln["kind"] for ln in hunk["lines"]]
        assert "remove" in kinds and "add" in kinds and "context" in kinds

        identical = compute_structured_diff("a: 1\nb: 2\n", "a: 1\nb: 2\n")
        assert identical["has_changes"] is False
        assert identical["added_lines"] == 0
        assert identical["removed_lines"] == 0
        assert identical["blocks"][0]["type"] == "collapsed"
        assert identical["blocks"][0]["count"] == 2


class TestHelmHTTPIntegration:
    @pytest.mark.asyncio
    async def test_helm_list_history_details_and_diff_endpoints(
        self, async_client: httpx.AsyncClient, mock_helm_cluster
    ):
        # 1. List all releases (with resources, health status, normalized timestamps, namespaces)
        r_list = await async_client.get("/api/clusters/obs-sbx/helm")
        assert r_list.status_code == 200
        body = r_list.json()
        assert body["available"] is True
        assert body["namespaces"] == ["dynatrace", "splunk"]
        assert len(body["items"]) == 2
        splunk = next(i for i in body["items"] if i["name"] == "splunk-sc4s")
        assert splunk["updated"] == "2026-11-23T08:15:00Z"
        assert splunk["resource_count"] == 3
        deploy_res = next(r for r in splunk["resources"] if r["kind"] == "Deployment")
        assert deploy_res["status"] == "healthy"
        dyna = next(i for i in body["items"] if i["name"] == "dynatrace-operator")
        assert dyna["resources"][0]["status"] == "critical"

        # 2. Namespace filter preserves namespaces dropdown while filtering items
        r_filtered = await async_client.get("/api/clusters/obs-sbx/helm?namespace=splunk")
        assert r_filtered.status_code == 200
        assert len(r_filtered.json()["items"]) == 1
        assert r_filtered.json()["namespaces"] == ["dynatrace", "splunk"]

        # 3. History endpoint sorted newest-first with is_current
        r_hist = await async_client.get("/api/clusters/obs-sbx/helm/splunk/splunk-sc4s/history")
        assert r_hist.status_code == 200
        hist = r_hist.json()
        assert hist["current_revision"] == 23
        assert hist["earliest_revision"] == 18
        assert [i["revision"] for i in hist["items"]] == [23, 19, 18]
        assert hist["items"][0]["is_current"] is True
        assert hist["items"][1]["is_current"] is False
        assert hist["items"][2]["status"] == "failed"

        # 4. Release details endpoint masks secrets + returns private, no-store header
        r_det = await async_client.get("/api/clusters/obs-sbx/helm/splunk/splunk-sc4s")
        assert r_det.status_code == 200
        assert "no-store" in r_det.headers.get("cache-control", "")
        det = r_det.json()
        assert det["masked_server_side"] is True
        assert "splunk-existing-ref" in det["user_values_yaml"]
        assert "super-secret-hec-stable-999" not in det["user_values_yaml"]
        assert "rotated-secret-v2" not in det["user_values_yaml"]
        assert "c3VwZXItc2VjcmV0LWgy" not in det["manifest_yaml"]
        assert "abcdefghij123456" not in det["notes"]
        assert det["resource_count"] == 3

        # 5. Diff endpoint across values, all_values, and manifest
        r_diff = await async_client.get(
            "/api/clusters/obs-sbx/helm/splunk/splunk-sc4s/diff?from_revision=19&to_revision=23&mode=values"
        )
        assert r_diff.status_code == 200
        assert "no-store" in r_diff.headers.get("cache-control", "")
        diff_json = r_diff.json()
        assert diff_json["masked_server_side"] is True
        assert diff_json["has_changes"] is True
        serialized_diff = json.dumps(diff_json)
        assert "super-secret-hec-stable-999" not in serialized_diff
        assert "rotated-secret-v1" not in serialized_diff
        assert "rotated-secret-v2" not in serialized_diff
        # Changed rotated apiToken produces a - and + with ***MASKED*** (fp:...)
        hunk_lines = [ln for b in diff_json["blocks"] if b["type"] == "hunk" for ln in b["lines"]]
        assert any(ln["kind"] == "remove" and "apiToken" in ln["text"] and "***MASKED*** (fp:" in ln["text"] for ln in hunk_lines)
        assert any(ln["kind"] == "add" and "apiToken" in ln["text"] and "***MASKED*** (fp:" in ln["text"] for ln in hunk_lines)

        r_manifest_diff = await async_client.get(
            "/api/clusters/obs-sbx/helm/splunk/splunk-sc4s/diff?from_revision=19&to_revision=23&mode=manifest"
        )
        assert r_manifest_diff.status_code == 200
        m_diff_str = json.dumps(r_manifest_diff.json())
        assert "c3VwZXItc2VjcmV0LWgx" not in m_diff_str
        assert "c3VwZXItc2VjcmV0LWgy" not in m_diff_str

        # 6. Error & validation checks
        r_404 = await async_client.get("/api/clusters/obs-sbx/helm/splunk/missing-rel/history")
        assert r_404.status_code == 404
        assert (await async_client.get("/api/clusters/obs-sbx/helm/splunk/--flag/history")).status_code == 422
        assert (
            await async_client.get(
                "/api/clusters/obs-sbx/helm/splunk/splunk-sc4s/diff?from_revision=19&to_revision=23&mode=badmode"
            )
        ).status_code == 422


class TestHelmContractsAndMCP:
    def test_frontend_and_agent_helm_wiring(self):
        root = Path(__file__).resolve().parent.parent
        html = (root / "static" / "index.html").read_text(encoding="utf-8")
        helm_js = (root / "static" / "helm.js").read_text(encoding="utf-8")
        agui_views = (root / "static" / "agui_views.js").read_text(encoding="utf-8")

        for token in (
            'data-view="helm"',
            'data-view="helm-history"',
            'data-view="helm-diff"',
            'data-view="ecosystem"',
            'id="view-helm"',
            'id="view-helm-history"',
            'id="view-helm-diff"',
            'id="helm-details-modal"',
            "helm.js",
        ):
            assert token in html

        assert "export function createHelmViewsController" in helm_js
        for tool_name in ("get_helm_release_details", "get_helm_release_history", "diff_helm_revisions"):
            assert tool_name in agui_views

        assert {"helm", "helm-history", "helm-diff"}.issubset(ai_agent._PORTAL_VIEWS)

        catalog = {entry["name"]: entry for entry in get_mcp_tool_catalog()}
        assert len(catalog) == 30
        for tool_name in ("get_helm_releases", "get_helm_release_details", "get_helm_release_history", "diff_helm_revisions"):
            assert catalog[tool_name]["read_only"] is True


class TestSecurityAndPerformanceGuards:
    def test_sync_sdk_list_handles_gateway_api_and_rejects_knative_services(self, monkeypatch: pytest.MonkeyPatch):
        import kubernetes.client
        from kube_client import _sdk

        calls: list[tuple[str, str]] = []

        class _FakeCustomObjectsApi:
            def __init__(self, api_client=None):
                pass

            def list_namespaced_custom_object(self, group, version, namespace, plural, _request_timeout=None):
                calls.append(("custom", f"{group}/{plural}"))
                return {"items": [{"metadata": {"name": "gw-1"}}]}

            def list_cluster_custom_object(self, group, version, plural, _request_timeout=None):
                calls.append(("custom_cluster", f"{group}/{plural}"))
                return {"items": []}

        class _FailCoreV1Api:
            def __init__(self, api_client=None):
                pass

            def list_service_for_all_namespaces(self, *args, **kwargs):
                raise AssertionError("CoreV1Api must NOT be called for Knative services.serving.knative.dev")

            def list_namespaced_service(self, *args, **kwargs):
                raise AssertionError("CoreV1Api must NOT be called for Knative services.serving.knative.dev")

        monkeypatch.setattr(_sdk, "_get_or_create_sdk_client", lambda ctx, kcfg: object())
        monkeypatch.setattr(kubernetes.client, "CustomObjectsApi", _FakeCustomObjectsApi)
        monkeypatch.setattr(kubernetes.client, "CoreV1Api", _FailCoreV1Api)

        gw_items, gw_handled = _sdk._sync_sdk_list("ctx", Path("/tmp/kcfg"), "gateways.gateway.networking.k8s.io", "default")
        assert gw_handled is True
        assert gw_items == [{"metadata": {"name": "gw-1"}}]
        assert ("custom", "gateway.networking.k8s.io/gateways") in calls

        knative_items, knative_handled = _sdk._sync_sdk_list("ctx", Path("/tmp/kcfg"), "services.serving.knative.dev", None)
        assert knative_handled is False
        assert knative_items is None

    @pytest.mark.asyncio
    async def test_get_pods_skip_metrics_avoids_kubectl_top(self, monkeypatch: pytest.MonkeyPatch):
        from kube_client import resources

        cli_called = False

        async def _fake_list(ctx, kcfg, res, ns, ttl, **kwargs):
            return (
                [
                    {
                        "metadata": {"name": "web-1", "namespace": "default"},
                        "spec": {"containers": [{"name": "app", "image": "nginx"}]},
                        "status": {"phase": "Running", "containerStatuses": [{"ready": True, "restartCount": 0}]},
                    }
                ],
                True,
            )

        async def _fail_run_cli(*args, **kwargs):
            nonlocal cli_called
            cli_called = True
            raise AssertionError("kubectl top pods must not run when include_metrics=False")

        monkeypatch.setattr(resources, "cached_kubectl_list", _fake_list)
        monkeypatch.setattr(resources, "run_cli", _fail_run_cli)

        pods = await resources.get_pods("ctx", Path("/tmp/kcfg"), "default", include_metrics=False)
        assert len(pods) == 1
        assert pods[0]["name"] == "web-1"
        assert cli_called is False

    @pytest.mark.asyncio
    async def test_get_resource_yaml_blocks_secrets(self):
        from kube_client.logs import get_resource_yaml

        for secret_kind in ("Secret", "secret", "secrets", "secrets.v1", "v1/Secret"):
            with pytest.raises(PermissionError):
                await get_resource_yaml("ctx", Path("/tmp/kcfg"), "default", secret_kind, "my-secret")

    @pytest.mark.asyncio
    async def test_cluster_refresh_endpoint_invalidates_server_data_cache(
        self,
        async_client: httpx.AsyncClient,
        isolated_env: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ):
        from kube_client._cache import _cache

        kcfg_home = isolated_env["kubeconfig_home"]
        monkeypatch.setattr("app.build_provider", lambda cfg: _MockHelmProvider(cfg, kcfg_home))

        await _cache.set("mock-helm-ctx:pods:_all_", 30, {"items": [{"name": "stale"}], "available": True})
        await _cache.set("provider-meta:obs-sbx", 120, {"stale": True})
        assert await _cache.get("mock-helm-ctx:pods:_all_") is not None
        assert await _cache.get("provider-meta:obs-sbx") is not None

        res = await async_client.post("/api/clusters/obs-sbx/refresh")
        assert res.status_code == 200
        assert res.json() == {"status": "ok", "cluster_id": "obs-sbx"}
        assert await _cache.get("mock-helm-ctx:pods:_all_") is None
        assert await _cache.get("provider-meta:obs-sbx") is None

        root = Path(__file__).resolve().parent.parent
        html = (root / "static" / "index.html").read_text(encoding="utf-8")
        app_js = (root / "static" / "app.js").read_text(encoding="utf-8")
        assert "diagnostics-reanalyze-btn" not in html
        assert "diagnostics-reanalyze-btn" not in app_js
        assert "refreshClusterCache" in app_js

    @pytest.mark.asyncio
    async def test_strict_read_only_mode_default_blocks_writes_and_exposes_status(
        self,
        async_client: httpx.AsyncClient,
        isolated_env: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ):
        kcfg_home = isolated_env["kubeconfig_home"]
        monkeypatch.setattr("app.build_provider", lambda cfg: _MockHelmProvider(cfg, kcfg_home))

        status_res = await async_client.get("/api/status")
        assert status_res.status_code == 200
        assert status_res.json()["read_only_mode"] is True

        rb_res = await async_client.post(
            "/api/clusters/obs-sbx/workloads/deployment/web/rollback?namespace=default",
            json={"to_revision": 1},
        )
        assert rb_res.status_code == 403
        assert "read-only" in rb_res.json()["detail"].lower()

        hitl = ai_agent.ui_propose_rollback("default", "deployment", "web", "CrashLoopBackOff")
        assert hitl["ok"] is False
        assert "read-only" in hitl["error"].lower()
