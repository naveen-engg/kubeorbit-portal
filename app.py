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
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


import auth
import kube_client
from auth import CurrentUser, get_current_user
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

app = FastAPI(title="KubeOrbit", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_runtime_env().cors_allow_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
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
    cluster_id: str, user: CurrentUser = Depends(get_current_user)
) -> ClusterContext:
    cluster = _get_cluster_or_404(cluster_id)
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
    store = get_config_store()
    env = get_runtime_env()
    binaries = {b: shutil.which(b) is not None for b in ("kubectl", "helm", "gcloud", "aws", "az")}
    return {
        "version": APP_VERSION,
        "auth_mode": env.auth_mode,
        "user": {"subject": user.subject, "method": user.method},
        "providers": available_providers(),
        "cli_binaries": binaries,
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
    id: str = Field(..., description="Unique URL-safe cluster ID")
    name: str = Field(..., description="Human readable cluster name")
    provider: str = Field(..., description="gke | eks | aks")
    environment: str = Field(..., description="sandbox | nonprod | prod")
    team_id: str = Field("platform", description="Team identifier")
    team_name: str = Field("Platform Engineering", description="Team name")
    description: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)


class ParseKubeconfigRequest(BaseModel):
    kubeconfig_path: str = Field("~/.kube/config", description="Path to local kubeconfig file")


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
                })
    return {"clusters": clusters}


@app.post("/api/admin/clusters/test-connection")
async def test_cluster_connection(
    req: OnboardClusterRequest, user: CurrentUser = Depends(get_current_user)
) -> Dict[str, Any]:
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
    logger.info("Onboarded cluster '%s' for team '%s' (user=%s)", req.id, req.team_id, user.subject)
    return {"status": "ok", "teams": store.teams_payload()}


@app.delete("/api/admin/clusters/{cluster_id}")
async def delete_cluster(
    cluster_id: str, user: CurrentUser = Depends(get_current_user)
) -> Dict[str, Any]:
    from config import delete_cluster_from_config
    store = delete_cluster_from_config(cluster_id)
    logger.info("Deleted cluster '%s' (user=%s)", cluster_id, user.subject)
    return {"status": "ok", "teams": store.teams_payload()}


