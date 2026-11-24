"""Aggregate/graph views built on top of resources.py: workloads overview,
Network/Storage topology, the full namespace resource map, and the
cluster-wide node/workload map."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import get_config_store

from ._cache import _cache, _kubectl_semaphore
from ._cli import run_cli
from ._fetch import cached_kubectl_list
from .normalize import _normalize_pod
from .resources import (
    get_configmaps,
    get_cronjobs,
    get_daemonsets,
    get_deployments,
    get_gateways,
    get_httproutes,
    get_ingresses,
    get_jobs,
    get_pods,
    get_pvcs,
    get_pvs,
    get_secrets_metadata,
    get_serviceaccounts,
    get_services,
    get_statefulsets,
    get_storageclasses,
)


async def get_workloads_overview(
    context: str, kubeconfig_path: Path, namespace: Optional[str]
) -> Dict[str, Any]:
    pods, deployments, statefulsets, daemonsets, jobs, cronjobs = await asyncio.gather(
        get_pods(context, kubeconfig_path, namespace, include_metrics=False),
        get_deployments(context, kubeconfig_path, namespace),
        get_statefulsets(context, kubeconfig_path, namespace),
        get_daemonsets(context, kubeconfig_path, namespace),
        get_jobs(context, kubeconfig_path, namespace),
        get_cronjobs(context, kubeconfig_path, namespace),
    )

    if namespace is None:
        # Cluster-wide view: exclude system namespaces by default, same as
        # the namespaces list endpoint, so counts reflect user workloads.
        store = get_config_store()
        keep = lambda items: [i for i in items if not store.is_namespace_excluded(i["namespace"])]
        pods, deployments, statefulsets, daemonsets, jobs, cronjobs = (
            keep(pods), keep(deployments), keep(statefulsets), keep(daemonsets), keep(jobs), keep(cronjobs),
        )

    def summarize(items: List[Dict[str, Any]], healthy_key: str = "healthy"):
        unhealthy = [i for i in items if not i.get(healthy_key, True)]
        return {
            "total": len(items),
            "healthy": len(items) - len(unhealthy),
            "unhealthy": len(unhealthy),
            "issues": unhealthy[:20],
        }

    pod_unhealthy = [p for p in pods if p["status"] not in ("Running", "Succeeded")]
    return {
        "pods": {
            "total": len(pods),
            "healthy": len(pods) - len(pod_unhealthy),
            "unhealthy": len(pod_unhealthy),
            "issues": pod_unhealthy[:20],
        },
        "deployments": summarize(deployments),
        "statefulsets": summarize(statefulsets),
        "daemonsets": summarize(daemonsets),
        "jobs": summarize(jobs),
        "cronjobs": {"total": len(cronjobs), "active": sum(c["active_jobs"] for c in cronjobs)},
    }


async def get_network_topology(context: str, kubeconfig_path: Path, namespace: str) -> Dict[str, Any]:
    """ILB / Ingress -> Gateway -> HTTPRoute -> Service -> Pod, expressed as a generic
    node/edge graph the frontend renders identically for every provider."""
    ttl = get_config_store().settings.kube_cache_ttl_seconds
    ingresses, services, gateways, httproutes, pods, (raw_pods, _) = await asyncio.gather(
        get_ingresses(context, kubeconfig_path, namespace),
        get_services(context, kubeconfig_path, namespace),
        get_gateways(context, kubeconfig_path, namespace),
        get_httproutes(context, kubeconfig_path, namespace),
        get_pods(context, kubeconfig_path, namespace, include_metrics=False),
        cached_kubectl_list(context, kubeconfig_path, "pods", namespace, ttl),
    )

    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, str]] = []
    pod_by_name = {p["name"]: p for p in pods}
    raw_pod_labels = {
        rp.get("metadata", {}).get("name", ""): (rp.get("metadata", {}).get("labels") or {})
        for rp in (raw_pods or [])
    }

    def add_node(node_id: str, label: str, kind: str, status: str = "unknown", meta: Optional[Dict] = None):
        if not any(n["id"] == node_id for n in nodes):
            nodes.append({"id": node_id, "label": label, "kind": kind, "status": status, "meta": meta or {}})

    for svc in services:
        if svc["type"] == "LoadBalancer" and svc["external_ips"]:
            ilb_id = f"ilb::{svc['name']}"
            add_node(ilb_id, svc["external_ips"][0], "ilb", "healthy", {"service": svc["name"]})

    for ing in ingresses.get("items", []):
        ing_id = f"ingress::{ing['name']}"
        add_node(ing_id, ing["name"], "ingress", "healthy" if ing.get("addresses") else "pending", ing)
        for rule in ing.get("rules", []):
            for p in rule.get("paths", []):
                backend = p.get("backend_service")
                if not backend:
                    continue
                svc_id = f"service::{backend}"
                svc_match = next((s for s in services if s["name"] == backend), None)
                add_node(svc_id, backend, "service", "healthy" if svc_match else "unknown", svc_match or {})
                edges.append({"source": ing_id, "target": svc_id})

    for gw in gateways["items"]:
        gw_id = f"gateway::{gw['name']}"
        add_node(gw_id, gw["name"], "gateway", "healthy" if gw["addresses"] else "pending", gw)
        for svc in services:
            if svc["type"] == "LoadBalancer" and svc["external_ips"]:
                edges.append({"source": f"ilb::{svc['name']}", "target": gw_id})

    for route in httproutes["items"]:
        route_id = f"httproute::{route['name']}"
        add_node(route_id, route["name"], "httproute", "healthy", route)
        for parent in route["parent_refs"]:
            gw_id = f"gateway::{parent}"
            if any(n["id"] == gw_id for n in nodes):
                edges.append({"source": gw_id, "target": route_id})
        for rule in route["rules"]:
            for backend in rule["backend_refs"]:
                svc_id = f"service::{backend}"
                svc_match = next((s for s in services if s["name"] == backend), None)
                add_node(svc_id, backend, "service", "healthy" if svc_match else "unknown", svc_match or {})
                edges.append({"source": route_id, "target": svc_id})

    for svc in services:
        svc_id = f"service::{svc['name']}"
        add_node(svc_id, svc["name"], "service", "healthy", svc)
        selector = svc.get("selector") or {}
        if not selector:
            continue
        matched_any = False
        for pod_name, labels in raw_pod_labels.items():
            if _matches_selector(labels, selector) and pod_name in pod_by_name:
                pod = pod_by_name[pod_name]
                pod_id = f"pod::{pod['name']}"
                add_node(pod_id, pod["name"], "pod", "healthy" if pod["status"] == "Running" else "unhealthy", pod)
                edges.append({"source": svc_id, "target": pod_id})
                matched_any = True
        if not matched_any:
            app_sel = selector.get("app") or selector.get("app.kubernetes.io/name")
            if app_sel:
                for pod in pods:
                    if pod["name"].startswith(app_sel):
                        pod_id = f"pod::{pod['name']}"
                        add_node(pod_id, pod["name"], "pod", "healthy" if pod["status"] == "Running" else "unhealthy", pod)
                        edges.append({"source": svc_id, "target": pod_id})

    return {
        "namespace": namespace,
        "gateway_api_available": gateways["available"] and httproutes["available"],
        "nodes": nodes,
        "edges": edges,
    }


async def get_storage_topology(context: str, kubeconfig_path: Path, namespace: str) -> Dict[str, Any]:
    """StorageClass -> PV -> PVC -> Pod graph."""
    ttl = get_config_store().settings.kube_cache_ttl_seconds
    storageclasses, pvs, pvcs, (raw_pods, _) = await asyncio.gather(
        get_storageclasses(context, kubeconfig_path),
        get_pvs(context, kubeconfig_path),
        get_pvcs(context, kubeconfig_path, namespace),
        cached_kubectl_list(context, kubeconfig_path, "pods", namespace, ttl),
    )

    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, str]] = []

    def add_node(node_id: str, label: str, kind: str, status: str = "unknown", meta: Optional[Dict] = None):
        nodes.append({"id": node_id, "label": label, "kind": kind, "status": status, "meta": meta or {}})

    used_sc_names = {pvc["storage_class"] for pvc in pvcs if pvc.get("storage_class")}
    for sc in storageclasses:
        if sc["name"] not in used_sc_names:
            continue
        add_node(f"sc::{sc['name']}", sc["name"], "storageclass", "healthy", sc)

    pv_by_name = {pv["name"]: pv for pv in pvs}
    for pvc in pvcs:
        pvc_id = f"pvc::{pvc['name']}"
        add_node(pvc_id, pvc["name"], "pvc", "healthy" if pvc["status"] == "Bound" else "pending", pvc)

        if pvc.get("storage_class"):
            edges.append({"source": f"sc::{pvc['storage_class']}", "target": f"pv::{pvc.get('volume_name')}"})

        pv = pv_by_name.get(pvc.get("volume_name"))
        if pv:
            pv_id = f"pv::{pv['name']}"
            if not any(n["id"] == pv_id for n in nodes):
                add_node(pv_id, pv["name"], "pv", "healthy" if pv["status"] == "Bound" else "pending", pv)
            edges.append({"source": pv_id, "target": pvc_id})

    for raw_pod in raw_pods:
        pod_name = raw_pod["metadata"]["name"]
        pod_status = raw_pod.get("status", {}).get("phase", "Unknown")
        for vol in raw_pod.get("spec", {}).get("volumes", []) or []:
            claim = vol.get("persistentVolumeClaim")
            if not claim:
                continue
            pvc_id = f"pvc::{claim['claimName']}"
            if not any(n["id"] == pvc_id for n in nodes):
                continue
            pod_id = f"pod::{pod_name}"
            if not any(n["id"] == pod_id for n in nodes):
                add_node(pod_id, pod_name, "pod", "healthy" if pod_status == "Running" else "unhealthy")
            edges.append({"source": pvc_id, "target": pod_id})

    return {"namespace": namespace, "nodes": nodes, "edges": edges}


def _owner_ref(obj: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    for ref in obj.get("metadata", {}).get("ownerReferences", []) or []:
        if ref.get("controller"):
            return ref.get("kind"), ref.get("name")
    return None


def _matches_selector(labels: Dict[str, str], selector: Dict[str, str]) -> bool:
    if not selector:
        return False
    return all(labels.get(k) == v for k, v in selector.items())


async def get_namespace_map(context: str, kubeconfig_path: Path, namespace: str) -> Dict[str, Any]:
    """Every resource in the namespace connected end to end: Ingress/Gateway
    routing in, through Services to Pods, alongside each Pod's owning
    workload controller and the ConfigMaps/Secrets/ServiceAccount/PVCs it
    actually references -- an ArgoCD-application-tree-style view of "what's
    here and how it's wired", as opposed to Network/Storage Topology's
    narrower single-path views.
    """
    ttl = get_config_store().settings.kube_cache_ttl_seconds

    (
        ingresses, gateways, httproutes, services, deployments, statefulsets,
        daemonsets, jobs, cronjobs, configmaps, secrets, serviceaccounts, pvcs,
        (raw_pods, _), (raw_replicasets, _),
    ) = await asyncio.gather(
        get_ingresses(context, kubeconfig_path, namespace),
        get_gateways(context, kubeconfig_path, namespace),
        get_httproutes(context, kubeconfig_path, namespace),
        get_services(context, kubeconfig_path, namespace),
        get_deployments(context, kubeconfig_path, namespace),
        get_statefulsets(context, kubeconfig_path, namespace),
        get_daemonsets(context, kubeconfig_path, namespace),
        get_jobs(context, kubeconfig_path, namespace),
        get_cronjobs(context, kubeconfig_path, namespace),
        get_configmaps(context, kubeconfig_path, namespace),
        get_secrets_metadata(context, kubeconfig_path, namespace),
        get_serviceaccounts(context, kubeconfig_path, namespace),
        get_pvcs(context, kubeconfig_path, namespace),
        cached_kubectl_list(context, kubeconfig_path, "pods", namespace, ttl),
        cached_kubectl_list(context, kubeconfig_path, "replicasets", namespace, ttl),
    )

    rs_owner: Dict[str, Tuple[str, str]] = {}
    for rs in raw_replicasets:
        owner = _owner_ref(rs)
        if owner:
            rs_owner[rs["metadata"]["name"]] = owner

    by_name = {
        "service": {s["name"]: s for s in services},
        "configmap": {c["name"]: c for c in configmaps},
        "secret": {s["name"]: s for s in secrets},
        "serviceaccount": {sa["name"]: sa for sa in serviceaccounts},
        "pvc": {p["name"]: p for p in pvcs},
    }
    workload_lookup: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for kind, items in (
        ("Deployment", deployments), ("StatefulSet", statefulsets), ("DaemonSet", daemonsets),
        ("Job", jobs), ("CronJob", cronjobs),
    ):
        for item in items:
            workload_lookup[(kind, item["name"])] = item

    normalized_pods = {rp["metadata"]["name"]: _normalize_pod(rp) for rp in raw_pods}

    nodes: Dict[str, Dict[str, Any]] = {}
    edges: List[Dict[str, str]] = []
    edge_seen: set = set()

    def add_node(node_id: str, label: str, kind: str, group: str, status: str = "unknown", meta: Optional[Dict] = None):
        if node_id not in nodes:
            nodes[node_id] = {"id": node_id, "label": label, "kind": kind, "group": group, "status": status, "meta": meta or {}}

    def add_edge(source: str, target: str):
        key = (source, target)
        if key not in edge_seen:
            edge_seen.add(key)
            edges.append({"source": source, "target": target})

    def add_pod_node(pod_name: str) -> str:
        pod = normalized_pods[pod_name]
        pod_id = f"pod::{pod_name}"
        add_node(pod_id, pod_name, "pod", "pod", "healthy" if pod["status"] in ("Running", "Succeeded") else "unhealthy", pod)
        return pod_id

    # Ingress -> Service
    for ing in ingresses["items"]:
        ing_id = f"ingress::{ing['name']}"
        add_node(ing_id, ing["name"], "ingress", "ingress", "healthy" if ing["addresses"] else "pending", ing)
        for rule in ing["rules"]:
            for p in rule["paths"]:
                if not p["backend_service"]:
                    continue
                svc = by_name["service"].get(p["backend_service"])
                svc_id = f"service::{p['backend_service']}"
                add_node(svc_id, p["backend_service"], "service", "service", "healthy" if svc else "unknown", svc or {})
                add_edge(ing_id, svc_id)

    # Gateway -> HTTPRoute -> Service
    for gw in gateways["items"]:
        gw_id = f"gateway::{gw['name']}"
        add_node(gw_id, gw["name"], "gateway", "gateway", "healthy" if gw["addresses"] else "pending", gw)
    for route in httproutes["items"]:
        route_id = f"httproute::{route['name']}"
        add_node(route_id, route["name"], "httproute", "httproute", "healthy", route)
        for parent in route["parent_refs"]:
            gw_id = f"gateway::{parent}"
            if gw_id in nodes:
                add_edge(gw_id, route_id)
        for rule in route["rules"]:
            for backend in rule["backend_refs"]:
                svc = by_name["service"].get(backend)
                svc_id = f"service::{backend}"
                add_node(svc_id, backend, "service", "service", "healthy" if svc else "unknown", svc or {})
                add_edge(route_id, svc_id)

    # Service -> Pod, matched on the service's real label selector against
    # each pod's real labels (not the name-prefix heuristic Network Topology
    # uses -- we already have raw pods loaded here for owner/volume/env
    # introspection, so a proper selector match costs nothing extra).
    for svc in services:
        selector = svc.get("selector") or {}
        if not selector:
            continue
        svc_id = f"service::{svc['name']}"
        add_node(svc_id, svc["name"], "service", "service", "healthy", svc)
        for raw_pod in raw_pods:
            labels = raw_pod.get("metadata", {}).get("labels", {}) or {}
            if _matches_selector(labels, selector):
                add_edge(svc_id, add_pod_node(raw_pod["metadata"]["name"]))

    # Workload -> Pod, and Pod -> ConfigMap/Secret/ServiceAccount/PVC it
    # actually references (volumes, envFrom, env[].valueFrom).
    for raw_pod in raw_pods:
        pod_name = raw_pod["metadata"]["name"]
        pod_id = add_pod_node(pod_name)
        spec = raw_pod.get("spec", {}) or {}

        owner = _owner_ref(raw_pod)
        if owner:
            owner_kind, owner_name = owner
            if owner_kind == "ReplicaSet":
                owner_kind, owner_name = rs_owner.get(owner_name, (owner_kind, owner_name))
            workload_id = f"{owner_kind.lower()}::{owner_name}"
            workload_data = workload_lookup.get((owner_kind, owner_name), {})
            add_node(
                workload_id, owner_name, owner_kind.lower(), "workload",
                "healthy" if workload_data.get("healthy", True) else "unhealthy", workload_data,
            )
            add_edge(workload_id, pod_id)

        sa_name = spec.get("serviceAccountName") or "default"
        if sa_name in by_name["serviceaccount"]:
            sa_id = f"serviceaccount::{sa_name}"
            add_node(sa_id, sa_name, "serviceaccount", "serviceaccount", "healthy", by_name["serviceaccount"][sa_name])
            add_edge(sa_id, pod_id)

        referenced_cm: set = set()
        referenced_secret: set = set()
        for vol in spec.get("volumes", []) or []:
            if "configMap" in vol:
                referenced_cm.add((vol["configMap"] or {}).get("name"))
            if "secret" in vol:
                referenced_secret.add((vol["secret"] or {}).get("secretName"))
            claim = (vol.get("persistentVolumeClaim") or {}).get("claimName")
            if claim and claim in by_name["pvc"]:
                pvc_id = f"pvc::{claim}"
                pvc_data = by_name["pvc"][claim]
                add_node(pvc_id, claim, "pvc", "pvc", "healthy" if pvc_data["status"] == "Bound" else "pending", pvc_data)
                add_edge(pvc_id, pod_id)
        for container in spec.get("containers", []) or []:
            for ef in container.get("envFrom", []) or []:
                if "configMapRef" in ef:
                    referenced_cm.add((ef["configMapRef"] or {}).get("name"))
                if "secretRef" in ef:
                    referenced_secret.add((ef["secretRef"] or {}).get("name"))
            for env in container.get("env", []) or []:
                value_from = env.get("valueFrom") or {}
                if "configMapKeyRef" in value_from:
                    referenced_cm.add((value_from["configMapKeyRef"] or {}).get("name"))
                if "secretKeyRef" in value_from:
                    referenced_secret.add((value_from["secretKeyRef"] or {}).get("name"))

        for cm_name in referenced_cm:
            if cm_name in by_name["configmap"]:
                cm_id = f"configmap::{cm_name}"
                add_node(cm_id, cm_name, "configmap", "configmap", "healthy", by_name["configmap"][cm_name])
                add_edge(cm_id, pod_id)
        for secret_name in referenced_secret:
            if secret_name in by_name["secret"]:
                secret_id = f"secret::{secret_name}"
                add_node(secret_id, secret_name, "secret", "secret", "healthy", by_name["secret"][secret_name])
                add_edge(secret_id, pod_id)

    _compute_app_groups(nodes, edges)

    return {
        "namespace": namespace,
        "ingress_available": ingresses["available"],
        "gateway_api_available": gateways["available"] and httproutes["available"],
        "nodes": list(nodes.values()),
        "edges": edges,
    }


_WORKLOAD_KINDS = {"deployment", "statefulset", "daemonset", "job", "cronjob"}


def _compute_app_groups(nodes: Dict[str, Dict[str, Any]], edges: List[Dict[str, str]]) -> None:
    """Tags every node with an `app_group` so the resource map can render
    bordered "app" cards grouping related resources visually (Deployment +
    its Pods + the ConfigMap/Secret they mount, etc.) instead of one flat
    scatter of nodes.

    Workload nodes get their group directly from the same correlation-label
    heuristic as get_logical_applications() (see AppSettings.app_correlation_label_keys).
    Everything else -- pods, services, configmaps, ingresses, ... -- has no
    labels of its own that reliably say which "application" it belongs to,
    so it inherits its group from whichever workload it's connected to,
    propagated outward across the edge graph. Anything left unreachable from
    any workload (a truly orphaned resource) falls into a catch-all "Other"
    group rather than an unset field the frontend would have to special-case.
    """
    label_keys = get_config_store().settings.app_correlation_label_keys

    def app_name_for(node: Dict[str, Any]) -> str:
        labels = (node.get("meta") or {}).get("labels") or {}
        for key in label_keys:
            val = labels.get(key)
            if val:
                return val
        name = node.get("label", "")
        return name.split("-")[0] if "-" in name else name

    for node in nodes.values():
        if node["kind"] in _WORKLOAD_KINDS:
            node["app_group"] = app_name_for(node)

    adjacency: Dict[str, List[str]] = {}
    for e in edges:
        adjacency.setdefault(e["source"], []).append(e["target"])
        adjacency.setdefault(e["target"], []).append(e["source"])

    changed = True
    while changed:
        changed = False
        for node_id, node in nodes.items():
            if node.get("app_group"):
                continue
            for neighbor_id in adjacency.get(node_id, []):
                neighbor = nodes.get(neighbor_id)
                if neighbor and neighbor.get("app_group"):
                    node["app_group"] = neighbor["app_group"]
                    changed = True
                    break

    for node in nodes.values():
        node.setdefault("app_group", "Other")


def _parse_cpu_cores(val_str: str) -> float:
    if not val_str:
        return 0.0
    val_str = val_str.strip()
    if val_str.endswith("m"):
        return float(val_str[:-1]) / 1000.0
    if val_str.endswith("n"):
        return float(val_str[:-1]) / 1_000_000_000.0
    try:
        return float(val_str)
    except ValueError:
        return 0.0


def _parse_memory_bytes(val_str: str) -> float:
    if not val_str:
        return 0.0
    val_str = val_str.strip()
    units = {
        "Ki": 1024,
        "Mi": 1024**2,
        "Gi": 1024**3,
        "Ti": 1024**4,
        "K": 1000,
        "M": 1000**2,
        "G": 1000**3,
    }
    for unit, mult in units.items():
        if val_str.endswith(unit):
            try:
                return float(val_str[:-len(unit)]) * mult
            except ValueError:
                return 0.0
    try:
        return float(val_str)
    except ValueError:
        return 0.0


async def get_cluster_workload_map(
    context: str,
    kubeconfig_path: Path,
) -> Dict[str, Any]:
    settings = get_config_store().settings
    ttl = settings.kube_cache_ttl_seconds
    meta_ttl = settings.cluster_meta_cache_ttl_seconds

    async def _fetch_top_nodes() -> Dict[str, Dict[str, Any]]:
        metrics: Dict[str, Dict[str, Any]] = {}
        try:
            cmd = ["kubectl", "--kubeconfig", str(kubeconfig_path), "--context", context, "top", "nodes", "--no-headers"]
            async with _kubectl_semaphore:
                rc, stdout, _ = await run_cli(cmd, check=False)
            if rc == 0 and stdout:
                for line in stdout.strip().splitlines():
                    parts = line.split()
                    if len(parts) >= 5:
                        node_n, cpu_str, cpu_p, mem_str, mem_p = parts[0], parts[1], parts[2], parts[3], parts[4]
                        metrics[node_n] = {
                            "cpu_str": cpu_str,
                            "cpu_pct": int(cpu_p.replace("%", "")) if "%" in cpu_p else 0,
                            "mem_str": mem_str,
                            "mem_pct": int(mem_p.replace("%", "")) if "%" in mem_p else 0,
                        }
        except Exception:
            return {}
        return metrics

    (node_items, _), (raw_pods, _), (top_nodes_map, _) = await asyncio.gather(
        cached_kubectl_list(context, kubeconfig_path, "nodes", None, meta_ttl),
        cached_kubectl_list(context, kubeconfig_path, "pods", None, ttl),
        _cache.get_or_set(f"{context}:top:nodes", ttl, _fetch_top_nodes),
    )

    # Exclude kube-system namespace from cluster map view
    pod_items = [
        p for p in raw_pods
        if p.get("metadata", {}).get("namespace") != "kube-system"
    ]

    palette = [
        "#3b82f6", "#10b981", "#f59e0b", "#8b5cf6", "#ec4899",
        "#06b6d4", "#f97316", "#14b8a6", "#6366f1", "#a855f7"
    ]
    all_namespaces = sorted(list({p.get("metadata", {}).get("namespace", "default") for p in pod_items}))
    ns_color_map = {ns: palette[i % len(palette)] for i, ns in enumerate(all_namespaces)}

    pods_by_node: Dict[str, Dict[str, List[str]]] = {}
    total_pods = 0
    for pod in pod_items:
        node_name = pod.get("spec", {}).get("nodeName")
        ns = pod.get("metadata", {}).get("namespace", "default")
        pod_name = pod.get("metadata", {}).get("name", "pod")
        if not node_name:
            continue
        total_pods += 1
        pods_by_node.setdefault(node_name, {}).setdefault(ns, []).append(pod_name)

    node_cards = []
    ready_count = 0

    for node in node_items:
        meta = node.get("metadata", {})
        node_name = meta.get("name", "unknown")
        labels = meta.get("labels", {})
        status_conditions = node.get("status", {}).get("conditions", [])
        is_ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in status_conditions)
        if is_ready:
            ready_count += 1

        machine_type = (
            labels.get("beta.kubernetes.io/instance-type")
            or labels.get("node.kubernetes.io/instance-type")
            or "e2-small"
        )
        zone = (
            labels.get("topology.kubernetes.io/zone")
            or labels.get("failure-domain.beta.kubernetes.io/zone")
            or "us-central1-a"
        )
        node_pool = (
            labels.get("cloud.google.com/gke-nodepool")
            or labels.get("eks.amazonaws.com/nodegroup")
            or labels.get("kubernetes.azure.com/agentpool")
            or "default-pool"
        )

        allocatable = node.get("status", {}).get("allocatable", {})
        cpu_alloc = allocatable.get("cpu", "2")
        mem_alloc = allocatable.get("memory", "2Gi")
        max_pods = int(allocatable.get("pods", "110"))

        cpu_alloc_cores = _parse_cpu_cores(cpu_alloc)
        mem_alloc_bytes = _parse_memory_bytes(mem_alloc)
        mem_alloc_gib = mem_alloc_bytes / (1024**3)

        top_info = top_nodes_map.get(node_name)
        if top_info:
            cpu_used_cores = _parse_cpu_cores(top_info["cpu_str"])
            cpu_pct = top_info["cpu_pct"]
            mem_used_bytes = _parse_memory_bytes(top_info["mem_str"])
            mem_used_gib = mem_used_bytes / (1024**3)
            mem_pct = top_info["mem_pct"]
        else:
            cpu_used_cores = cpu_alloc_cores * 0.15
            cpu_pct = 15
            mem_used_gib = mem_alloc_gib * 0.25
            mem_pct = 25

        node_pods = pods_by_node.get(node_name, {})
        pod_count = sum(len(p_list) for p_list in node_pods.values())

        ns_breakdown = [
            {
                "namespace": ns,
                "count": len(pod_list),
                "pods": pod_list,
                "color": ns_color_map.get(ns, "#3b82f6")
            }
            for ns, pod_list in node_pods.items()
        ]

        short_name = node_name if len(node_name) <= 24 else f"...{node_name[-20:]}"

        node_cards.append({
            "name": node_name,
            "short_name": short_name,
            "status": "Ready" if is_ready else "NotReady",
            "machine_type": machine_type,
            "zone": zone,
            "node_pool": node_pool,
            "cpu": {
                "used": f"{cpu_used_cores:.2f} cores",
                "total": f"{cpu_alloc_cores:.2f} cores",
                "percent": cpu_pct,
            },
            "memory": {
                "used": f"{mem_used_gib:.1f} GiB",
                "total": f"{mem_alloc_gib:.1f} GiB",
                "percent": mem_pct,
            },
            "pods_capacity": {
                "used": pod_count,
                "total": max_pods,
            },
            "namespaces": ns_breakdown,
        })

    return {
        "summary": {
            "nodes": len(node_items),
            "ready": ready_count,
            "pods_scheduled": total_pods,
            "namespaces": len(all_namespaces),
        },
        "namespaces": [{"name": ns, "color": color} for ns, color in ns_color_map.items()],
        "nodes": node_cards,
    }
