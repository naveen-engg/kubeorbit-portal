"""
Two distinct kinds of "auth" live in this module, and they must not be
confused:

1. Portal auth & 3-Tier RBAC -- who is allowed to use the KubeOrbit web UI/API
   and what role they hold (`viewer` < `operator` < `admin`):
   Controlled by KUBEORBIT_AUTH_MODE:
     - "iap"      trust GCP Identity-Aware Proxy's validated identity header
                  (and optionally verify `X-Goog-IAP-JWT-Assertion` when
                  `KUBEORBIT_IAP_AUDIENCE` is set).
     - "azure_ad" trust Azure App Service/Container Apps Easy Auth's
                  validated principal header.
     - "api_key"  shared-secret header (`X-API-Key`).
     - "none"     no portal auth (local development only).

   3-Tier RBAC Hierarchy:
     - "viewer":   Read-only access to clusters, workloads, topologies, events, logs, and YAML.
     - "operator": All viewer permissions + Secret Reveal (`POST /secrets/{name}/reveal`)
                   and Workload Rollback (`POST /workloads/{type}/{name}/rollback`).
     - "admin":    All operator permissions + Cluster Onboarding, Editing, and Deletion
                   (`/api/admin/clusters*`).

2. Cluster/provider auth -- getting kubectl access to a specific cloud
   cluster, delegated to providers/{gke,eks,aks}.py with singleflight TTL caching.
"""
from __future__ import annotations

import hmac
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException, Request

from config import ClusterConfig, get_config_store, get_runtime_env
from kube_client import TTLCache
from providers.base import BaseKubeProvider, ClusterAuthResult, ProviderError

logger = logging.getLogger("kubeorbit.auth")

_auth_cache = TTLCache()
_gcp_iam_lock = threading.Lock()
_gcp_iam_cache: Dict[str, Tuple[float, Dict[str, List[str]]]] = {}
_GCP_IAM_CACHE_TTL_SEC = 300.0

ROLE_RANK = {"none": 0, "viewer": 1, "operator": 2, "admin": 3}

GCP_CUSTOM_ROLE_SUFFIX_MAP = {
    "/roles/kubeorbitadmin": "admin",
    "/roles/kubeorbitoperator": "operator",
    "/roles/kubeorbitviewer": "viewer",
}


def invalidate_gcp_iam_cache() -> None:
    """Clear the cached GCP project IAM custom role bindings."""
    with _gcp_iam_lock:
        _gcp_iam_cache.clear()


def _fetch_gcp_iam_policy_from_api(project_id: str) -> Dict[str, List[str]]:
    """Query GCP Cloud Resource Manager v1 `projects:getIamPolicy` for custom
    roles `kubeorbitAdmin`, `kubeorbitOperator`, and `kubeorbitViewer`."""
    out: Dict[str, List[str]] = {"admin": [], "operator": [], "viewer": []}
    if not project_id:
        return out
    try:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        session = AuthorizedSession(credentials)
        url = f"https://cloudresourcemanager.googleapis.com/v1/projects/{project_id}:getIamPolicy"
        resp = session.post(url, json={}, timeout=8)
        if resp.status_code != 200:
            logger.warning(
                "GCP getIamPolicy returned HTTP %s for project '%s': %s",
                resp.status_code,
                project_id,
                resp.text[:200],
            )
            return out
        policy = resp.json() or {}
        for binding in policy.get("bindings", []):
            role_name = (binding.get("role") or "").strip().lower()
            members = [m.strip().lower() for m in (binding.get("members") or []) if m]
            for suffix, tier in GCP_CUSTOM_ROLE_SUFFIX_MAP.items():
                if role_name.endswith(suffix):
                    out[tier].extend(members)
    except Exception as exc:
        logger.warning("Could not fetch GCP IAM policy for project '%s': %s", project_id, exc)
    return out


def _get_gcp_custom_role_bindings(project_id: str) -> Dict[str, List[str]]:
    """Return cached `{ 'admin': [...], 'operator': [...], 'viewer': [...] }`
    member lists from GCP Project IAM for `project_id` (cached for 300s)."""
    now = time.monotonic()
    with _gcp_iam_lock:
        cached = _gcp_iam_cache.get(project_id)
        if cached and cached[0] > now:
            return cached[1]

    bindings = _fetch_gcp_iam_policy_from_api(project_id)
    with _gcp_iam_lock:
        _gcp_iam_cache[project_id] = (time.monotonic() + _GCP_IAM_CACHE_TTL_SEC, bindings)
    return bindings


def _matches_iam_member_list(email: str, members: List[str]) -> bool:
    """Return True if `email` matches an IAM member entry (`user:email`,
    `serviceaccount:email`, plain `email`, `*`, or `domain:corp.com`)."""
    if not email or not members:
        return False
    normalized = [m.strip().lower() for m in members if m]
    if (
        email in normalized
        or f"user:{email}" in normalized
        or f"serviceaccount:{email}" in normalized
        or "*" in normalized
        or "allauthenticatedusers" in normalized
    ):
        return True
    if "@" in email:
        domain = email.split("@", 1)[1]
        if f"domain:{domain}" in normalized:
            return True
    return False


