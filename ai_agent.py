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
from __future__ import annotations

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
        "backend": backend,
        "model": model_name,
        "project": project if backend == "vertex_ai" else None,
        "location": location if backend == "vertex_ai" else None,
    }


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
            "Every conversation specifies an active `cluster_id` -- always pass that `cluster_id` to your tool calls."
        ),
        tools=list(_TOOL_FUNCTIONS),
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
