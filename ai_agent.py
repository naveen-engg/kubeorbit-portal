"""In-app "Diagnose with AI" and "Ask AI" agent powered by Google ADK & Vertex AI Gemini.

Reuses `mcp_server/tools.py`'s read-only async functions directly as Google ADK
(`google-adk`) tools in the same process as the FastAPI app.

Authentication & Model Configuration (GCP Vertex AI first):
  1. Vertex AI (Recommended for GCP / Cloud Run & Local Dev with gcloud ADC):
       GOOGLE_GENAI_USE_VERTEXAI=TRUE
       GOOGLE_CLOUD_PROJECT=adk-enterprise-observability
       GOOGLE_CLOUD_LOCATION=us-central1
       KUBEORBIT_AI_MODEL=gemini-2.5-flash
     Uses Application Default Credentials (`gcloud auth application-default login`
     locally, or the attached `kubeorbit-runner` Service Account with
     `roles/aiplatform.user` on Cloud Run) -- zero API keys required.

  2. Gemini Developer API (Optional fallback if GOOGLE_API_KEY is explicitly set):
       GOOGLE_GENAI_USE_VERTEXAI=FALSE
       GOOGLE_API_KEY=<gemini-api-key>
       KUBEORBIT_AI_MODEL=gemini-2.5-flash
"""
from collections import OrderedDict
import logging
import os
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("kubeorbit.ai")

DEFAULT_GCP_PROJECT = "adk-enterprise-observability"
DEFAULT_GCP_LOCATION = "global"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"

_AGENT = None  # built lazily so importing this module never requires ADK/credentials to be ready
_RUNNER = None  # one shared InMemoryRunner -- its session_service is what makes chat() multi-turn
_AGENT_CONFIG_SIGNATURE: Optional[Tuple[str, str, str, str]] = None
_MAX_ACTIVE_SESSIONS = 200
_ACTIVE_SESSION_KEYS: "OrderedDict[Tuple[str, str], bool]" = OrderedDict()


def ensure_vertex_ai_env() -> Dict[str, Any]:
    """Ensure Google GenAI / ADK environment variables are configured for
    Vertex AI in the user's GCP project unless GOOGLE_API_KEY is explicitly used."""
    api_key = (os.environ.get("GOOGLE_API_KEY") or "").strip()
    use_vertex_raw = (os.environ.get("GOOGLE_GENAI_USE_VERTEXAI") or "").strip().upper()

    # Default to Vertex AI when no GOOGLE_API_KEY is provided or when explicitly enabled
    if use_vertex_raw in ("TRUE", "1", "YES") or not api_key:
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE"
        project = (
            os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("GCP_PROJECT")
            or DEFAULT_GCP_PROJECT
        ).strip()
        location = (
            os.environ.get("GOOGLE_CLOUD_LOCATION")
            or os.environ.get("GOOGLE_CLOUD_REGION")
            or DEFAULT_GCP_LOCATION
        ).strip()
        os.environ["GOOGLE_CLOUD_PROJECT"] = project
        os.environ["GOOGLE_CLOUD_LOCATION"] = location
        backend = "vertex_ai"
    else:
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "FALSE"
        project = (os.environ.get("GOOGLE_CLOUD_PROJECT") or "").strip()
        location = (os.environ.get("GOOGLE_CLOUD_LOCATION") or "").strip()
        backend = "gemini_api_key"

    model_name = (os.environ.get("KUBEORBIT_AI_MODEL") or DEFAULT_GEMINI_MODEL).strip()
    os.environ["KUBEORBIT_AI_MODEL"] = model_name

    return {
        "enabled": True,
        "configured": True,
        "backend": backend,
        "model": model_name,
        "project": project if backend == "vertex_ai" else None,
        "location": location if backend == "vertex_ai" else None,
    }


import inspect as _inspect
import json as _json
import time as _time
import uuid as _uuid
from typing import Any as _Any, AsyncIterator as _AsyncIterator, Dict as _Dict, List as _List, Optional as _Optional
from pydantic import BaseModel as _BaseModel, ConfigDict as _ConfigDict, Field as _Field


_PORTAL_VIEWS = frozenset(
    {
        "overview",
        "clusters",
        "applications",
        "workloads",
        "diagnostics",
        "audit",
        "nodes",
        "namespaces",
        "resourcemap",
        "map",
        "network",
        "storage",
        "events",
        "helm",
        "helm-history",
        "helm-diff",
        "ecosystem",
        "cost",
    }
)

