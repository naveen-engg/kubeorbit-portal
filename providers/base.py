"""
Abstract provider interface shared by every cloud backend (GKE, EKS, AKS, ...).

Design goal: app.py and the frontend never branch on `provider`. They call
methods on a `BaseKubeProvider` instance and render whatever comes back
through the common dataclasses defined here. A new cloud is added by:

  1. Dropping a new module in providers/ that subclasses BaseKubeProvider and
     decorates itself with @register_provider("name"). It is auto-discovered
     by providers/__init__.py -- no edits to app.py, kube_client.py, or the
     frontend are required.
  2. Documenting the config.* keys it expects in config/clusters.example.yaml.

Everything below the registry is intentionally provider-agnostic: kubectl
access itself (listing pods, services, etc.) is identical for every provider
once a kubeconfig context exists, so that logic lives once in kube_client.py.
Providers are only responsible for (a) authenticating / producing a kubeconfig
context and (b) describing cloud-level metadata kubectl cannot see (node
pools, Kubernetes control-plane version, console links, pod-identity wiring).
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Type


# --------------------------------------------------------------------------- #
# Shared result / metadata types (provider-agnostic, frontend-facing shapes)
# --------------------------------------------------------------------------- #

@dataclass
class ProviderIdentity:
    provider: str
    account: Optional[str] = None
    project_or_subscription: Optional[str] = None
    impersonated_as: Optional[str] = None


@dataclass
class ClusterAuthResult:
    context_name: str
    kubeconfig_path: str
    authenticated_at: float
    identity: ProviderIdentity
    from_cache: bool = False


@dataclass
class NodePoolInfo:
    name: str
    node_count: int
    machine_type: Optional[str] = None
    zones: List[str] = field(default_factory=list)
    autoscaling: bool = False
    min_nodes: Optional[int] = None
    max_nodes: Optional[int] = None


@dataclass
class ClusterMeta:
    provider: str
    cluster_id: str
    name: str
    kubernetes_version: Optional[str]
    location: Optional[str]
    status: Optional[str]
    node_pools: List[NodePoolInfo] = field(default_factory=list)
    console_url: Optional[str] = None
    logs_url: Optional[str] = None
    workload_identity: Dict[str, Any] = field(default_factory=dict)
    # Control-plane networking facts (API server endpoint, VPC/VNet, pod/
    # service CIDR, CNI, network policy, private-cluster flag) -- common keys
    # across every provider so the frontend's Control Plane panel never has
    # to branch on cloud. A provider leaves a key out (None) rather than
    # guessing when its API genuinely doesn't expose it.
    network: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)
    degraded: bool = False
    error: Optional[str] = None


@dataclass
class NodePoolCost:
    name: str
    machine_type: Optional[str]
    node_count: int
    hourly_price_usd: Optional[float] = None  # per node, on-demand list price
    monthly_estimate_usd: Optional[float] = None
    priced: bool = False
    note: Optional[str] = None  # why unpriced, or a caveat (e.g. "approximate")


_AVG_HOURS_PER_MONTH = 730.0

# Shown in the UI next to every cost figure -- this is not the customer's
# invoice. See providers/pricing.py's module docstring for what's excluded.
COST_DISCLAIMER = (
    "Estimated from public on-demand list pricing (compute only). Excludes "
    "committed-use/reserved/savings-plan discounts, spot/preemptible pricing, "
    "storage, network egress, load balancers, and control-plane fees. Not "
    "your actual invoice."
)


@dataclass
class CostEstimate:
    provider: str
    pools: List[NodePoolCost] = field(default_factory=list)
    monthly_total_usd: Optional[float] = None
    currency: str = "USD"
    disclaimer: str = COST_DISCLAIMER
    error: Optional[str] = None


class ProviderError(Exception):
    """Base class for provider-layer failures."""


class ProviderAuthError(ProviderError):
    """Raised when the underlying cloud CLI cannot authenticate / mint a kubeconfig."""


class ProviderConfigError(ProviderError):
    """Raised when a cluster's config.* block is missing a required key."""


