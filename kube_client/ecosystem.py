"""Auto-discovery of well-known Kubernetes ecosystem CRDs.

Rather than hardcoding deep support for one CRD at a time (as the app
already does for Gateway API's Gateway/HTTPRoute), this maintains a
registry of commonly-installed platform tools and cheaply checks which are
actually present on a given cluster -- same "optional" list pattern already
used for Gateway API, so a CRD that isn't installed just reports as
unavailable rather than erroring.

This intentionally only detects *presence and count*, not deep per-resource
normalization/rendering (e.g. a full cert-manager Certificate detail view).
That's real follow-up work best done against a cluster that actually has
these installed, rather than guessed at here.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List

from ._fetch import cached_kubectl_list

# (plural.group resource name for `kubectl get`, display label, category)
KNOWN_CRDS = [
    ("certificates.cert-manager.io", "Certificate", "cert-manager"),
    ("clusterissuers.cert-manager.io", "ClusterIssuer", "cert-manager"),
    ("scaledobjects.keda.sh", "ScaledObject", "KEDA"),
    ("scaledjobs.keda.sh", "ScaledJob", "KEDA"),
    ("nodepools.karpenter.sh", "NodePool", "Karpenter"),
    ("ec2nodeclasses.karpenter.k8s.aws", "EC2NodeClass", "Karpenter"),
    ("backups.velero.io", "Backup", "Velero"),
    ("schedules.velero.io", "Schedule", "Velero"),
    ("externalsecrets.external-secrets.io", "ExternalSecret", "External Secrets"),
    ("secretstores.external-secrets.io", "SecretStore", "External Secrets"),
    ("clusterpolicies.kyverno.io", "ClusterPolicy", "Kyverno"),
    ("policies.kyverno.io", "Policy", "Kyverno"),
    ("sealedsecrets.bitnami.com", "SealedSecret", "Sealed Secrets"),
    ("clusters.postgresql.cnpg.io", "Cluster", "CloudNativePG"),
    ("services.serving.knative.dev", "Service", "Knative"),
    ("vulnerabilityreports.aquasecurity.github.io", "VulnerabilityReport", "Trivy"),
    ("applications.argoproj.io", "Application", "Argo CD"),
    ("kustomizations.kustomize.toolkit.fluxcd.io", "Kustomization", "Flux"),
    ("gitrepositories.source.toolkit.fluxcd.io", "GitRepository", "Flux"),
]

_ECOSYSTEM_CACHE_TTL_SECONDS = 300  # CRD installs change rarely; poll gently


async def get_ecosystem_overview(context: str, kubeconfig: Path) -> Dict[str, Any]:
    """Check every known CRD in KNOWN_CRDS and report which are installed,
    grouped by the tool/project that owns them."""

    async def check(resource: str, label: str, category: str) -> Dict[str, Any]:
        items, available = await cached_kubectl_list(
            context, kubeconfig, resource, None, _ECOSYSTEM_CACHE_TTL_SECONDS, optional=True
        )
        return {"resource": resource, "kind": label, "category": category, "installed": available, "count": len(items)}

    results = await asyncio.gather(*(check(r, l, c) for r, l, c in KNOWN_CRDS))

    by_category: Dict[str, List[Dict[str, Any]]] = {}
    for r in results:
        by_category.setdefault(r["category"], []).append(r)

    integrations = [
        {
            "category": category,
            "installed": any(r["installed"] for r in items),
            "resources": items,
            "total_count": sum(r["count"] for r in items),
        }
        for category, items in by_category.items()
    ]
    integrations.sort(key=lambda i: (not i["installed"], i["category"]))

    return {
        "integrations": integrations,
        "detected_count": sum(1 for i in integrations if i["installed"]),
        "total_checked": len(integrations),
    }