_ROLLBACK_TYPE_MAP = {
    "deployment": "deployment",
    "deployments": "deployment",
    "deploy": "deployment",
    "statefulset": "statefulset",
    "statefulsets": "statefulset",
    "sts": "statefulset",
    "daemonset": "daemonset",
    "daemonsets": "daemonset",
    "ds": "daemonset",
}


class RunAgentInput(_BaseModel):
    """AG-UI Protocol RunAgentInput payload supporting camelCase and snake_case keys."""

    model_config = _ConfigDict(populate_by_name=True, extra="ignore")

    thread_id: str = _Field(default="", alias="threadId")
    run_id: str = _Field(default="", alias="runId")
    state: _Dict[str, _Any] = _Field(default_factory=dict)
    messages: _List[_Dict[str, _Any]] = _Field(default_factory=list)
    tools: _List[_Dict[str, _Any]] = _Field(default_factory=list)
    context: _List[_Dict[str, _Any]] = _Field(default_factory=list)
    forwarded_props: _Dict[str, _Any] = _Field(default_factory=dict, alias="forwardedProps")


def encode_ag_ui_event(event_type: str, **payload: _Any) -> str:
    """Encode an AG-UI protocol event as a Server-Sent Events frame."""
    body: _Dict[str, _Any] = {
        "type": event_type,
        "timestamp": int(_time.time() * 1000),
    }
    for key, value in payload.items():
        if value is not None:
            body[key] = value
    return f"data: {_json.dumps(body, default=str)}\n\n"


def ui_navigate_portal(view: str, namespace: str = "") -> dict:
    """Navigate the KubeOrbit portal UI to a specific view and optional namespace filter.

    Args:
        view: Target portal view name (e.g. overview, workloads, diagnostics, events, helm, helm-history, helm-diff, map, cost).
        namespace: Optional Kubernetes namespace filter to apply in the UI (empty for all namespaces).
    """
    normalized = (view or "").strip().lower()
    if normalized not in _PORTAL_VIEWS:
        return {
            "ok": False,
            "error": f"Unknown portal view '{view}'. Valid views: {', '.join(sorted(_PORTAL_VIEWS))}",
        }
    return {
        "__ag_ui_kind__": "ui_action",
        "ok": True,
        "action": "navigate",
        "view": normalized,
        "namespace": (namespace or "").strip(),
    }


def ui_filter_12step_machine(namespace: str = "", step_number: int = 0) -> dict:
    """Open the 12-Step kubectl apply Machine view and filter by namespace and/or pipeline step.

    Args:
        namespace: Optional Kubernetes namespace filter (empty clears or shows all).
        step_number: Optional step number 1-12 to focus in the 12-Step machine (0 shows all steps).
    """
    if step_number < 0 or step_number > 12:
        return {
            "ok": False,
            "error": "step_number must be between 1 and 12 (or 0 for all steps).",
        }
    return {
        "__ag_ui_kind__": "ui_action",
        "ok": True,
        "action": "filter_12step",
        "view": "diagnostics",
        "namespace": (namespace or "").strip(),
        "step_number": int(step_number) if step_number else None,
    }


def ui_inspect_pod(pod_name: str, namespace: str = "default") -> dict:
    """Open the interactive Pod Inspector modal in KubeOrbit Portal for a specific pod.

    Args:
        pod_name: Exact pod name.
        namespace: Kubernetes namespace of the pod (defaults to 'default').
    """
    pod = (pod_name or "").strip()
    if not pod:
        return {"ok": False, "error": "pod_name is required."}
    return {
        "__ag_ui_kind__": "ui_action",
        "ok": True,
        "action": "inspect_pod",
        "pod_name": pod,
        "namespace": (namespace or "default").strip(),
    }


def ui_open_pod_logs(pod_name: str, namespace: str = "default") -> dict:
    """Open the live Pod Logs modal in KubeOrbit Portal for a specific pod.

    Args:
        pod_name: Exact pod name.
        namespace: Kubernetes namespace of the pod (defaults to 'default').
    """
    pod = (pod_name or "").strip()
    if not pod:
        return {"ok": False, "error": "pod_name is required."}
    return {
        "__ag_ui_kind__": "ui_action",
        "ok": True,
        "action": "open_logs",
        "pod_name": pod,
        "namespace": (namespace or "default").strip(),
    }


