"""
KubeOrbit FastAPI application.

Every route under /api/clusters/{cluster_id}/... is provider-agnostic: it
resolves the cluster's config, authenticates via the matching provider
adapter (providers/{gke,eks,aks}.py), and then reads Kubernetes resources
through kube_client.py, which behaves identically no matter which cloud is
behind the cluster. Nothing here branches on `cluster.provider` except when
building the small "badge" descriptor the frontend renders.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import shutil
import sys
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Dict, Optional

if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except (AttributeError, RuntimeError):
        pass

from dotenv import load_dotenv

load_dotenv()  # .env is optional (e.g. Docker sets real env vars instead) -- see .env.example

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi import Path as ApiPath
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import auth
import kube_client
from auth import CurrentUser, get_current_user, require_role
from config import ConfigError, get_config_store, get_runtime_env
from providers import ProviderError, available_providers, build_provider
from providers.base import ClusterMeta

logging.basicConfig(
    level=get_runtime_env().log_level.upper(),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("kubeorbit.app")

APP_VERSION = "1.0.0"
K8S_NAME_PATTERN = r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$"
K8S_CLUSTER_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"
K8S_KIND_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9.-]{0,127}$"

app = FastAPI(title="KubeOrbit", version=APP_VERSION)

class SSEAwareGZipMiddleware(GZipMiddleware):
    """Compress JSON/JS/CSS/HTML payloads without buffering AG-UI SSE streams."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and (scope.get("path") or "").endswith("/ai/ag-ui"):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


app.add_middleware(SSEAwareGZipMiddleware, minimum_size=1024, compresslevel=6)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_runtime_env().cors_allow_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


