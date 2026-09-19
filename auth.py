"""
Two distinct kinds of "auth" live in this module, and they must not be
confused:

1. Portal auth -- who is allowed to use the KubeOrbit web UI/API at all.
   Controlled by KUBEORBIT_AUTH_MODE:
     - "iap"      trust GCP Identity-Aware Proxy's validated identity header.
                  Only correct if the app is deployed *behind* IAP (Cloud Run
                  + IAP, see README) so the header cannot be spoofed directly.
     - "azure_ad" trust Azure App Service/Container Apps Easy Auth's
                  validated principal header. Same caveat: only correct
                  behind Easy Auth.
     - "api_key"  shared-secret header, for environments without IAP/Entra
                  fronting (e.g. bare EKS deployments without an AWS-native
                  equivalent of IAP).
     - "none"     no portal auth (local development only). Logs a loud
                  warning on every request so it's impossible to miss in prod
                  logs if left on by accident.

2. Cluster/provider auth -- getting kubectl access to a specific cloud
   cluster, delegated to providers/{gke,eks,aks}.py. This module just adds a
   cache in front of provider.authenticate() so switching back to a
   recently-used cluster doesn't re-shell out to gcloud/aws/az on every poll,
   and logs every attempt/failure clearly per the audit requirements.
"""
from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request

from config import ClusterConfig, get_config_store, get_runtime_env
from kube_client import TTLCache
from providers.base import BaseKubeProvider, ClusterAuthResult, ProviderError

logger = logging.getLogger("kubeorbit.auth")

_auth_cache = TTLCache()


@dataclass
class CurrentUser:
    subject: str
    method: str


def _from_iap_header(request: Request) -> Optional[CurrentUser]:
    header = request.headers.get("X-Goog-Authenticated-User-Email")
    if not header:
        return None
    # IAP sends "accounts.google.com:user@example.com"
    subject = header.split(":")[-1]
    return CurrentUser(subject=subject, method="iap")


def _from_azure_easy_auth_header(request: Request) -> Optional[CurrentUser]:
    header = request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME")
    if not header:
        return None
    return CurrentUser(subject=header, method="azure_ad")


def _from_api_key(request: Request, expected_key: Optional[str]) -> Optional[CurrentUser]:
    if not expected_key:
        return None
    provided = request.headers.get("X-API-Key")
    if provided and hmac.compare_digest(provided, expected_key):
        return CurrentUser(subject="api-key-client", method="api_key")
    return None


async def get_current_user(request: Request) -> CurrentUser:
    """FastAPI dependency guarding every /api/* route except /api/health."""
    env = get_runtime_env()
    mode = env.auth_mode.lower()

    if mode == "iap":
        user = _from_iap_header(request)
        if user is None:
            raise HTTPException(status_code=401, detail="IAP identity header missing; app must run behind Identity-Aware Proxy")
        return user

    if mode == "azure_ad":
        user = _from_azure_easy_auth_header(request)
        if user is None:
            raise HTTPException(status_code=401, detail="Azure AD identity header missing; app must run behind App Service/Container Apps Easy Auth")
        return user

    if mode == "api_key":
        user = _from_api_key(request, env.api_key)
        if user is None:
            raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header")
        return user

    if mode != "none":
        raise HTTPException(status_code=500, detail=f"unknown KUBEORBIT_AUTH_MODE '{env.auth_mode}'")

    logger.warning("KUBEORBIT_AUTH_MODE=none -- portal auth is disabled, request unauthenticated")
    return _from_iap_header(request) or _from_azure_easy_auth_header(request) or CurrentUser(subject="anonymous", method="none")


# --------------------------------------------------------------------------- #
# Cluster/provider auth orchestration
# --------------------------------------------------------------------------- #

async def ensure_cluster_authenticated(
    cluster: ClusterConfig, provider: BaseKubeProvider, user: Optional[CurrentUser] = None
) -> ClusterAuthResult:
    """Authenticate `provider` against its cloud, using a short-lived cache so
    repeated dashboard polling of the same cluster doesn't re-invoke
    gcloud/aws/az on every request."""
    settings = get_config_store().settings
    cache_key = f"cluster-auth:{cluster.id}"
    who = user.subject if user else "unknown"

    async def factory() -> ClusterAuthResult:
        logger.info("Authenticating cluster '%s' (provider=%s) on behalf of %s", cluster.id, cluster.provider, who)
        try:
            result = await provider.authenticate()
        except ProviderError as exc:
            logger.error("Cluster auth FAILED for '%s' (provider=%s): %s", cluster.id, cluster.provider, exc)
            raise
        logger.info(
            "Cluster '%s' authenticated -> context=%s identity=%s",
            cluster.id, result.context_name, result.identity.account,
        )
        return result

    result, from_cache = await _auth_cache.get_or_set(cache_key, settings.auth_cache_ttl_seconds, factory)
    
    # If returned from cache but file was deleted/missing from disk, re-authenticate
    from pathlib import Path
    if from_cache and not Path(result.kubeconfig_path).exists():
        logger.info("Cached kubeconfig for '%s' missing on disk, regenerating...", cluster.id)
        _auth_cache.invalidate_prefix(cache_key)
        result, from_cache = await _auth_cache.get_or_set(cache_key, settings.auth_cache_ttl_seconds, factory)

    result.from_cache = from_cache
    return result


def invalidate_cluster_auth(cluster_id: str) -> None:
    _auth_cache.invalidate_prefix(f"cluster-auth:{cluster_id}")