def ui_open_manifest(kind: str, name: str, namespace: str = "default") -> dict:
    """Open the live YAML Manifest viewer modal in KubeOrbit Portal for a Kubernetes object.

    Args:
        kind: Kubernetes resource kind (e.g. Pod, Deployment, Service, ConfigMap).
        name: Kubernetes object name.
        namespace: Kubernetes namespace (defaults to 'default').
    """
    from kube_client.logs import is_secret_kind

    res_kind = (kind or "").strip()
    res_name = (name or "").strip()
    if not res_kind or not res_name:
        return {"ok": False, "error": "Both kind and name are required."}
    if is_secret_kind(res_kind):
        return {
            "ok": False,
            "error": (
                "Kubernetes Secret value and YAML retrieval is disabled for security reasons. "
                "Only Secret metadata (name, type, key names) is available."
            ),
        }
    return {
        "__ag_ui_kind__": "ui_action",
        "ok": True,
        "action": "open_manifest",
        "kind": res_kind,
        "name": res_name,
        "namespace": (namespace or "default").strip(),
    }


def ui_propose_rollback(
    namespace: str,
    workload_type: str,
    name: str,
    reason: str,
    to_revision: int = 0,
) -> dict:
    """Propose a workload rollback for explicit Human-in-the-Loop (HITL) confirmation in the portal UI.

    This tool NEVER executes the rollback itself; it renders an interactive approval card for an
    Operator or Admin to approve or reject in the UI.

    Args:
        namespace: Kubernetes namespace containing the workload.
        workload_type: Workload kind (deployment, statefulset, or daemonset).
        name: Workload name.
        reason: Concise evidence-based reason why rollback is recommended.
        to_revision: Optional rollout revision number (0 rolls back to previous revision).
    """
    from config import get_runtime_env

    if get_runtime_env().read_only_mode:
        return {
            "ok": False,
            "error": (
                "KubeOrbit is running in strictly read-only mode (KUBEORBIT_READ_ONLY=true). "
                "Provide rollback guidance as text/kubectl commands instead of staging write actions."
            ),
        }

    ns = (namespace or "").strip()
    wtype_raw = (workload_type or "").strip().lower()
    wtype = _ROLLBACK_TYPE_MAP.get(wtype_raw)
    wname = (name or "").strip()
    why = (reason or "").strip()
    if not ns or not wname or not why:
        return {"ok": False, "error": "namespace, name, and reason are required."}
    if not wtype:
        return {
            "ok": False,
            "error": "workload_type must be deployment, statefulset, or daemonset.",
        }
    return {
        "__ag_ui_kind__": "hitl_approval",
        "ok": True,
        "status": "awaiting_operator_confirmation",
        "action": "rollback_workload",
        "namespace": ns,
        "workload_type": wtype,
        "name": wname,
        "to_revision": int(to_revision) if to_revision and to_revision > 0 else None,
        "reason": why,
    }


_FRONTEND_UI_TOOLS = (
    ui_navigate_portal,
    ui_filter_12step_machine,
    ui_inspect_pod,
    ui_open_pod_logs,
    ui_open_manifest,
    ui_propose_rollback,
)


