"""
Provider package. Every sibling module (except base.py) is auto-imported so
it can self-register via @register_provider(...) -- this is what lets a
fourth cloud (OCI, DigitalOcean, ...) be added by dropping a new file here
with zero edits to app.py or the frontend.
"""
from __future__ import annotations

import importlib
import pkgutil

from providers.base import (  # noqa: F401
    BaseKubeProvider,
    ClusterAuthResult,
    ClusterMeta,
    NodePoolInfo,
    ProviderAuthError,
    ProviderConfigError,
    ProviderError,
    ProviderIdentity,
    available_providers,
    get_provider_class,
)

for _, module_name, _is_pkg in pkgutil.iter_modules(__path__):
    if module_name != "base":
        importlib.import_module(f"{__name__}.{module_name}")


def build_provider(cluster_config) -> BaseKubeProvider:
    """Construct the right provider adapter for a cluster's config.provider,
    bound to that cluster's isolated kubeconfig path."""
    import kube_client  # local import avoids a circular import at module load time

    provider_cls = get_provider_class(cluster_config.provider)
    kubeconfig_path = kube_client.kubeconfig_path_for_cluster(cluster_config.id)
    return provider_cls(cluster_config, kubeconfig_path)