@dataclass
class CurrentUser:
    subject: str
    method: str
    role: str = "admin"


def _verify_iap_jwt_if_configured(request: Request, audience: Optional[str]) -> Optional[str]:
    """If KUBEORBIT_IAP_AUDIENCE is configured, cryptographically verify
    Google IAP's signed `X-Goog-IAP-JWT-Assertion` header using google.oauth2.id_token."""
    if not audience:
        return None
    jwt_token = request.headers.get("X-Goog-IAP-JWT-Assertion")
    if not jwt_token:
        raise HTTPException(status_code=401, detail="Missing X-Goog-IAP-JWT-Assertion header required by KUBEORBIT_IAP_AUDIENCE")
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        claims = id_token.verify_token(
            jwt_token,
            google_requests.Request(),
            audience=audience,
            certs_url="https://www.gstatic.com/iap/verify/public_key",
        )
        return claims.get("email") or claims.get("sub")
    except Exception as exc:
        logger.warning("IAP JWT cryptographic verification failed: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid X-Goog-IAP-JWT-Assertion signature or audience") from exc


def _from_iap_header(request: Request, iap_audience: Optional[str] = None) -> Optional[CurrentUser]:
    verified_email = _verify_iap_jwt_if_configured(request, iap_audience)
    if verified_email:
        return CurrentUser(subject=verified_email.lower(), method="iap")

    header = request.headers.get("X-Goog-Authenticated-User-Email")
    if not header:
        return None
    # IAP sends "accounts.google.com:user@example.com"
    subject = header.split(":")[-1].strip().lower()
    return CurrentUser(subject=subject, method="iap")


def _from_azure_easy_auth_header(request: Request) -> Optional[CurrentUser]:
    header = request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME")
    if not header:
        return None
    return CurrentUser(subject=header.strip().lower(), method="azure_ad")


def _from_api_key(request: Request, expected_key: Optional[str]) -> Optional[CurrentUser]:
    if not expected_key:
        return None
    provided = request.headers.get("X-API-Key")
    if provided and hmac.compare_digest(provided, expected_key):
        return CurrentUser(subject="api-key-client", method="api_key")
    return None


def resolve_user_role(user: CurrentUser, team_id: Optional[str] = None) -> str:
    """Resolve the user's effective 3-tier role ('admin' | 'operator' | 'viewer' | 'none').

    Resolution hierarchy (highest matching rank wins):
    1. Global env vars (`KUBEORBIT_ADMIN_EMAILS`, `KUBEORBIT_OPERATOR_EMAILS`, `KUBEORBIT_VIEWER_EMAILS`).
    2. GCP Project Custom IAM Roles (`kubeorbitAdmin`, `kubeorbitOperator`, `kubeorbitViewer`)
       when `KUBEORBIT_GCP_IAM_RBAC=true`. Supports `user:<email>` and `domain:<domain>`.
    3. Team-level `rbac` in `clusters.yaml` (`admins`, `operators`, `viewers`).
    4. Fallback:
       - If `KUBEORBIT_STRICT_RBAC=true`, unlisted users resolve to `'none'` (Zero-Trust 403 Forbidden).
       - Otherwise, if any RBAC policy is configured, unlisted users default to least-privilege `'viewer'`,
         or `'admin'` if no RBAC policy is configured anywhere.
    """
    if user.method == "none":
        return "admin"

    env = get_runtime_env()
    subj = (user.subject or "").strip().lower()

    if _matches_iam_member_list(subj, env.admin_emails):
        return "admin"

    store = get_config_store()
    any_rbac_configured = bool(
        env.admin_emails or env.operator_emails or env.viewer_emails or env.gcp_iam_rbac_enabled or env.strict_rbac
    )

    highest_rank = 0

    if _matches_iam_member_list(subj, env.operator_emails):
        highest_rank = max(highest_rank, ROLE_RANK["operator"])
    elif _matches_iam_member_list(subj, env.viewer_emails):
        highest_rank = max(highest_rank, ROLE_RANK["viewer"])

    # Check GCP Project Custom IAM Roles (`kubeorbitAdmin`, `kubeorbitOperator`, `kubeorbitViewer`)
    if env.gcp_iam_rbac_enabled:
        gcp_bindings = _get_gcp_custom_role_bindings(env.gcp_project_id)
        if _matches_iam_member_list(subj, gcp_bindings.get("admin", [])):
            return "admin"
        if _matches_iam_member_list(subj, gcp_bindings.get("operator", [])):
            highest_rank = max(highest_rank, ROLE_RANK["operator"])
        elif _matches_iam_member_list(subj, gcp_bindings.get("viewer", [])):
            highest_rank = max(highest_rank, ROLE_RANK["viewer"])

    teams_to_check = []
    if team_id:
        t_obj = store.get_team(team_id)
        if t_obj:
            teams_to_check.append(t_obj)
    else:
        teams_to_check = list(store.teams)

    for t in store.teams:
        if t.rbac and (t.rbac.admins or t.rbac.operators or t.rbac.viewers):
            any_rbac_configured = True

    for t in teams_to_check:
        if not t.rbac:
            continue
        if _matches_iam_member_list(subj, t.rbac.admins):
            highest_rank = max(highest_rank, ROLE_RANK["admin"])
        elif _matches_iam_member_list(subj, t.rbac.operators):
            highest_rank = max(highest_rank, ROLE_RANK["operator"])
        elif _matches_iam_member_list(subj, t.rbac.viewers):
            highest_rank = max(highest_rank, ROLE_RANK["viewer"])

    if highest_rank == ROLE_RANK["admin"]:
        return "admin"
    if highest_rank == ROLE_RANK["operator"]:
        return "operator"
    if highest_rank == ROLE_RANK["viewer"]:
        return "viewer"

    if env.strict_rbac:
        return "none"

    return "viewer" if any_rbac_configured else "admin"


