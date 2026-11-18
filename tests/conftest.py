"""Shared pytest fixtures for KubeOrbit unit, integration, and concurrency tests.

Every test runs against an isolated temporary `clusters.yaml` and temporary
`kubeconfig_home` directory so tests never mutate the live `config/clusters.yaml`
or invoke real cloud CLIs (`gcloud`, `aws`, `az`, `kubectl`).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import AsyncGenerator

import httpx
import pytest
import pytest_asyncio
import yaml

import auth
import config
import kube_client
from app import app
from kube_client import _cache as kc_cache
from kube_client import _sdk as kc_sdk
from kube_client import audit as kc_audit


SAMPLE_CONFIG_DICT = {
    "settings": {
        "excluded_namespace_patterns": ["kube-system", "kube-public", "gke-managed*"],
        "default_refresh_interval_seconds": 30,
        "allowed_refresh_intervals_seconds": [15, 30, 60, 120],
        "kube_cache_ttl_seconds": 15,
        "cluster_meta_cache_ttl_seconds": 120,
        "auth_cache_ttl_seconds": 600,
        "cli_timeout_seconds": 45,
    },
    "teams": [
        {
            "id": "platform",
            "name": "Platform Engineering",
            "environments": [
                {
                    "id": "sandbox",
                    "clusters": [
                        {
                            "id": "obs-sbx",
                            "name": "OBS SBX",
                            "provider": "gke",
                            "environment": "sandbox",
                            "description": "GKE cluster OBS SBX in adk-enterprise-observability",
                            "config": {
                                "project": "adk-enterprise-observability",
                                "zone": "us-central1-a",
                                "cluster_name": "kubeorbit-gke",
                            },
                        }
                    ],
                },
                {
                    "id": "prod",
                    "clusters": [
                        {
                            "id": "eks-prod-1",
                            "name": "EKS Production",
                            "provider": "eks",
                            "environment": "prod",
                            "description": "Production EKS cluster",
                            "config": {
                                "region": "us-east-1",
                                "cluster_name": "prod-eks",
                            },
                        }
                    ],
                },
            ],
        }
    ],
}


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect config path and kubeconfig home to tmp_path and clear all caches."""
    cfg_file = tmp_path / "clusters.yaml"
    cfg_file.write_text(yaml.dump(SAMPLE_CONFIG_DICT, sort_keys=False), encoding="utf-8")

    kcfg_dir = tmp_path / "kubeconfigs"
    kcfg_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("KUBEORBIT_CONFIG_PATH", str(cfg_file))
    monkeypatch.setenv("KUBEORBIT_KUBECONFIG_HOME", str(kcfg_dir))
    monkeypatch.setenv("KUBEORBIT_AUTH_MODE", "none")
    monkeypatch.setenv("USE_GCP_SECRET_MANAGER", "false")

    config.get_config_path.cache_clear()
    config.get_config_store.cache_clear()
    config.get_runtime_env.cache_clear()
    kc_cache._cache.clear()
    auth._auth_cache.clear()
    kc_sdk._api_client_pool.clear()
    kc_audit._AUDIT_LOGS.clear()

    yield {
        "config_file": cfg_file,
        "kubeconfig_home": kcfg_dir,
        "tmp_path": tmp_path,
    }

    config.get_config_path.cache_clear()
    config.get_config_store.cache_clear()
    config.get_runtime_env.cache_clear()
    kc_cache._cache.clear()
    auth._auth_cache.clear()
    kc_sdk._api_client_pool.clear()
    kc_audit._AUDIT_LOGS.clear()


@pytest_asyncio.fixture
async def async_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
