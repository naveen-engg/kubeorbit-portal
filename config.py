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
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger("kubeorbit.config")

_config_write_lock = threading.Lock()
_store_lock = threading.RLock()
_SECRET_SYNC_TTL_SEC = float(os.environ.get("KUBEORBIT_SECRET_SYNC_TTL_SEC", "120"))
_MAX_OPTIMISTIC_RETRIES = 3
_last_secret_sync_ts: float = 0.0
_cached_store: Optional["ConfigStore"] = None
_secret_refresh_in_flight: bool = False
_config_epoch: int = 0

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


class TeamRBAC(BaseModel):
    """3-Tier per-team RBAC mapping (Admin: cluster CRUD, Operator: secret reveal & rollback, Viewer: read-only)."""

    admins: List[str] = Field(default_factory=list)
    operators: List[str] = Field(default_factory=list)
    viewers: List[str] = Field(default_factory=list)


class TeamConfig(BaseModel):
    id: str
    name: str
    environments: List[EnvironmentGroup] = Field(default_factory=list)
    rbac: Optional[TeamRBAC] = None


class AppSettings(BaseModel):
    excluded_namespace_patterns: List[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDED_NAMESPACE_PATTERNS)
    )
    default_refresh_interval_seconds: int = 30
    allowed_refresh_intervals_seconds: List[int] = Field(default_factory=lambda: [15, 30, 60, 120])
    kube_cache_ttl_seconds: int = 30
    cluster_meta_cache_ttl_seconds: int = 120
    auth_cache_ttl_seconds: int = 1800
    cli_timeout_seconds: int = 45
    app_correlation_label_keys: List[str] = Field(
        default_factory=lambda: ["app.kubernetes.io/part-of", "app.kubernetes.io/name", "app", "tier"]
    )


class RawConfigFile(BaseModel):
    teams: List[TeamConfig] = Field(default_factory=list)
    settings: AppSettings = Field(default_factory=AppSettings)


class ConfigError(Exception):
    """Raised for malformed or unreadable cluster configuration."""


class ConfigConflictError(ConfigError):
    """Raised when an optimistic concurrency check detects a newer config version in Secret Manager."""


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

    def get_team(self, team_id: str) -> Optional[TeamConfig]:
        for t in self.teams:
            if t.id == team_id:
                return t
        return None

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


def _mutate_config_with_retry(mutator: Callable[[Dict[str, Any]], None]) -> "ConfigStore":
    """Load, mutate, and atomically persist clusters config with optimistic retry on Secret Manager conflicts."""
    last_exc: Optional[Exception] = None
    for attempt in range(_MAX_OPTIMISTIC_RETRIES):
        with _config_write_lock:
            path = get_config_path()
            raw_dict, expected_version, expected_etag = _load_yaml_with_meta(path)
            mutator(raw_dict)
            try:
                save_config(
                    raw_dict,
                    expected_version=expected_version,
                    expected_etag=expected_etag,
                )
                return get_config_store()
            except ConfigConflictError as exc:
                last_exc = exc
                logger.warning(
                    "Optimistic config conflict on attempt %d/%d, retrying: %s",
                    attempt + 1,
                    _MAX_OPTIMISTIC_RETRIES,
                    exc,
                )
        time.sleep(0.05 * (attempt + 1))
    raise ConfigConflictError(f"Failed to save config after {_MAX_OPTIMISTIC_RETRIES} retries: {last_exc}")


def add_cluster_to_config(
    team_id: str,
    team_name: str,
    environment: str,
    cluster_data: Dict[str, Any],
) -> ConfigStore:
    def _apply(raw_dict: Dict[str, Any]) -> None:
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
        target_id = (cluster_data.get("id") or "").strip()
        # Remove any duplicate with the same ID across OTHER teams/environments first
        for t in teams:
            for e in t.get("environments", []):
                if t is team_obj and e is env_obj:
                    continue
                e["clusters"] = [c for c in e.get("clusters", []) if c.get("id") != target_id]
        # Update in-place if already in target environment to preserve card order, else append
        replaced = False
        new_clusters = []
        for c in clusters:
            if c.get("id") == target_id:
                if not replaced:
                    new_clusters.append(cluster_data)
                    replaced = True
            else:
                new_clusters.append(c)
        if not replaced:
            new_clusters.append(cluster_data)
        env_obj["clusters"] = new_clusters

    return _mutate_config_with_retry(_apply)


def delete_cluster_from_config(cluster_id: str) -> ConfigStore:
    target = cluster_id.strip().lower()

    def _apply(raw_dict: Dict[str, Any]) -> None:
        teams = raw_dict.get("teams", [])
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

    store = _mutate_config_with_retry(_apply)

    # Clean up isolated kubeconfig file if it exists (only if cluster_id is URL-safe)
    if re.match(r"^[a-z0-9][a-z0-9-]*$", target):
        try:
            env = get_runtime_env()
            kcfg_file = env.kubeconfig_home / f"{target}.yaml"
            if kcfg_file.exists():
                kcfg_file.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Could not delete isolated kubeconfig for '%s': %s", cluster_id, exc)

    return store