def _json(obj: Any) -> Any:
    """Recursively turn dataclasses (ClusterMeta, NodePoolInfo, ...) into
    plain dicts so FastAPI's default JSON encoder can serialize them."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _json(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json(v) for v in obj]
    return obj


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #

@app.on_event("startup")
async def on_startup() -> None:
    try:
        store = get_config_store()
    except ConfigError as exc:
        logger.error("Config failed to load at startup: %s", exc)
        return
    logger.info(
        "KubeOrbit starting: %d team(s), %d cluster(s), providers=%s, auth_mode=%s",
        len(store.teams), store.cluster_count, available_providers(), get_runtime_env().auth_mode,
    )


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #

@app.exception_handler(ProviderError)
async def provider_error_handler(request: Request, exc: ProviderError) -> JSONResponse:
    from providers.base import ProviderAuthError, ProviderConfigError

    status_code = 400 if isinstance(exc, ProviderConfigError) else 424 if isinstance(exc, ProviderAuthError) else 502
    return JSONResponse(status_code=status_code, content={"error": type(exc).__name__, "message": str(exc)})


@app.exception_handler(kube_client.CLIError)
async def cli_error_handler(request: Request, exc: kube_client.CLIError) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={"error": type(exc).__name__, "message": str(exc), "command": exc.cmd[0] if exc.cmd else None},
    )


@app.exception_handler(ConfigError)
async def config_error_handler(request: Request, exc: ConfigError) -> JSONResponse:
    return JSONResponse(status_code=500, content={"error": "ConfigError", "message": str(exc)})


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"error": "InternalError", "message": "An unexpected error occurred."})


# --------------------------------------------------------------------------- #
# Shared dependencies
# --------------------------------------------------------------------------- #

@dataclass
class ClusterContext:
    cluster: Any
    provider: Any
    auth_result: Any

    @property
    def context(self) -> str:
        return self.auth_result.context_name

    @property
    def kubeconfig(self) -> Path:
        return Path(self.auth_result.kubeconfig_path)

    def badge(self) -> Dict[str, Any]:
        meta = self.provider.get_display_meta()
        return {
            "cluster_id": self.cluster.id,
            "name": self.cluster.name,
            "team_id": self.cluster.team_id,
            "team_name": self.cluster.team_name,
            "environment": self.cluster.environment,
            **meta,
        }


def _get_cluster_or_404(cluster_id: str):
    try:
        return get_config_store().get_cluster(cluster_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown cluster '{cluster_id}'") from None


async def get_cluster_context(
    cluster_id: str = ApiPath(..., pattern=K8S_CLUSTER_ID_PATTERN, max_length=63),
    user: CurrentUser = Depends(get_current_user),
) -> ClusterContext:
    cluster = _get_cluster_or_404(cluster_id)
    require_role(user, "viewer", team_id=cluster.team_id)
    provider = build_provider(cluster)
    auth_result = await auth.ensure_cluster_authenticated(cluster, provider, user)
    return ClusterContext(cluster=cluster, provider=provider, auth_result=auth_result)


RequiredNamespaceQuery = Query(..., pattern=K8S_NAME_PATTERN, max_length=63)
OptionalNamespaceQuery = Query(None, pattern=K8S_NAME_PATTERN, max_length=63)
NamespaceSegment = ApiPath(..., pattern=K8S_NAME_PATTERN, max_length=63)
# Two distinct Path(...) instances even though they're identical in every
# argument: FastAPI keys its internal field-building off the FieldInfo
# object's identity, so reusing one instance as the default for parameters
# with two different names (here "name" vs "pod_name") makes it bind BOTH
# routes' fields to whichever parameter name it saw first -- every request to
# the second route then 422s with "field required: name", regardless of what
# was actually in the URL.
ResourceNameSegment = ApiPath(..., pattern=K8S_NAME_PATTERN, max_length=253)
PodNameSegment = ApiPath(..., pattern=K8S_NAME_PATTERN, max_length=253)


# --------------------------------------------------------------------------- #
# Health / readiness (no auth -- for load balancer / uptime probes)
# --------------------------------------------------------------------------- #

@app.get("/api/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/api/ready")
async def ready() -> JSONResponse:
    checks: Dict[str, Any] = {"kubectl_available": shutil.which("kubectl") is not None}
    try:
        store = get_config_store()
        checks["config_loaded"] = True
        checks["cluster_count"] = store.cluster_count
    except ConfigError as exc:
        checks["config_loaded"] = False
        checks["config_error"] = str(exc)

    is_ready = checks["config_loaded"] and checks["kubectl_available"]
    return JSONResponse(status_code=200 if is_ready else 503, content=checks)


# --------------------------------------------------------------------------- #
# Status / teams
# --------------------------------------------------------------------------- #

@app.get("/api/status")
async def status(user: CurrentUser = Depends(get_current_user)) -> Dict[str, Any]:
    import ai_agent
    from mcp_server.server import get_mcp_tool_catalog

    store = get_config_store()
    env = get_runtime_env()
    binaries = {b: shutil.which(b) is not None for b in ("kubectl", "helm", "gcloud", "aws", "az", "kubelogin")}
    providers_list = available_providers()
    if env.disable_local_provider:
        providers_list = [p for p in providers_list if p != "local"]
    return {
        "version": APP_VERSION,
        "auth_mode": env.auth_mode,
        "read_only_mode": env.read_only_mode,
        "user": {"subject": user.subject, "method": user.method, "role": user.role},
        "disable_local_provider": env.disable_local_provider,
        "providers": providers_list,
        "cli_binaries": binaries,
        "ai": ai_agent.ensure_vertex_ai_env(),
        "protocols": {
            "ag_ui": {
                "version": "1.0",
                "transport": "sse",
                "endpoint": "/api/clusters/{cluster_id}/ai/ag-ui",
            },
            "mcp": {
                "transport": "inprocess+stdio",
                "read_only": True,
                "tool_count": len(get_mcp_tool_catalog()),
            },
            "adk": {
                "framework": "google-adk",
                "streaming": "sse",
            },
        },
        "team_count": len(store.teams),
        "cluster_count": store.cluster_count,
        "excluded_namespace_patterns": store.settings.excluded_namespace_patterns,
        "refresh_intervals_seconds": store.settings.allowed_refresh_intervals_seconds,
        "default_refresh_interval_seconds": store.settings.default_refresh_interval_seconds,
    }


@app.get("/api/teams")
async def teams(user: CurrentUser = Depends(get_current_user)) -> Dict[str, Any]:
    return {"teams": get_config_store().teams_payload()}


# --------------------------------------------------------------------------- #
# Dynamic Cluster Onboarding & Administration APIs
# --------------------------------------------------------------------------- #

class OnboardClusterRequest(BaseModel):
    id: str = Field(..., pattern=K8S_CLUSTER_ID_PATTERN, description="Unique URL-safe cluster ID")
    name: str = Field(..., min_length=1, max_length=128, description="Human readable cluster name")
    provider: str = Field(..., description="gke | eks | aks")
    environment: str = Field(..., description="sandbox | nonprod | prod")
    team_id: str = Field("platform", pattern=K8S_CLUSTER_ID_PATTERN, description="Team identifier")
    team_name: str = Field("Platform Engineering", max_length=128, description="Team name")
    description: Optional[str] = Field(None, max_length=512)
    config: Dict[str, Any] = Field(default_factory=dict)


class ParseKubeconfigRequest(BaseModel):
    kubeconfig_path: str = Field("~/.kube/config", max_length=1024, description="Path to local kubeconfig file")
    kubeconfig_content: Optional[str] = Field(
        None, max_length=262_144, description="Optional raw kubeconfig YAML content uploaded from browser"
    )
    filename: Optional[str] = Field(None, max_length=256, description="Optional original filename for uploaded kubeconfig")


def _enforce_provider_policy(provider_name: str, cluster_config: Optional[Dict[str, Any]] = None) -> None:
    if provider_name.strip().lower() == "local":
        if get_runtime_env().disable_local_provider:
            raise HTTPException(
                status_code=403,
                detail="The 'local' kubeconfig provider is disabled in Cloud Run / IAP enterprise mode.",
            )
        if cluster_config and cluster_config.get("kubeconfig_path"):
            from providers.local import validate_safe_kubeconfig_path
            try:
                validate_safe_kubeconfig_path(str(cluster_config["kubeconfig_path"]), require_exists=False)
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc


def _invalidate_cluster_caches(cluster_id: str) -> None:
    auth.invalidate_cluster_auth(cluster_id)
    try:
        from kube_client._cache import _cache
        from kube_client._sdk import evict_sdk_clients_for_cluster
        _cache.invalidate_prefix(f"provider-meta:{cluster_id}")
        _cache.invalidate_prefix(f"cost-estimate:{cluster_id}")
        evict_sdk_clients_for_cluster(cluster_id)
    except Exception:
        pass


@app.get("/api/admin/clusters")
async def list_clusters(user: CurrentUser = Depends(get_current_user)) -> Dict[str, Any]:
    """Return all configured clusters with provider metadata for the sidebar cluster list."""
    store = get_config_store()
    clusters = []
    for team in store.teams:
        for env_group in team.environments:
            for cluster in env_group.clusters:
                clusters.append({
                    "id": cluster.id,
                    "name": cluster.name,
                    "provider": cluster.provider,
                    "environment": cluster.environment,
                    "team_id": team.id,
                    "team_name": team.name,
                    "description": getattr(cluster, "description", "") or "",
                    "config": dict(cluster.config or {}),
                })
    return {"clusters": clusters}


@app.post("/api/admin/clusters/test-connection")
async def test_cluster_connection(
    req: OnboardClusterRequest, user: CurrentUser = Depends(get_current_user)
) -> Dict[str, Any]:
    require_role(user, "admin", team_id=req.team_id)
    _enforce_provider_policy(req.provider, req.config)
    from config import ClusterConfig
    cluster_cfg = ClusterConfig(
        id=req.id,
        name=req.name,
        provider=req.provider,
        environment=req.environment,
        description=req.description,
        config=req.config,
        team_id=req.team_id,
        team_name=req.team_name,
    )
    provider = build_provider(cluster_cfg)
    auth_res = await auth.ensure_cluster_authenticated(cluster_cfg, provider, user)
    meta = await provider.get_cluster_metadata()
    return {
        "status": "ok",
        "context_name": auth_res.context_name,
        "kubernetes_version": meta.kubernetes_version,
        "location": meta.location,
        "node_pools_count": len(meta.node_pools),
    }


@app.post("/api/admin/clusters")
async def onboard_cluster(
    req: OnboardClusterRequest, user: CurrentUser = Depends(get_current_user)
) -> Dict[str, Any]:
    require_role(user, "admin", team_id=req.team_id)
    _enforce_provider_policy(req.provider, req.config)
    from config import add_cluster_to_config
    cluster_data = {
        "id": req.id,
        "name": req.name,
        "provider": req.provider,
        "environment": req.environment,
        "description": req.description or f"{req.provider.upper()} cluster {req.name}",
        "config": req.config,
    }
    store = add_cluster_to_config(
        team_id=req.team_id,
        team_name=req.team_name,
        environment=req.environment,
        cluster_data=cluster_data,
    )
    _invalidate_cluster_caches(req.id)
    kube_client.record_audit_event(
        user=user.subject,
        action="ONBOARD_CLUSTER",
        cluster_id=req.id,
        namespace="cluster-wide",
        resource=f"cluster/{req.id}",
        details=f"Onboarded {req.provider.upper()} cluster '{req.name}' in {req.environment}",
    )
    logger.info("Onboarded cluster '%s' for team '%s' (user=%s)", req.id, req.team_id, user.subject)
    return {"status": "ok", "teams": store.teams_payload()}


@app.put("/api/admin/clusters/{cluster_id}")
async def update_cluster(
    req: OnboardClusterRequest,
    cluster_id: str = ApiPath(..., pattern=K8S_CLUSTER_ID_PATTERN, max_length=63),
    user: CurrentUser = Depends(get_current_user),
) -> Dict[str, Any]:
    require_role(user, "admin", team_id=req.team_id)
    _enforce_provider_policy(req.provider, req.config)
    from config import add_cluster_to_config
    cluster_data = {
        "id": cluster_id,
        "name": req.name,
        "provider": req.provider,
        "environment": req.environment,
        "description": req.description or f"{req.provider.upper()} cluster {req.name}",
        "config": req.config,
    }
    store = add_cluster_to_config(
        team_id=req.team_id,
        team_name=req.team_name,
        environment=req.environment,
        cluster_data=cluster_data,
    )
    _invalidate_cluster_caches(cluster_id)
    kube_client.record_audit_event(
        user=user.subject,
        action="UPDATE_CLUSTER",
        cluster_id=cluster_id,
        namespace="cluster-wide",
        resource=f"cluster/{cluster_id}",
        details=f"Updated {req.provider.upper()} cluster '{req.name}' in {req.environment}",
    )
    logger.info("Updated cluster '%s' ('%s') for team '%s' (user=%s)", cluster_id, req.name, req.team_id, user.subject)
    return {"status": "ok", "teams": store.teams_payload()}


@app.delete("/api/admin/clusters/{cluster_id}")
async def delete_cluster(
    cluster_id: str = ApiPath(..., pattern=K8S_CLUSTER_ID_PATTERN, max_length=63),
    user: CurrentUser = Depends(get_current_user),
) -> Dict[str, Any]:
    existing_team_id = None
    try:
        existing_team_id = get_config_store().get_cluster(cluster_id).team_id
    except Exception:
        pass
    require_role(user, "admin", team_id=existing_team_id)
    from config import delete_cluster_from_config
    store = delete_cluster_from_config(cluster_id)
    _invalidate_cluster_caches(cluster_id)
    kube_client.record_audit_event(
        user=user.subject,
        action="DELETE_CLUSTER",
        cluster_id=cluster_id,
        namespace="cluster-wide",
        resource=f"cluster/{cluster_id}",
        details=f"Deleted cluster '{cluster_id}'",
    )
    logger.info("Deleted cluster '%s' (user=%s)", cluster_id, user.subject)
    return {"status": "ok", "teams": store.teams_payload()}


def _parse_and_store_kubeconfig(req: ParseKubeconfigRequest) -> Dict[str, Any]:
    import yaml
    from providers.local import (
        MAX_KUBECONFIG_BYTES,
        validate_safe_kubeconfig_dict,
        validate_safe_kubeconfig_path,
    )

    if req.kubeconfig_content is not None:
        if len(req.kubeconfig_content.encode("utf-8")) > MAX_KUBECONFIG_BYTES:
            raise HTTPException(status_code=400, detail=f"Kubeconfig exceeds {MAX_KUBECONFIG_BYTES} byte limit.")
        try:
            content = yaml.safe_load(req.kubeconfig_content) or {}
            validate_safe_kubeconfig_dict(content, is_uploaded=True)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid or unsafe kubeconfig: {str(exc)}")
        raw_name = Path(req.filename or "uploaded-kubeconfig.yaml").stem
        safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw_name).strip("-") or "uploaded"
        dest_dir = get_runtime_env().kubeconfig_home
        dest_dir.mkdir(parents=True, exist_ok=True)
        path_obj = dest_dir / f"uploaded-{safe_name}-{secrets.token_hex(4)}.yaml"
        path_obj.write_text(req.kubeconfig_content, encoding="utf-8")
        try:
            os.chmod(path_obj, 0o600)
        except OSError:
            pass
    else:
        try:
            path_obj = validate_safe_kubeconfig_path(req.kubeconfig_path)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Kubeconfig file not found at: {req.kubeconfig_path}") from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            with open(path_obj, "r", encoding="utf-8") as f:
                content = yaml.safe_load(f) or {}
            validate_safe_kubeconfig_dict(content, is_uploaded=False)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid or unsafe kubeconfig: {str(exc)}")

    current_ctx = content.get("current-context", "")
    contexts = []
    for ctx in content.get("contexts", []):
        if not isinstance(ctx, dict):
            continue
        ctx_name = ctx.get("name", "")
        cluster_ref = (ctx.get("context") or {}).get("cluster", ctx_name)
        if ctx_name:
            contexts.append({"name": ctx_name, "cluster": cluster_ref})

    suggested_id = current_ctx.lower().replace("/", "-").replace(":", "-").replace("_", "-") if current_ctx else ""

    return {
        "status": "ok",
        "kubeconfig_path": str(path_obj),
        "current_context": current_ctx,
        "contexts": contexts,
        "suggested_id": suggested_id,
    }


@app.post("/api/admin/clusters/parse-kubeconfig")
async def parse_kubeconfig(
    req: ParseKubeconfigRequest, user: CurrentUser = Depends(get_current_user)
) -> Dict[str, Any]:
    require_role(user, "admin")
    if get_runtime_env().disable_local_provider:
        raise HTTPException(
            status_code=403,
            detail="Parsing local server kubeconfig files is disabled in Cloud Run / IAP enterprise mode.",
        )
    return await asyncio.to_thread(_parse_and_store_kubeconfig, req)


# --------------------------------------------------------------------------- #
# Cluster switch / info / nodes
# --------------------------------------------------------------------------- #

@app.post("/api/clusters/{cluster_id}/switch")
async def switch_cluster(ctx: ClusterContext = Depends(get_cluster_context)) -> Dict[str, Any]:
    logger.info("Cluster switch -> %s (provider=%s)", ctx.cluster.id, ctx.cluster.provider)
    return {
        **ctx.badge(),
        "context_name": ctx.context,
        "authenticated_at": ctx.auth_result.authenticated_at,
        "from_cache": ctx.auth_result.from_cache,
        "identity": _json(ctx.auth_result.identity),
    }


@app.post("/api/clusters/{cluster_id}/refresh")
async def refresh_cluster_cache(ctx: ClusterContext = Depends(get_cluster_context)) -> Dict[str, Any]:
    """Invalidate server-side cached Kubernetes/Helm/metadata reads for one cluster
    so an explicit UI refresh fetches fresh live data immediately."""
    kube_client.invalidate_cluster_data_cache(ctx.context, ctx.cluster.id)
    return {"status": "ok", "cluster_id": ctx.cluster.id}


@app.get("/api/clusters/{cluster_id}/info")
async def cluster_info(ctx: ClusterContext = Depends(get_cluster_context)) -> Dict[str, Any]:
    meta_ttl = get_config_store().settings.cluster_meta_cache_ttl_seconds

    async def _fetch_nodes_safe():
        try:
            return await kube_client.get_nodes(ctx.context, ctx.kubeconfig), None
        except kube_client.CLIError as exc:
            return [], str(exc)

    meta, (nodes, node_fetch_error), helm = await asyncio.gather(
        kube_client.cached(f"provider-meta:{ctx.cluster.id}", meta_ttl, ctx.provider.get_cluster_metadata),
        _fetch_nodes_safe(),
        kube_client.get_helm_releases(ctx.context, ctx.kubeconfig, include_resources=False),
    )

    if nodes:
        counts: Dict[str, int] = {}
        for n in nodes:
            pool = n.get("node_pool") or "unassigned"
            counts[pool] = counts.get(pool, 0) + 1
        for pool in meta.node_pools:
            if pool.name in counts:
                pool.node_count = counts[pool.name]

    return {
        "badge": ctx.badge(),
        "meta": _json(meta),
        "node_count": len(nodes),
        "node_fetch_error": node_fetch_error,
        "helm": helm,
        "nodes": nodes,
    }


@app.get("/api/clusters/{cluster_id}/cost")
async def cluster_cost_estimate(ctx: ClusterContext = Depends(get_cluster_context)) -> Dict[str, Any]:
    """Estimated monthly compute cost from each cloud's public on-demand list
    pricing (see providers/pricing.py). Not the customer's actual invoice --
    every response carries a `disclaimer` field that should be shown
    alongside any figure from this endpoint."""
    meta_ttl = get_config_store().settings.cluster_meta_cache_ttl_seconds
    meta: ClusterMeta = await kube_client.cached(
        f"provider-meta:{ctx.cluster.id}", meta_ttl, ctx.provider.get_cluster_metadata
    )
    cache_key = f"cost-estimate:{ctx.cluster.id}"
    cost = await kube_client.cached(cache_key, meta_ttl, lambda: ctx.provider.get_cost_estimate(meta.node_pools))
    return {"cluster_id": ctx.cluster.id, **_json(cost)}


@app.get("/api/clusters/{cluster_id}/nodes")
async def cluster_nodes(ctx: ClusterContext = Depends(get_cluster_context)) -> Dict[str, Any]:
    nodes = await kube_client.get_nodes(ctx.context, ctx.kubeconfig)
    return {"cluster_id": ctx.cluster.id, "items": nodes, "count": len(nodes)}


@app.get("/api/clusters/{cluster_id}/namespaces")
async def cluster_namespaces(
    include_system: bool = Query(False),
    ctx: ClusterContext = Depends(get_cluster_context),
) -> Dict[str, Any]:
    namespaces = await kube_client.get_namespaces(ctx.context, ctx.kubeconfig, include_system=include_system)
    return {"cluster_id": ctx.cluster.id, "items": namespaces, "count": len(namespaces)}


# --------------------------------------------------------------------------- #
# Workloads overview
# --------------------------------------------------------------------------- #

@app.get("/api/clusters/{cluster_id}/workloads")
async def workloads_overview(
    namespace: Optional[str] = OptionalNamespaceQuery,
    ctx: ClusterContext = Depends(get_cluster_context),
) -> Dict[str, Any]:
    overview = await kube_client.get_workloads_overview(ctx.context, ctx.kubeconfig, namespace)
    return {"cluster_id": ctx.cluster.id, "namespace": namespace, "overview": overview}


# --------------------------------------------------------------------------- #
# Namespace resources
# --------------------------------------------------------------------------- #

@app.get("/api/clusters/{cluster_id}/namespaces/{namespace}/resources")
async def namespace_resources(
    namespace: str = NamespaceSegment,
    ctx: ClusterContext = Depends(get_cluster_context),
) -> Dict[str, Any]:
    pods, services, configmaps, secrets, gateways, httproutes, pvcs, serviceaccounts = await asyncio.gather(
        kube_client.get_pods(ctx.context, ctx.kubeconfig, namespace),
        kube_client.get_services(ctx.context, ctx.kubeconfig, namespace),
        kube_client.get_configmaps(ctx.context, ctx.kubeconfig, namespace),
        kube_client.get_secrets_metadata(ctx.context, ctx.kubeconfig, namespace),
        kube_client.get_gateways(ctx.context, ctx.kubeconfig, namespace),
        kube_client.get_httproutes(ctx.context, ctx.kubeconfig, namespace),
        kube_client.get_pvcs(ctx.context, ctx.kubeconfig, namespace),
        kube_client.get_serviceaccounts(ctx.context, ctx.kubeconfig, namespace),
    )
    return {
        "cluster_id": ctx.cluster.id,
        "namespace": namespace,
        "pods": pods,
        "services": services,
        "configmaps": configmaps,
        "secrets": secrets,
        "gateways": gateways,
        "httproutes": httproutes,
        "pvcs": pvcs,
        "serviceaccounts": serviceaccounts,
    }


# --------------------------------------------------------------------------- #
# Topology
# --------------------------------------------------------------------------- #

@app.get("/api/clusters/{cluster_id}/topology/network")
async def network_topology(
    namespace: str = RequiredNamespaceQuery,
    ctx: ClusterContext = Depends(get_cluster_context),
) -> Dict[str, Any]:
    graph = await kube_client.get_network_topology(ctx.context, ctx.kubeconfig, namespace)
    return {"cluster_id": ctx.cluster.id, **graph}


@app.get("/api/clusters/{cluster_id}/networkpolicy/simulate")
async def simulate_network_policy(
    source_namespace: str = Query(..., pattern=K8S_NAME_PATTERN, max_length=63),
    source_pod: str = Query(..., pattern=K8S_NAME_PATTERN, max_length=253),
    dest_namespace: str = Query(..., pattern=K8S_NAME_PATTERN, max_length=63),
    dest_pod: Optional[str] = Query(None, pattern=K8S_NAME_PATTERN, max_length=253),
    dest_service: Optional[str] = Query(None, pattern=K8S_NAME_PATTERN, max_length=253),
    port: Optional[int] = Query(None, ge=1, le=65535),
    protocol: str = Query("TCP", pattern="^(TCP|UDP|SCTP)$"),
    ctx: ClusterContext = Depends(get_cluster_context),
) -> Dict[str, Any]:
    """Simulate L3/L4 Kubernetes NetworkPolicy reachability (strictly read-only)."""
    if not dest_pod and not dest_service:
        raise HTTPException(
            status_code=400,
            detail="Provide either 'dest_pod' or 'dest_service' to simulate reachability.",
        )
    try:
        result = await kube_client.simulate_network_policy(
            ctx.context,
            ctx.kubeconfig,
            source_namespace=source_namespace,
            source_pod=source_pod,
            dest_namespace=dest_namespace,
            dest_pod=dest_pod,
            dest_service=dest_service,
            port=port,
            protocol=protocol,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return {"cluster_id": ctx.cluster.id, **result}


@app.get("/api/clusters/{cluster_id}/topology/storage")
async def storage_topology(
    namespace: str = RequiredNamespaceQuery,
    ctx: ClusterContext = Depends(get_cluster_context),
) -> Dict[str, Any]:
    graph = await kube_client.get_storage_topology(ctx.context, ctx.kubeconfig, namespace)
    return {"cluster_id": ctx.cluster.id, **graph}


@app.get("/api/clusters/{cluster_id}/topology/map")
async def namespace_resource_map(
    namespace: str = RequiredNamespaceQuery,
    ctx: ClusterContext = Depends(get_cluster_context),
) -> Dict[str, Any]:
    graph = await kube_client.get_namespace_map(ctx.context, ctx.kubeconfig, namespace)
    return {"cluster_id": ctx.cluster.id, **graph}


@app.get("/api/clusters/{cluster_id}/topology/clustermap")
async def cluster_workload_map(
    ctx: ClusterContext = Depends(get_cluster_context),
) -> Dict[str, Any]:
    data = await kube_client.get_cluster_workload_map(ctx.context, ctx.kubeconfig)
    return {"cluster_id": ctx.cluster.id, **data}


# --------------------------------------------------------------------------- #
# Events / helm
# --------------------------------------------------------------------------- #

@app.get("/api/clusters/{cluster_id}/events")
async def cluster_events(
    namespace: Optional[str] = OptionalNamespaceQuery,
    limit: int = Query(100, ge=1, le=500),
    ctx: ClusterContext = Depends(get_cluster_context),
) -> Dict[str, Any]:
    events = await kube_client.get_events(ctx.context, ctx.kubeconfig, namespace, limit=limit)
    return {"cluster_id": ctx.cluster.id, "namespace": namespace, "items": events, "count": len(events)}


def _translate_helm_cli_error(exc: kube_client.CLIError, namespace: str, release_name: str) -> None:
    msg = str(exc).lower()
    if "not found" in msg:
        raise HTTPException(
            status_code=404,
            detail=f"Helm release '{release_name}' not found in namespace '{namespace}'.",
        ) from exc
    if "invalid" in msg:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


@app.get("/api/clusters/{cluster_id}/helm")
async def cluster_helm(
    namespace: Optional[str] = OptionalNamespaceQuery,
    ctx: ClusterContext = Depends(get_cluster_context),
) -> Dict[str, Any]:
    helm = await kube_client.get_helm_releases(ctx.context, ctx.kubeconfig, namespace=namespace)
    return {"cluster_id": ctx.cluster.id, **helm}


@app.get("/api/clusters/{cluster_id}/helm/{namespace}/{release_name}/history")
async def cluster_helm_history(
    namespace: str = ApiPath(..., pattern=K8S_NAME_PATTERN, max_length=63),
    release_name: str = ApiPath(..., pattern=K8S_NAME_PATTERN, max_length=253),
    max_revisions: int = Query(256, ge=1, le=256),
    ctx: ClusterContext = Depends(get_cluster_context),
) -> Dict[str, Any]:
    try:
        data = await kube_client.get_helm_release_history(
            ctx.context, ctx.kubeconfig, namespace, release_name, max_revisions=max_revisions
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except kube_client.CLIError as exc:
        _translate_helm_cli_error(exc, namespace, release_name)
    return {"cluster_id": ctx.cluster.id, **data}


@app.get("/api/clusters/{cluster_id}/helm/{namespace}/{release_name}/diff")
async def cluster_helm_diff(
    namespace: str = ApiPath(..., pattern=K8S_NAME_PATTERN, max_length=63),
    release_name: str = ApiPath(..., pattern=K8S_NAME_PATTERN, max_length=253),
    from_revision: int = Query(..., ge=1),
    to_revision: int = Query(..., ge=1),
    mode: str = Query("values", pattern=r"^(values|all_values|manifest)$"),
    ctx: ClusterContext = Depends(get_cluster_context),
) -> JSONResponse:
    try:
        data = await kube_client.diff_helm_release_revisions(
            ctx.context,
            ctx.kubeconfig,
            namespace,
            release_name,
            from_revision=from_revision,
            to_revision=to_revision,
            compare_mode=mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except kube_client.CLIError as exc:
        _translate_helm_cli_error(exc, namespace, release_name)
    return JSONResponse(
        content={"cluster_id": ctx.cluster.id, **data},
        headers={"Cache-Control": "private, no-store"},
    )


@app.get("/api/clusters/{cluster_id}/helm/{namespace}/{release_name}")
async def cluster_helm_details(
    namespace: str = ApiPath(..., pattern=K8S_NAME_PATTERN, max_length=63),
    release_name: str = ApiPath(..., pattern=K8S_NAME_PATTERN, max_length=253),
    revision: Optional[int] = Query(None, ge=1),
    ctx: ClusterContext = Depends(get_cluster_context),
) -> JSONResponse:
    try:
        data = await kube_client.get_helm_release_details(
            ctx.context, ctx.kubeconfig, namespace, release_name, revision=revision
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except kube_client.CLIError as exc:
        _translate_helm_cli_error(exc, namespace, release_name)
    return JSONResponse(
        content={"cluster_id": ctx.cluster.id, **data},
        headers={"Cache-Control": "private, no-store"},
    )


@app.get("/api/clusters/{cluster_id}/namespaces/{namespace}/pods/{pod_name}/logs")
async def pod_logs(
    namespace: str = ApiPath(..., pattern=K8S_NAME_PATTERN, max_length=63),
    pod_name: str = ApiPath(..., pattern=K8S_NAME_PATTERN, max_length=253),
    container: Optional[str] = Query(None, pattern=K8S_NAME_PATTERN, max_length=63),
    tail_lines: int = Query(200, ge=10, le=5000),
    ctx: ClusterContext = Depends(get_cluster_context),
) -> Dict[str, Any]:
    logs = await kube_client.get_pod_logs(
        ctx.context, ctx.kubeconfig, namespace, pod_name, container=container, tail_lines=tail_lines
    )
    return {
        "cluster_id": ctx.cluster.id,
        "namespace": namespace,
        "pod_name": pod_name,
        "container": container,
        "tail_lines": tail_lines,
        "logs": logs,
    }


@app.get("/api/clusters/{cluster_id}/manifest")
async def resource_yaml(
    kind: str = Query(..., pattern=K8S_KIND_PATTERN, max_length=128),
    name: str = Query(..., pattern=K8S_NAME_PATTERN, max_length=253),
    namespace: Optional[str] = OptionalNamespaceQuery,
    ctx: ClusterContext = Depends(get_cluster_context),
) -> Dict[str, Any]:
    if kube_client.is_secret_kind(kind):
        raise HTTPException(
            status_code=403,
            detail="Kubernetes Secret value and YAML retrieval is disabled for security reasons.",
        )
    yaml_content = await kube_client.get_resource_yaml(
        ctx.context, ctx.kubeconfig, namespace, kind, name
    )
    return {
        "cluster_id": ctx.cluster.id,
        "namespace": namespace,
        "kind": kind,
        "name": name,
        "yaml": yaml_content,
    }


# --------------------------------------------------------------------------- #
# Enterprise Extensions: Fleet, Catalog, Rollouts, Applications, Inspector & RCA
# --------------------------------------------------------------------------- #

@app.get("/api/fleet/overview")
async def fleet_overview(user: CurrentUser = Depends(get_current_user)) -> Dict[str, Any]:
    """Unified multi-cluster fleet dashboard aggregating all registered clusters concurrently."""
    store = get_config_store()
    all_clusters = [
        cluster_cfg
        for team in store.teams
        for env_group in team.environments
        for cluster_cfg in env_group.clusters
    ]

    async def _probe_cluster(cluster_cfg) -> Dict[str, Any]:
        try:
            provider = build_provider(cluster_cfg)
            auth_res = await auth.ensure_cluster_authenticated(cluster_cfg, provider, user)
            kcfg_path = Path(auth_res.kubeconfig_path)
            nodes, namespaces, pods = await asyncio.gather(
                kube_client.get_nodes(auth_res.context_name, kcfg_path),
                kube_client.get_namespaces(auth_res.context_name, kcfg_path, include_system=True),
                kube_client.get_pods(auth_res.context_name, kcfg_path, ""),
            )
            c_nodes = len(nodes)
            c_pods = len(pods)
            c_ns = len(namespaces)

            c_cpu = 0.0
            c_mem = 0.0
            for n in nodes:
                c_cpu += float(n.get("cpu_capacity", 0) or 0)
                mem_str = str(n.get("memory_capacity", "0")).lower().replace("ki", "")
                try:
                    c_mem += float(mem_str) / (1024 * 1024)
                except ValueError:
                    pass

            healthy_pod_count = sum(1 for p in pods if p.get("status") in ("Running", "Succeeded"))
            c_health_score = round((healthy_pod_count / c_pods) * 100) if c_pods else 100

            return {
                "id": cluster_cfg.id,
                "name": cluster_cfg.name,
                "provider": cluster_cfg.provider,
                "environment": cluster_cfg.environment,
                "status": "ONLINE",
                "node_count": c_nodes,
                "pod_count": c_pods,
                "namespace_count": c_ns,
                "cpu_cores": round(c_cpu, 1),
                "memory_gib": round(c_mem, 1),
                "health_score": c_health_score,
                "health_status": "Healthy" if c_health_score >= 80 else "Degraded",
                "_raw_cpu": c_cpu,
                "_raw_mem": c_mem,
            }
        except Exception as exc:
            return {
                "id": cluster_cfg.id,
                "name": cluster_cfg.name,
                "provider": cluster_cfg.provider,
                "environment": cluster_cfg.environment,
                "status": "DEGRADED",
                "error": str(exc),
                "node_count": 0,
                "pod_count": 0,
                "namespace_count": 0,
                "cpu_cores": 0,
                "memory_gib": 0,
                "health_score": 0,
                "health_status": "Unreachable",
                "_raw_cpu": 0.0,
                "_raw_mem": 0.0,
            }

    probed = await asyncio.gather(*(_probe_cluster(c) for c in all_clusters))

    clusters_info = []
    total_nodes = 0
    total_pods = 0
    total_namespaces = 0
    total_cpu_cores = 0.0
    total_mem_gib = 0.0
    online_count = 0

    for item in probed:
        raw_cpu = item.pop("_raw_cpu", 0.0)
        raw_mem = item.pop("_raw_mem", 0.0)
        if item["status"] == "ONLINE":
            online_count += 1
            total_nodes += item["node_count"]
            total_pods += item["pod_count"]
            total_namespaces += item["namespace_count"]
            total_cpu_cores += raw_cpu
            total_mem_gib += raw_mem
        clusters_info.append(item)

    fleet_health_score = int((online_count / max(1, len(clusters_info))) * 100)
    healthy_cluster_count = sum(1 for c in clusters_info if c.get("health_score", 0) >= 80)

    return {
        "summary": {
            "total_clusters": len(clusters_info),
            "healthy_clusters": healthy_cluster_count,
            "online_clusters": online_count,
            "degraded_clusters": len(clusters_info) - online_count,
            "fleet_health_score": fleet_health_score,
            "total_nodes": total_nodes,
            "total_pods": total_pods,
            "total_namespaces": total_namespaces,
            "total_cpu_cores": round(total_cpu_cores, 1),
            "total_memory_gib": round(total_mem_gib, 1),
        },
        "clusters": clusters_info,
    }


@app.get("/api/clusters/{cluster_id}/workloads/catalog")
async def workloads_catalog(
    namespace: Optional[str] = OptionalNamespaceQuery,
    ctx: ClusterContext = Depends(get_cluster_context),
) -> Dict[str, Any]:
    """Catalog of Pods, Deployments, StatefulSets, DaemonSets, Jobs, and CronJobs."""
    catalog = await kube_client.get_workloads_catalog(ctx.context, ctx.kubeconfig, namespace)
    return {"cluster_id": ctx.cluster.id, **catalog}


@app.get("/api/clusters/{cluster_id}/workloads/{workload_type}/{name}/rollout/history")
async def workload_rollout_history(
    workload_type: str = ApiPath(..., pattern=K8S_KIND_PATTERN, max_length=63),
    name: str = ApiPath(..., pattern=K8S_NAME_PATTERN, max_length=253),
    namespace: str = RequiredNamespaceQuery,
    ctx: ClusterContext = Depends(get_cluster_context),
) -> Dict[str, Any]:
    """Inspect revisions and rollout history."""
    res = await kube_client.get_rollout_history(ctx.context, ctx.kubeconfig, namespace, workload_type, name)
    return {"cluster_id": ctx.cluster.id, **res}


class RollbackRequest(BaseModel):
    to_revision: Optional[int] = None


@app.post("/api/clusters/{cluster_id}/workloads/{workload_type}/{name}/rollback")
async def workload_rollback(
    req: RollbackRequest,
    workload_type: str = ApiPath(..., pattern=K8S_KIND_PATTERN, max_length=63),
    name: str = ApiPath(..., pattern=K8S_NAME_PATTERN, max_length=253),
    namespace: str = RequiredNamespaceQuery,
    ctx: ClusterContext = Depends(get_cluster_context),
    user: CurrentUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Execute rollback for a workload and record an audit log."""
    if get_runtime_env().read_only_mode:
        raise HTTPException(
            status_code=403,
            detail=(
                "KubeOrbit is running in strictly read-only mode (KUBEORBIT_READ_ONLY=true). "
                "Cluster write operations are disabled."
            ),
        )
    require_role(user, "operator", team_id=ctx.cluster.team_id)
    res = await kube_client.rollout_undo(
        ctx.context, ctx.kubeconfig, namespace, workload_type, name, to_revision=req.to_revision
    )
    kube_client.record_audit_event(
        user=user.subject,
        action="ROLLBACK_WORKLOAD",
        cluster_id=ctx.cluster.id,
        namespace=namespace,
        resource=f"{workload_type}/{name}",
        details=f"Rolled back to revision {req.to_revision or 'previous'}",
    )
    return {"cluster_id": ctx.cluster.id, **res}


