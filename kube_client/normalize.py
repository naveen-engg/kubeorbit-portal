"""Shape normalizers -- raw kubectl/SDK JSON -> unified, frontend-friendly dicts.

Every provider (GKE/EKS/AKS/local) produces identical raw Kubernetes API
objects once a kubeconfig context exists, so these normalizers are the one
place resource shapes are defined for the whole app/frontend.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def _age_iso(obj: Dict[str, Any]) -> Optional[str]:
    return obj.get("metadata", {}).get("creationTimestamp")


NODE_POOL_LABELS = (
    "cloud.google.com/gke-nodepool",
    "eks.amazonaws.com/nodegroup",
    "kubernetes.azure.com/agentpool",
)
ZONE_LABELS = ("topology.kubernetes.io/zone", "failure-domain.beta.kubernetes.io/zone")
INSTANCE_TYPE_LABELS = ("node.kubernetes.io/instance-type", "beta.kubernetes.io/instance-type")


def _normalize_node(n: Dict[str, Any]) -> Dict[str, Any]:
    labels = n.get("metadata", {}).get("labels", {}) or {}
    status = n.get("status", {})
    conditions = {c["type"]: c["status"] for c in status.get("conditions", [])}
    node_pool = next((labels[k] for k in NODE_POOL_LABELS if k in labels), None)
    zone = next((labels[k] for k in ZONE_LABELS if k in labels), None)
    instance_type = next((labels[k] for k in INSTANCE_TYPE_LABELS if k in labels), None)
    roles = [
        k.split("/", 1)[1] for k in labels if k.startswith("node-role.kubernetes.io/")
    ] or ["worker"]
    info = status.get("nodeInfo", {})
    addresses = {a.get("type"): a.get("address") for a in status.get("addresses", []) or []}
    taints = n.get("spec", {}).get("taints", []) or []
    return {
        "name": n.get("metadata", {}).get("name"),
        "status": "Ready" if conditions.get("Ready") == "True" else "NotReady",
        "roles": roles,
        "kubernetes_version": info.get("kubeletVersion"),
        "os_image": info.get("osImage"),
        "container_runtime": info.get("containerRuntimeVersion"),
        "kernel_version": info.get("kernelVersion"),
        "architecture": info.get("architecture"),
        "zone": zone,
        "node_pool": node_pool,
        "instance_type": instance_type,
        "internal_ip": addresses.get("InternalIP"),
        "external_ip": addresses.get("ExternalIP"),
        "cpu_capacity": status.get("capacity", {}).get("cpu"),
        "memory_capacity": status.get("capacity", {}).get("memory"),
        "pods_capacity": status.get("capacity", {}).get("pods", "110"),
        "conditions": {
            "MemoryPressure": conditions.get("MemoryPressure") == "True",
            "DiskPressure": conditions.get("DiskPressure") == "True",
            "PIDPressure": conditions.get("PIDPressure") == "True",
            "NetworkUnavailable": conditions.get("NetworkUnavailable") == "True",
        },
        "taints": [f"{t.get('key')}={t.get('value', '')}:{t.get('effect')}" for t in taints],
        "created_at": _age_iso(n),
    }


def _pod_phase_and_reason(p: Dict[str, Any]) -> Tuple[str, Optional[str], int]:
    status = p.get("status", {})
    phase = status.get("phase", "Unknown")
    restarts = sum(cs.get("restartCount", 0) for cs in status.get("containerStatuses", []))
    reason = None
    for cs in status.get("containerStatuses", []):
        waiting = cs.get("state", {}).get("waiting")
        if waiting and waiting.get("reason") not in (None, "ContainerCreating"):
            reason = waiting["reason"]
            phase = "Unhealthy"
    return phase, reason, restarts


def _parse_cpu_m(val_str: str) -> int:
    if not val_str:
        return 0
    val_str = str(val_str).strip()
    if val_str.endswith("m"):
        try:
            return int(float(val_str[:-1]))
        except ValueError:
            return 0
    if val_str.endswith("n"):
        try:
            return max(1, int(float(val_str[:-1]) / 1_000_000.0))
        except ValueError:
            return 0
    try:
        return int(float(val_str) * 1000)
    except ValueError:
        return 0


def _parse_mem_mi(val_str: str) -> int:
    if not val_str:
        return 0
    val_str = str(val_str).strip()
    units = {
        "Ki": 1 / 1024.0,
        "Mi": 1.0,
        "Gi": 1024.0,
        "Ti": 1024.0 * 1024.0,
        "K": 1000.0 / (1024.0 * 1024.0),
        "M": 1_000_000.0 / (1024.0 * 1024.0),
        "G": 1_000_000_000.0 / (1024.0 * 1024.0),
    }
    for unit, mult in units.items():
        if val_str.endswith(unit):
            try:
                return int(round(float(val_str[:-len(unit)]) * mult))
            except ValueError:
                return 0
    try:
        return int(round(float(val_str) / (1024.0 * 1024.0)))
    except ValueError:
        return 0


def _format_cpu_short(m: int) -> str:
    if m <= 0:
        return "—"
    if m >= 1000 and m % 100 == 0:
        return f"{m / 1000:.2f}".rstrip("0").rstrip(".") if m % 1000 != 0 else f"{m / 1000:.2f}"
    return f"{m}m"


def _format_mem_short(mi: int) -> str:
    if mi <= 0:
        return "—"
    if mi >= 1024 and mi % 512 == 0:
        return f"{mi / 1024:.1f}Gi".replace(".0Gi", "Gi")
    return f"{mi}Mi"


def _normalize_pod(p: Dict[str, Any]) -> Dict[str, Any]:
    meta = p.get("metadata", {})
    spec = p.get("spec", {})
    status = p.get("status", {})
    phase, reason, restarts = _pod_phase_and_reason(p)
    containers = status.get("containerStatuses", []) or []
    spec_containers = spec.get("containers", []) or []
    ready_count = sum(1 for c in containers if c.get("ready"))

    req_cpu_m = 0
    req_mem_mi = 0
    lim_cpu_m = 0
    lim_mem_mi = 0
    for sc in spec_containers:
        res = sc.get("resources", {}) or {}
        reqs = res.get("requests", {}) or {}
        lims = res.get("limits", {}) or {}
        req_cpu_m += _parse_cpu_m(reqs.get("cpu", ""))
        req_mem_mi += _parse_mem_mi(reqs.get("memory", ""))
        lim_cpu_m += _parse_cpu_m(lims.get("cpu", ""))
        lim_mem_mi += _parse_mem_mi(lims.get("memory", ""))

    owners = meta.get("ownerReferences", []) or []
    owner_ref = f"{owners[0].get('kind')}/{owners[0].get('name')}" if owners else None

    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "status": phase,
        "reason": reason,
        "ready": f"{ready_count}/{len(containers) or len(spec_containers)}",
        "restarts": restarts,
        "node": spec.get("nodeName"),
        "pod_ip": status.get("podIP"),
        "qos_class": status.get("qosClass", "BestEffort"),
        "owner": owner_ref,
        "created_at": _age_iso(p),
        "resources": {
            "req_cpu_m": req_cpu_m,
            "req_mem_mi": req_mem_mi,
            "lim_cpu_m": lim_cpu_m,
            "lim_mem_mi": lim_mem_mi,
            "req_cpu_str": _format_cpu_short(req_cpu_m),
            "req_mem_str": _format_mem_short(req_mem_mi),
            "lim_cpu_str": _format_cpu_short(lim_cpu_m),
            "lim_mem_str": _format_mem_short(lim_mem_mi),
        },
        "containers": [
            {
                "name": c.get("name"),
                "image": c.get("image"),
                "ready": c.get("ready", False),
                "restart_count": c.get("restartCount", 0),
            }
            for c in containers
        ],
        "labels": meta.get("labels", {}) or {},
    }


def _normalize_workload(w: Dict[str, Any], kind: str) -> Dict[str, Any]:
    meta = w.get("metadata", {})
    spec = w.get("spec", {})
    status = w.get("status", {})
    if kind == "daemonset":
        desired = status.get("desiredNumberScheduled", 0)
        ready = status.get("numberReady", 0)
        available = status.get("numberAvailable", 0)
        updated = status.get("updatedNumberScheduled", 0)
    else:
        desired = spec.get("replicas", 0) or 0
        ready = status.get("readyReplicas", 0) or 0
        available = status.get("availableReplicas", 0) or 0
        updated = status.get("updatedReplicas", 0) or 0
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "desired": desired,
        "ready": ready,
        "available": available,
        "updated": updated,
        "healthy": ready >= desired,
        "created_at": _age_iso(w),
        "labels": meta.get("labels", {}) or {},
    }


def _normalize_job(j: Dict[str, Any]) -> Dict[str, Any]:
    meta = j.get("metadata", {})
    spec = j.get("spec", {})
    status = j.get("status", {})
    completions = spec.get("completions", 1)
    succeeded = status.get("succeeded", 0)
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "completions": completions,
        "succeeded": succeeded,
        "failed": status.get("failed", 0),
        "active": status.get("active", 0),
        "healthy": status.get("failed", 0) == 0,
        "created_at": _age_iso(j),
    }


def _normalize_cronjob(cj: Dict[str, Any]) -> Dict[str, Any]:
    meta = cj.get("metadata", {})
    spec = cj.get("spec", {})
    status = cj.get("status", {})
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "schedule": spec.get("schedule"),
        "suspend": spec.get("suspend", False),
        "last_schedule_time": status.get("lastScheduleTime"),
        "active_jobs": len(status.get("active", []) or []),
        "created_at": _age_iso(cj),
    }


def _normalize_service(s: Dict[str, Any]) -> Dict[str, Any]:
    meta = s.get("metadata", {})
    spec = s.get("spec", {})
    status = s.get("status", {})
    external_ips = [
        ing.get("ip") or ing.get("hostname")
        for ing in status.get("loadBalancer", {}).get("ingress", [])
    ]
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "type": spec.get("type", "ClusterIP"),
        "cluster_ip": spec.get("clusterIP"),
        "external_ips": [ip for ip in external_ips if ip],
        "ports": [
            {"port": p.get("port"), "target_port": p.get("targetPort"), "protocol": p.get("protocol")}
            for p in spec.get("ports", [])
        ],
        "selector": spec.get("selector", {}) or {},
        "created_at": _age_iso(s),
        "labels": meta.get("labels", {}) or {},
    }


def _normalize_configmap(cm: Dict[str, Any]) -> Dict[str, Any]:
    meta = cm.get("metadata", {})
    data = cm.get("data", {}) or {}
    preview = {}
    for k, v in data.items():
        v = v or ""
        preview[k] = v if len(v) <= 200 else v[:200] + "…(truncated)"
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "key_count": len(data) + len(cm.get("binaryData", {}) or {}),
        "data_preview": preview,
        "created_at": _age_iso(cm),
    }


def _normalize_secret_meta(s: Dict[str, Any]) -> Dict[str, Any]:
    meta = s.get("metadata", {})
    data = s.get("data", {}) or {}
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "type": s.get("type", "Opaque"),
        "key_count": len(data),
        "keys": sorted(data.keys()),
        "created_at": _age_iso(s),
    }


def _normalize_serviceaccount(sa: Dict[str, Any]) -> Dict[str, Any]:
    meta = sa.get("metadata", {})
    annotations = meta.get("annotations", {}) or {}
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "secrets": [s.get("name") for s in sa.get("secrets", []) or []],
        "workload_identity_annotations": {
            k: v
            for k, v in annotations.items()
            if any(
                marker in k
                for marker in (
                    "iam.gke.io",
                    "eks.amazonaws.com",
                    "azure.workload.identity",
                )
            )
        },
        "created_at": _age_iso(sa),
    }


def _normalize_gateway(g: Dict[str, Any]) -> Dict[str, Any]:
    meta = g.get("metadata", {})
    spec = g.get("spec", {})
    status = g.get("status", {})
    addresses = [a.get("value") for a in status.get("addresses", []) or []]
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "gateway_class": spec.get("gatewayClassName"),
        "addresses": [a for a in addresses if a],
        "listeners": [
            {"name": l.get("name"), "port": l.get("port"), "protocol": l.get("protocol")}
            for l in spec.get("listeners", []) or []
        ],
        "created_at": _age_iso(g),
    }


def _normalize_httproute(r: Dict[str, Any]) -> Dict[str, Any]:
    meta = r.get("metadata", {})
    spec = r.get("spec", {})
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "hostnames": spec.get("hostnames", []) or [],
        "parent_refs": [p.get("name") for p in spec.get("parentRefs", []) or []],
        "rules": [
            {
                "backend_refs": [b.get("name") for b in (rule.get("backendRefs") or [])],
                "match_count": len(rule.get("matches", []) or []),
            }
            for rule in spec.get("rules", []) or []
        ],
        "created_at": _age_iso(r),
    }


def _normalize_ingress(ing: Dict[str, Any]) -> Dict[str, Any]:
    meta = ing.get("metadata", {})
    spec = ing.get("spec", {})
    status = ing.get("status", {})
    rules = []
    for rule in spec.get("rules", []) or []:
        paths = []
        for p in (rule.get("http") or {}).get("paths", []) or []:
            backend = p.get("backend") or {}
            svc = backend.get("service") or {}
            paths.append(
                {
                    "path": p.get("path") or "/",
                    "backend_service": svc.get("name"),
                    "backend_port": (svc.get("port") or {}).get("number"),
                }
            )
        rules.append({"host": rule.get("host"), "paths": paths})
    addresses = [
        i.get("ip") or i.get("hostname") for i in (status.get("loadBalancer", {}) or {}).get("ingress", []) or []
    ]
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "ingress_class": spec.get("ingressClassName"),
        "rules": rules,
        "addresses": [a for a in addresses if a],
        "created_at": _age_iso(ing),
    }


def _normalize_pvc(p: Dict[str, Any]) -> Dict[str, Any]:
    meta = p.get("metadata", {})
    spec = p.get("spec", {})
    status = p.get("status", {})
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "status": status.get("phase"),
        "volume_name": spec.get("volumeName"),
        "capacity": status.get("capacity", {}).get("storage"),
        "access_modes": spec.get("accessModes", []) or [],
        "storage_class": spec.get("storageClassName"),
        "created_at": _age_iso(p),
    }


def _normalize_pv(p: Dict[str, Any]) -> Dict[str, Any]:
    meta = p.get("metadata", {})
    spec = p.get("spec", {})
    status = p.get("status", {})
    claim = spec.get("claimRef") or {}
    return {
        "name": meta.get("name"),
        "status": status.get("phase"),
        "capacity": spec.get("capacity", {}).get("storage"),
        "access_modes": spec.get("accessModes", []) or [],
        "storage_class": spec.get("storageClassName"),
        "reclaim_policy": spec.get("persistentVolumeReclaimPolicy"),
        "claim_namespace": claim.get("namespace"),
        "claim_name": claim.get("name"),
        "created_at": _age_iso(p),
    }


def _normalize_storageclass(sc: Dict[str, Any]) -> Dict[str, Any]:
    meta = sc.get("metadata", {})
    annotations = meta.get("annotations", {}) or {}
    return {
        "name": meta.get("name"),
        "provisioner": sc.get("provisioner"),
        "reclaim_policy": sc.get("reclaimPolicy"),
        "volume_binding_mode": sc.get("volumeBindingMode"),
        "is_default": annotations.get("storageclass.kubernetes.io/is-default-class") == "true",
        "created_at": _age_iso(sc),
    }


def _normalize_event(e: Dict[str, Any]) -> Dict[str, Any]:
    involved = e.get("involvedObject", {}) or {}
    return {
        "namespace": e.get("metadata", {}).get("namespace"),
        "type": e.get("type"),
        "reason": e.get("reason"),
        "message": e.get("message"),
        "involved_kind": involved.get("kind"),
        "involved_name": involved.get("name"),
        "count": e.get("count", 1),
        "last_timestamp": e.get("lastTimestamp") or e.get("eventTime"),
        "first_timestamp": e.get("firstTimestamp"),
    }
