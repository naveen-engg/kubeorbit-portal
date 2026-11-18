"""Comprehensive Unit Tests for KubeOrbit modules:
- config.py (ClusterConfig, ConfigStore, add_cluster_to_config, delete_cluster_from_config)
- kube_client/normalize.py (CPU/Memory parsers, Pod/Node/Workload/ConfigMap/Secret normalizers)
- auth.py (Portal auth modes: iap, azure_ad, api_key, none + cluster auth caching & regeneration)
- kube_client/_cache.py & kube_client/audit.py (TTLCache & Audit ring buffer)

Covers happy paths, boundary values, null/empty inputs, and expected failure modes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import auth
import config
from config import (
    ClusterConfig,
    ConfigError,
    ConfigStore,
    RawConfigFile,
    add_cluster_to_config,
    delete_cluster_from_config,
    get_config_store,
)
from kube_client._cache import TTLCache
from kube_client.audit import _AUDIT_LOGS, _MAX_AUDIT_LOGS, get_audit_logs, record_audit_event
from kube_client.normalize import (
    _format_cpu_short,
    _format_mem_short,
    _normalize_configmap,
    _normalize_cronjob,
    _normalize_job,
    _normalize_node,
    _normalize_pod,
    _normalize_secret_meta,
    _normalize_service,
    _normalize_workload,
    _parse_cpu_m,
    _parse_mem_mi,
)
from providers.base import BaseKubeProvider, ClusterAuthResult, ProviderAuthError, ProviderIdentity


def _make_request(headers: Dict[str, str] | None = None) -> Request:
    raw_headers = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in (headers or {}).items()
    ]
    scope = {"type": "http", "method": "GET", "path": "/api/status", "headers": raw_headers}
    return Request(scope)


# =========================================================================== #
# 1. config.py Unit Tests
# =========================================================================== #


class TestConfigModule:
    def test_cluster_config_happy_path_and_boundaries(self):
        # Single-character ID boundary
        c1 = ClusterConfig(id="a", name="Single", provider="gke", environment="sandbox")
        assert c1.id == "a"
        assert c1.config == {}

        # Standard hyphenated ID
        c2 = ClusterConfig(
            id="obs-sbx-01",
            name="OBS Sandbox",
            provider="local",
            environment="prod",
            description="Test",
            config={"kubeconfig_path": "~/.kube/config"},
        )
        assert c2.environment == "prod"

    @pytest.mark.parametrize(
        "invalid_id",
        [
            "",
            "-leading-hyphen",
            "UPPERCASE",
            "has_underscore",
            "has space",
            "../../etc/passwd",
        ],
    )
    def test_cluster_config_rejects_invalid_ids(self, invalid_id: str):
        with pytest.raises(ValueError, match="cluster id"):
            ClusterConfig(id=invalid_id, name="Test", provider="gke", environment="sandbox")

    @pytest.mark.parametrize("invalid_env", ["", "dev", "staging", "PROD", "production"])
    def test_cluster_config_rejects_invalid_environments(self, invalid_env: str):
        with pytest.raises(ValueError, match="environment"):
            ClusterConfig(id="valid-id", name="Test", provider="gke", environment=invalid_env)

    def test_config_store_loads_and_indexes_clusters(self):
        store = get_config_store()
        assert store.cluster_count == 2
        obs = store.get_cluster("obs-sbx")
        assert obs.name == "OBS SBX"
        assert obs.team_id == "platform"
        assert obs.team_name == "Platform Engineering"

        with pytest.raises(KeyError):
            store.get_cluster("non-existent-id")

    def test_config_store_namespace_exclusion_globs(self):
        store = get_config_store()
        assert store.is_namespace_excluded("kube-system") is True
        assert store.is_namespace_excluded("gke-managed-cim") is True
        assert store.is_namespace_excluded("gke-managed") is True
        assert store.is_namespace_excluded("kubeorbit-demo") is False
        assert store.is_namespace_excluded("") is False

    def test_config_store_rejects_mismatched_env_or_duplicate_ids(self):
        # Environment mismatch between group and cluster
        raw_mismatch = RawConfigFile.model_validate(
            {
                "teams": [
                    {
                        "id": "t1",
                        "name": "Team 1",
                        "environments": [
                            {
                                "id": "sandbox",
                                "clusters": [
                                    {
                                        "id": "c1",
                                        "name": "C1",
                                        "provider": "gke",
                                        "environment": "prod",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        )
        with pytest.raises(ConfigError, match="does not match its parent group"):
            ConfigStore(raw_mismatch)

        # Duplicate cluster ID across teams
        raw_dup = RawConfigFile.model_validate(
            {
                "teams": [
                    {
                        "id": "t1",
                        "name": "Team 1",
                        "environments": [
                            {
                                "id": "sandbox",
                                "clusters": [{"id": "dup-1", "name": "C1", "provider": "gke", "environment": "sandbox"}],
                            },
                            {
                                "id": "prod",
                                "clusters": [{"id": "dup-1", "name": "C2", "provider": "eks", "environment": "prod"}],
                            },
                        ],
                    }
                ]
            }
        )
        with pytest.raises(ConfigError, match="duplicate cluster id"):
            ConfigStore(raw_dup)

    def test_add_and_update_cluster_in_place_and_across_environments(self):
        # 1. Add a second cluster to sandbox
        add_cluster_to_config(
            team_id="platform",
            team_name="Platform Engineering",
            environment="sandbox",
            cluster_data={
                "id": "second-sbx",
                "name": "Second Sandbox",
                "provider": "local",
                "environment": "sandbox",
                "description": "Second",
                "config": {"kubeconfig_path": "~/.kube/config"},
            },
        )
        store = get_config_store()
        assert [c.id for c in store.list_clusters()] == ["obs-sbx", "second-sbx", "eks-prod-1"]

        # 2. Update first cluster in-place (rename OBS SBX -> Renamed GKE) and verify order preserved
        add_cluster_to_config(
            team_id="platform",
            team_name="Platform Engineering",
            environment="sandbox",
            cluster_data={
                "id": "obs-sbx",
                "name": "Renamed GKE",
                "provider": "gke",
                "environment": "sandbox",
                "description": "Updated description",
                "config": {"project": "adk-enterprise-observability", "zone": "us-central1-a", "cluster_name": "kubeorbit-gke"},
            },
        )
        store = get_config_store()
        assert [c.id for c in store.list_clusters()] == ["obs-sbx", "second-sbx", "eks-prod-1"]
        assert store.get_cluster("obs-sbx").name == "Renamed GKE"

        # 3. Move obs-sbx from sandbox to nonprod without causing a duplicate ID error
        add_cluster_to_config(
            team_id="platform",
            team_name="Platform Engineering",
            environment="nonprod",
            cluster_data={
                "id": "obs-sbx",
                "name": "Renamed GKE NonProd",
                "provider": "gke",
                "environment": "nonprod",
                "description": "Moved to nonprod",
                "config": {"project": "adk-enterprise-observability", "zone": "us-central1-a", "cluster_name": "kubeorbit-gke"},
            },
        )
        store = get_config_store()
        assert store.cluster_count == 3
        assert store.get_cluster("obs-sbx").environment == "nonprod"
        assert store.get_cluster("obs-sbx").name == "Renamed GKE NonProd"

    def test_delete_cluster_and_cleanup_kubeconfig(self, isolated_env: Dict[str, Any]):
        kcfg = isolated_env["kubeconfig_home"] / "obs-sbx.yaml"
        kcfg.write_text("apiVersion: v1\n", encoding="utf-8")
        assert kcfg.exists()

        store = delete_cluster_from_config("obs-sbx")
        assert store.cluster_count == 1
        assert not kcfg.exists()

        # Deleting a non-existent cluster is a safe no-op
        store2 = delete_cluster_from_config("does-not-exist")
        assert store2.cluster_count == 1


# =========================================================================== #
# 2. kube_client/normalize.py Unit Tests
# =========================================================================== #


class TestNormalizeModule:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("", 0),
            (None, 0),
            ("0", 0),
            ("250m", 250),
            ("10.5m", 10),
            ("1", 1000),
            ("1.5", 1500),
            ("500000000n", 500),
            ("100n", 1),  # min 1m when nanocores > 0
            ("invalid", 0),
            ("abcm", 0),
            ("abcn", 0),
        ],
    )
    def test_parse_cpu_m(self, raw: Any, expected: int):
        assert _parse_cpu_m(raw) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("", 0),
            (None, 0),
            ("0", 0),
            ("1024Ki", 1),
            ("256Mi", 256),
            ("2Gi", 2048),
            ("1Ti", 1048576),
            ("1048576", 1),  # raw bytes
            ("500M", 477),
            ("invalid", 0),
            ("xyzMi", 0),
        ],
    )
    def test_parse_mem_mi(self, raw: Any, expected: int):
        assert _parse_mem_mi(raw) == expected

    def test_format_cpu_and_mem_short(self):
        assert _format_cpu_short(0) == "—"
        assert _format_cpu_short(-10) == "—"
        assert _format_cpu_short(250) == "250m"
        assert _format_cpu_short(1000) == "1.00"
        assert _format_cpu_short(1500) == "1.5"

        assert _format_mem_short(0) == "—"
        assert _format_mem_short(256) == "256Mi"
        assert _format_mem_short(1024) == "1Gi"
        assert _format_mem_short(1536) == "1.5Gi"

    def test_normalize_pod_happy_and_edge_cases(self):
        # Empty / minimal pod dict
        empty_pod = _normalize_pod({})
        assert empty_pod["name"] is None
        assert empty_pod["status"] == "Unknown"
        assert empty_pod["ready"] == "0/0"
        assert empty_pod["restarts"] == 0
        assert empty_pod["qos_class"] == "BestEffort"
        assert empty_pod["resources"]["req_cpu_str"] == "—"

        # CrashLoopBackOff pod with resources and ownerReferences
        crash_pod = _normalize_pod(
            {
                "metadata": {
                    "name": "faulty-worker-abc",
                    "namespace": "kubeorbit-demo",
                    "creationTimestamp": "2026-11-06T12:00:00Z",
                    "ownerReferences": [{"kind": "ReplicaSet", "name": "faulty-worker-7f8"}],
                    "labels": {"app": "faulty-worker"},
                },
                "spec": {
                    "nodeName": "node-1",
                    "containers": [
                        {
                            "name": "worker",
                            "resources": {
                                "requests": {"cpu": "20m", "memory": "32Mi"},
                                "limits": {"cpu": "100m", "memory": "64Mi"},
                            },
                        }
                    ],
                },
                "status": {
                    "phase": "Running",
                    "podIP": "10.0.0.15",
                    "qosClass": "Burstable",
                    "containerStatuses": [
                        {
                            "name": "worker",
                            "image": "busybox:1.36",
                            "ready": False,
                            "restartCount": 5,
                            "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                        }
                    ],
                },
            }
        )
        assert crash_pod["status"] == "Unhealthy"
        assert crash_pod["reason"] == "CrashLoopBackOff"
        assert crash_pod["restarts"] == 5
        assert crash_pod["ready"] == "0/1"
        assert crash_pod["owner"] == "ReplicaSet/faulty-worker-7f8"
        assert crash_pod["resources"]["req_cpu_m"] == 20
        assert crash_pod["resources"]["lim_mem_mi"] == 64

    def test_normalize_node_conditions_and_taints(self):
        node = _normalize_node(
            {
                "metadata": {
                    "name": "gke-node-1",
                    "labels": {
                        "cloud.google.com/gke-nodepool": "default-pool",
                        "topology.kubernetes.io/zone": "us-central1-a",
                        "node.kubernetes.io/instance-type": "e2-medium",
                        "node-role.kubernetes.io/control-plane": "",
                    },
                },
                "spec": {
                    "taints": [{"key": "dedicated", "value": "gpu", "effect": "NoSchedule"}]
                },
                "status": {
                    "conditions": [
                        {"type": "Ready", "status": "True"},
                        {"type": "MemoryPressure", "status": "True"},
                    ],
                    "addresses": [{"type": "InternalIP", "address": "10.128.0.2"}],
                    "capacity": {"cpu": "2", "memory": "4096Ki", "pods": "110"},
                    "nodeInfo": {"kubeletVersion": "v1.36.4", "architecture": "amd64"},
                },
            }
        )
        assert node["status"] == "Ready"
        assert node["roles"] == ["control-plane"]
        assert node["node_pool"] == "default-pool"
        assert node["conditions"]["MemoryPressure"] is True
        assert node["conditions"]["DiskPressure"] is False
        assert node["taints"] == ["dedicated=gpu:NoSchedule"]

    def test_normalize_workloads_configmaps_and_secrets(self):
        # ConfigMap truncation at >200 chars
        long_val = "x" * 250
        cm = _normalize_configmap(
            {
                "metadata": {"name": "app-cfg", "namespace": "default"},
                "data": {"short": "ok", "long": long_val, "empty": None},
                "binaryData": {"bin": "AAA="},
            }
        )
        assert cm["key_count"] == 4
        assert cm["data_preview"]["short"] == "ok"
        assert cm["data_preview"]["empty"] == ""
        assert cm["data_preview"]["long"].endswith("…(truncated)")

        # Secret metadata never exposes raw secret values
        sec = _normalize_secret_meta(
            {
                "metadata": {"name": "db-pass", "namespace": "default"},
                "type": "Opaque",
                "data": {"password": "c2VjcmV0", "username": "YWRtaW4="},
            }
        )
        assert sec["key_count"] == 2
        assert sec["keys"] == ["password", "username"]
        assert "data" not in sec
        assert "c2VjcmV0" not in str(sec)

        # Workload (Deployment vs DaemonSet), Job, CronJob, Service
        ds = _normalize_workload(
            {"metadata": {"name": "mon"}, "status": {"desiredNumberScheduled": 3, "numberReady": 2}},
            "daemonset",
        )
        assert ds["healthy"] is False
        job = _normalize_job({"metadata": {"name": "mig"}, "spec": {"completions": 1}, "status": {"succeeded": 1, "failed": 0}})
        assert job["healthy"] is True
        cj = _normalize_cronjob({"metadata": {"name": "sync"}, "spec": {"schedule": "*/5 * * * *"}, "status": {"active": [{}, {}]}})
        assert cj["active_jobs"] == 2
        svc = _normalize_service({"metadata": {"name": "web"}, "spec": {"type": "LoadBalancer"}, "status": {"loadBalancer": {"ingress": [{"ip": "34.1.2.3"}]}}})
        assert svc["external_ips"] == ["34.1.2.3"]


# =========================================================================== #
# 3. auth.py Unit Tests
# =========================================================================== #


class DummyProvider(BaseKubeProvider):
    def __init__(self, cluster: ClusterConfig, kubeconfig_dir: Path, should_fail: bool = False):
        self._kcfg = kubeconfig_dir / f"{cluster.id}.yaml"
        super().__init__(cluster, self._kcfg)
        self.should_fail = should_fail
        self.auth_calls = 0

    async def authenticate(self) -> ClusterAuthResult:
        self.auth_calls += 1
        if self.should_fail:
            raise ProviderAuthError("gcloud auth failed")
        self._kcfg.write_text("apiVersion: v1\ncurrent-context: test-ctx\n", encoding="utf-8")
        return ClusterAuthResult(
            context_name="test-ctx",
            kubeconfig_path=str(self._kcfg),
            authenticated_at="2026-11-06T00:00:00Z",
            identity=ProviderIdentity(provider="gke", account="test@example.com", project_or_subscription="proj"),
        )

    async def get_cluster_metadata(self):
        raise NotImplementedError

    def get_console_url(self):
        return None

    def get_workload_identity_info(self):
        return {}


class TestAuthModule:
    @pytest.mark.asyncio
    async def test_portal_auth_modes(self, monkeypatch: pytest.MonkeyPatch):
        # 1. IAP mode
        monkeypatch.setenv("KUBEORBIT_AUTH_MODE", "iap")
        config.get_runtime_env.cache_clear()
        with pytest.raises(HTTPException) as exc_info:
            await auth.get_current_user(_make_request())
        assert exc_info.value.status_code == 401

        user = await auth.get_current_user(
            _make_request({"X-Goog-Authenticated-User-Email": "accounts.google.com:alice@example.com"})
        )
        assert user.subject == "alice@example.com"
        assert user.method == "iap"

        # 2. Azure AD mode
        monkeypatch.setenv("KUBEORBIT_AUTH_MODE", "azure_ad")
        config.get_runtime_env.cache_clear()
        with pytest.raises(HTTPException) as exc_info:
            await auth.get_current_user(_make_request())
        assert exc_info.value.status_code == 401

        user = await auth.get_current_user(_make_request({"X-MS-CLIENT-PRINCIPAL-NAME": "bob@contoso.com"}))
        assert user.subject == "bob@contoso.com"
        assert user.method == "azure_ad"

        # 3. API Key mode
        monkeypatch.setenv("KUBEORBIT_AUTH_MODE", "api_key")
        monkeypatch.setenv("KUBEORBIT_API_KEY", "super-secret-key")
        config.get_runtime_env.cache_clear()
        with pytest.raises(HTTPException) as exc_info:
            await auth.get_current_user(_make_request({"X-API-Key": "wrong-key"}))
        assert exc_info.value.status_code == 401

        user = await auth.get_current_user(_make_request({"X-API-Key": "super-secret-key"}))
        assert user.subject == "api-key-client"
        assert user.method == "api_key"

        # 4. Unknown auth mode -> 500
        monkeypatch.setenv("KUBEORBIT_AUTH_MODE", "kerberos")
        config.get_runtime_env.cache_clear()
        with pytest.raises(HTTPException) as exc_info:
            await auth.get_current_user(_make_request())
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_ensure_cluster_authenticated_caching_and_regeneration(self, isolated_env: Dict[str, Any]):
        cluster = get_config_store().get_cluster("obs-sbx")
        provider = DummyProvider(cluster, isolated_env["kubeconfig_home"])

        # First call -> cache miss, writes kubeconfig
        res1 = await auth.ensure_cluster_authenticated(cluster, provider)
        assert res1.from_cache is False
        assert provider.auth_calls == 1

        # Second call -> cache hit
        res2 = await auth.ensure_cluster_authenticated(cluster, provider)
        assert res2.from_cache is True
        assert provider.auth_calls == 1

        # Delete kubeconfig file on disk -> auto-regenerates even when cache entry is still valid
        Path(res2.kubeconfig_path).unlink()
        res3 = await auth.ensure_cluster_authenticated(cluster, provider)
        assert provider.auth_calls == 2
        assert Path(res3.kubeconfig_path).exists()


# =========================================================================== #
# 4. TTLCache & Audit Module Unit Tests
# =========================================================================== #


class TestCacheAndAudit:
    @pytest.mark.asyncio
    async def test_ttl_cache_expiry_and_exception_propagation(self):
        cache = TTLCache()
        calls = 0

        async def ok_factory():
            nonlocal calls
            calls += 1
            return {"count": calls}

        val1, hit1 = await cache.get_or_set("k1", 60, ok_factory)
        assert hit1 is False and val1["count"] == 1

        val2, hit2 = await cache.get_or_set("k1", 60, ok_factory)
        assert hit2 is True and val2["count"] == 1

        # TTL = 0 expires immediately
        val3, hit3 = await cache.get_or_set("k_zero", 0, ok_factory)
        val4, hit4 = await cache.get_or_set("k_zero", 0, ok_factory)
        assert hit3 is False and hit4 is False
        assert calls == 3

        # Exception inside factory does not poison cache or deadlock lock
        async def failing_factory():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await cache.get_or_set("k_fail", 60, failing_factory)

        val5, hit5 = await cache.get_or_set("k_fail", 60, ok_factory)
        assert hit5 is False and val5["count"] == 4

    def test_audit_log_ring_buffer_and_filtering(self):
        for i in range(_MAX_AUDIT_LOGS + 10):
            record_audit_event(
                user="" if i == 0 else f"user-{i}",
                action="TEST_ACTION",
                cluster_id="obs-sbx" if i % 2 == 0 else "eks-prod-1",
                namespace="default",
                resource=f"pod-{i}",
            )
        assert len(_AUDIT_LOGS) == _MAX_AUDIT_LOGS
        obs_logs = get_audit_logs(cluster_id="obs-sbx", limit=20)
        assert len(obs_logs) == 20
        assert all(l["cluster_id"] == "obs-sbx" for l in obs_logs)