def _build_agent():
    from google.adk.agents import LlmAgent
    from mcp_server.server import _TOOL_FUNCTIONS

    cfg = ensure_vertex_ai_env()
    logger.info(
        "Initializing KubeOrbit Google ADK Agent: model=%s backend=%s project=%s location=%s",
        cfg["model"], cfg["backend"], cfg["project"], cfg["location"],
    )
    return LlmAgent(
        model=cfg["model"],
        name="kubeorbit_diagnostic_agent",
        instruction=(
            "You are an expert Kubernetes SRE & Cloud Architect assistant embedded in KubeOrbit, "
            "an enterprise multi-cloud Kubernetes portal spanning GKE, AWS EKS, and Azure AKS. "
            "You help operators diagnose cluster issues, inspect workloads, analyze network/storage "
            "topologies, review rollouts, and optimize costs.\n\n"
            "When diagnosing any workload, pod, service, or ingress problem, ALWAYS structure your "
            "root-cause analysis using the **12-Step `kubectl apply` Machine** mental model "
            "('Nothing talks to anything; everything talks to the API server'):\n"
            "- **Steps 1–6 (API Server & etcd)**: 1. kubectl client -> 2. authn -> 3. rbac-authz -> "
            "4. mutating-admission -> 5. schema/validating-admission -> 6. etcd storage.\n"
            "- **Steps 7–8 (Controllers & PodSecurity)**: 7. deployment-controller creates ReplicaSet -> "
            "8. replicaset-controller creates Pods (stalls here with 0 pods if PodSecurity, ResourceQuota, or ServiceAccount fails).\n"
            "- **Step 9 (kube-scheduler)**: Filter (CPU/Mem capacity, node taints/tolerations, nodeSelector/affinity, unbound PVC) -> Score -> Bind (`spec.nodeName`). Stalls here as `Pending`.\n"
            "- **Step 10 (kubelet on worker node)**: 10a. CSI volume/Secret/ConfigMap mount (`FailedMount`) -> "
            "10b. CNI network sandbox & IP (`FailedCreatePodSandBox`) -> 10c. Image pull (`ImagePullBackOff`) -> "
            "10d. InitContainers & containerd runtime (`CreateContainerConfigError`, `CrashLoopBackOff`, `OOMKilled`).\n"
            "- **Step 11 (Readiness & Startup Probes)**: Container is Running, but `readinessProbe` fails (`0/1 Ready`, `Unhealthy` event).\n"
            "- **Step 12 (EndpointSlice & Traffic Routing)**: `endpointslice-controller` adds Ready Pod IPs matching Service label selector; "
            "stalls here with HTTP 503 at Ingress/LB if Service selector has 0 matching or 0 Ready pods.\n\n"
            "Explicitly state which of the 12 steps stalled, cite exact pod names, namespaces, node names, "
            "events, or log lines from your read-only tools, and provide concrete `kubectl` remediation commands. "
            "Every conversation specifies an active `cluster_id` -- always pass that `cluster_id` to your tool calls.\n"
            "When running inside the KubeOrbit Portal AG-UI session, you also have interactive UI tools "
            "(ui_navigate_portal, ui_filter_12step_machine, ui_inspect_pod, ui_open_pod_logs, "
            "ui_open_manifest, ui_propose_rollback) to synchronize views, filters, and inspector modals "
            "for the user. Only use ui_propose_rollback when an operator explicitly asks to rollback or you "
            "have clear MCP evidence recommending rollback; ui_propose_rollback only stages an approval "
            "card and never mutates the cluster directly — never claim a rollback executed unless a "
            "subsequent user/tool confirmation confirms success."
        ),
        tools=list(_TOOL_FUNCTIONS) + list(_FRONTEND_UI_TOOLS),
    )


def _get_runner():
    from google.adk.runners import InMemoryRunner

    global _AGENT, _RUNNER, _AGENT_CONFIG_SIGNATURE
    cfg = ensure_vertex_ai_env()
    sig = (
        cfg["model"],
        cfg["backend"],
        str(cfg["project"] or ""),
        str(cfg["location"] or ""),
    )
    if _RUNNER is None or _AGENT is None or _AGENT_CONFIG_SIGNATURE != sig:
        _AGENT = _build_agent()
        _RUNNER = InMemoryRunner(agent=_AGENT, app_name="kubeorbit")
        _AGENT_CONFIG_SIGNATURE = sig
    return _RUNNER


def _build_prompt(
    cluster_id: str,
    namespace: Optional[str],
    resource_type: Optional[str],
    resource_name: Optional[str],
    question: Optional[str],
) -> str:
    context_lines = [f"cluster_id: {cluster_id}"]
    if namespace:
        context_lines.append(f"namespace: {namespace}")
    if resource_type and resource_name:
        context_lines.append(f"resource: {resource_type}/{resource_name}")
    context = "\n".join(context_lines)

    if question:
        return f"Context:\n{context}\n\nQuestion: {question}"

    if resource_type and resource_name:
        return (
            f"Context:\n{context}\n\n"
            f"Investigate why {resource_type}/{resource_name} in namespace '{namespace}' "
            f"on cluster '{cluster_id}' might be unhealthy or worth attention. Use the "
            f"available tools to look at its details/logs/events, explain the likely "
            f"root cause, and suggest a concrete next step."
        )

    scope = f"cluster '{cluster_id}', namespace '{namespace}'" if namespace else f"cluster '{cluster_id}'"
    return (
        f"Context:\n{context}\n\n"
        f"Give a short diagnostic summary of {scope} using the available tools: "
        f"overall health, and the single most important thing to look at next."
    )