@app.get("/api/clusters/{cluster_id}/applications")
async def logical_applications(
    namespace: Optional[str] = OptionalNamespaceQuery,
    ctx: ClusterContext = Depends(get_cluster_context),
) -> Dict[str, Any]:
    """Group workloads and services into logical applications."""
    apps = await kube_client.get_logical_applications(ctx.context, ctx.kubeconfig, namespace)
    return {"cluster_id": ctx.cluster.id, **apps}


@app.get("/api/clusters/{cluster_id}/namespaces/{namespace}/pods/{pod_name}/details")
async def pod_details(
    namespace: str = NamespaceSegment,
    pod_name: str = PodNameSegment,
    ctx: ClusterContext = Depends(get_cluster_context),
) -> Dict[str, Any]:
    """Deep inspection of a pod including probe health, restart reasons, and resources."""
    details = await kube_client.get_pod_inspector(ctx.context, ctx.kubeconfig, namespace, pod_name)
    return {"cluster_id": ctx.cluster.id, **details}


@app.get("/api/clusters/{cluster_id}/events/extended")
async def extended_events(
    namespace: Optional[str] = OptionalNamespaceQuery,
    limit: int = Query(200, ge=10, le=1000),
    ctx: ClusterContext = Depends(get_cluster_context),
) -> Dict[str, Any]:
    """Fetch extended event history beyond Kubernetes 1-hour eviction."""
    events = await kube_client.get_extended_events(ctx.context, ctx.kubeconfig, namespace, limit=limit)
    return {"cluster_id": ctx.cluster.id, "namespace": namespace, **events}


