"""Static Kubernetes NetworkPolicy reachability simulator (read-only L3/L4 evaluator).

Implements Kubernetes `networking.k8s.io/v1` NetworkPolicy evaluation semantics:
- Additive allow union across all policies whose `podSelector` matches a pod
- Default-allow when zero NetworkPolicies select the pod for that direction
- Default-deny when at least one policy selects the pod for Egress or Ingress
  and no rule permits the peer + port/protocol
- Proper AND semantics when `podSelector` and `namespaceSelector` appear in
  the same `from`/`to` peer object, vs. OR semantics across array elements
- IPv4/IPv6 `ipBlock` CIDR + `except` matching, numeric & named container
  ports, and `endPort` port ranges
"""
from __future__ import annotations

import asyncio
import ipaddress
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from config import get_config_store

from ._fetch import cached_kubectl_list


def matches_label_selector(
    labels: Optional[Dict[str, str]],
    selector: Optional[Dict[str, Any]],
) -> bool:
    """Evaluate a Kubernetes LabelSelector (`matchLabels` + `matchExpressions`)."""
    if selector is None:
        return False
    pod_labels = labels or {}

    match_labels = selector.get("matchLabels") or {}
    for k, expected in match_labels.items():
        if pod_labels.get(k) != str(expected):
            return False

    for expr in selector.get("matchExpressions") or []:
        key = expr.get("key")
        op = expr.get("operator")
        values = [str(v) for v in (expr.get("values") or [])]
        if not key or not op:
            return False
        if op == "In":
            if key not in pod_labels or pod_labels[key] not in values:
                return False
        elif op == "NotIn":
            if key in pod_labels and pod_labels[key] in values:
                return False
        elif op == "Exists":
            if key not in pod_labels:
                return False
        elif op == "DoesNotExist":
            if key in pod_labels:
                return False
        else:
            return False

    return True


def _ip_in_block(ip_str: Optional[str], ip_block: Dict[str, Any]) -> bool:
    if not ip_str:
        return False
    cidr = ip_block.get("cidr")
    if not cidr:
        return False
    try:
        addr = ipaddress.ip_address(str(ip_str).strip())
        net = ipaddress.ip_network(str(cidr).strip(), strict=False)
    except ValueError:
        return False

    if addr.version != net.version or addr not in net:
        return False

    for excluded in ip_block.get("except") or []:
        try:
            ex_net = ipaddress.ip_network(str(excluded).strip(), strict=False)
        except ValueError:
            continue
        if addr.version == ex_net.version and addr in ex_net:
            return False
    return True


def _policy_types(policy: Dict[str, Any]) -> List[str]:
    spec = policy.get("spec") or {}
    explicit = spec.get("policyTypes")
    if isinstance(explicit, list) and explicit:
        return [str(t) for t in explicit]
    types = ["Ingress"]
    if "egress" in spec and spec.get("egress") is not None:
        types.append("Egress")
    return types


def _resolve_pod_named_port(
    pod: Optional[Dict[str, Any]],
    port_name: str,
    protocol: str,
) -> Optional[int]:
    if not pod:
        return None
    target_proto = protocol.upper()
    for container in (pod.get("spec") or {}).get("containers") or []:
        for p in container.get("ports") or []:
            c_proto = str(p.get("protocol") or "TCP").upper()
            if p.get("name") == port_name and c_proto == target_proto:
                c_port = p.get("containerPort")
                if isinstance(c_port, int):
                    return c_port
    return None


def _rule_allows_port(
    rule_ports: Optional[List[Dict[str, Any]]],
    target_port: Optional[int],
    target_protocol: str,
    target_pod: Optional[Dict[str, Any]],
) -> bool:
    if not rule_ports:
        return True

    wanted_proto = target_protocol.upper()
    for rp in rule_ports:
        proto = str(rp.get("protocol") or "TCP").upper()
        if proto != wanted_proto:
            continue
        port_spec = rp.get("port")
        if port_spec is None:
            return True
        if target_port is None:
            # Protocol matches when caller did not constrain port number.
            return True

        resolved_port: Optional[int]
        if isinstance(port_spec, int):
            resolved_port = port_spec
        else:
            s_val = str(port_spec).strip()
            if s_val.isdigit():
                resolved_port = int(s_val)
            else:
                resolved_port = _resolve_pod_named_port(target_pod, s_val, wanted_proto)

        if resolved_port is None:
            continue

        end_port = rp.get("endPort")
        if isinstance(end_port, int) and end_port >= resolved_port:
            if resolved_port <= target_port <= end_port:
                return True
        elif target_port == resolved_port:
            return True

    return False