@app.post("/api/admin/clusters/parse-kubeconfig")
async def parse_kubeconfig(
    req: ParseKubeconfigRequest, user: CurrentUser = Depends(get_current_user)
) -> Dict[str, Any]:
    raw_path = req.kubeconfig_path.strip()
    if not raw_path:
        raw_path = "~/.kube/config"
    expanded_path = os.path.expanduser(raw_path)
    path_obj = Path(expanded_path)
    
    if not path_obj.exists():
        raise HTTPException(status_code=404, detail=f"Kubeconfig file not found at: {raw_path}")

    try:
        import yaml
        with open(path_obj, "r", encoding="utf-8") as f:
            content = yaml.safe_load(f) or {}
        
        current_ctx = content.get("current-context", "")
        contexts = []
        for ctx in content.get("contexts", []):
            ctx_name = ctx.get("name", "")
            cluster_ref = ctx.get("context", {}).get("cluster", ctx_name)
            if ctx_name:
                contexts.append({"name": ctx_name, "cluster": cluster_ref})

        # Derive a safe suggested ID from current context
        suggested_id = current_ctx.lower().replace("/", "-").replace(":", "-").replace("_", "-") if current_ctx else ""

        return {
            "status": "ok",
            "kubeconfig_path": str(path_obj),
            "current_context": current_ctx,
            "contexts": contexts,
            "suggested_id": suggested_id,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to parse kubeconfig: {str(exc)}")



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


@app.get("/api/clusters/{cluster_id}/info")
async def cluster_info(ctx: ClusterContext = Depends(get_cluster_context)) -> Dict[str, Any]:
    meta_ttl = get_config_store().settings.cluster_meta_cache_ttl_seconds
    meta: ClusterMeta = await kube_client.cached(
        f"provider-meta:{ctx.cluster.id}", meta_ttl, ctx.provider.get_cluster_metadata
    )

    try:
        nodes = await kube_client.get_nodes(ctx.context, ctx.kubeconfig)
        counts: Dict[str, int] = {}
        for n in nodes:
            pool = n.get("node_pool") or "unassigned"
            counts[pool] = counts.get(pool, 0) + 1
        for pool in meta.node_pools:
            if pool.name in counts:
                pool.node_count = counts[pool.name]
        node_fetch_error = None
    except kube_client.CLIError as exc:
        nodes = []
        node_fetch_error = str(exc)

    helm = await kube_client.get_helm_releases(ctx.context, ctx.kubeconfig)

    return {
        "badge": ctx.badge(),
        "meta": _json(meta),
        "node_count": len(nodes),
        "node_fetch_error": node_fetch_error,
        "helm": helm,
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


@app.post("/api/clusters/{cluster_id}/namespaces/{namespace}/secrets/{name}/reveal")
async def reveal_secret(
    namespace: str = NamespaceSegment,
    name: str = ResourceNameSegment,
    ctx: ClusterContext = Depends(get_cluster_context),
    user: CurrentUser = Depends(get_current_user),
) -> JSONResponse:
    """Explicit, audited, uncached decode of one secret's values. Never
    invoked implicitly by any list/overview endpoint."""
    logger.warning(
        "SECRET REVEAL: user=%s cluster=%s namespace=%s secret=%s",
        user.subject, ctx.cluster.id, namespace, name,
    )
    result = await kube_client.reveal_secret(ctx.context, ctx.kubeconfig, namespace, name)
    response = JSONResponse(content=_json(result))
    response.headers["Cache-Control"] = "no-store"
    return response


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


@app.get("/api/clusters/{cluster_id}/helm")
async def cluster_helm(ctx: ClusterContext = Depends(get_cluster_context)) -> Dict[str, Any]:
    helm = await kube_client.get_helm_releases(ctx.context, ctx.kubeconfig)
    return {"cluster_id": ctx.cluster.id, **helm}


@app.get("/api/clusters/{cluster_id}/namespaces/{namespace}/pods/{pod_name}/logs")
async def pod_logs(
    namespace: str,
    pod_name: str,
    container: Optional[str] = Query(None),
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
    kind: str = Query(...),
    name: str = Query(...),
    namespace: Optional[str] = OptionalNamespaceQuery,
    ctx: ClusterContext = Depends(get_cluster_context),
) -> Dict[str, Any]:
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
    """Unified multi-cluster fleet dashboard aggregating all registered clusters."""
    store = get_config_store()
    clusters_info = []
    total_nodes = 0
    total_pods = 0
    total_namespaces = 0
    total_cpu_cores = 0.0
    total_mem_gib = 0.0
    online_count = 0

    for team in store.teams:
        for env_group in team.environments:
            for cluster_cfg in env_group.clusters:
                try:
                    provider = build_provider(cluster_cfg)
                    auth_res = await auth.ensure_cluster_authenticated(cluster_cfg, provider, user)
                    nodes = await kube_client.get_nodes(auth_res.context_name, Path(auth_res.kubeconfig_path))
                    namespaces = await kube_client.get_namespaces(auth_res.context_name, Path(auth_res.kubeconfig_path), include_system=True)
                    pods = await kube_client.get_pods(auth_res.context_name, Path(auth_res.kubeconfig_path), "")

                    c_nodes = len(nodes)
                    c_pods = len(pods)
                    c_ns = len(namespaces)
                    online_count += 1

                    # Sum CPU & memory from nodes
                    c_cpu = 0.0
                    c_mem = 0.0
                    for n in nodes:
                        c_cpu += float(n.get("cpu_capacity", 0) or 0)
                        # mem in Ki
                        mem_str = str(n.get("memory_capacity", "0")).lower().replace("ki", "")
                        try:
                            c_mem += float(mem_str) / (1024 * 1024)
                        except ValueError:
                            pass

                    total_nodes += c_nodes
                    total_pods += c_pods
                    total_namespaces += c_ns
                    total_cpu_cores += c_cpu
                    total_mem_gib += c_mem

                    # Per-cluster health: % of pods actually healthy (same
                    # Running/Succeeded definition as get_workloads_overview),
                    # not just "did we manage to connect" -- that's already
                    # covered separately by `status` below. A cluster with no
                    # pods at all reads as 100% (nothing to be unhealthy).
                    healthy_pod_count = sum(1 for p in pods if p.get("status") in ("Running", "Succeeded"))
                    c_health_score = round((healthy_pod_count / c_pods) * 100) if c_pods else 100

                    clusters_info.append({
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
                    })
                except Exception as exc:
                    clusters_info.append({
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
                    })

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
    """Catalog of Deployments, StatefulSets, DaemonSets, Jobs, and CronJobs."""
    catalog = await kube_client.get_workloads_catalog(ctx.context, ctx.kubeconfig, namespace)
    return {"cluster_id": ctx.cluster.id, **catalog}


@app.get("/api/clusters/{cluster_id}/workloads/{workload_type}/{name}/rollout/history")
async def workload_rollout_history(
    workload_type: str,
    name: str,
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
    workload_type: str,
    name: str,
    req: RollbackRequest,
    namespace: str = RequiredNamespaceQuery,
    ctx: ClusterContext = Depends(get_cluster_context),
    user: CurrentUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Execute rollback for a workload and record an audit log."""
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
    namespace: Optional[str] = OptionalNamespaceQuery,
    ctx: ClusterContext = Depends(get_cluster_context),
) -> Dict[str, Any]:
    """AI-assisted root cause analysis, failure folding, and blast radius mapping."""
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

    result = await ai_agent.diagnose(
        cluster_id=ctx.cluster.id,
        namespace=req.namespace,
        resource_type=req.resource_type,
        resource_name=req.resource_name,
        question=req.question,
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

    result = await ai_agent.chat(cluster_id=ctx.cluster.id, message=req.message, session_id=req.session_id)
    logger.info(
        "AI chat: user=%s cluster=%s session=%s ok=%s",
        user.subject, ctx.cluster.id, req.session_id, "error" not in result,
    )
    return {"cluster_id": ctx.cluster.id, **result}


# --------------------------------------------------------------------------- #
# Static frontend (must be mounted last so it never shadows /api/* routes)
# --------------------------------------------------------------------------- #

class NoCacheStaticFiles(StaticFiles):
    """Force browsers to revalidate every request instead of trusting their
    own cache heuristics for how long an unlabeled static file stays fresh --
    without this, editing styles.css/app.js can leave a user staring at a
    stale cached copy with no visible sign anything is wrong. `no-cache`
    (not `no-store`) still lets the browser use a cached body once the
    server confirms via ETag/Last-Modified that it hasn't changed."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/", NoCacheStaticFiles(directory=str(_static_dir), html=True), name="static")