@app.get("/api/audit/logs")
async def audit_logs(
    cluster_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    user: CurrentUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Audit trail of privileged user actions."""
    logs = kube_client.get_audit_logs(cluster_id=cluster_id, limit=limit)
    return {"items": logs, "count": len(logs)}


@app.get("/api/clusters/{cluster_id}/ecosystem")
async def ecosystem_overview(ctx: ClusterContext = Depends(get_cluster_context)) -> Dict[str, Any]:
    """Which well-known platform CRDs (cert-manager, KEDA, Karpenter, Velero,
    External Secrets, Kyverno, Argo CD, Flux, ...) are actually installed on
    this cluster, with resource counts. Detection only -- no deep per-CRD
    rendering yet."""
    eco = await kube_client.get_ecosystem_overview(ctx.context, ctx.kubeconfig)
    return {"cluster_id": ctx.cluster.id, **eco}


@app.get("/api/clusters/{cluster_id}/diagnostics/rca")
async def root_cause_analysis(
    cluster_id: str,
    namespace: Optional[str] = OptionalNamespaceQuery,
    simulate: bool = Query(False, description="Simulate all 12 steps of the kubectl apply Machine"),
    user: CurrentUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """AI-assisted root cause analysis, failure folding, and blast radius mapping."""
    if simulate:
        from kube_client.rca import get_simulated_12_step_rca

        diag = get_simulated_12_step_rca(namespace or "kubeorbit-demo")
        return {"cluster_id": cluster_id, "namespace": namespace, **diag}

    ctx = await get_cluster_context(cluster_id=cluster_id, user=user)
    diag = await kube_client.diagnose_cluster_issues(ctx.context, ctx.kubeconfig, namespace)
    return {"cluster_id": ctx.cluster.id, "namespace": namespace, **diag}


class AIDiagnoseRequest(BaseModel):
    namespace: Optional[str] = None
    resource_type: Optional[str] = None
    resource_name: Optional[str] = None
    question: Optional[str] = None


@app.post("/api/clusters/{cluster_id}/ai/diagnose")
async def ai_diagnose(
    req: AIDiagnoseRequest,
    ctx: ClusterContext = Depends(get_cluster_context),
    user: CurrentUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """One-turn AI agent investigation, reusing the same read-only tool set
    mcp_server/ exposes over MCP (see ai_agent.py). Never mutates the
    cluster; degrades to a clear error if no model credentials are
    configured rather than failing the whole request."""
    import ai_agent
    from mcp_server.client import configure_inprocess_app

    configure_inprocess_app(app)
    result = await ai_agent.diagnose(
        cluster_id=ctx.cluster.id,
        namespace=req.namespace,
        resource_type=req.resource_type,
        resource_name=req.resource_name,
        question=req.question,
        user_id=user.subject,
    )
    logger.info(
        "AI diagnose: user=%s cluster=%s namespace=%s resource=%s/%s ok=%s",
        user.subject, ctx.cluster.id, req.namespace, req.resource_type, req.resource_name, "error" not in result,
    )
    return {"cluster_id": ctx.cluster.id, **result}


class AIChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


@app.post("/api/clusters/{cluster_id}/ai/chat")
async def ai_chat(
    req: AIChatRequest,
    ctx: ClusterContext = Depends(get_cluster_context),
    user: CurrentUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """One turn of the floating "Ask AI" chat panel (see ai_agent.chat()).
    Multi-turn: pass back the `session_id` a prior call returned to continue
    that conversation, or omit it to start a new one. cluster_id always
    comes from this URL's path, resolved server-side by get_cluster_context
    like every other cluster route -- the request body cannot point the
    conversation at a different cluster."""
    import ai_agent
    from mcp_server.client import configure_inprocess_app

    configure_inprocess_app(app)
    result = await ai_agent.chat(
        cluster_id=ctx.cluster.id,
        message=req.message,
        session_id=req.session_id,
        user_id=user.subject,
    )
    logger.info(
        "AI chat: user=%s cluster=%s session=%s ok=%s",
        user.subject, ctx.cluster.id, req.session_id, "error" not in result,
    )
    return {"cluster_id": ctx.cluster.id, **result}


@app.get("/api/mcp/tools")
async def list_mcp_tools(user: CurrentUser = Depends(get_current_user)) -> Dict[str, Any]:
    """Return the read-only MCP tool catalog and interactive AG-UI frontend tools."""
    import ai_agent
    from mcp_server.server import get_mcp_tool_catalog

    mcp_tools = get_mcp_tool_catalog()
    return {
        "protocol": "mcp",
        "read_only": True,
        "count": len(mcp_tools),
        "tools": mcp_tools,
        "ag_ui_frontend_tools": [fn.__name__ for fn in ai_agent._FRONTEND_UI_TOOLS],
    }


from ai_agent import RunAgentInput, stream_ag_ui_run


@app.post("/api/clusters/{cluster_id}/ai/ag-ui")
async def ai_ag_ui(
    req: RunAgentInput,
    ctx: ClusterContext = Depends(get_cluster_context),
    user: CurrentUser = Depends(get_current_user),
) -> StreamingResponse:
    """AG-UI Protocol (v1.0) SSE streaming endpoint for interactive ADK + MCP agent runs."""
    from mcp_server.client import configure_inprocess_app

    configure_inprocess_app(app)
    logger.info(
        "AG-UI stream: user=%s role=%s cluster=%s thread=%s",
        user.subject, user.role, ctx.cluster.id, req.thread_id or "(new)",
    )
    return StreamingResponse(
        stream_ag_ui_run(
            ctx.cluster.id,
            req,
            user_id=user.subject,
            user_role=user.role,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-AG-UI-Protocol": "1.0",
        },
    )


# --------------------------------------------------------------------------- #
# Static frontend (must be mounted last so it never shadows /api/* routes)
# --------------------------------------------------------------------------- #

class NoCacheStaticFiles(StaticFiles):
    """Serve versioned (`?v=...`) non-HTML static assets with browser caching
    while forcing revalidation (`no-cache`, allowing 304 Not Modified via ETag)
    for index.html and unversioned assets so deploys never serve stale code."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        qs = scope.get("query_string", b"") or b""
        is_html = path == "" or path.endswith(".html")
        if b"v=" in qs and not is_html:
            response.headers["Cache-Control"] = "public, max-age=3600"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/", NoCacheStaticFiles(directory=str(_static_dir), html=True), name="static")