def _secret_manager_coords() -> Tuple[str, str]:
    project_id = (
        os.environ.get("GCP_PROJECT_ID")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or "adk-enterprise-observability"
    ).strip()
    secret_id = os.environ.get("GCP_SECRET_NAME", "kubeorbit-clusters-yaml").strip()
    return project_id, secret_id


def _load_from_secret_manager_with_meta() -> Optional[Tuple[Dict[str, Any], Optional[str], Optional[str]]]:
    if os.environ.get("USE_GCP_SECRET_MANAGER", "false").lower() != "true":
        return None
    project_id, secret_id = _secret_manager_coords()
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        parent = f"projects/{project_id}/secrets/{secret_id}"
        etag: Optional[str] = None
        try:
            secret_meta = client.get_secret(request={"name": parent})
            etag = getattr(secret_meta, "etag", None) or None
        except Exception:
            etag = None
        name = f"{parent}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        payload = response.payload.data.decode("UTF-8")
        data = yaml.safe_load(payload) or {}
        if isinstance(data, dict) and "teams" in data:
            logger.info("Loaded config from GCP Secret Manager secret '%s' (project: %s)", secret_id, project_id)
            return data, getattr(response, "name", None), etag
    except Exception as exc:
        logger.warning("Could not load config from GCP Secret Manager (%s): %s", secret_id, exc)
    return None


def _load_from_secret_manager() -> Optional[Dict[str, Any]]:
    res = _load_from_secret_manager_with_meta()
    return res[0] if res is not None else None


def _load_yaml_with_meta(path: Path) -> Tuple[Dict[str, Any], Optional[str], Optional[str]]:
    secret_res = _load_from_secret_manager_with_meta()
    if secret_res is not None:
        return secret_res

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
    return data, None, None


def _load_yaml(path: Path) -> Dict[str, Any]:
    data, _, _ = _load_yaml_with_meta(path)
    return data


def _save_to_secret_manager_optimistic(
    yaml_str: str,
    expected_version: Optional[str],
    expected_etag: Optional[str],
) -> None:
    """Persist updated clusters YAML to Secret Manager with version/etag conflict detection."""
    project_id, secret_id = _secret_manager_coords()
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    parent = f"projects/{project_id}/secrets/{secret_id}"

    if expected_version:
        latest_resp = client.access_secret_version(request={"name": f"{parent}/versions/latest"})
        latest_name = getattr(latest_resp, "name", None)
        if latest_name and latest_name != expected_version:
            raise ConfigConflictError(
                f"Secret '{secret_id}' changed concurrently ({expected_version} -> {latest_name})."
            )

    if expected_etag:
        try:
            client.update_secret(
                request={
                    "secret": {
                        "name": parent,
                        "etag": expected_etag,
                        "annotations": {"kubeorbit-rev": uuid.uuid4().hex},
                    },
                    "update_mask": {"paths": ["annotations"]},
                }
            )
        except Exception as exc:
            exc_name = type(exc).__name__.lower()
            exc_str = str(exc).lower()
            if any(tok in exc_name or tok in exc_str for tok in ("failedprecondition", "aborted", "conflict", "etag", "412", "409")):
                raise ConfigConflictError(f"Secret '{secret_id}' etag conflict: {exc}") from exc
            if "permissiondenied" in exc_name or "403" in exc_str:
                logger.debug("Runner lacks secretmanager.secrets.update; relying on version check: %s", exc)
            else:
                logger.debug("Non-fatal update_secret check failure: %s", exc)

    client.add_secret_version(
        request={"parent": parent, "payload": {"data": yaml_str.encode("UTF-8")}}
    )
    logger.info("Saved updated config version to GCP Secret Manager '%s'", secret_id)


def save_config(
    raw_data: Dict[str, Any],
    *,
    expected_version: Optional[str] = None,
    expected_etag: Optional[str] = None,
) -> None:
    """Atomically save updated config dictionary to local YAML file (and GCP Secret Manager if enabled)."""
    yaml_str = yaml.dump(raw_data, sort_keys=False)

    # 1. Update GCP Secret Manager BEFORE writing local cache if enabled (so conflicts abort cleanly)
    if os.environ.get("USE_GCP_SECRET_MANAGER", "false").lower() == "true":
        try:
            _save_to_secret_manager_optimistic(yaml_str, expected_version, expected_etag)
        except ConfigConflictError:
            raise
        except Exception as exc:
            logger.warning("Could not save config to GCP Secret Manager: %s", exc)

    # 2. Atomic write to local file via temp file in same directory + os.replace
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".clusters-", suffix=".yaml.tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(yaml_str)
            fh.flush()
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    logger.info("Saved updated config to local file '%s'", path)

    # 3. Reload cached config store
    reload_config_store()


@lru_cache(maxsize=1)
def get_config_path() -> Path:
    return Path(os.environ.get("KUBEORBIT_CONFIG_PATH", "config/clusters.yaml")).resolve()