async def _get_or_create_session(session_id: Optional[str] = None, user_id: str = "portal"):
    """Resolves `session_id` to a live ADK session scoped to `user_id`, or starts a fresh one.

    Returns (session, is_new). Enforces per-user session isolation so one user cannot
    resume another user's session_id, and bounds total in-memory sessions with LRU eviction."""
    runner = _get_runner()
    uid = (user_id or "portal").strip().lower()
    session = None
    if session_id:
        session = await runner.session_service.get_session(
            app_name="kubeorbit", user_id=uid, session_id=session_id
        )
    is_new = session is None
    if is_new:
        while len(_ACTIVE_SESSION_KEYS) >= _MAX_ACTIVE_SESSIONS:
            old_uid, old_sid = _ACTIVE_SESSION_KEYS.popitem(last=False)[0]
            try:
                await runner.session_service.delete_session(
                    app_name="kubeorbit", user_id=old_uid, session_id=old_sid
                )
            except Exception:
                pass
        session = await runner.session_service.create_session(
            app_name="kubeorbit", user_id=uid, session_id=session_id
        )
    _ACTIVE_SESSION_KEYS[(uid, session.id)] = True
    _ACTIVE_SESSION_KEYS.move_to_end((uid, session.id))
    return session, is_new


async def _run_turn(prompt: str, session, user_id: str = "portal") -> Dict[str, Any]:
    """Sends one prompt to `session` and collects the agent's text reply.
    Returns {"answer": str} on success or {"error": str} on any failure."""
    try:
        from google.genai import types
    except ImportError:
        return {"error": "google-adk is not installed on the server. Run `pip install google-adk` to enable this."}

    from mcp_server.client import reset_inprocess_user, set_inprocess_user

    uid = (user_id or "portal").strip().lower()
    token = set_inprocess_user(uid)
    try:
        runner = _get_runner()
        content = types.Content(role="user", parts=[types.Part(text=prompt)])

        answer_parts = []
        async for event in runner.run_async(user_id=uid, session_id=session.id, new_message=content):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        answer_parts.append(part.text)

        answer = "\n".join(answer_parts).strip()
        cfg = ensure_vertex_ai_env()
        return {
            "answer": answer or "The AI agent completed but returned no text response.",
            "model": cfg["model"],
            "backend": cfg["backend"],
        }
    except Exception as exc:
        logger.warning("AI agent call failed: %s", exc)
        cfg = ensure_vertex_ai_env()
        return {
            "error": (
                f"AI agent call failed ({cfg['backend']} / {cfg['model']} in project={cfg['project']}): {exc}. "
                f"Ensure Vertex AI API (aiplatform.googleapis.com) is enabled and ADC / Service Account has roles/aiplatform.user."
            )
        }
    finally:
        reset_inprocess_user(token)


async def diagnose(
    cluster_id: str,
    namespace: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_name: Optional[str] = None,
    question: Optional[str] = None,
    user_id: str = "portal",
) -> Dict[str, Any]:
    """Run one standalone turn of the diagnostic agent."""
    try:
        session, _ = await _get_or_create_session(user_id=user_id)
    except ImportError:
        return {"error": "google-adk is not installed on the server. Run `pip install google-adk` to enable this."}
    prompt = _build_prompt(cluster_id, namespace, resource_type, resource_name, question)
    return await _run_turn(prompt, session, user_id=user_id)


async def chat(
    cluster_id: str,
    message: str,
    session_id: Optional[str] = None,
    user_id: str = "portal",
) -> Dict[str, Any]:
    """One turn of the floating "Ask AI" chat panel. Reuses `session_id`
    across calls scoped to `user_id` so the agent remembers earlier turns."""
    try:
        session, is_new = await _get_or_create_session(session_id=session_id, user_id=user_id)
    except ImportError:
        return {"error": "google-adk is not installed on the server. Run `pip install google-adk` to enable this."}
    prompt = f"Context:\ncluster_id: {cluster_id}\n\n{message}" if is_new else message
    result = await _run_turn(prompt, session, user_id=user_id)
    result["session_id"] = session.id
    return result


