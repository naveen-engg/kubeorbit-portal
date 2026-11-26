"""Tests for strict read-only RBAC 403 graceful degradation on Secrets & optional resources."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import httpx
import pytest
from kubernetes.client.exceptions import ApiException

from kube_client import ResourceForbiddenError, cached_kubectl_list, get_secrets_metadata, get_storageclasses, run_cli
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
                account="ko-ro-kubeorbit-gke@example.iam.gserviceaccount.com",
                project_or_subscription="test-proj",
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

    def get_console_url(self) -> str:
        return "https://console.cloud.google.com/kubernetes/list"

    def get_workload_identity_info(self) -> Dict[str, Any]:
        return {"enabled": True}


class TestStrictReadonlyRBACFallback:
    @pytest.mark.asyncio
    async def test_run_cli_and_sdk_convert_403_to_resource_forbidden_error(
        self,
        isolated_env: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ):
        import kubernetes.client
        from kube_client import _cli, _sdk

        # 1. run_cli converts kubectl "(Forbidden)" stderr into ResourceForbiddenError
        monkeypatch.setattr(_cli.shutil, "which", lambda b: "/usr/bin/kubectl")
        monkeypatch.setattr(
            _cli,
            "_sync_subprocess_exec",
            lambda *args, **kwargs: (
                1,
                b"",
                b'Error from server (Forbidden): secrets is forbidden: User "ko-ro" cannot list',
            ),
        )
        with pytest.raises(ResourceForbiddenError):
            await run_cli(["kubectl", "get", "secrets", "-n", "default"])

        # 2. _sync_sdk_list converts ApiException(status=403) into ResourceForbiddenError
        class _ForbiddenCoreV1Api:
            def __init__(self, api_client=None):
                pass

            def list_namespaced_secret(self, *args, **kwargs):
                raise ApiException(status=403, reason="Forbidden")

        monkeypatch.setattr(_sdk, "_get_or_create_sdk_client", lambda ctx, kcfg: object())
        monkeypatch.setattr(kubernetes.client, "CoreV1Api", _ForbiddenCoreV1Api)

        with pytest.raises(ResourceForbiddenError):
            _sdk._sync_sdk_list("ctx-1", isolated_env["kubeconfig_home"] / "obs-sbx.yaml", "secrets", "default")

    @pytest.mark.asyncio
    async def test_optional_fetchers_allow_and_cache_403_while_required_calls_raise(
        self,
        isolated_env: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ):
        calls = {"secrets": 0, "storageclasses": 0}
        kcfg = isolated_env["kubeconfig_home"] / "obs-sbx.yaml"

        async def _fake_list(context, kubeconfig_path, resource, namespace, ttl, **kwargs):
            calls[resource] = calls.get(resource, 0) + 1
            raise ResourceForbiddenError(["kubectl", "get", resource], 1, f"{resource} is forbidden")

        monkeypatch.setattr("kube_client.resources.cached_kubectl_list", _fake_list)

        assert await get_secrets_metadata("ctx-1", kcfg, "default") == []
        assert await get_secrets_metadata("ctx-1", kcfg, "default") == []
        assert calls["secrets"] == 1  # negative cache skips repeat 403 calls

        assert await get_storageclasses("ctx-1", kcfg) == []
        assert calls["storageclasses"] == 1

        # Direct required calls via cached_kubectl_list still propagate 403 ResourceForbiddenError
        monkeypatch.setattr(
            "kube_client._fetch._sync_sdk_list",
            lambda *args, **kwargs: (_ for _ in ()).throw(ResourceForbiddenError(["sdk"], 403, "403")),
        )
        with pytest.raises(ResourceForbiddenError):
            await cached_kubectl_list("ctx-1", kcfg, "pods", "default", 15)

    @pytest.mark.asyncio
    async def test_namespaces_map_and_storage_survive_403_on_secrets_sa_storageclass(
        self,
        async_client: httpx.AsyncClient,
        isolated_env: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ):
        kcfg_home = isolated_env["kubeconfig_home"]
        monkeypatch.setattr("app.build_provider", lambda cfg: _StubProvider(cfg, kcfg_home))

        async def _fake_cli_kubectl(context, kubeconfig_path, args, check=True):
            res = args[1] if len(args) > 1 else ""
            if res in ("secrets", "serviceaccounts", "storageclasses"):
                raise ResourceForbiddenError(
                    ["kubectl", "get", res],
                    1,
                    f"Error from server (Forbidden): {res} is forbidden",
                )
            if res == "pods":
                return {
                    "items": [
                        {
                            "metadata": {"name": "web-1", "namespace": "default"},
                            "spec": {
                                "serviceAccountName": "default",
                                "volumes": [{"persistentVolumeClaim": {"claimName": "data-pvc"}}],
                                "containers": [{"name": "nginx", "image": "nginx"}],
                            },
                            "status": {
                                "phase": "Running",
                                "podIP": "10.0.0.5",
                                "containerStatuses": [{"ready": True, "restartCount": 0}],
                            },
                        }
                    ]
                }
            if res == "services":
                return {
                    "items": [
                        {
                            "metadata": {"name": "web-svc", "namespace": "default"},
                            "spec": {
                                "type": "ClusterIP",
                                "clusterIP": "10.1.0.10",
                                "ports": [{"port": 80, "targetPort": 80}],
                                "selector": {"app": "web"},
                            },
                        }
                    ]
                }
            if res == "configmaps":
                return {"items": [{"metadata": {"name": "app-cfg", "namespace": "default"}, "data": {"ENV": "prod"}}]}
            if res == "persistentvolumeclaims":
                return {
                    "items": [
                        {
                            "metadata": {"name": "data-pvc", "namespace": "default"},
                            "spec": {
                                "storageClassName": "standard-rwo",
                                "volumeName": "pv-1",
                                "accessModes": ["ReadWriteOnce"],
                                "resources": {"requests": {"storage": "1Gi"}},
                            },
                            "status": {"phase": "Bound", "capacity": {"storage": "1Gi"}},
                        }
                    ]
                }
            if res == "persistentvolumes":
                return {
                    "items": [
                        {
                            "metadata": {"name": "pv-1"},
                            "spec": {
                                "capacity": {"storage": "1Gi"},
                                "accessModes": ["ReadWriteOnce"],
                                "storageClassName": "standard-rwo",
                            },
                            "status": {"phase": "Bound"},
                        }
                    ]
                }
            return {"items": []}

        monkeypatch.setattr("kube_client._fetch._sync_sdk_list", lambda *args, **kwargs: (None, False))
        monkeypatch.setattr("kube_client._fetch.kubectl_json", _fake_cli_kubectl)

        r_ns = await async_client.get("/api/clusters/obs-sbx/namespaces/default/resources")
        assert r_ns.status_code == 200
        ns_body = r_ns.json()
        assert ns_body["secrets"] == []
        assert ns_body["serviceaccounts"] == []
        assert len(ns_body["pods"]) == 1
        assert len(ns_body["services"]) == 1
        assert len(ns_body["configmaps"]) == 1
        assert len(ns_body["pvcs"]) == 1

        r_map = await async_client.get("/api/clusters/obs-sbx/topology/map", params={"namespace": "default"})
        assert r_map.status_code == 200
        map_body = r_map.json()
        assert any(n["kind"] == "pod" for n in map_body["nodes"])
        assert not any(n["kind"] == "secret" for n in map_body["nodes"])

        r_stor = await async_client.get("/api/clusters/obs-sbx/topology/storage", params={"namespace": "default"})
        assert r_stor.status_code == 200
        stor_body = r_stor.json()
        assert any(n["kind"] == "pvc" for n in stor_body["nodes"])
        assert any(n["kind"] == "pod" for n in stor_body["nodes"])