def require_role(user: CurrentUser, minimum_role: str, team_id: Optional[str] = None) -> str:
    """Enforce that `user` holds at least `minimum_role` ('viewer' | 'operator' | 'admin').
    Raises HTTP 403 Forbidden if insufficient."""
    effective_role = resolve_user_role(user, team_id=team_id)
    if ROLE_RANK.get(effective_role, 0) < ROLE_RANK.get(minimum_role, 99):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Zero-Trust Forbidden: action requires '{minimum_role}' role, but user '{user.subject}' "
                f"has '{effective_role}' role"
            ),
        )
    return effective_role


async def get_current_user(request: Request) -> CurrentUser:
    """FastAPI dependency guarding every /api/* route except /api/health."""
    env = get_runtime_env()
    mode = env.auth_mode.lower()

    if mode == "iap":
        user = _from_iap_header(request, env.iap_audience)
        if user is None:
            raise HTTPException(status_code=401, detail="IAP identity header missing; app must run behind Identity-Aware Proxy")
        user.role = resolve_user_role(user)
        if user.role == "none":
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Zero-Trust Forbidden: user '{user.subject}' does not hold kubeorbitAdmin, "
                    "kubeorbitOperator, or kubeorbitViewer in GCP IAM or KubeOrbit RBAC."
                ),
            )
        return user

    if mode == "azure_ad":
        user = _from_azure_easy_auth_header(request)
        if user is None:
            raise HTTPException(status_code=401, detail="Azure AD identity header missing; app must run behind App Service/Container Apps Easy Auth")
        user.role = resolve_user_role(user)
        if user.role == "none":
            raise HTTPException(
                status_code=403,
                detail=f"Zero-Trust Forbidden: user '{user.subject}' has no assigned KubeOrbit role.",
            )
        return user

    if mode == "api_key":
        user = _from_api_key(request, env.api_key)
        if user is None:
            raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header")
        user.role = resolve_user_role(user)
        if user.role == "none":
            raise HTTPException(
                status_code=403,
                detail=f"Zero-Trust Forbidden: user '{user.subject}' has no assigned KubeOrbit role.",
            )
        return user

    if mode != "none":
        raise HTTPException(status_code=500, detail=f"unknown KUBEORBIT_AUTH_MODE '{env.auth_mode}'")

    logger.warning("KUBEORBIT_AUTH_MODE=none -- portal auth is disabled, request unauthenticated")
    user = _from_iap_header(request) or _from_azure_easy_auth_header(request) or CurrentUser(subject="anonymous", method="none")
    user.role = "admin"
    return user


# --------------------------------------------------------------------------- #
# Cluster/provider auth orchestration
# --------------------------------------------------------------------------- #

async def ensure_cluster_authenticated(
    cluster: ClusterConfig, provider: BaseKubeProvider, user: Optional[CurrentUser] = None
) -> ClusterAuthResult:
    """Authenticate `provider` against its cloud, using a singleflight TTL cache
    so repeated or concurrent dashboard polling of the same cluster doesn't
    re-invoke gcloud/aws/az on every request."""
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
    if from_cache and not Path(result.kubeconfig_path).exists():
        logger.info("Cached kubeconfig for '%s' missing on disk, regenerating...", cluster.id)
        _auth_cache.invalidate_prefix(cache_key)
        result, from_cache = await _auth_cache.get_or_set(cache_key, settings.auth_cache_ttl_seconds, factory)

    # Return a shallow copy so setting `from_cache` per caller doesn't mutate the shared cached instance
    return ClusterAuthResult(
        context_name=result.context_name,
        kubeconfig_path=result.kubeconfig_path,
        authenticated_at=result.authenticated_at,
        identity=result.identity,
        from_cache=from_cache,
    )


def invalidate_cluster_auth(cluster_id: str) -> None:
    _auth_cache.invalidate_prefix(f"cluster-auth:{cluster_id}")