def _message_text_from_ag_ui(msg: _Dict[str, _Any]) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: _List[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts).strip()
    return ""


def _extract_ag_ui_prompt(
    messages: _List[_Dict[str, _Any]],
    cluster_id: str,
    state: _Dict[str, _Any],
    is_new: bool,
    user_role: str,
) -> _Optional[str]:
    if not messages:
        return None

    target_msg: _Optional[_Dict[str, _Any]] = None
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") in ("user", "tool"):
            target_msg = msg
            break
    if target_msg is None:
        return None

    role = target_msg.get("role")
    text = _message_text_from_ag_ui(target_msg)
    if not text:
        return None

    if role == "tool":
        call_id = target_msg.get("toolCallId") or target_msg.get("tool_call_id") or ""
        raw_prompt = (
            f"[Portal HITL / Frontend Tool Result toolCallId={call_id}]\n{text}\n"
            "Acknowledge this result accurately to the user (and verify live pod/workload status "
            "via MCP tools if the action executed)."
        )
    else:
        raw_prompt = text

    active_view = str(state.get("activeView") or state.get("active_view") or "overview")
    active_ns = str(state.get("namespace") or "")
    selected_step = state.get("selectedStep") or state.get("selected_step")
    ui_meta = (
        f"cluster_id: {cluster_id}\n"
        f"user_role: {user_role}\n"
        f"active_view: {active_view}\n"
        f"selected_namespace: {active_ns or '(all)'}"
    )
    if selected_step:
        ui_meta += f"\nselected_12step: {selected_step}"

    if is_new:
        return f"Context:\n{ui_meta}\n\n{raw_prompt}"
    return f"[Portal UI state: view={active_view}, ns={active_ns or 'all'}, role={user_role}]\n{raw_prompt}"


def _unwrap_adk_tool_response(resp_obj: _Any) -> _Any:
    if isinstance(resp_obj, dict) and set(resp_obj.keys()) == {"result"}:
        return resp_obj["result"]
    return resp_obj


def _build_ui_state_delta(payload: _Dict[str, _Any]) -> _List[_Dict[str, _Any]]:
    ops: _List[_Dict[str, _Any]] = []
    if payload.get("view"):
        ops.append({"op": "replace", "path": "/activeView", "value": payload["view"]})
    if "namespace" in payload:
        ops.append({"op": "replace", "path": "/namespace", "value": payload.get("namespace") or ""})
    if payload.get("action") == "filter_12step":
        ops.append({"op": "replace", "path": "/selectedStep", "value": payload.get("step_number")})
    return ops