def _clear_config_store_cache() -> None:
    global _cached_store, _last_secret_sync_ts, _config_epoch
    with _store_lock:
        _cached_store = None
        _last_secret_sync_ts = 0.0
        _config_epoch += 1


def _load_validated_store(path: Path) -> ConfigStore:
    raw_dict = _load_yaml(path)
    try:
        raw = RawConfigFile.model_validate(raw_dict)
    except Exception as exc:  # pydantic.ValidationError
        raise ConfigError(f"invalid config file '{path}': {exc}") from exc
    return ConfigStore(raw)


def _schedule_background_config_refresh(epoch: int) -> None:
    global _secret_refresh_in_flight
    with _store_lock:
        if _secret_refresh_in_flight:
            return
        _secret_refresh_in_flight = True

    def _worker() -> None:
        global _cached_store, _last_secret_sync_ts, _secret_refresh_in_flight
        try:
            path = get_config_path()
            fresh_store = _load_validated_store(path)
            with _store_lock:
                if _config_epoch == epoch:
                    _cached_store = fresh_store
                    _last_secret_sync_ts = time.monotonic()
                    logger.info(
                        "Refreshed config in background: %d team(s), %d cluster(s)",
                        len(_cached_store.teams),
                        _cached_store.cluster_count,
                    )
        except Exception as exc:
            logger.warning("Background config refresh failed (keeping cached config): %s", exc)
        finally:
            with _store_lock:
                _secret_refresh_in_flight = False

    thread = threading.Thread(target=_worker, name="kubeorbit-config-refresh", daemon=True)
    thread.start()


def get_config_store() -> ConfigStore:
    global _cached_store, _last_secret_sync_ts
    use_sm = os.environ.get("USE_GCP_SECRET_MANAGER", "false").lower() == "true"
    with _store_lock:
        if _cached_store is None:
            path = get_config_path()
            _cached_store = _load_validated_store(path)
            _last_secret_sync_ts = time.monotonic()
            logger.info(
                "Loaded config: %d team(s), %d cluster(s)",
                len(_cached_store.teams),
                _cached_store.cluster_count,
            )
            return _cached_store

        stale = use_sm and (time.monotonic() - _last_secret_sync_ts >= _SECRET_SYNC_TTL_SEC)
        if stale:
            _schedule_background_config_refresh(_config_epoch)
        return _cached_store


get_config_store.cache_clear = _clear_config_store_cache  # type: ignore[attr-defined]


def reload_config_store() -> ConfigStore:
    """Clear cached config and reload from Secret Manager or disk."""
    _clear_config_store_cache()
    return get_config_store()


@dataclass(frozen=True)
class RuntimeEnv:
    """Non-cluster runtime settings sourced from environment variables."""

    auth_mode: str = field(default_factory=lambda: os.environ.get("KUBEORBIT_AUTH_MODE", "none"))
    api_key: Optional[str] = field(default_factory=lambda: os.environ.get("KUBEORBIT_API_KEY"))
    iap_audience: Optional[str] = field(default_factory=lambda: os.environ.get("KUBEORBIT_IAP_AUDIENCE"))
    admin_emails: List[str] = field(
        default_factory=lambda: [
            e.strip().lower()
            for e in os.environ.get("KUBEORBIT_ADMIN_EMAILS", "").split(",")
            if e.strip()
        ]
    )
    operator_emails: List[str] = field(
        default_factory=lambda: [
            e.strip().lower()
            for e in os.environ.get("KUBEORBIT_OPERATOR_EMAILS", "").split(",")
            if e.strip()
        ]
    )
    viewer_emails: List[str] = field(
        default_factory=lambda: [
            e.strip().lower()
            for e in os.environ.get("KUBEORBIT_VIEWER_EMAILS", "").split(",")
            if e.strip()
        ]
    )
    gcp_iam_rbac_enabled: bool = field(
        default_factory=lambda: os.environ.get("KUBEORBIT_GCP_IAM_RBAC", "false").lower() == "true"
    )
    strict_rbac: bool = field(
        default_factory=lambda: os.environ.get("KUBEORBIT_STRICT_RBAC", "false").lower() == "true"
    )
    read_only_mode: bool = field(
        default_factory=lambda: os.environ.get("KUBEORBIT_READ_ONLY", "true").lower() != "false"
    )
    gcp_project_id: str = field(
        default_factory=lambda: (
            os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("GCP_PROJECT_ID")
            or "adk-enterprise-observability"
        ).strip()
    )
    disable_local_provider: bool = field(
        default_factory=lambda: (
            os.environ.get("KUBEORBIT_DISABLE_LOCAL_PROVIDER", "").lower() == "true"
            if "KUBEORBIT_DISABLE_LOCAL_PROVIDER" in os.environ
            else (
                os.environ.get("KUBEORBIT_AUTH_MODE", "none").lower() == "iap"
                or bool(os.environ.get("K_SERVICE"))
            )
        )
    )
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
