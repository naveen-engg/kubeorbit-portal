"""In-app "Diagnose with AI" agent.

Reuses mcp_server/tools.py's plain async functions directly as ADK tools --
no MCP subprocess/transport needed here since this runs in the same
process as the FastAPI app. mcp_server/ remains the way an *external* agent
(Claude Desktop, a standalone ADK deployment, ...) reaches these same
read-only tools over MCP; this module is the in-product "ask about this
cluster" shortcut on top of the identical tool set, so both paths can never
drift out of sync with what's actually exposed.

Model/auth configuration is standard google-adk: set exactly one of

  ANTHROPIC_API_KEY=<claude-api-key>   -- Claude (see console.anthropic.com)
  GOOGLE_API_KEY=<gemini-api-key>      -- Gemini Developer API
  GOOGLE_GENAI_USE_VERTEXAI=TRUE + GOOGLE_CLOUD_PROJECT + GOOGLE_CLOUD_LOCATION  -- Vertex AI

KUBEORBIT_AI_MODEL picks the model and, via its name, which of the above
credentials get used -- a "claude-*" string resolves to Anthropic, anything
else to Gemini (both are auto-registered by google-adk's LLMRegistry from
the model name alone, no separate provider flag needed). Defaults to
gemini-3.1-flash-lite, Google's current lowest-cost model, if unset.

If no valid credentials/model access are configured, every call here fails
closed with a clear, displayable error -- it never fabricates an answer.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger("kubeorbit.ai")

_AGENT = None  # built lazily so importing this module never requires ADK/credentials to be ready
_RUNNER = None  # one shared InMemoryRunner -- its session_service is what makes chat() multi-turn


def _resolve_model(model_name: str):
    """The model name as-is, or an explicit AnthropicLlm for a "claude-*" name.

    google-adk registers two classes for the same "claude-.*" pattern --
    AnthropicLlm (direct Anthropic API, ANTHROPIC_API_KEY) and Claude
    (Vertex AI, GOOGLE_CLOUD_PROJECT/LOCATION) -- and Claude's registration
    wins for any bare model string, since it's imported after and both
    classes claim the identical regex key. Passing "claude-haiku-4-5-..."
    straight to LlmAgent therefore always resolves to the Vertex path and
    fails closed asking for GCP project/location, even with a valid
    ANTHROPIC_API_KEY set. Building AnthropicLlm explicitly is the only way
    to reach the direct API with a plain key -- see
    google.adk.models.anthropic_llm."""
    if not model_name.startswith("claude-"):
        return model_name
    from google.adk.models.anthropic_llm import AnthropicLlm

    return AnthropicLlm(model=model_name)


def _build_agent():
    from google.adk.agents import LlmAgent

    from mcp_server.server import _TOOL_FUNCTIONS

    # `or`, not `.get(key, default)`: python-dotenv sets a blank .env line
    # (KUBEORBIT_AI_MODEL=) to "" in os.environ, which .get()'s default
    # would not catch since the key *exists* -- silently sending "" as the
    # model name, which ADK falls back on to its own default model rather
    # than raising, defeating the low-cost-model choice with no warning.
    model_name = os.environ.get("KUBEORBIT_AI_MODEL") or "gemini-3.1-flash-lite"
    model = _resolve_model(model_name)
    return LlmAgent(
        model=model,
        name="kubeorbit_diagnostic_agent",
        instruction=(
            "You are an assistant embedded in KubeOrbit, a multi-cloud Kubernetes "
            "portal. You help operators understand what's happening in a cluster. "
            "You only have read-only tools -- you cannot create, modify, or delete "
            "anything in any cluster, so never claim to have taken an action; only "
            "report what you found and, if useful, suggest a kubectl command the "
            "operator can run themselves. Be concise and concrete: name the actual "
            "resource, namespace, and likely cause, not generic advice. If a tool "
            "call fails or returns nothing useful, say so plainly instead of "
            "guessing. Every message tells you which cluster_id is active -- stick "
            "to that cluster for every tool call unless the operator names a "
            "different registered cluster explicitly, in which case say which "
            "cluster you're answering about."
        ),
        tools=list(_TOOL_FUNCTIONS),
    )


def _get_agent():
    global _AGENT
    if _AGENT is None:
        _AGENT = _build_agent()
    return _AGENT


def _get_runner():
    from google.adk.runners import InMemoryRunner

    global _RUNNER
    if _RUNNER is None:
        _RUNNER = InMemoryRunner(agent=_get_agent(), app_name="kubeorbit")
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


async def _get_or_create_session(session_id: Optional[str] = None):
    """Resolves `session_id` to a live ADK session, or starts a fresh one.

    Returns (session, is_new). A falsy or not-found session_id creates a new
    session (auto-generating an id if none was given); a session_id that's
    still live is reused as-is, carrying whatever turns ADK already stored
    for it -- that reuse is what makes chat() multi-turn. diagnose() always
    wants a fresh session, so it calls this with no session_id."""
    runner = _get_runner()
    session = None
    if session_id:
        session = await runner.session_service.get_session(app_name="kubeorbit", user_id="portal", session_id=session_id)
    is_new = session is None
    if is_new:
        session = await runner.session_service.create_session(app_name="kubeorbit", user_id="portal", session_id=session_id)
    return session, is_new


async def _run_turn(prompt: str, session) -> Dict[str, Any]:
    """Sends one prompt to `session` and collects the agent's text reply.
    Returns {"answer": str} on success or {"error": str} on any failure --
    never raises, since both callers are API routes that should degrade
    gracefully (missing credentials, model not enabled, network issues)
    rather than 500 the whole request."""
    try:
        from google.genai import types
    except ImportError:
        return {"error": "google-adk is not installed on the server. Run `pip install google-adk` to enable this."}

    try:
        runner = _get_runner()
        content = types.Content(role="user", parts=[types.Part(text=prompt)])

        answer_parts = []
        async for event in runner.run_async(user_id="portal", session_id=session.id, new_message=content):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        answer_parts.append(part.text)

        answer = "\n".join(answer_parts).strip()
        return {"answer": answer or "The AI agent completed but returned no text response."}
    except Exception as exc:
        logger.warning("AI agent call failed: %s", exc)
        return {
            "error": (
                f"AI agent call failed: {exc}. Check KUBEORBIT_AI_MODEL / "
                f"GOOGLE_API_KEY / Vertex AI configuration (see ai_agent.py)."
            )
        }


async def diagnose(
    cluster_id: str,
    namespace: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_name: Optional[str] = None,
    question: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one, standalone turn of the diagnostic agent -- the existing
    "Diagnose with AI" buttons on findings/pods, unrelated to the chat panel
    below and always starting a fresh session."""
    try:
        session, _ = await _get_or_create_session()
    except ImportError:
        return {"error": "google-adk is not installed on the server. Run `pip install google-adk` to enable this."}
    prompt = _build_prompt(cluster_id, namespace, resource_type, resource_name, question)
    return await _run_turn(prompt, session)


async def chat(cluster_id: str, message: str, session_id: Optional[str] = None) -> Dict[str, Any]:
    """One turn of the floating "Ask AI" chat panel. Reuses `session_id`
    across calls so the agent remembers earlier turns in the same
    conversation -- pass back the `session_id` this returns on the next
    call to continue it, or omit it to start a new conversation.

    cluster_id is only injected into the first message of a session; ADK's
    conversation history carries it for every turn after that, so repeating
    "Context: cluster_id: ..." on every message would just burn tokens once
    the model already has it. Returns {"answer": str, "session_id": str} on
    success or {"error": str} on failure -- never raises, same as diagnose()."""
    try:
        session, is_new = await _get_or_create_session(session_id)
    except ImportError:
        return {"error": "google-adk is not installed on the server. Run `pip install google-adk` to enable this."}
    prompt = f"Context:\ncluster_id: {cluster_id}\n\n{message}" if is_new else message
    result = await _run_turn(prompt, session)
    result["session_id"] = session.id
    return result
