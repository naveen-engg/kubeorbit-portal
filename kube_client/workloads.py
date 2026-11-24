"""Workload catalog, rollout history/rollback, and logical application grouping."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Optional

from config import get_config_store

from ._cli import run_cli
from .logs import get_pod_logs
from .resources import (
    get_cronjobs,
    get_daemonsets,
    get_deployments,
    get_jobs,
    get_pods,
    get_services,
    get_statefulsets,
)


async def get_workloads_catalog(
    context: str, kubeconfig: Path, namespace: Optional[str] = None
) -> Dict[str, Any]:
    """Return complete catalog of Pods, Deployments, StatefulSets, DaemonSets, Jobs, and CronJobs."""
    ns = namespace if namespace else ""
    deps, ssets, dsets, jobs, cjobs, pods = await asyncio.gather(
        get_deployments(context, kubeconfig, ns),
        get_statefulsets(context, kubeconfig, ns),
        get_daemonsets(context, kubeconfig, ns),
        get_jobs(context, kubeconfig, ns),
        get_cronjobs(context, kubeconfig, ns),
        get_pods(context, kubeconfig, ns),
    )

    # Compute workload health rollup
    total_workloads = len(deps) + len(ssets) + len(dsets) + len(jobs) + len(cjobs)
    unhealthy_count = 0

    for d in deps:
        if d.get("ready_replicas", 0) < d.get("replicas", 0):
            d["healthy"] = False
            unhealthy_count += 1
        else:
            d["healthy"] = True

    for s in ssets:
        if s.get("ready_replicas", 0) < s.get("replicas", 0):
            s["healthy"] = False
            unhealthy_count += 1
        else:
            s["healthy"] = True

    for ds in dsets:
        if ds.get("number_ready", 0) < ds.get("desired_number_scheduled", 0):
            ds["healthy"] = False
            unhealthy_count += 1
        else:
            ds["healthy"] = True

    return {
        "namespace": namespace,
        "summary": {
            "total_workloads": total_workloads,
            "healthy_count": total_workloads - unhealthy_count,
            "unhealthy_count": unhealthy_count,
            "deployments_count": len(deps),
            "statefulsets_count": len(ssets),
            "daemonsets_count": len(dsets),
            "jobs_count": len(jobs),
            "cronjobs_count": len(cjobs),
            "pods_count": len(pods),
        },
        "deployments": deps,
        "statefulsets": ssets,
        "daemonsets": dsets,
        "jobs": jobs,
        "cronjobs": cjobs,
        "pods": pods,
    }


async def get_rollout_history(
    context: str, kubeconfig: Path, namespace: str, workload_type: str, name: str
) -> Dict[str, Any]:
    """Fetch revision history of a Deployment, StatefulSet, or DaemonSet."""
    cmd = ["kubectl", "rollout", "history", f"{workload_type}/{name}", "-n", namespace]
    rc, stdout, stderr = await run_cli(cmd, env={"KUBECONFIG": str(kubeconfig)}, check=False)

    revisions = []
    if rc == 0 and stdout:
        lines = stdout.strip().split("\n")
        # Header is usually REVISION  CHANGE-CAUSE
        for line in lines[1:]:
            parts = line.strip().split(None, 1)
            if parts and parts[0].isdigit():
                rev_num = int(parts[0])
                cause = parts[1] if len(parts) > 1 else "<none>"
                revisions.append({
                    "revision": rev_num,
                    "change_cause": cause,
                })

    # Try fetching detailed image info for each revision
    for r in revisions:
        rev_cmd = ["kubectl", "rollout", "history", f"{workload_type}/{name}", "-n", namespace, f"--revision={r['revision']}"]
        rev_rc, rev_out, _ = await run_cli(rev_cmd, env={"KUBECONFIG": str(kubeconfig)}, check=False)
        if rev_rc == 0 and rev_out:
            for l in rev_out.split("\n"):
                if "Image:" in l or "image:" in l:
                    r["image"] = l.split(":", 1)[-1].strip()

    return {
        "workload_type": workload_type,
        "name": name,
        "namespace": namespace,
        "revisions": sorted(revisions, key=lambda x: x["revision"], reverse=True),
        "current_revision": revisions[0]["revision"] if revisions else 1,
    }


async def rollout_undo(
    context: str, kubeconfig: Path, namespace: str, workload_type: str, name: str, to_revision: Optional[int] = None
) -> Dict[str, Any]:
    """Roll back a Deployment, StatefulSet, or DaemonSet."""
    cmd = ["kubectl", "rollout", "undo", f"{workload_type}/{name}", "-n", namespace]
    if to_revision:
        cmd.append(f"--to-revision={to_revision}")

    rc, stdout, stderr = await run_cli(cmd, env={"KUBECONFIG": str(kubeconfig)}, check=True)
    return {
        "status": "ok",
        "message": stdout.strip() or f"Rolled back {workload_type}/{name}",
        "workload_type": workload_type,
        "name": name,
        "namespace": namespace,
        "to_revision": to_revision,
    }


async def get_logical_applications(
    context: str, kubeconfig: Path, namespace: Optional[str] = None
) -> Dict[str, Any]:
    """Group workloads, services, and ingresses into logical high-level Applications."""
    ns = namespace if namespace else ""
    deps, ssets, dsets, svcs, pods = await asyncio.gather(
        get_deployments(context, kubeconfig, ns),
        get_statefulsets(context, kubeconfig, ns),
        get_daemonsets(context, kubeconfig, ns),
        get_services(context, kubeconfig, ns),
        get_pods(context, kubeconfig, ns, include_metrics=False),
    )

    apps_map: Dict[str, Dict[str, Any]] = {}
    label_keys = get_config_store().settings.app_correlation_label_keys

    def _get_app_name(item: Dict[str, Any]) -> str:
        # Try each configured correlation label in order (see
        # AppSettings.app_correlation_label_keys -- different orgs label
        # differently), falling back to the name's first hyphen-delimited
        # segment when none are present.
        name = item.get("name", "")
        for k in label_keys:
            val = (item.get("labels") or {}).get(k)
            if val:
                return val
        if "-" in name:
            return name.split("-")[0]
        return name

    # Group Deployments
    for d in deps:
        app_name = _get_app_name(d)
        app_entry = apps_map.setdefault(app_name, {
            "name": app_name,
            "namespace": d.get("namespace", "default"),
            "deployments": [],
            "statefulsets": [],
            "daemonsets": [],
            "services": [],
            "pods": [],
            "healthy": True,
            "total_replicas": 0,
            "ready_replicas": 0,
        })
        app_entry["deployments"].append(d)
        app_entry["total_replicas"] += d.get("replicas", 0)
        app_entry["ready_replicas"] += d.get("ready_replicas", 0)
        if d.get("ready_replicas", 0) < d.get("replicas", 0):
            app_entry["healthy"] = False

    # Group StatefulSets
    for s in ssets:
        app_name = _get_app_name(s)
        app_entry = apps_map.setdefault(app_name, {
            "name": app_name,
            "namespace": s.get("namespace", "default"),
            "deployments": [],
            "statefulsets": [],
            "daemonsets": [],
            "services": [],
            "pods": [],
            "healthy": True,
            "total_replicas": 0,
            "ready_replicas": 0,
        })
        app_entry["statefulsets"].append(s)
        app_entry["total_replicas"] += s.get("replicas", 0)
        app_entry["ready_replicas"] += s.get("ready_replicas", 0)
        if s.get("ready_replicas", 0) < s.get("replicas", 0):
            app_entry["healthy"] = False

    # Group DaemonSets
    for ds in dsets:
        app_name = _get_app_name(ds)
        app_entry = apps_map.setdefault(app_name, {
            "name": app_name,
            "namespace": ds.get("namespace", "default"),
            "deployments": [],
            "statefulsets": [],
            "daemonsets": [],
            "services": [],
            "pods": [],
            "healthy": True,
            "total_replicas": 0,
            "ready_replicas": 0,
        })
        app_entry["daemonsets"].append(ds)

    # Attach Services
    for svc in svcs:
        app_name = _get_app_name(svc)
        if app_name in apps_map:
            apps_map[app_name]["services"].append(svc)

    # Attach Pods
    for p in pods:
        app_name = _get_app_name(p)
        if app_name in apps_map:
            apps_map[app_name]["pods"].append(p)

    apps_list = sorted(apps_map.values(), key=lambda a: a["name"])
    return {
        "namespace": namespace,
        "items": apps_list,
        "count": len(apps_list),
        "healthy_count": sum(1 for a in apps_list if a["healthy"]),
    }


async def get_pod_inspector(
    context: str, kubeconfig: Path, namespace: str, pod_name: str
) -> Dict[str, Any]:
    """Return in-depth details of a pod including probes, container resources,
    restart reasons, 12-Step pipeline lifecycle status, and logs."""
    from .resources import get_events

    (_, stdout, _), events, services = await asyncio.gather(
        run_cli(
            ["kubectl", "--kubeconfig", str(kubeconfig), "--context", context, "get", "pod", pod_name, "-n", namespace, "-o", "json"],
            env={"KUBECONFIG": str(kubeconfig)},
            check=True,
        ),
        get_events(context, kubeconfig, namespace, limit=100),
        get_services(context, kubeconfig, namespace),
    )
    pod_data = json.loads(stdout)
    meta = pod_data.get("metadata", {}) or {}
    spec = pod_data.get("spec", {}) or {}
    status = pod_data.get("status", {}) or {}
    pod_labels = meta.get("labels") or {}
    conds_by_type = {c.get("type"): c for c in (status.get("conditions") or []) if c.get("type")}
    pod_events = [e for e in events if e.get("involved_name") == pod_name or pod_name in (e.get("message") or "")]
    event_reasons = {e.get("reason", "") for e in pod_events}

    containers_info = []
    statuses_by_name = {cs["name"]: cs for cs in status.get("containerStatuses", [])}

    for c in spec.get("containers", []):
        c_name = c["name"]
        c_status = statuses_by_name.get(c_name, {})

        # Analyze probes
        probes = {}
        for probe_type in ("livenessProbe", "readinessProbe", "startupProbe"):
            p_spec = c.get(probe_type)
            if p_spec:
                p_desc = ""
                if "httpGet" in p_spec:
                    p_desc = f"HTTP GET {p_spec['httpGet'].get('path', '/')} on port {p_spec['httpGet'].get('port')}"
                elif "tcpSocket" in p_spec:
                    p_desc = f"TCP Socket on port {p_spec['tcpSocket'].get('port')}"
                elif "exec" in p_spec:
                    p_desc = f"Exec: {' '.join(p_spec['exec'].get('command', []))}"
                probes[probe_type] = {
                    "configured": True,
                    "type": list(p_spec.keys())[0] if p_spec else "unknown",
                    "description": p_desc,
                    "initial_delay_seconds": p_spec.get("initialDelaySeconds", 0),
                    "period_seconds": p_spec.get("periodSeconds", 10),
                    "timeout_seconds": p_spec.get("timeoutSeconds", 1),
                    "failure_threshold": p_spec.get("failureThreshold", 3),
                }

        # Last state / termination reason
        last_state = c_status.get("lastState", {})
        term = last_state.get("terminated", {})
        waiting = c_status.get("state", {}).get("waiting", {})

        containers_info.append({
            "name": c_name,
            "image": c.get("image", ""),
            "ready": c_status.get("ready", False),
            "restart_count": c_status.get("restartCount", 0),
            "state": list(c_status.get("state", {}).keys())[0] if c_status.get("state") else "unknown",
            "waiting_reason": waiting.get("reason"),
            "waiting_message": waiting.get("message"),
            "last_termination_reason": term.get("reason"),
            "last_exit_code": term.get("exitCode"),
            "resources": c.get("resources", {}),
            "probes": probes,
            "ports": c.get("ports", []),
            "volume_mounts": [vm["name"] for vm in c.get("volumeMounts", [])],
        })

    # Compute 12-Step Lifecycle Checklist for this Pod
    owner_refs = meta.get("ownerReferences") or []
    owner_str = f"{owner_refs[0].get('kind')}/{owner_refs[0].get('name')}" if owner_refs else "Standalone Pod"
    node_name = spec.get("nodeName")
    pod_scheduled = (conds_by_type.get("PodScheduled") or {}).get("status") == "True" or bool(node_name)
    sched_msg = (conds_by_type.get("PodScheduled") or {}).get("message") or f"Bound to node {node_name}" if node_name else "Unscheduled (Pending)"

    has_mount_err = "FailedMount" in event_reasons or "FailedAttachVolume" in event_reasons
    has_cni_err = "FailedCreatePodSandBox" in event_reasons
    pod_ip = status.get("podIP")
    csi_cni_ok = pod_scheduled and not has_mount_err and not has_cni_err and bool(pod_ip)
    csi_cni_detail = (
        next((e["message"] for e in pod_events if e.get("reason") in ("FailedMount", "FailedAttachVolume", "FailedCreatePodSandBox")), "")
        if (has_mount_err or has_cni_err)
        else (f"Pod IP {pod_ip} allocated & volumes mounted" if pod_ip else "Waiting for CSI volume mount / CNI sandbox")
    )

    waiting_reasons = [c.get("waiting_reason") for c in containers_info if c.get("waiting_reason")]
    runtime_ok = csi_cni_ok and not waiting_reasons and all(c.get("state") == "running" for c in containers_info)
    runtime_detail = (
        f"Stalled: {', '.join(waiting_reasons)}"
        if waiting_reasons
        else ("All containers running in containerd" if runtime_ok else "Waiting for container startup")
    )

    ready_cond = (conds_by_type.get("Ready") or {}).get("status") == "True"
    unhealthy_ev = next((e["message"] for e in pod_events if e.get("reason") == "Unhealthy"), None)
    probe_detail = (
        "Readiness & startup probes passing (Ready=True)"
        if ready_cond
        else (unhealthy_ev or "Pod Ready=False (readiness probe failing or pending)")
    )

    matching_svcs = [
        s["name"]
        for s in services
        if s.get("selector") and all(pod_labels.get(k) == v for k, v in s["selector"].items())
    ]
    step12_ok = ready_cond and len(matching_svcs) > 0
    step12_detail = (
        f"Active in EndpointSlice for Service(s): {', '.join(matching_svcs)}"
        if step12_ok
        else (
            f"Excluded from Service({', '.join(matching_svcs)}) EndpointSlice because Ready=False"
            if matching_svcs
            else "No Service selector matches this Pod's labels"
        )
    )

    pipeline_steps = [
        {
            "step": "Steps 1–8",
            "title": "API Server, etcd & Controller",
            "status": "passed",
            "detail": f"Admitted, stored in etcd & created by {owner_str}",
        },
        {
            "step": "Step 9",
            "title": "kube-scheduler (Filter / Score / Bind)",
            "status": "passed" if pod_scheduled else "failed",
            "detail": sched_msg,
        },
        {
            "step": "Step 10a/b",
            "title": "kubelet CSI Mount & CNI Network",
            "status": "passed" if csi_cni_ok else ("failed" if (has_mount_err or has_cni_err) else "pending"),
            "detail": csi_cni_detail,
        },
        {
            "step": "Step 10c/d",
            "title": "kubelet Image Pull & containerd Runtime",
            "status": "passed" if runtime_ok else ("failed" if waiting_reasons else "pending"),
            "detail": runtime_detail,
        },
        {
            "step": "Step 11",
            "title": "Readiness & Startup Probes",
            "status": "passed" if ready_cond else ("failed" if (runtime_ok or unhealthy_ev) else "pending"),
            "detail": probe_detail,
        },
        {
            "step": "Step 12",
            "title": "EndpointSlice & Service Traffic",
            "status": "passed" if step12_ok else ("warning" if not matching_svcs else "failed"),
            "detail": step12_detail,
        },
    ]

    # Fetch recent logs for primary container
    primary_container = containers_info[0]["name"] if containers_info else None
    logs = ""
    if primary_container:
        try:
            logs = await get_pod_logs(context, kubeconfig, namespace, pod_name, container=primary_container, tail_lines=150)
        except Exception as exc:
            logs = f"Could not retrieve logs: {exc}"

    return {
        "name": pod_name,
        "namespace": namespace,
        "phase": status.get("phase", "Unknown"),
        "node_name": node_name,
        "pod_ip": pod_ip,
        "host_ip": status.get("hostIP"),
        "start_time": status.get("startTime"),
        "containers": containers_info,
        "conditions": status.get("conditions", []),
        "pipeline_steps": pipeline_steps,
        "matching_services": matching_svcs,
        "recent_logs": logs,
        "labels": pod_labels,
    }
