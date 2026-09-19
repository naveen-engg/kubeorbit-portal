"""
Application configuration.

Loads the multi-cloud / multi-team / multi-environment cluster topology from a
YAML file (see config/clusters.example.yaml) and exposes runtime settings that
come from environment variables. This is the single source of truth every
other module (providers, kube_client, auth, app) reads cluster/team metadata
from.

Provider-specific fields are intentionally kept as a loosely-typed dict
(`ClusterConfig.config`) rather than a discriminated union. That is what lets
a brand-new provider (e.g. "oci") be introduced by adding a file under
providers/ without editing this module — the provider implementation owns
interpreting its own config keys and raising a clear error if one is missing.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger("kubeorbit.config")

DEFAULT_EXCLUDED_NAMESPACE_PATTERNS = [
    "kube-system",
    "kube-public",
    "kube-node-lease",
    "gke-managed*",
    "gmp-system",
    "azure-*",
    "eks-*",
    "amazon-*",
    "kubernetes-dashboard",
]

VALID_ENVIRONMENTS = {"sandbox", "nonprod", "prod"}
VALID_PROVIDERS = {"gke", "eks", "aks"}  # informational only; registry is authoritative


class ClusterConfig(BaseModel):
    """A single Kubernetes cluster entry, scoped to a team + environment."""

    id: str = Field(..., description="Unique, URL-safe cluster identifier")
    name: str = Field(..., description="Human readable cluster name")
    provider: str = Field(..., description="Provider key, e.g. gke | eks | aks")
    environment: str = Field(..., description="sandbox | nonprod | prod")
    description: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict, description="Provider-specific settings")

    # populated by ConfigStore after load, not present in raw YAML
    team_id: str = ""
    team_name: str = ""

    @field_validator("id")
    @classmethod
    def _id_is_url_safe(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9][a-z0-9-]*$", v):
            raise ValueError(f"cluster id '{v}' must be lowercase alphanumeric with hyphens")
        return v

    @field_validator("environment")
    @classmethod
    def _environment_is_known(cls, v: str) -> str:
        if v not in VALID_ENVIRONMENTS:
            raise ValueError(f"environment '{v}' must be one of {sorted(VALID_ENVIRONMENTS)}")
        return v


class EnvironmentGroup(BaseModel):
    id: str
    clusters: List[ClusterConfig] = Field(default_factory=list)


class TeamConfig(BaseModel):
    id: str
    name: str
    environments: List[EnvironmentGroup] = Field(default_factory=list)


class AppSettings(BaseModel):
    excluded_namespace_patterns: List[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDED_NAMESPACE_PATTERNS)
    )
    default_refresh_interval_seconds: int = 30
    allowed_refresh_intervals_seconds: List[int] = Field(default_factory=lambda: [15, 30, 60, 120])
    kube_cache_ttl_seconds: int = 15
    cluster_meta_cache_ttl_seconds: int = 120
    auth_cache_ttl_seconds: int = 600
    cli_timeout_seconds: int = 45
    # Label keys tried in order (first match wins) to correlate a workload to
    # a logical "application" in get_logical_applications(); falls back to
    # splitting the resource name on its first hyphen if none match. Kept
    # configurable (rather than hardcoded) since every org labels
    # differently -- e.g. a team using Kustomize's `app.kubernetes.io/name`
    # everywhere, or a legacy `team`/`service` label instead of `app`.
    app_correlation_label_keys: List[str] = Field(
        default_factory=lambda: ["app.kubernetes.io/part-of", "app.kubernetes.io/name", "app", "tier"]
    )


class RawConfigFile(BaseModel):
    teams: List[TeamConfig] = Field(default_factory=list)
    settings: AppSettings = Field(default_factory=AppSettings)


class ConfigError(Exception):
    """Raised for malformed or unreadable cluster configuration."""


class ConfigStore:
    """In-memory index over the loaded YAML config, built once at startup."""

    def __init__(self, raw: RawConfigFile):
        self.settings = raw.settings
        self.teams: List[TeamConfig] = raw.teams
        self._clusters_by_id: Dict[str, ClusterConfig] = {}

        for team in self.teams:
            for env_group in team.environments:
                if env_group.id not in VALID_ENVIRONMENTS:
                    raise ConfigError(
                        f"team '{team.id}' has unknown environment group '{env_group.id}'"
                    )
                for cluster in env_group.clusters:
                    cluster.team_id = team.id
                    cluster.team_name = team.name
                    if cluster.environment != env_group.id:
                        raise ConfigError(
                            f"cluster '{cluster.id}' environment field '{cluster.environment}' "
                            f"does not match its parent group '{env_group.id}'"
                        )
                    if cluster.id in self._clusters_by_id:
                        raise ConfigError(f"duplicate cluster id '{cluster.id}' in config")
                    self._clusters_by_id[cluster.id] = cluster

        self._excluded_patterns_re = [
            re.compile("^" + re.escape(p).replace(r"\*", ".*") + "$")
            for p in self.settings.excluded_namespace_patterns
        ]

    @property
    def cluster_count(self) -> int:
        return len(self._clusters_by_id)

    def get_cluster(self, cluster_id: str) -> ClusterConfig:
        cluster = self._clusters_by_id.get(cluster_id)
        if cluster is None:
            raise KeyError(cluster_id)
        return cluster

    def list_clusters(self) -> List[ClusterConfig]:
        return list(self._clusters_by_id.values())

    def is_namespace_excluded(self, namespace: str) -> bool:
        return any(p.match(namespace) for p in self._excluded_patterns_re)

    def teams_payload(self) -> List[Dict[str, Any]]:
        """Frontend-friendly team -> environment -> cluster tree (no secrets)."""
        out = []
        for team in self.teams:
            team_entry = {"id": team.id, "name": team.name, "environments": []}
            for env_group in team.environments:
                env_entry = {
                    "id": env_group.id,
                    "clusters": [
                        {
                            "id": c.id,
                            "name": c.name,
                            "provider": c.provider,
                            "environment": c.environment,
                            "description": c.description,
                        }
                        for c in env_group.clusters
                    ],
                }
                team_entry["environments"].append(env_entry)
            out.append(team_entry)
        return out


def add_cluster_to_config(
    team_id: str,
    team_name: str,
    environment: str,
    cluster_data: Dict[str, Any],
) -> ConfigStore:
    path = get_config_path()
    raw_dict = _load_yaml(path)
    teams = raw_dict.setdefault("teams", [])

    team_obj = None
    for t in teams:
        if t.get("id") == team_id:
            team_obj = t
            break
    if not team_obj:
        team_obj = {"id": team_id, "name": team_name or team_id.title(), "environments": []}
        teams.append(team_obj)

    envs = team_obj.setdefault("environments", [])
    env_obj = None
    for e in envs:
        if e.get("id") == environment:
            env_obj = e
            break
    if not env_obj:
        env_obj = {"id": environment, "clusters": []}
        envs.append(env_obj)

    clusters = env_obj.setdefault("clusters", [])
    clusters = [c for c in clusters if c.get("id") != cluster_data["id"]]
    clusters.append(cluster_data)
    env_obj["clusters"] = clusters

    save_config(raw_dict)
    return get_config_store()


def delete_cluster_from_config(cluster_id: str) -> ConfigStore:
    path = get_config_path()
    raw_dict = _load_yaml(path)
    teams = raw_dict.get("teams", [])
    target = cluster_id.strip().lower()
    for t in teams:
        for e in t.get("environments", []):
            clusters = e.get("clusters", [])
            new_clusters = []
            for c in clusters:
                c_id = (c.get("id") or "").strip().lower()
                c_name = (c.get("name") or "").strip().lower()
                if c_id == target or c_name == target or c_id.replace(" ", "-") == target:
                    continue
                new_clusters.append(c)
            e["clusters"] = new_clusters

    save_config(raw_dict)

    # Clean up isolated kubeconfig file if it exists
    try:
        env = get_runtime_env()
        kcfg_file = env.kubeconfig_home / f"{cluster_id}.yaml"
        if kcfg_file.exists():
            kcfg_file.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("Could not delete isolated kubeconfig for '%s': %s", cluster_id, exc)

    return get_config_store()



def _load_from_secret_manager() -> Optional[Dict[str, Any]]:
    if os.environ.get("USE_GCP_SECRET_MANAGER", "false").lower() != "true":
        return None
    project_id = os.environ.get("GCP_PROJECT_ID", "project-4bc40d10-9765-40c3-805")
    secret_id = os.environ.get("GCP_SECRET_NAME", "kubeorbit-cluster-config")
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        payload = response.payload.data.decode("UTF-8")
        data = yaml.safe_load(payload) or {}
        if isinstance(data, dict) and "teams" in data:
            logger.info("Loaded config from GCP Secret Manager secret '%s' (project: %s)", secret_id, project_id)
            return data
    except Exception as exc:
        logger.warning("Could not load config from GCP Secret Manager (%s): %s", secret_id, exc)
    return None


def _load_yaml(path: Path) -> Dict[str, Any]:
    # Try GCP Secret Manager only if explicitly enabled via USE_GCP_SECRET_MANAGER=true
    secret_data = _load_from_secret_manager()
    if secret_data is not None:
        return secret_data

    if not path.exists():
        raise ConfigError(
            f"config file not found at '{path}'. Copy config/clusters.example.yaml to "
            f"config/clusters.yaml (or set KUBEORBIT_CONFIG_PATH) and fill in your clusters."
        )
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"failed to parse YAML config '{path}': {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config file '{path}' must contain a top-level mapping")
    return data


def save_config(raw_data: Dict[str, Any]) -> None:
    """Save updated config dictionary to local YAML file (and GCP Secret Manager if USE_GCP_SECRET_MANAGER=true)."""
    yaml_str = yaml.dump(raw_data, sort_keys=False)
    
    # 1. Update local file
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write(yaml_str)
    logger.info("Saved updated config to local file '%s'", path)

    # 2. Update GCP Secret Manager if explicitly enabled
    if os.environ.get("USE_GCP_SECRET_MANAGER", "false").lower() == "true":
        project_id = os.environ.get("GCP_PROJECT_ID", "project-4bc40d10-9765-40c3-805")
        secret_id = os.environ.get("GCP_SECRET_NAME", "kubeorbit-cluster-config")
        try:
            from google.cloud import secretmanager
            client = secretmanager.SecretManagerServiceClient()
            parent = f"projects/{project_id}/secrets/{secret_id}"
            client.add_secret_version(
                request={"parent": parent, "payload": {"data": yaml_str.encode("UTF-8")}}
            )
            logger.info("Saved updated config version to GCP Secret Manager '%s'", secret_id)
        except Exception as exc:
            logger.warning("Could not save config to GCP Secret Manager: %s", exc)

    # 3. Reload cached config store
    reload_config_store()



@lru_cache(maxsize=1)
def get_config_path() -> Path:
    return Path(os.environ.get("KUBEORBIT_CONFIG_PATH", "config/clusters.yaml")).resolve()


@lru_cache(maxsize=1)
def get_config_store() -> ConfigStore:
    path = get_config_path()
    raw_dict = _load_yaml(path)
    try:
        raw = RawConfigFile.model_validate(raw_dict)
    except Exception as exc:  # pydantic.ValidationError
        raise ConfigError(f"invalid config file '{path}': {exc}") from exc
    store = ConfigStore(raw)
    logger.info(
        "Loaded config: %d team(s), %d cluster(s)",
        len(store.teams),
        store.cluster_count,
    )
    return store


def reload_config_store() -> ConfigStore:
    """Clear cached config and reload from Secret Manager or disk."""
    get_config_store.cache_clear()
    return get_config_store()


@dataclass(frozen=True)
class RuntimeEnv:
    """Non-cluster runtime settings sourced from environment variables."""

    auth_mode: str = field(default_factory=lambda: os.environ.get("KUBEORBIT_AUTH_MODE", "none"))
    api_key: Optional[str] = field(default_factory=lambda: os.environ.get("KUBEORBIT_API_KEY"))
    # os.path.expanduser, not a bare Path(...): Path() never expands a
    # leading "~" on its own, so KUBEORBIT_KUBECONFIG_HOME=~/.kubeorbit/... in
    # .env (a literal, unexpanded string once python-dotenv reads it) created
    # a real directory named "~" in the working directory instead of
    # resolving to the home folder.
    kubeconfig_home: Path = field(
        default_factory=lambda: Path(
            os.path.expanduser(os.environ.get("KUBEORBIT_KUBECONFIG_HOME", str(Path.home() / ".kubeorbit" / "kubeconfigs")))
        )
    )
    log_level: str = field(default_factory=lambda: os.environ.get("KUBEORBIT_LOG_LEVEL", "INFO"))
    cors_allow_origins: List[str] = field(
        default_factory=lambda: [
            o.strip()
            for o in os.environ.get("KUBEORBIT_CORS_ORIGINS", "http://localhost:8000,http://localhost:8080").split(",")
            if o.strip()
        ]
    )

    def __post_init__(self):
        self.kubeconfig_home.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_runtime_env() -> RuntimeEnv:
    return RuntimeEnv()