def _peer_matches(
    peer: Dict[str, Any],
    policy_ns: str,
    candidate_ns: str,
    candidate_pod: Dict[str, Any],
    namespaces_labels: Dict[str, Dict[str, str]],
) -> bool:
    if not peer:
        return False

    if "ipBlock" in peer and peer["ipBlock"] is not None:
        pod_ip = (candidate_pod.get("status") or {}).get("podIP")
        return _ip_in_block(pod_ip, peer["ipBlock"])

    has_pod_sel = "podSelector" in peer and peer["podSelector"] is not None
    has_ns_sel = "namespaceSelector" in peer and peer["namespaceSelector"] is not None
    if not has_pod_sel and not has_ns_sel:
        return False

    if has_ns_sel:
        ns_labels = namespaces_labels.get(candidate_ns, {})
        if not matches_label_selector(ns_labels, peer["namespaceSelector"]):
            return False
    elif candidate_ns != policy_ns:
        # Kubernetes default: podSelector without namespaceSelector scopes strictly to policy namespace.
        return False

    if has_pod_sel:
        pod_labels = (candidate_pod.get("metadata") or {}).get("labels") or {}
        if not matches_label_selector(pod_labels, peer["podSelector"]):
            return False

    return True


def _rule_matches_peer_list(
    peers: Optional[List[Dict[str, Any]]],
    policy_ns: str,
    candidate_ns: str,
    candidate_pod: Dict[str, Any],
    namespaces_labels: Dict[str, Dict[str, str]],
) -> bool:
    # Omitted peer key (`None`) allows all sources/destinations; explicit `[]` allows none.
    if peers is None:
        return True
    if not peers:
        return False
    return any(
        _peer_matches(peer, policy_ns, candidate_ns, candidate_pod, namespaces_labels)
        for peer in peers
    )


def _evaluate_direction(
    *,
    direction: str,
    selected_pod: Dict[str, Any],
    selected_ns: str,
    peer_pod: Dict[str, Any],
    peer_ns: str,
    target_port: Optional[int],
    target_protocol: str,
    port_lookup_pod: Dict[str, Any],
    policies: List[Dict[str, Any]],
    namespaces_labels: Dict[str, Dict[str, str]],
) -> Tuple[bool, str, List[str], List[str]]:
    rules_key = "egress" if direction == "Egress" else "ingress"
    peer_key = "to" if direction == "Egress" else "from"
    selected_labels = (selected_pod.get("metadata") or {}).get("labels") or {}

    selecting: List[str] = []
    allowing: List[str] = []

    for pol in policies:
        meta = pol.get("metadata") or {}
        pol_ns = meta.get("namespace") or selected_ns
        if pol_ns != selected_ns:
            continue
        if direction not in _policy_types(pol):
            continue
        spec = pol.get("spec") or {}
        pod_selector = spec.get("podSelector")
        if pod_selector is None:
            pod_selector = {}
        if not matches_label_selector(selected_labels, pod_selector):
            continue

        pol_name = meta.get("name") or "unknown"
        selecting.append(pol_name)

        rules = spec.get(rules_key) or []
        for rule in rules:
            if not _rule_allows_port(rule.get("ports"), target_port, target_protocol, port_lookup_pod):
                continue
            if _rule_matches_peer_list(rule.get(peer_key), pol_ns, peer_ns, peer_pod, namespaces_labels):
                allowing.append(pol_name)
                break

    if not selecting:
        return True, "default-allow", [], []
    return bool(allowing), "policy-evaluated", selecting, allowing