# --------------------------------------------------------------------------- #
# Registry -- lets providers/__init__.py auto-discover implementations
# --------------------------------------------------------------------------- #

_REGISTRY: Dict[str, Type["BaseKubeProvider"]] = {}


def register_provider(name: str):
    def _decorator(cls: Type["BaseKubeProvider"]):
        cls.provider_name = name
        _REGISTRY[name] = cls
        return cls

    return _decorator


def get_provider_class(name: str) -> Type["BaseKubeProvider"]:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ProviderConfigError(
            f"unknown provider '{name}'. Registered providers: {sorted(_REGISTRY.keys())}"
        ) from None


def available_providers() -> List[str]:
    return sorted(_REGISTRY.keys())


# --------------------------------------------------------------------------- #
# Abstract base class
# --------------------------------------------------------------------------- #

class BaseKubeProvider(ABC):
    """One instance is created per (cluster_config) pair, on demand, per request."""

    provider_name: str = "base"

    def __init__(self, cluster_config, kubeconfig_path: str):
        self.cluster = cluster_config
        self.kubeconfig_path = kubeconfig_path

    # -- required config helper ------------------------------------------- #
    def require(self, key: str) -> Any:
        value = self.cluster.config.get(key)
        if value in (None, ""):
            raise ProviderConfigError(
                f"cluster '{self.cluster.id}' (provider={self.provider_name}) is missing "
                f"required config key '{key}'"
            )
        return value

    # -- must be implemented by every provider ------------------------------ #
    @abstractmethod
    async def authenticate(self) -> ClusterAuthResult:
        """Run the provider CLI to (re)generate an isolated kubeconfig for this
        cluster and return the resulting kubectl context name. Must be
        idempotent/cheap to call repeatedly; callers are expected to cache the
        result, but implementations should not assume they won't be re-invoked."""

    @abstractmethod
    async def get_cluster_metadata(self) -> ClusterMeta:
        """Cloud-level facts kubectl cannot provide: control-plane version,
        node pools/groups, zones/region, status."""

    @abstractmethod
    def get_console_url(self) -> str:
        """Deep link into the cloud provider's console for this cluster."""

    def get_logs_url(self) -> Optional[str]:
        """Deep link into this cluster's cloud-native log destination
        (Cloud Logging / CloudWatch / Log Analytics) -- None if the provider
        doesn't have one (local) or the cluster's config doesn't carry
        enough to build one (e.g. AKS with no Log Analytics workspace
        configured). Default: unsupported; overridden per provider."""
        return None

    @abstractmethod
    def get_workload_identity_info(self) -> Dict[str, Any]:
        """Static description of the pod-level identity mechanism (Workload
        Identity / IRSA / Azure Workload Identity) plus, where cheaply
        detectable, whether it looks enabled on this cluster."""

    async def get_cost_estimate(self, node_pools: List[NodePoolInfo]) -> CostEstimate:
        """Public on-demand list-price estimate for this cluster's node
        pools. Default: unsupported -- overridden per provider in
        gke.py/eks.py/aks.py. Never raises; callers get a CostEstimate with
        `error` set instead."""
        return CostEstimate(provider=self.provider_name, error="Cost estimation is not implemented for this provider.")

    def get_display_meta(self) -> Dict[str, str]:
        """Small badge-friendly descriptor consumed identically by the frontend
        regardless of provider."""
        return {
            "provider": self.provider_name,
            "label": {"gke": "GKE", "eks": "EKS", "aks": "AKS"}.get(self.provider_name, self.provider_name.upper()),
            "cloud": {"gke": "GCP", "eks": "AWS", "aks": "Azure"}.get(self.provider_name, "Unknown"),
        }


def now() -> float:
    return time.time()
