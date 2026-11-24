"""Helm release inventory, revision history, server-side secret masking,
manifest resource extraction, and structured revision diff engine."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import difflib
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple

import yaml

from config import get_config_store

from ._cache import _cache
from ._cli import BinaryNotFoundError, CLIError
from ._fetch import cached_kubectl_list

logger = logging.getLogger("kubeorbit.kube_client.helm")

K8S_TOKEN_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
MASK_NOTICE = (
    "Secret data and credential-like values are masked server-side. "
    "The fp fingerprint changes when the underlying value changes."
)

_MAX_EXPANDABLE_DIFF_LINES = 4000

_BENIGN_REF_KEY_RE = re.compile(
    r"^("
    r"existing_?secret|"
    r"secret_?(name|ref|key_?ref|key|namespace|mount_?path|annotations|labels|enabled|create)|"
    r"token_?(file|path|key|projection|expiration|audience|enabled)|"
    r"password_?(file|key|required|enabled)|"
    r"use_?password|"
    r"enable_?auth|"
    r"auth_?(enabled|mode|type|method|provider|header)|"
    r".*service_?account.*token.*|"
    r"(image_?)?pull_?secrets?|"
    r"rbac_?create|"
    r"enabled|create|name|namespace|annotations|labels|type|mount_?path|path|key|mode"
    r")$"
)

_SENSITIVE_KEY_RE = re.compile(
    r"("
    r"password|passwd|pwd|passphrase|secret|token|api_?key|access_?key|secret_?key|"
    r"private_?key|signing_?key|client_?secret|auth_?token|bearer|credential|jwt|"
    r"cookie_?secret|encryption_?key|master_?key|tls_?key|ssh_?key|gpg_?key|"
    r"connection_?string|dsn|jdbc|webhook_?secret|hec_?token|paas_?token|data_?ingest_?token|"
    r"^auth$|_auth$"
    r")"
)

_SECRET_CONTAINER_KEYS = frozenset(
    {
        "secret",
        "secrets",
        "secret_data",
        "credential",
        "credentials",
        "creds",
        "tokens",
    }
)

_SENSITIVE_VALUE_RE = re.compile(
    r"(-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|"
    r"\bBearer\s+[A-Za-z0-9._\-+/=]{8,}|"
    r"\bBasic\s+[A-Za-z0-9+/=]{8,}|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"\bgh[pousr]_[A-Za-z0-9_]{16,}\b|"
    r"\bsk-[A-Za-z0-9_-]{16,}\b|"
    r"\bdt0[a-z][0-9]{2}\.[A-Za-z0-9.]+\b|"
    r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@)"
)

_ALREADY_MASKED_PREFIX = "***MASKED***"


def _validate_name(value: str, label: str) -> str:
    cleaned = (value or "").strip()
    if (
        not cleaned
        or cleaned.startswith("-")
        or "/" in cleaned
        or "\\" in cleaned
        or not K8S_TOKEN_RE.match(cleaned)
    ):
        raise ValueError(f"Invalid {label}: {value!r}")
    return cleaned


def _snake_key(raw_key: Any) -> str:
    s = str(raw_key or "").strip()
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower()
    return s


def _is_benign_key(raw_key: Any) -> bool:
    norm = _snake_key(raw_key)
    return bool(norm and _BENIGN_REF_KEY_RE.match(norm))


def _is_sensitive_key(raw_key: Any) -> bool:
    norm = _snake_key(raw_key)
    if not norm or _BENIGN_REF_KEY_RE.match(norm):
        return False
    return bool(_SENSITIVE_KEY_RE.search(norm))


def _secret_fingerprint(value: Any) -> str:
    if isinstance(value, str):
        raw = value
    elif value is None:
        raw = ""
    else:
        raw = json.dumps(value, sort_keys=True, default=str)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{_ALREADY_MASKED_PREFIX} (fp:{digest})"


def _is_maskable_scalar(value: Any) -> bool:
    if value is None or isinstance(value, (bool, dict, list)):
        return False
    s = str(value)
    if not s or s.startswith(_ALREADY_MASKED_PREFIX):
        return False
    return True


def _mask_scalar_if_needed(value: Any, *, force: bool = False) -> Any:
    if not _is_maskable_scalar(value):
        return value
    s = str(value)
    if force or _SENSITIVE_VALUE_RE.search(s):
        return _secret_fingerprint(value)
    return value


def _mask_secret_mapping(mapping: Any) -> Any:
    if not isinstance(mapping, dict):
        return mapping
    out: Dict[Any, Any] = {}
    for k, v in mapping.items():
        if _is_maskable_scalar(v):
            out[k] = _secret_fingerprint(v)
        elif isinstance(v, (dict, list)):
            out[k] = _secret_fingerprint(v)
        else:
            out[k] = v
    return out


def _mask_value_tree(
    obj: Any,
    *,
    is_manifest: bool = False,
    in_secret_map: bool = False,
) -> Any:
    if isinstance(obj, list):
        return [
            _mask_value_tree(item, is_manifest=is_manifest, in_secret_map=in_secret_map)
            for item in obj
        ]
    if not isinstance(obj, dict):
        return _mask_scalar_if_needed(obj, force=in_secret_map)

    if is_manifest and str(obj.get("kind") or "").lower() == "secret":
        masked_doc: Dict[Any, Any] = {}
        for k, v in obj.items():
            if k in ("data", "stringData") and isinstance(v, dict):
                masked_doc[k] = _mask_secret_mapping(v)
            elif k == "metadata" and isinstance(v, dict):
                meta_copy = dict(v)
                anns = meta_copy.get("annotations")
                if isinstance(anns, dict) and "kubectl.kubernetes.io/last-applied-configuration" in anns:
                    anns_copy = dict(anns)
                    val = anns_copy["kubectl.kubernetes.io/last-applied-configuration"]
                    if _is_maskable_scalar(val):
                        anns_copy["kubectl.kubernetes.io/last-applied-configuration"] = _secret_fingerprint(val)
                    meta_copy["annotations"] = anns_copy
                masked_doc[k] = _mask_value_tree(meta_copy, is_manifest=False, in_secret_map=False)
            else:
                masked_doc[k] = _mask_value_tree(v, is_manifest=False, in_secret_map=False)
        return masked_doc

    out: Dict[Any, Any] = {}
    env_name = obj.get("name")
    env_name_sensitive = (
        isinstance(env_name, str)
        and "value" in obj
        and _is_sensitive_key(env_name)
    )

    for k, v in obj.items():
        norm_k = _snake_key(k)
        if k == "value" and env_name_sensitive and _is_maskable_scalar(v):
            out[k] = _secret_fingerprint(v)
            continue

        if _is_benign_key(k):
            if isinstance(v, (dict, list)):
                out[k] = _mask_value_tree(v, is_manifest=is_manifest, in_secret_map=False)
            else:
                out[k] = _mask_scalar_if_needed(v, force=False)
            continue

        if _is_sensitive_key(k):
            if isinstance(v, dict):
                child_secret_map = norm_k in _SECRET_CONTAINER_KEYS
                out[k] = _mask_value_tree(
                    v,
                    is_manifest=is_manifest,
                    in_secret_map=child_secret_map,
                )
            elif isinstance(v, list):
                out[k] = [
                    _mask_value_tree(el, is_manifest=is_manifest, in_secret_map=True)
                    if not isinstance(el, dict)
                    else _mask_value_tree(el, is_manifest=is_manifest, in_secret_map=False)
                    for el in v
                ]
            else:
                out[k] = _mask_scalar_if_needed(v, force=True)
            continue

        if isinstance(v, (dict, list)):
            out[k] = _mask_value_tree(v, is_manifest=is_manifest, in_secret_map=in_secret_map)
        else:
            out[k] = _mask_scalar_if_needed(v, force=in_secret_map)

    return out


def _mask_yaml_lines_fallback(raw_yaml: str) -> str:
    masked_lines: List[str] = []
    kv_re = re.compile(r"^(\s*(?:-\s+)?)([A-Za-z0-9_.\-/\"']+)(\s*:\s*)(.+?)(\s*)$")
    for line in raw_yaml.splitlines():
        m = kv_re.match(line)
        if m:
            prefix, raw_key, sep, raw_val, suffix = m.groups()
            clean_key = raw_key.strip("\"'")
            val_stripped = raw_val.strip()
            if (
                val_stripped
                and val_stripped not in ("|", ">", "|-", ">-", "{}", "[]", "null", "~")
                and not val_stripped.startswith("#")
                and (_is_sensitive_key(clean_key) or _SENSITIVE_VALUE_RE.search(val_stripped))
            ):
                fp = _secret_fingerprint(val_stripped.strip("\"'"))
                masked_lines.append(f'{prefix}{raw_key}{sep}"{fp}"{suffix}')
                continue
        if _SENSITIVE_VALUE_RE.search(line):
            fp = _secret_fingerprint(line.strip())
            masked_lines.append(f'"{fp}"')
        else:
            masked_lines.append(line)
    return "\n".join(masked_lines) + ("\n" if raw_yaml.endswith("\n") else "")


def mask_helm_yaml(raw_yaml: str, *, is_manifest: bool = False) -> str:
    """Mask secrets & credential-like values deterministically before returning YAML."""
    stripped = (raw_yaml or "").strip()
    if stripped in ("", "null", "Null", "NULL", "~", "{}"):
        return ""
    try:
        docs = [doc for doc in yaml.safe_load_all(raw_yaml) if doc is not None]
    except yaml.YAMLError:
        return _mask_yaml_lines_fallback(raw_yaml)

    if not docs or docs == [{}]:
        return ""

    masked_docs = [_mask_value_tree(doc, is_manifest=is_manifest) for doc in docs]
    dumped = yaml.safe_dump_all(
        masked_docs,
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=True,
    )
    return "" if dumped.strip() in ("", "null", "{}") else dumped


def _mask_plain_text(text: str) -> str:
    if not text:
        return ""
    lines: List[str] = []
    for line in text.splitlines():
        if _SENSITIVE_VALUE_RE.search(line):
            lines.append(_SENSITIVE_VALUE_RE.sub(lambda m: _secret_fingerprint(m.group(0)), line))
        else:
            lines.append(line)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _normalize_helm_timestamp(raw: Any) -> str:
    """Normalize Helm's Go time.Time output or ISO timestamp to UTC ISO-8601."""
    if not raw:
        return ""
    text = str(raw).strip()
    if not text:
        return ""

    # Standard ISO-8601 first
    iso_candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(iso_candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        pass

    # Go time.Time format: "2026-11-23 14:22:05.123456789 +0000 UTC"
    cleaned = re.sub(r"\s+m=[+-]?\d+(\.\d+)?$", "", text)
    cleaned = re.sub(r"\s+[A-Z]{2,5}$", "", cleaned)
    cleaned = re.sub(r"(\.\d{6})\d+", r"\1", cleaned)
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f %z",
        "%Y-%m-%d %H:%M:%S %z",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(cleaned, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue

    return text


def compute_structured_diff(old_text: str, new_text: str, context: int = 3) -> Dict[str, Any]:
    """Compute a unified diff structure with collapsible unchanged blocks and dual line numbers."""
    old_lines = (old_text or "").splitlines()
    new_lines = (new_text or "").splitlines()

    if old_lines == new_lines:
        count = len(old_lines)
        blocks: List[Dict[str, Any]] = []
        if count > 0:
            block: Dict[str, Any] = {
                "type": "collapsed",
                "count": count,
                "old_start": 1,
                "new_start": 1,
            }
            if count <= _MAX_EXPANDABLE_DIFF_LINES:
                block["lines"] = [
                    {
                        "kind": "context",
                        "old_line": i + 1,
                        "new_line": i + 1,
                        "text": old_lines[i],
                    }
                    for i in range(count)
                ]
            blocks.append(block)
        return {
            "has_changes": False,
            "added_lines": 0,
            "removed_lines": 0,
            "total_old_lines": len(old_lines),
            "total_new_lines": len(new_lines),
            "blocks": blocks,
        }

    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    include_collapsed_lines = max(len(old_lines), len(new_lines)) <= _MAX_EXPANDABLE_DIFF_LINES
    blocks = []
    added_lines = 0
    removed_lines = 0

    for group in sm.get_grouped_opcodes(n=context):
        group_first_old = group[0][1]
        group_first_new = group[0][3]

        # Track previous end lines to identify omitted unchanged blocks before each hunk
        prev_old_end = 0
        prev_new_end = 0
        if blocks:
            last_hunk = blocks[-1]
            for ln in reversed(last_hunk["lines"]):
                if prev_old_end == 0 and ln.get("old_line") is not None:
                    prev_old_end = ln["old_line"]
                if prev_new_end == 0 and ln.get("new_line") is not None:
                    prev_new_end = ln["new_line"]
                if prev_old_end and prev_new_end:
                    break

        gap = group_first_old - prev_old_end
        if gap > 0:
            collapsed: Dict[str, Any] = {
                "type": "collapsed",
                "count": gap,
                "old_start": prev_old_end + 1,
                "new_start": prev_new_end + 1,
            }
            if include_collapsed_lines:
                collapsed["lines"] = [
                    {
                        "kind": "context",
                        "old_line": prev_old_end + offset + 1,
                        "new_line": prev_new_end + offset + 1,
                        "text": old_lines[prev_old_end + offset],
                    }
                    for offset in range(gap)
                ]
            blocks.append(collapsed)

        hunk_lines: List[Dict[str, Any]] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for offset in range(i2 - i1):
                    hunk_lines.append(
                        {
                            "kind": "context",
                            "old_line": i1 + offset + 1,
                            "new_line": j1 + offset + 1,
                            "text": old_lines[i1 + offset],
                        }
                    )
            else:
                if tag in ("replace", "delete"):
                    for i in range(i1, i2):
                        removed_lines += 1
                        hunk_lines.append(
                            {
                                "kind": "remove",
                                "old_line": i + 1,
                                "new_line": None,
                                "text": old_lines[i],
                            }
                        )
                if tag in ("replace", "insert"):
                    for j in range(j1, j2):
                        added_lines += 1
                        hunk_lines.append(
                            {
                                "kind": "add",
                                "old_line": None,
                                "new_line": j + 1,
                                "text": new_lines[j],
                            }
                        )
        blocks.append({"type": "hunk", "lines": hunk_lines})

    # Trailing collapsed unchanged block after the final hunk
    if blocks:
        last_old_end = 0
        last_new_end = 0
        for ln in reversed(blocks[-1]["lines"]):
            if last_old_end == 0 and ln.get("old_line") is not None:
                last_old_end = ln["old_line"]
            if last_new_end == 0 and ln.get("new_line") is not None:
                last_new_end = ln["new_line"]
            if last_old_end and last_new_end:
                break
        trailing = len(old_lines) - last_old_end
        if trailing > 0:
            trailing_block: Dict[str, Any] = {
                "type": "collapsed",
                "count": trailing,
                "old_start": last_old_end + 1,
                "new_start": last_new_end + 1,
            }
            if include_collapsed_lines:
                trailing_block["lines"] = [
                    {
                        "kind": "context",
                        "old_line": last_old_end + offset + 1,
                        "new_line": last_new_end + offset + 1,
                        "text": old_lines[last_old_end + offset],
                    }
                    for offset in range(trailing)
                ]
            blocks.append(trailing_block)

    return {
        "has_changes": added_lines > 0 or removed_lines > 0,
        "added_lines": added_lines,
        "removed_lines": removed_lines,
        "total_old_lines": len(old_lines),
        "total_new_lines": len(new_lines),
        "blocks": blocks,
    }


async def _run_helm(
    cmd: List[str],
    timeout: Optional[int] = None,
    check: bool = True,
) -> Tuple[int, str, str]:
    # Delegate through kube_client.resources.run_cli so existing test suites that
    # monkeypatch kube_client.resources.run_cli keep intercepting all Helm CLIs.
    from . import resources as _resources

    return await _resources.run_cli(cmd, timeout=timeout, check=check)


def _extract_manifest_resources(raw_manifest: str, default_namespace: str) -> List[Dict[str, Any]]:
    if not (raw_manifest or "").strip():
        return []
    try:
        raw_docs = list(yaml.safe_load_all(raw_manifest))
    except yaml.YAMLError:
        return []

    expanded: List[Dict[str, Any]] = []
    for doc in raw_docs:
        if not isinstance(doc, dict):
            continue
        if doc.get("kind") == "List" and isinstance(doc.get("items"), list):
            for item in doc["items"]:
                if isinstance(item, dict):
                    expanded.append(item)
        else:
            expanded.append(doc)

    seen: set[Tuple[str, str, str]] = set()
    resources: List[Dict[str, Any]] = []
    for doc in expanded:
        kind = str(doc.get("kind") or "").strip()
        meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
        name = str(meta.get("name") or "").strip()
        if not kind or not name:
            continue
        ns = str(meta.get("namespace") or default_namespace or "").strip()
        dedup_key = (kind.lower(), ns, name)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        resources.append(
            {
                "kind": kind,
                "name": name,
                "namespace": ns,
                "api_version": str(doc.get("apiVersion") or ""),
                "status": "unknown",
            }
        )
    return resources


def _workload_item_health(kind_lower: str, item: Dict[str, Any]) -> str:
    spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
    status = item.get("status") if isinstance(item.get("status"), dict) else {}
    if kind_lower in ("deployment", "statefulset"):
        desired = spec.get("replicas")
        desired_int = 1 if desired is None else int(desired)
        ready = int(status.get("readyReplicas") or 0)
        if ready >= desired_int:
            return "healthy"
        return "warning" if ready > 0 else "critical"
    if kind_lower == "daemonset":
        desired = int(status.get("desiredNumberScheduled") or 0)
        ready = int(status.get("numberReady") or 0)
        if ready >= desired:
            return "healthy"
        return "warning" if ready > 0 else "critical"
    if kind_lower == "pod":
        phase = str(status.get("phase") or "").lower()
        if phase == "succeeded":
            return "healthy"
        if phase == "running":
            c_statuses = status.get("containerStatuses") or []
            if not c_statuses or all(c.get("ready") for c in c_statuses if isinstance(c, dict)):
                return "healthy"
            return "warning"
        if phase == "pending":
            return "warning"
        return "critical" if phase else "unknown"
    if kind_lower == "job":
        if int(status.get("failed") or 0) > 0:
            return "critical"
        if int(status.get("succeeded") or 0) > 0:
            return "healthy"
        return "warning"
    return "healthy"


async def _build_live_health_index(
    context: str,
    kubeconfig_path: Path,
) -> Dict[Tuple[str, str, str], str]:
    ttl = get_config_store().settings.kube_cache_ttl_seconds
    targets = [
        ("deployments", "deployment"),
        ("statefulsets", "statefulset"),
        ("daemonsets", "daemonset"),
        ("pods", "pod"),
        ("jobs", "job"),
    ]

    async def _fetch_kind(plural: str, singular: str):
        try:
            items, _ = await cached_kubectl_list(context, kubeconfig_path, plural, None, ttl)
            return singular, items
        except Exception as exc:
            logger.debug("Live health index lookup skipped for %s: %s", plural, exc)
            return singular, []

    results = await asyncio.gather(*(_fetch_kind(p, s) for p, s in targets))
    index: Dict[Tuple[str, str, str], str] = {}
    for singular, items in results:
        for item in items or []:
            meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            name = str(meta.get("name") or "").strip()
            ns = str(meta.get("namespace") or "").strip()
            if name:
                index[(singular, ns, name)] = _workload_item_health(singular, item)
    return index


def _fallback_status_from_release(release_status: str) -> str:
    st = (release_status or "").lower()
    if st == "deployed":
        return "healthy"
    if st.startswith("pending"):
        return "warning"
    if st == "failed":
        return "critical"
    return "unknown"


def _enrich_resources_status(
    resources: List[Dict[str, Any]],
    release_status: str,
    health_index: Dict[Tuple[str, str, str], str],
) -> List[Dict[str, Any]]:
    fallback = _fallback_status_from_release(release_status)
    enriched: List[Dict[str, Any]] = []
    for res in resources:
        kind = str(res.get("kind") or "")
        ns = str(res.get("namespace") or "")
        name = str(res.get("name") or "")
        status = health_index.get((kind.lower(), ns, name)) or fallback
        enriched.append({**res, "status": status})
    return enriched


async def _get_raw_manifest(
    context: str,
    kubeconfig_path: Path,
    namespace: str,
    release_name: str,
    revision: Optional[int] = None,
) -> str:
    ttl = get_config_store().settings.cluster_meta_cache_ttl_seconds
    rev_tag = f"r{revision}" if revision is not None else "latest"
    cache_key = f"{context}:helm:rawmanifest:{namespace}:{release_name}:{rev_tag}"

    async def factory() -> str:
        cmd = [
            "helm",
            "get",
            "manifest",
            release_name,
            "-n",
            namespace,
            "--kube-context",
            context,
            "--kubeconfig",
            str(kubeconfig_path),
        ]
        if revision is not None:
            cmd.extend(["--revision", str(revision)])
        try:
            _, stdout, _ = await _run_helm(cmd)
            return stdout or ""
        except (BinaryNotFoundError, CLIError) as exc:
            logger.debug("helm get manifest failed for %s/%s (%s): %s", namespace, release_name, rev_tag, exc)
            return ""

    return (await _cache.get_or_set(cache_key, ttl, factory))[0]


async def _get_helm_artifact(
    context: str,
    kubeconfig_path: Path,
    namespace: str,
    release_name: str,
    subcommand: List[str],
    revision: Optional[int] = None,
    *,
    raise_on_not_found: bool = True,
) -> str:
    verb = subcommand[0]
    extra_flags = subcommand[1:]
    cmd = [
        "helm",
        "get",
        verb,
        release_name,
        *extra_flags,
        "-n",
        namespace,
        "--kube-context",
        context,
        "--kubeconfig",
        str(kubeconfig_path),
    ]
    if revision is not None:
        cmd.extend(["--revision", str(revision)])

    async def _exec() -> str:
        try:
            _, stdout, _ = await _run_helm(cmd)
            return stdout or ""
        except CLIError:
            if not raise_on_not_found:
                return ""
            raise

    if revision is not None:
        ttl = get_config_store().settings.cluster_meta_cache_ttl_seconds
        sub_key = "_".join(subcommand)
        cache_key = f"{context}:helm:artifact:{sub_key}:{namespace}:{release_name}:r{revision}"
        return (await _cache.get_or_set(cache_key, ttl, _exec))[0]

    return await _exec()


def _normalize_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    try:
        rev_num = int(entry.get("revision") or 0)
    except (TypeError, ValueError):
        rev_num = 0
    return {
        "revision": rev_num,
        "updated": _normalize_helm_timestamp(entry.get("updated")),
        "status": str(entry.get("status") or ""),
        "chart": str(entry.get("chart") or ""),
        "app_version": str(entry.get("app_version") or ""),
        "description": str(entry.get("description") or ""),
    }


async def get_helm_releases(
    context: str,
    kubeconfig_path: Path,
    namespace: Optional[str] = None,
    include_resources: bool = True,
) -> Dict[str, Any]:
    """List Helm releases across the cluster, optionally filtered by namespace and enriched with resources."""
    if namespace is not None and namespace != "":
        namespace = _validate_name(namespace, "namespace")
    else:
        namespace = None

    ttl = get_config_store().settings.cluster_meta_cache_ttl_seconds
    cache_key = f"{context}:helm:releases"

    async def factory():
        try:
            _, stdout, _ = await _run_helm(
                [
                    "helm",
                    "list",
                    "--all-namespaces",
                    "--kube-context",
                    context,
                    "--kubeconfig",
                    str(kubeconfig_path),
                    "-o",
                    "json",
                ]
            )
        except BinaryNotFoundError:
            return {"available": False, "items": []}
        except CLIError as exc:
            logger.warning("helm list failed for context %s: %s", context, exc)
            return {"available": True, "items": [], "error": str(exc)}

        releases = json.loads(stdout) if stdout.strip() else []
        items = [
            {
                "name": r.get("name"),
                "namespace": r.get("namespace"),
                "chart": r.get("chart"),
                "app_version": r.get("app_version"),
                "revision": str(r.get("revision") or ""),
                "status": r.get("status"),
                "updated": _normalize_helm_timestamp(r.get("updated")),
            }
            for r in releases
            if isinstance(r, dict)
        ]
        return {"available": True, "items": items}

    base_result, _ = await _cache.get_or_set(cache_key, ttl, factory)
    all_items = list(base_result.get("items") or [])
    namespaces = sorted({str(r.get("namespace")) for r in all_items if r.get("namespace")})

    filtered_items = (
        [r for r in all_items if r.get("namespace") == namespace]
        if namespace
        else all_items
    )

    if include_resources and filtered_items and base_result.get("available"):
        res_ttl = get_config_store().settings.kube_cache_ttl_seconds
        enriched_cache_key = f"{context}:helm:releases:enriched:{namespace or '_all_'}"

        async def _build_enriched_payload() -> Dict[str, Any]:
            sem = asyncio.Semaphore(5)

            async def _load_manifest_for(rel: Dict[str, Any]) -> Tuple[str, str, List[Dict[str, Any]]]:
                rel_name = str(rel.get("name") or "")
                rel_ns = str(rel.get("namespace") or "default")
                try:
                    rev_int = int(rel.get("revision")) if rel.get("revision") not in (None, "") else None
                except (TypeError, ValueError):
                    rev_int = None
                async with sem:
                    raw_manifest = await _get_raw_manifest(context, kubeconfig_path, rel_ns, rel_name, rev_int)
                return rel_ns, rel_name, _extract_manifest_resources(raw_manifest, rel_ns)

            health_index_task = asyncio.create_task(_build_live_health_index(context, kubeconfig_path))
            manifest_results = await asyncio.gather(*(_load_manifest_for(r) for r in filtered_items))
            health_index = await health_index_task

            res_by_key = {(ns, name): res for ns, name, res in manifest_results}
            enriched_items: List[Dict[str, Any]] = []
            for rel in filtered_items:
                rel_name = str(rel.get("name") or "")
                rel_ns = str(rel.get("namespace") or "default")
                raw_res = res_by_key.get((rel_ns, rel_name), [])
                enriched_res = _enrich_resources_status(raw_res, str(rel.get("status") or ""), health_index)
                enriched_items.append(
                    {
                        **rel,
                        "resources": enriched_res,
                        "resource_count": len(enriched_res),
                    }
                )

            payload: Dict[str, Any] = {
                "available": base_result.get("available", False),
                "items": enriched_items,
                "namespaces": namespaces,
            }
            if base_result.get("error"):
                payload["error"] = base_result["error"]
            return payload

        cached_payload, _ = await _cache.get_or_set(enriched_cache_key, res_ttl, _build_enriched_payload)
        return cached_payload

    enriched_items = [
        {**rel, "resources": [], "resource_count": 0}
        for rel in filtered_items
    ]

    payload: Dict[str, Any] = {
        "available": base_result.get("available", False),
        "items": enriched_items,
        "namespaces": namespaces,
    }
    if base_result.get("error"):
        payload["error"] = base_result["error"]
    return payload


async def get_helm_release_history(
    context: str,
    kubeconfig_path: Path,
    namespace: str,
    release_name: str,
    max_revisions: int = 256,
) -> Dict[str, Any]:
    """Return full revision history for a Helm release ordered newest-first."""
    ns = _validate_name(namespace, "namespace")
    rel = _validate_name(release_name, "release_name")
    capped = max(1, min(int(max_revisions), 256))

    ttl = get_config_store().settings.kube_cache_ttl_seconds
    cache_key = f"{context}:helm:history:{ns}:{rel}:{capped}"

    async def factory():
        _, stdout, _ = await _run_helm(
            [
                "helm",
                "history",
                rel,
                "-n",
                ns,
                "--max",
                str(capped),
                "--kube-context",
                context,
                "--kubeconfig",
                str(kubeconfig_path),
                "-o",
                "json",
            ]
        )
        raw_list = json.loads(stdout) if stdout.strip() else []
        revs = [_normalize_history_entry(item) for item in raw_list if isinstance(item, dict)]
        revs.sort(key=lambda r: r["revision"], reverse=True)

        current_rev = revs[0]["revision"] if revs else None
        earliest_rev = revs[-1]["revision"] if revs else None
        for r in revs:
            r["is_current"] = current_rev is not None and r["revision"] == current_rev

        return {
            "release": rel,
            "namespace": ns,
            "current_revision": current_rev,
            "earliest_revision": earliest_rev,
            "items": revs,
            "count": len(revs),
        }

    result, _ = await _cache.get_or_set(cache_key, ttl, factory)
    return result


async def get_helm_release_details(
    context: str,
    kubeconfig_path: Path,
    namespace: str,
    release_name: str,
    revision: Optional[int] = None,
) -> Dict[str, Any]:
    """Return release metadata, masked user/computed values, masked manifest YAML, notes, and live resources."""
    ns = _validate_name(namespace, "namespace")
    rel = _validate_name(release_name, "release_name")
    if revision is not None and int(revision) < 1:
        raise ValueError("revision must be >= 1")
    target_rev = int(revision) if revision is not None else None

    history_data, user_raw, computed_raw, manifest_raw, notes_raw, health_index = await asyncio.gather(
        get_helm_release_history(context, kubeconfig_path, ns, rel),
        _get_helm_artifact(context, kubeconfig_path, ns, rel, ["values", "-o", "yaml"], target_rev, raise_on_not_found=False),
        _get_helm_artifact(context, kubeconfig_path, ns, rel, ["values", "--all", "-o", "yaml"], target_rev, raise_on_not_found=False),
        _get_helm_artifact(context, kubeconfig_path, ns, rel, ["manifest"], target_rev, raise_on_not_found=False),
        _get_helm_artifact(context, kubeconfig_path, ns, rel, ["notes"], target_rev, raise_on_not_found=False),
        _build_live_health_index(context, kubeconfig_path),
    )

    revisions = history_data.get("items") or []
    matched = None
    if target_rev is not None:
        matched = next((r for r in revisions if r["revision"] == target_rev), None)
    if matched is None and revisions:
        matched = revisions[0]

    if matched is None and not manifest_raw.strip() and not computed_raw.strip():
        raise CLIError(["helm", "get", "values", rel], 1, f"Error: release: not found")

    rel_status = (matched or {}).get("status") or ""
    raw_resources = _extract_manifest_resources(manifest_raw, ns)
    enriched_resources = _enrich_resources_status(raw_resources, rel_status, health_index)

    return {
        "name": rel,
        "namespace": ns,
        "revision": target_rev or (matched or {}).get("revision"),
        "current_revision": history_data.get("current_revision"),
        "revision_count": history_data.get("count", len(revisions)),
        "chart": (matched or {}).get("chart", ""),
        "app_version": (matched or {}).get("app_version", ""),
        "status": rel_status,
        "updated": (matched or {}).get("updated", ""),
        "description": (matched or {}).get("description", ""),
        "masked_server_side": True,
        "mask_notice": MASK_NOTICE,
        "user_values_yaml": mask_helm_yaml(user_raw, is_manifest=False),
        "computed_values_yaml": mask_helm_yaml(computed_raw, is_manifest=False),
        "manifest_yaml": mask_helm_yaml(manifest_raw, is_manifest=True),
        "notes": _mask_plain_text(notes_raw),
        "resources": enriched_resources,
        "resource_count": len(enriched_resources),
    }


async def diff_helm_release_revisions(
    context: str,
    kubeconfig_path: Path,
    namespace: str,
    release_name: str,
    from_revision: int,
    to_revision: int,
    compare_mode: str = "values",
) -> Dict[str, Any]:
    """Compare two Helm revisions across user values, computed values, or rendered manifest YAML (masked server-side)."""
    ns = _validate_name(namespace, "namespace")
    rel = _validate_name(release_name, "release_name")
    from_rev = int(from_revision)
    to_rev = int(to_revision)
    if from_rev < 1 or to_rev < 1:
        raise ValueError("from_revision and to_revision must be >= 1")

    mode = (compare_mode or "values").strip().lower()
    if mode not in ("values", "all_values", "manifest"):
        raise ValueError(f"Unsupported compare mode: {compare_mode!r}")

    if mode == "values":
        subcmd = ["values", "-o", "yaml"]
        is_manifest = False
    elif mode == "all_values":
        subcmd = ["values", "--all", "-o", "yaml"]
        is_manifest = False
    else:
        subcmd = ["manifest"]
        is_manifest = True

    history_data, from_raw, to_raw = await asyncio.gather(
        get_helm_release_history(context, kubeconfig_path, ns, rel),
        _get_helm_artifact(context, kubeconfig_path, ns, rel, subcmd, from_rev),
        _get_helm_artifact(context, kubeconfig_path, ns, rel, subcmd, to_rev),
    )

    from_masked = mask_helm_yaml(from_raw, is_manifest=is_manifest)
    to_masked = mask_helm_yaml(to_raw, is_manifest=is_manifest)
    diff_result = compute_structured_diff(from_masked, to_masked, context=3)

    rev_lookup = {r["revision"]: r for r in history_data.get("items") or []}
    return {
        "release": rel,
        "namespace": ns,
        "from_revision": from_rev,
        "to_revision": to_rev,
        "compare_mode": mode,
        "from_meta": rev_lookup.get(from_rev, {"revision": from_rev}),
        "to_meta": rev_lookup.get(to_rev, {"revision": to_rev}),
        "masked_server_side": True,
        "mask_notice": MASK_NOTICE,
        **diff_result,
    }