def evaluate_network_policy_reachability(
    *,
    source_pod: Dict[str, Any],
    dest_pod: Dict[str, Any],
    source_netpols: List[Dict[str, Any]],
    dest_netpols: List[Dict[str, Any]],
    namespaces_labels: Optional[Dict[str, Dict[str, str]]] = None,
    port: Optional[int] = None,
    protocol: str = "TCP",
    dest_service_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Evaluate L3/L4 reachability from `source_pod` to `dest_pod` against NetworkPolicies."""
    src_meta = source_pod.get("metadata") or {}
    dst_meta = dest_pod.get("metadata") or {}
    src_ns = src_meta.get("namespace") or "default"
    dst_ns = dst_meta.get("namespace") or "default"
    ns_labels = namespaces_labels or {}
    norm_proto = str(protocol or "TCP").upper()

    egress_ok, egress_mode, sel_egress, allow_egress = _evaluate_direction(
        direction="Egress",
        selected_pod=source_pod,
        selected_ns=src_ns,
        peer_pod=dest_pod,
        peer_ns=dst_ns,
        target_port=port,
        target_protocol=norm_proto,
        port_lookup_pod=dest_pod,
        policies=source_netpols,
        namespaces_labels=ns_labels,
    )

    ingress_ok, ingress_mode, sel_ingress, allow_ingress = _evaluate_direction(
        direction="Ingress",
        selected_pod=dest_pod,
        selected_ns=dst_ns,
        peer_pod=source_pod,
        peer_ns=src_ns,
        target_port=port,
        target_protocol=norm_proto,
        port_lookup_pod=dest_pod,
        policies=dest_netpols,
        namespaces_labels=ns_labels,
    )

    allowed = egress_ok and ingress_ok
    if allowed:
        verdict = "ALLOWED"
    elif not egress_ok and not ingress_ok:
        verdict = "BLOCKED_BOTH"
    elif not egress_ok:
        verdict = "BLOCKED_EGRESS"
    else:
        verdict = "BLOCKED_INGRESS"

    reasons: List[str] = []
    if egress_ok:
        if egress_mode == "default-allow":
            reasons.append(
                f"Egress from '{src_ns}/{src_meta.get('name')}' is default-allow (no Egress NetworkPolicy selects the source pod)."
            )
        else:
            reasons.append(
                f"Egress allowed by NetworkPolicy: {', '.join(allow_egress)} in namespace '{src_ns}'."
            )
    else:
        reasons.append(
            f"Blocked at Egress in '{src_ns}': policy {', '.join(sel_egress)} selects pod "
            f"'{src_meta.get('name')}', but no egress rule allows '{dst_ns}/{dst_meta.get('name')}' on "
            f"{norm_proto}/{port if port is not None else '*'}."
        )

    if ingress_ok:
        if ingress_mode == "default-allow":
            reasons.append(
                f"Ingress to '{dst_ns}/{dst_meta.get('name')}' is default-allow (no Ingress NetworkPolicy selects the destination pod)."
            )
        else:
            reasons.append(
                f"Ingress allowed by NetworkPolicy: {', '.join(allow_ingress)} in namespace '{dst_ns}'."
            )
    else:
        reasons.append(
            f"Blocked at Ingress in '{dst_ns}': policy {', '.join(sel_ingress)} selects pod "
            f"'{dst_meta.get('name')}', but no ingress rule allows '{src_ns}/{src_meta.get('name')}' on "
            f"{norm_proto}/{port if port is not None else '*'}."
        )

    fix_hint = ""
    if not allowed:
        port_clause = f"\n      ports:\n        - protocol: {norm_proto}\n          port: {port}" if port is not None else ""
        if not ingress_ok:
            fix_hint = (
                f"Add an Ingress rule in namespace '{dst_ns}' (e.g., inside '{sel_ingress[0]}') "
                f"permitting namespace '{src_ns}' / pod labels {(src_meta.get('labels') or {})}:{port_clause}"
            )
        else:
            fix_hint = (
                f"Add an Egress rule in namespace '{src_ns}' (e.g., inside '{sel_egress[0]}') "
                f"permitting namespace '{dst_ns}' / pod labels {(dst_meta.get('labels') or {})}:{port_clause}"
            )
    elif port is None and (sel_egress or sel_ingress):
        reasons.append("Note: target port omitted — specify a TCP/UDP port for strict L4 port-level evaluation.")

    return {
        "allowed": allowed,
        "verdict": verdict,
        "egress_allowed": egress_ok,
        "ingress_allowed": ingress_ok,
        "egress_mode": egress_mode,
        "ingress_mode": ingress_mode,
        "evaluated_port": port,
        "evaluated_protocol": norm_proto,
        "source": {
            "namespace": src_ns,
            "pod": src_meta.get("name") or "",
            "pod_ip": (source_pod.get("status") or {}).get("podIP"),
            "labels": src_meta.get("labels") or {},
        },
        "destination": {
            "namespace": dst_ns,
            "pod": dst_meta.get("name") or "",
            "service": dest_service_name,
            "pod_ip": (dest_pod.get("status") or {}).get("podIP"),
            "labels": dst_meta.get("labels") or {},
        },
        "selecting_egress_policies": sel_egress,
        "selecting_ingress_policies": sel_ingress,
        "allowing_egress_policies": allow_egress,
        "allowing_ingress_policies": allow_ingress,
        "blocking_reasons": reasons,
        "suggested_fix_hint": fix_hint,
    }


def _pick_service_backend(
    service_obj: Dict[str, Any],
    pods: List[Dict[str, Any]],
    requested_port: Optional[int],
    requested_protocol: str,
) -> Tuple[Dict[str, Any], Optional[int]]:
    svc_meta = service_obj.get("metadata") or {}
    svc_spec = service_obj.get("spec") or {}
    selector = svc_spec.get("selector") or {}
    if not selector:
        raise ValueError(f"Service '{svc_meta.get('name')}' has no pod selector.")

    matching = [
        p for p in pods
        if matches_label_selector((p.get("metadata") or {}).get("labels") or {}, {"matchLabels": selector})
    ]
    if not matching:
        raise ValueError(
            f"Service '{svc_meta.get('name')}' in namespace '{svc_meta.get('namespace')}' matched 0 pods."
        )

    def _pod_rank(p: Dict[str, Any]) -> Tuple[int, str]:
        phase = (p.get("status") or {}).get("phase") or ""
        return (0 if phase == "Running" else 1, (p.get("metadata") or {}).get("name") or "")

    chosen = sorted(matching, key=_pod_rank)[0]
    effective_port = requested_port
    if effective_port is None:
        svc_ports = svc_spec.get("ports") or []
        if svc_ports:
            p0 = svc_ports[0]
            tp = p0.get("targetPort", p0.get("port"))
            if isinstance(tp, int):
                effective_port = tp
            elif isinstance(tp, str) and tp.isdigit():
                effective_port = int(tp)
            elif isinstance(tp, str):
                effective_port = _resolve_pod_named_port(
                    chosen,
                    tp,
                    str(p0.get("protocol") or requested_protocol or "TCP"),
                )
    return chosen, effective_port


async def simulate_network_policy(
    context: str,
    kubeconfig_path: Path,
    source_namespace: str,
    source_pod: str,
    dest_namespace: str,
    dest_pod: Optional[str] = None,
    dest_service: Optional[str] = None,
    port: Optional[int] = None,
    protocol: str = "TCP",
    ttl: Optional[int] = None,
) -> Dict[str, Any]:
    """Fetch live pods, namespaces, services, and NetworkPolicies and evaluate reachability."""
    if not dest_pod and not dest_service:
        raise ValueError("Provide either 'dest_pod' or 'dest_service' to simulate reachability.")

    settings = get_config_store().settings
    cache_ttl = ttl if ttl is not None else settings.kube_cache_ttl_seconds
    same_ns = source_namespace == dest_namespace

    tasks = [
        cached_kubectl_list(context, kubeconfig_path, "pods", source_namespace, cache_ttl),
        cached_kubectl_list(context, kubeconfig_path, "namespaces", None, settings.cluster_meta_cache_ttl_seconds),
        cached_kubectl_list(context, kubeconfig_path, "networkpolicies", source_namespace, cache_ttl, optional=True),
    ]
    if not same_ns:
        tasks.append(cached_kubectl_list(context, kubeconfig_path, "pods", dest_namespace, cache_ttl))
        tasks.append(
            cached_kubectl_list(context, kubeconfig_path, "networkpolicies", dest_namespace, cache_ttl, optional=True)
        )
    if dest_service:
        tasks.append(cached_kubectl_list(context, kubeconfig_path, "services", dest_namespace, cache_ttl))

    results = await asyncio.gather(*tasks)
    idx = 0
    src_pods = results[idx][0]; idx += 1
    ns_items = results[idx][0]; idx += 1
    src_netpols = results[idx][0]; idx += 1

    if not same_ns:
        dst_pods = results[idx][0]; idx += 1
        dst_netpols = results[idx][0]; idx += 1
    else:
        dst_pods = src_pods
        dst_netpols = src_netpols

    dst_services = results[idx][0] if dest_service else []

    src_pod_obj = next((p for p in src_pods if (p.get("metadata") or {}).get("name") == source_pod), None)
    if src_pod_obj is None:
        raise ValueError(f"Source pod '{source_namespace}/{source_pod}' not found.")

    resolved_port = port
    if dest_pod:
        dst_pod_obj = next((p for p in dst_pods if (p.get("metadata") or {}).get("name") == dest_pod), None)
        if dst_pod_obj is None:
            raise ValueError(f"Destination pod '{dest_namespace}/{dest_pod}' not found.")
    else:
        svc_obj = next((s for s in dst_services if (s.get("metadata") or {}).get("name") == dest_service), None)
        if svc_obj is None:
            raise ValueError(f"Destination service '{dest_namespace}/{dest_service}' not found.")
        dst_pod_obj, resolved_port = _pick_service_backend(svc_obj, dst_pods, port, protocol)

    namespaces_labels: Dict[str, Dict[str, str]] = {}
    for ns in ns_items:
        meta = ns.get("metadata") or {}
        ns_name = meta.get("name")
        if ns_name:
            labels = dict(meta.get("labels") or {})
            labels.setdefault("kubernetes.io/metadata.name", ns_name)
            namespaces_labels[ns_name] = labels

    return evaluate_network_policy_reachability(
        source_pod=src_pod_obj,
        dest_pod=dst_pod_obj,
        source_netpols=src_netpols,
        dest_netpols=dst_netpols,
        namespaces_labels=namespaces_labels,
        port=resolved_port,
        protocol=protocol,
        dest_service_name=dest_service,
    )


def find_netpol_isolated_services(
    raw_pods: Iterable[Dict[str, Any]],
    raw_services: Iterable[Dict[str, Any]],
    netpols_by_ns: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Detect Services whose backend pods are selected by Ingress NetworkPolicies with zero allowing rules."""
    isolated: List[Dict[str, Any]] = []
    pods_by_ns: Dict[str, List[Dict[str, Any]]] = {}
    for pod in raw_pods:
        ns = (pod.get("metadata") or {}).get("namespace") or "default"
        pods_by_ns.setdefault(ns, []).append(pod)

    for svc in raw_services:
        meta = svc.get("metadata") or {}
        ns = meta.get("namespace") or svc.get("namespace") or "default"
        svc_name = meta.get("name") or svc.get("name") or ""
        netpols = netpols_by_ns.get(ns) or []
        if not netpols:
            continue
        selector = (svc.get("spec") or {}).get("selector") or svc.get("selector") or {}
        if not selector:
            continue

        backends = [
            p for p in pods_by_ns.get(ns, [])
            if matches_label_selector((p.get("metadata") or {}).get("labels") or {}, {"matchLabels": selector})
        ]
        if not backends:
            continue

        sample_pod = backends[0]
        pod_labels = (sample_pod.get("metadata") or {}).get("labels") or {}
        selecting_policies: List[str] = []
        has_any_ingress_allow = False

        for pol in netpols:
            if "Ingress" not in _policy_types(pol):
                continue
            spec = pol.get("spec") or {}
            pod_sel = spec.get("podSelector")
            if pod_sel is None:
                pod_sel = {}
            if not matches_label_selector(pod_labels, pod_sel):
                continue

            pol_name = (pol.get("metadata") or {}).get("name") or "unknown"
            selecting_policies.append(pol_name)
            for rule in spec.get("ingress") or []:
                peers = rule.get("from")
                if peers is None or len(peers) > 0:
                    has_any_ingress_allow = True
                    break

        if selecting_policies and not has_any_ingress_allow:
            isolated.append({
                "namespace": ns,
                "service": svc_name,
                "selecting_policies": selecting_policies,
                "backend_pods": len(backends),
            })

    return isolated