async def stream_ag_ui_run(
    cluster_id: str,
    req: RunAgentInput,
    user_id: str = "anonymous",
    user_role: str = "viewer",
) -> _AsyncIterator[str]:
    """Run one AG-UI interactive streaming turn backed by Google ADK + MCP + Portal UI tools."""
    from google.genai import types
    from mcp_server.client import reset_inprocess_user, set_inprocess_user
    from mcp_server.server import _TOOL_FUNCTIONS

    cfg = ensure_vertex_ai_env()
    run_id = req.run_id or f"run-{_uuid.uuid4().hex[:12]}"

    if not cfg.get("configured", cfg.get("enabled", False)):
        yield encode_ag_ui_event(
            "RUN_STARTED",
            threadId=req.thread_id or f"th-{_uuid.uuid4().hex[:12]}",
            runId=run_id,
        )
        yield encode_ag_ui_event(
            "RUN_ERROR",
            message=cfg.get("error") or "AI agent is not configured.",
            code="AI_NOT_CONFIGURED",
        )
        return

    if not _extract_ag_ui_prompt(req.messages, cluster_id, req.state, is_new=True, user_role=user_role):
        yield encode_ag_ui_event(
            "RUN_STARTED",
            threadId=req.thread_id or f"th-{_uuid.uuid4().hex[:12]}",
            runId=run_id,
        )
        yield encode_ag_ui_event(
            "RUN_ERROR",
            message="No user or tool message provided in RunAgentInput.messages.",
            code="EMPTY_MESSAGE",
        )
        return

    session, is_new = await _get_or_create_session(req.thread_id or None, user_id=user_id)
    thread_id = session.id
    prompt = _extract_ag_ui_prompt(req.messages, cluster_id, req.state, is_new=is_new, user_role=user_role)

    yield encode_ag_ui_event("RUN_STARTED", threadId=thread_id, runId=run_id)

    snapshot: _Dict[str, _Any] = {
        **(req.state or {}),
        "clusterId": cluster_id,
        "activeView": str((req.state or {}).get("activeView") or (req.state or {}).get("active_view") or "overview"),
        "namespace": str((req.state or {}).get("namespace") or ""),
        "userRole": user_role,
        "protocol": "ag-ui/1.0",
        "mcpToolCount": len(_TOOL_FUNCTIONS),
        "uiToolCount": len(_FRONTEND_UI_TOOLS),
    }
    yield encode_ag_ui_event("STATE_SNAPSHOT", snapshot=snapshot)
    yield encode_ag_ui_event("STEP_STARTED", stepName="adk_agent_turn")

    runner = _get_runner()
    content = types.Content(role="user", parts=[types.Part(text=prompt)])

    run_kwargs: _Dict[str, _Any] = {
        "user_id": session.user_id,
        "session_id": session.id,
        "new_message": content,
    }
    try:
        sig = _inspect.signature(runner.run_async)
        accepts_run_config = "run_config" in sig.parameters or any(
            p.kind == _inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
    except (TypeError, ValueError):
        accepts_run_config = True

    if accepts_run_config:
        try:
            from google.adk.agents.run_config import RunConfig, StreamingMode

            run_kwargs["run_config"] = RunConfig(streaming_mode=StreamingMode.SSE)
        except Exception:
            pass

    active_text_msg_id: _Optional[str] = None
    streamed_partial_for_msg = False
    emitted_tool_calls: _Dict[str, str] = {}
    ended_tool_calls: set[str] = set()
    pending_by_name: _Dict[str, _List[str]] = {}
    tools_used: _List[_Dict[str, _Any]] = []

    uid = (user_id or "portal").strip().lower()
    tok = set_inprocess_user(uid)
    try:
        try:
            events_iter = runner.run_async(**run_kwargs)
        except TypeError:
            run_kwargs.pop("run_config", None)
            events_iter = runner.run_async(**run_kwargs)

        async for event in events_iter:
            ev_content = getattr(event, "content", None)
            parts = list(getattr(ev_content, "parts", None) or [])

            if hasattr(event, "get_function_calls"):
                fn_calls = list(event.get_function_calls() or [])
            else:
                fn_calls = [getattr(p, "function_call") for p in parts if getattr(p, "function_call", None)]

            if fn_calls:
                if active_text_msg_id is not None:
                    yield encode_ag_ui_event("TEXT_MESSAGE_END", messageId=active_text_msg_id)
                    active_text_msg_id = None
                    streamed_partial_for_msg = False

                for fc in fn_calls:
                    call_id = getattr(fc, "id", None) or f"tc-{_uuid.uuid4().hex[:10]}"
                    tool_name = getattr(fc, "name", None) or "unknown_tool"
                    args_dict = dict(getattr(fc, "args", None) or {})
                    emitted_tool_calls[call_id] = tool_name
                    pending_by_name.setdefault(tool_name, []).append(call_id)
                    yield encode_ag_ui_event(
                        "TOOL_CALL_START",
                        toolCallId=call_id,
                        toolCallName=tool_name,
                        parentMessageId=None,
                    )
                    yield encode_ag_ui_event(
                        "TOOL_CALL_ARGS",
                        toolCallId=call_id,
                        delta=_json.dumps(args_dict, default=str),
                    )

            if hasattr(event, "get_function_responses"):
                fn_responses = list(event.get_function_responses() or [])
            else:
                fn_responses = [getattr(p, "function_response") for p in parts if getattr(p, "function_response", None)]

            for fr in fn_responses:
                tool_name = getattr(fr, "name", None) or "unknown_tool"
                raw_id = getattr(fr, "id", None)
                if raw_id and raw_id in emitted_tool_calls:
                    call_id = raw_id
                    if tool_name in pending_by_name and call_id in pending_by_name[tool_name]:
                        pending_by_name[tool_name].remove(call_id)
                elif pending_by_name.get(tool_name):
                    call_id = pending_by_name[tool_name].pop(0)
                else:
                    call_id = raw_id or f"tc-{_uuid.uuid4().hex[:10]}"

                payload = _unwrap_adk_tool_response(getattr(fr, "response", None))
                if call_id not in ended_tool_calls:
                    yield encode_ag_ui_event("TOOL_CALL_END", toolCallId=call_id)
                    ended_tool_calls.add(call_id)

                yield encode_ag_ui_event(
                    "TOOL_CALL_RESULT",
                    messageId=f"msg-{call_id}",
                    toolCallId=call_id,
                    content=_json.dumps(payload, default=str),
                    role="tool",
                )

                is_ok = bool(payload.get("ok", True)) if isinstance(payload, dict) else True
                protocol_tag = "ag-ui" if tool_name.startswith("ui_") else "mcp"
                tools_used.append({"id": call_id, "name": tool_name, "protocol": protocol_tag, "ok": is_ok})

                if isinstance(payload, dict):
                    kind = payload.get("__ag_ui_kind__")
                    if kind == "ui_action":
                        yield encode_ag_ui_event("CUSTOM", name="kubeorbit:ui_action", value=payload)
                        delta_ops = _build_ui_state_delta(payload)
                        if delta_ops:
                            yield encode_ag_ui_event("STATE_DELTA", delta=delta_ops)
                    elif kind == "hitl_approval":
                        yield encode_ag_ui_event(
                            "CUSTOM",
                            name="kubeorbit:hitl_approval",
                            value={
                                **payload,
                                "toolCallId": call_id,
                                "userRole": user_role,
                                "allowed": user_role in ("operator", "admin"),
                            },
                        )

            text_chunks = [
                p.text
                for p in parts
                if getattr(p, "text", None) and not getattr(p, "thought", False)
            ]
            if text_chunks:
                chunk_text = "".join(text_chunks)
                is_partial = bool(getattr(event, "partial", False))
                if is_partial:
                    if active_text_msg_id is None:
                        active_text_msg_id = f"msg-{_uuid.uuid4().hex[:12]}"
                        yield encode_ag_ui_event(
                            "TEXT_MESSAGE_START",
                            messageId=active_text_msg_id,
                            role="assistant",
                        )
                    yield encode_ag_ui_event(
                        "TEXT_MESSAGE_CONTENT",
                        messageId=active_text_msg_id,
                        delta=chunk_text,
                    )
                    streamed_partial_for_msg = True
                else:
                    if streamed_partial_for_msg and active_text_msg_id is not None:
                        yield encode_ag_ui_event("TEXT_MESSAGE_END", messageId=active_text_msg_id)
                        active_text_msg_id = None
                        streamed_partial_for_msg = False
                    else:
                        if active_text_msg_id is None:
                            active_text_msg_id = f"msg-{_uuid.uuid4().hex[:12]}"
                            yield encode_ag_ui_event(
                                "TEXT_MESSAGE_START",
                                messageId=active_text_msg_id,
                                role="assistant",
                            )
                        yield encode_ag_ui_event(
                            "TEXT_MESSAGE_CONTENT",
                            messageId=active_text_msg_id,
                            delta=chunk_text,
                        )
                        yield encode_ag_ui_event("TEXT_MESSAGE_END", messageId=active_text_msg_id)
                        active_text_msg_id = None
                        streamed_partial_for_msg = False

        for cid in list(emitted_tool_calls.keys()):
            if cid not in ended_tool_calls:
                yield encode_ag_ui_event("TOOL_CALL_END", toolCallId=cid)
                ended_tool_calls.add(cid)

        if active_text_msg_id is not None:
            yield encode_ag_ui_event("TEXT_MESSAGE_END", messageId=active_text_msg_id)

        yield encode_ag_ui_event("STEP_FINISHED", stepName="adk_agent_turn")
        yield encode_ag_ui_event(
            "RUN_FINISHED",
            threadId=thread_id,
            runId=run_id,
            result={
                "thread_id": thread_id,
                "run_id": run_id,
                "tools_used": tools_used,
            },
        )
    except Exception as exc:
        logger.warning("AG-UI streaming turn failed (cluster=%s): %s", cluster_id, exc)
        if active_text_msg_id is not None:
            yield encode_ag_ui_event("TEXT_MESSAGE_END", messageId=active_text_msg_id)
        yield encode_ag_ui_event(
            "RUN_ERROR",
            message=str(exc),
            code="AG_UI_RUN_ERROR",
        )
    finally:
        reset_inprocess_user(tok)

