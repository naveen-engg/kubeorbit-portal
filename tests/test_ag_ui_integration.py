"""Unit and integration tests for KubeOrbit's AG-UI (ADK-to-UI) protocol stack."""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, List

import httpx
import pytest

import ai_agent
import auth
import config
from mcp_server.server import _TOOL_FUNCTIONS, get_mcp_tool_catalog
from providers.base import ClusterAuthResult, ProviderIdentity


def _parse_sse_events(body: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for raw_frame in body.replace("\r\n", "\n").split("\n\n"):
        data_lines = [
            line[5:].lstrip()
            for line in raw_frame.split("\n")
            if line.startswith("data:")
        ]
        if not data_lines:
            continue
        payload = "\n".join(data_lines).strip()
        if payload and payload != "[DONE]":
            events.append(json.loads(payload))
    return events


class _FakeSession:
    def __init__(self, session_id: str = "th-test-001", user_id: str = "sre@corp.com"):
        self.id = session_id
        self.user_id = user_id


def _make_adk_event(
    *,
    text: str | None = None,
    partial: bool = False,
    thought: bool = False,
    fn_calls: List[tuple[str, str, Dict[str, Any]]] | None = None,
    fn_responses: List[tuple[str, str, Any]] | None = None,
):
    parts = []
    if text is not None:
        parts.append(SimpleNamespace(text=text, thought=thought, function_call=None, function_response=None))
    for cid, name, args in fn_calls or []:
        parts.append(
            SimpleNamespace(
                text=None,
                thought=False,
                function_call=SimpleNamespace(id=cid, name=name, args=args),
                function_response=None,
            )
        )
    for cid, name, resp in fn_responses or []:
        parts.append(
            SimpleNamespace(
                text=None,
                thought=False,
                function_call=None,
                function_response=SimpleNamespace(id=cid, name=name, response=resp),
            )
        )
    content = SimpleNamespace(parts=parts)
    calls = [p.function_call for p in parts if p.function_call is not None]
    responses = [p.function_response for p in parts if p.function_response is not None]
    return SimpleNamespace(
        content=content,
        partial=partial,
        get_function_calls=lambda: calls,
        get_function_responses=lambda: responses,
    )


class TestAGUIProtocolModelsAndTools:
    def test_run_agent_input_aliases_and_defaults(self):
        camel = ai_agent.RunAgentInput.model_validate(
            {
                "threadId": "th-123",
                "runId": "run-456",
                "state": {"activeView": "diagnostics"},
                "messages": [{"role": "user", "content": "check pods"}],
                "forwardedProps": {"source": "portal"},
            }
        )
        assert camel.thread_id == "th-123"
        assert camel.run_id == "run-456"
        assert camel.forwarded_props == {"source": "portal"}

        snake = ai_agent.RunAgentInput(
            thread_id="th-789",
            run_id="run-012",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert snake.thread_id == "th-789"
        assert snake.run_id == "run-012"
        assert snake.tools == []
        assert snake.state == {}

    def test_encode_ag_ui_event_format(self):
        frame = ai_agent.encode_ag_ui_event("TEXT_MESSAGE_CONTENT", messageId="m1", delta="hello", ignored=None)
        assert frame.startswith("data: ")
        assert frame.endswith("\n\n")
        parsed = json.loads(frame[len("data: ") :].strip())
        assert parsed["type"] == "TEXT_MESSAGE_CONTENT"
        assert parsed["messageId"] == "m1"
        assert parsed["delta"] == "hello"
        assert "ignored" not in parsed
        assert isinstance(parsed["timestamp"], int)

    def test_mcp_tool_catalog_read_only_and_complete(self):
        catalog = get_mcp_tool_catalog()
        assert len(catalog) == len(_TOOL_FUNCTIONS) == 27
        for entry in catalog:
            assert entry["read_only"] is True
            assert entry["destructive"] is False
            assert entry["protocol"] == "mcp"
            assert entry["title"]
            assert entry["description"]
            assert isinstance(entry["parameters"], list)

    def test_fastmcp_schemas_complete_and_adk_compatible(self, monkeypatch: pytest.MonkeyPatch):
        import asyncio
        from mcp_server.server import mcp

        async def _verify():
            tools = await mcp.list_tools()
            assert len(tools) == 27
            for tool in tools:
                assert tool.title, f"Missing title on {tool.name}"
                assert tool.description and not tool.description.endswith("\n")
                assert tool.annotations is not None
                assert tool.annotations.readOnlyHint is True
                assert tool.annotations.destructiveHint is False
                assert tool.annotations.idempotentHint is True
                assert tool.annotations.openWorldHint is False

                in_props = (tool.inputSchema or {}).get("properties", {})
                for p_name, p_schema in in_props.items():
                    assert p_schema.get("description"), f"Missing input description for {tool.name}.{p_name}"

                out_props = (tool.outputSchema or {}).get("properties", {})
                assert out_props, f"Missing outputSchema properties on {tool.name}"
                for o_name, o_schema in out_props.items():
                    assert o_schema.get("description"), f"Missing output description for {tool.name}.{o_name}"

            # End-to-end structured output execution with extra provider fields
            class _FakeClient:
                async def get(self, path: str, params=None):
                    return {"cluster_id": "gke-demo", "items": [{"name": "node-1"}], "count": 1, "provider_extra": True}

            monkeypatch.setattr("mcp_server.tools.get_client", lambda: _FakeClient())
            content, structured = await mcp.call_tool("get_nodes", {"cluster_id": "gke-demo"})
            assert content
            assert structured["cluster_id"] == "gke-demo"
            assert structured["count"] == 1
            assert structured["provider_extra"] is True

        asyncio.run(_verify())

        from google.adk.tools import FunctionTool

        for fn in _TOOL_FUNCTIONS:
            decl = FunctionTool(fn)._get_declaration()
            assert decl is not None and decl.name == fn.__name__

    def test_interactive_portal_ui_tools_validation_and_shapes(self, monkeypatch: pytest.MonkeyPatch):
        nav_ok = ai_agent.ui_navigate_portal("Diagnostics", namespace="helm-demo")
        assert nav_ok == {
            "__ag_ui_kind__": "ui_action",
            "ok": True,
            "action": "navigate",
            "view": "diagnostics",
            "namespace": "helm-demo",
        }
        nav_bad = ai_agent.ui_navigate_portal("drop-table")
        assert nav_bad["ok"] is False

        step_ok = ai_agent.ui_filter_12step_machine(namespace="kubeorbit-demo", step_number=9)
        assert step_ok["__ag_ui_kind__"] == "ui_action"
        assert step_ok["step_number"] == 9
        assert ai_agent.ui_filter_12step_machine(step_number=13)["ok"] is False
        assert ai_agent.ui_filter_12step_machine(step_number=-1)["ok"] is False

        pod_ok = ai_agent.ui_inspect_pod("api-pod-1", "prod")
        assert pod_ok["action"] == "inspect_pod"
        assert ai_agent.ui_inspect_pod("")["ok"] is False

        logs_ok = ai_agent.ui_open_pod_logs("api-pod-1", "prod")
        assert logs_ok["action"] == "open_logs"
        assert ai_agent.ui_open_pod_logs(" ")["ok"] is False

        yaml_ok = ai_agent.ui_open_manifest("Deployment", "hello-storefront", "helm-demo")
        assert yaml_ok["action"] == "open_manifest"
        assert ai_agent.ui_open_manifest("", "x")["ok"] is False

        # Default strictly read-only mode blocks HITL rollback proposals
        assert ai_agent.ui_propose_rollback("helm-demo", "Deployments", "hello-storefront", "Read-only check")["ok"] is False

        # Opt-in write mode allows HITL rollback proposal staging
        monkeypatch.setenv("KUBEORBIT_READ_ONLY", "false")
        config.get_runtime_env.cache_clear()

        hitl_ok = ai_agent.ui_propose_rollback("helm-demo", "Deployments", "hello-storefront", "CrashLoopBackOff on rev 2", to_revision=1)
        assert hitl_ok == {
            "__ag_ui_kind__": "hitl_approval",
            "ok": True,
            "status": "awaiting_operator_confirmation",
            "action": "rollback_workload",
            "namespace": "helm-demo",
            "workload_type": "deployment",
            "name": "hello-storefront",
            "to_revision": 1,
            "reason": "CrashLoopBackOff on rev 2",
        }
        assert ai_agent.ui_propose_rollback("helm-demo", "Pod", "p1", "invalid")["ok"] is False

    @pytest.mark.asyncio
    async def test_ensure_vertex_ai_env_configured_and_ui_tool_declarations(self, monkeypatch: pytest.MonkeyPatch):
        from google.adk.tools import FunctionTool

        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        cfg = ai_agent.ensure_vertex_ai_env()
        assert cfg["enabled"] is True
        assert cfg["configured"] is True

        # Every frontend UI tool must expose real runtime annotations (no PEP 563 strings)
        # so Google GenAI / ADK automatic function calling schema parsing succeeds.
        for fn in ai_agent._FRONTEND_UI_TOOLS:
            for param_name, ann in (getattr(fn, "__annotations__", {}) or {}).items():
                if param_name != "return":
                    assert not isinstance(ann, str), f"{fn.__name__}.{param_name} has stringified annotation {ann!r}"
            decl = FunctionTool(fn)._get_declaration()
            assert decl is not None
            assert decl.name == fn.__name__

        # stream_ag_ui_run must succeed WITHOUT monkeypatching ensure_vertex_ai_env
        async def fake_session(sid, user_id="anonymous"):
            return _FakeSession(sid or "th-real-cfg", user_id=user_id), True

        class _FakeRunner:
            async def run_async(self, user_id, session_id, new_message):
                yield _make_adk_event(text="Configured OK", partial=False)

        monkeypatch.setattr(ai_agent, "_get_or_create_session", fake_session)
        monkeypatch.setattr(ai_agent, "_get_runner", lambda: _FakeRunner())

        req = ai_agent.RunAgentInput(messages=[{"role": "user", "content": "ping"}])
        events = _parse_sse_events("".join([c async for c in ai_agent.stream_ag_ui_run("obs-sbx", req)]))
        assert events[-1]["type"] == "RUN_FINISHED"
        assert "RUN_ERROR" not in [e["type"] for e in events]


class TestAGUIStreamingRuntime:
    @pytest.mark.asyncio
    async def test_stream_ag_ui_run_deduplicates_partial_and_final_text(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            ai_agent,
            "ensure_vertex_ai_env",
            lambda: {"configured": True, "backend": "vertex_ai", "project": "p", "location": "global", "model": "gemini-3.5-flash"},
        )
        monkeypatch.setenv("KUBEORBIT_READ_ONLY", "false")
        config.get_runtime_env.cache_clear()

        async def fake_session(sid, user_id="anonymous"):
            return _FakeSession(sid or "th-dedup", user_id=user_id), True

        class _FakeRunner:
            async def run_async(self, user_id, session_id, new_message, run_config=None):
                yield _make_adk_event(text="Hello ", partial=True)
                yield _make_adk_event(text="world!", partial=True)
                # ADK SSE mode appends a merged final event with the combined text
                yield _make_adk_event(text="Hello world!", partial=False)

        monkeypatch.setattr(ai_agent, "_get_or_create_session", fake_session)
        monkeypatch.setattr(ai_agent, "_get_runner", lambda: _FakeRunner())

        req = ai_agent.RunAgentInput(
            threadId="th-dedup",
            runId="run-dedup",
            messages=[{"role": "user", "content": "Say hello"}],
        )
        chunks = [c async for c in ai_agent.stream_ag_ui_run("obs-sbx", req, user_id="sre@corp.com", user_role="operator")]
        events = _parse_sse_events("".join(chunks))
        types = [e["type"] for e in events]
        assert types[0] == "RUN_STARTED"
        assert "TEXT_MESSAGE_START" in types
        assert "TEXT_MESSAGE_END" in types
        assert types[-1] == "RUN_FINISHED"

        deltas = [e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT"]
        assert "".join(deltas) == "Hello world!"

    @pytest.mark.asyncio
    async def test_stream_ag_ui_run_emits_mcp_ui_action_and_hitl_events(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KUBEORBIT_READ_ONLY", "false")
        config.get_runtime_env.cache_clear()
        monkeypatch.setattr(
            ai_agent,
            "ensure_vertex_ai_env",
            lambda: {"configured": True, "backend": "vertex_ai", "project": "p", "location": "global", "model": "gemini-3.5-flash"},
        )
        captured_prompt: List[str] = []

        async def fake_session(sid, user_id="anonymous"):
            return _FakeSession(sid or "th-tools", user_id=user_id), False

        class _FakeRunner:
            async def run_async(self, user_id, session_id, new_message):
                captured_prompt.append(new_message.parts[0].text)
                yield _make_adk_event(
                    fn_calls=[("tc-mcp-1", "get_pods", {"cluster_id": "obs-sbx", "namespace": "helm-demo"})]
                )
                yield _make_adk_event(
                    fn_responses=[("tc-mcp-1", "get_pods", {"result": {"pods": [{"name": "hello-1", "status": "CrashLoopBackOff"}]}})]
                )
                ui_res = ai_agent.ui_filter_12step_machine(namespace="helm-demo", step_number=10)
                yield _make_adk_event(fn_calls=[("tc-ui-1", "ui_filter_12step_machine", {"namespace": "helm-demo", "step_number": 10})])
                yield _make_adk_event(fn_responses=[("tc-ui-1", "ui_filter_12step_machine", ui_res)])
                hitl_res = ai_agent.ui_propose_rollback("helm-demo", "deployment", "hello-storefront", "Bad image tag", 2)
                yield _make_adk_event(fn_calls=[("tc-hitl-1", "ui_propose_rollback", {"namespace": "helm-demo"})])
                yield _make_adk_event(fn_responses=[("tc-hitl-1", "ui_propose_rollback", hitl_res)])
                yield _make_adk_event(text="Filtered 12-Step Machine and staged rollback approval.", partial=False)

        monkeypatch.setattr(ai_agent, "_get_or_create_session", fake_session)
        monkeypatch.setattr(ai_agent, "_get_runner", lambda: _FakeRunner())

        req = ai_agent.RunAgentInput(
            threadId="th-tools",
            runId="run-tools",
            state={"activeView": "overview", "namespace": "helm-demo"},
            messages=[{"role": "user", "content": "Diagnose helm-demo"}],
        )
        chunks = [c async for c in ai_agent.stream_ag_ui_run("obs-sbx", req, user_id="viewer@corp.com", user_role="viewer")]
        events = _parse_sse_events("".join(chunks))

        custom_events = [e for e in events if e["type"] == "CUSTOM"]
        ui_actions = [e for e in custom_events if e["name"] == "kubeorbit:ui_action"]
        hitl_events = [e for e in custom_events if e["name"] == "kubeorbit:hitl_approval"]
        deltas = [e for e in events if e["type"] == "STATE_DELTA"]

        assert len(ui_actions) == 1
        assert ui_actions[0]["value"]["action"] == "filter_12step"
        assert len(deltas) == 1
        assert {"op": "replace", "path": "/selectedStep", "value": 10} in deltas[0]["delta"]
        assert len(hitl_events) == 1
        assert hitl_events[0]["value"]["allowed"] is False  # viewer cannot approve
        assert hitl_events[0]["value"]["toolCallId"] == "tc-hitl-1"
        assert "active_view" in captured_prompt[0] or "view=overview" in captured_prompt[0]

        finished = [e for e in events if e["type"] == "RUN_FINISHED"][0]
        protocols = {t["name"]: t["protocol"] for t in finished["result"]["tools_used"]}
        assert protocols["get_pods"] == "mcp"
        assert protocols["ui_filter_12step_machine"] == "ag-ui"
        assert protocols["ui_propose_rollback"] == "ag-ui"

    @pytest.mark.asyncio
    async def test_stream_ag_ui_run_handles_empty_unconfigured_and_tool_continuation(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            ai_agent,
            "ensure_vertex_ai_env",
            lambda: {"configured": False, "error": "Vertex AI disabled"},
        )
        req_empty = ai_agent.RunAgentInput(messages=[{"role": "user", "content": "hello"}])
        ev_unconf = _parse_sse_events("".join([c async for c in ai_agent.stream_ag_ui_run("obs-sbx", req_empty)]))
        assert [e["type"] for e in ev_unconf] == ["RUN_STARTED", "RUN_ERROR"]
        assert ev_unconf[1]["code"] == "AI_NOT_CONFIGURED"

        monkeypatch.setattr(
            ai_agent,
            "ensure_vertex_ai_env",
            lambda: {"configured": True, "backend": "vertex_ai"},
        )
        ev_no_msg = _parse_sse_events("".join([c async for c in ai_agent.stream_ag_ui_run("obs-sbx", ai_agent.RunAgentInput(messages=[]))]))
        assert [e["type"] for e in ev_no_msg] == ["RUN_STARTED", "RUN_ERROR"]
        assert ev_no_msg[1]["code"] == "EMPTY_MESSAGE"

        # Tool role continuation passes tool output to ADK runner
        seen: List[str] = []

        async def fake_session(sid, user_id="anonymous"):
            return _FakeSession("th-cont", user_id=user_id), False

        class _FakeRunner:
            async def run_async(self, user_id, session_id, new_message):
                seen.append(new_message.parts[0].text)
                yield _make_adk_event(text="Rollback confirmed.", partial=False)

        monkeypatch.setattr(ai_agent, "_get_or_create_session", fake_session)
        monkeypatch.setattr(ai_agent, "_get_runner", lambda: _FakeRunner())

        req_tool = ai_agent.RunAgentInput(
            threadId="th-cont",
            messages=[{"role": "tool", "toolCallId": "tc-hitl-1", "content": '{"ok": true, "approved": true}'}],
        )
        ev_tool = _parse_sse_events("".join([c async for c in ai_agent.stream_ag_ui_run("obs-sbx", req_tool, user_role="operator")]))
        assert ev_tool[-1]["type"] == "RUN_FINISHED"
        assert "tc-hitl-1" in seen[0]
        assert "approved" in seen[0]


class TestAGUIHTTPEndpointsAndRBAC:
    @pytest.mark.asyncio
    async def test_status_and_mcp_tools_endpoints(self, async_client: httpx.AsyncClient):
        r_status = await async_client.get("/api/status")
        assert r_status.status_code == 200
        data = r_status.json()
        assert data["protocols"]["ag_ui"]["version"] == "1.0"
        assert data["protocols"]["ag_ui"]["transport"] == "sse"
        assert data["protocols"]["mcp"]["tool_count"] == 27
        assert data["protocols"]["mcp"]["read_only"] is True
        assert data["protocols"]["adk"]["framework"] == "google-adk"

        r_tools = await async_client.get("/api/mcp/tools")
        assert r_tools.status_code == 200
        tools_payload = r_tools.json()
        assert tools_payload["protocol"] == "mcp"
        assert tools_payload["read_only"] is True
        assert tools_payload["count"] == 27
        assert len(tools_payload["tools"]) == 27
        assert len(tools_payload["ag_ui_frontend_tools"]) == 6

    @pytest.mark.asyncio
    async def test_ag_ui_sse_endpoint_and_strict_iap_rbac(
        self,
        async_client: httpx.AsyncClient,
        isolated_env: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("KUBEORBIT_AUTH_MODE", "iap")
        monkeypatch.setenv("KUBEORBIT_GCP_IAM_RBAC", "true")
        monkeypatch.setenv("KUBEORBIT_STRICT_RBAC", "true")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "adk-enterprise-observability")
        config.get_runtime_env.cache_clear()
        auth.invalidate_gcp_iam_cache()

        kcfg = isolated_env["kubeconfig_home"] / "mock-kcfg.yaml"
        kcfg.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")

        class _FakeProvider:
            def get_display_meta(self):
                return {"provider": "gke"}

        monkeypatch.setattr("app.build_provider", lambda c: _FakeProvider())

        async def _fake_auth(cluster, provider, user):
            return ClusterAuthResult(
                context_name="mock-ctx",
                kubeconfig_path=str(kcfg),
                authenticated_at=1.0,
                identity=ProviderIdentity(provider="gke"),
            )

        monkeypatch.setattr("auth.ensure_cluster_authenticated", _fake_auth)
        monkeypatch.setattr(
            "auth._get_gcp_custom_role_bindings",
            lambda project_id: {
                "admin": ["user:admin@corp.com"],
                "operator": ["user:ops@corp.com"],
                "viewer": ["user:viewer@corp.com"],
            },
        )
        monkeypatch.setattr(
            ai_agent,
            "ensure_vertex_ai_env",
            lambda: {"configured": True, "backend": "vertex_ai", "project": "p", "location": "global", "model": "gemini-3.5-flash"},
        )
        monkeypatch.setenv("KUBEORBIT_READ_ONLY", "false")
        config.get_runtime_env.cache_clear()

        async def fake_session(sid, user_id="anonymous"):
            return _FakeSession(sid or "th-http", user_id=user_id), True

        class _FakeRunner:
            async def run_async(self, user_id, session_id, new_message):
                hitl = ai_agent.ui_propose_rollback("helm-demo", "deployment", "hello-storefront", "Latency spike")
                yield _make_adk_event(fn_calls=[("tc-h1", "ui_propose_rollback", {"namespace": "helm-demo"})])
                yield _make_adk_event(fn_responses=[("tc-h1", "ui_propose_rollback", hitl)])
                yield _make_adk_event(text="Please review rollback.", partial=False)

        monkeypatch.setattr(ai_agent, "_get_or_create_session", fake_session)
        monkeypatch.setattr(ai_agent, "_get_runner", lambda: _FakeRunner())

        body = {
            "threadId": "th-http",
            "runId": "run-http-1",
            "state": {"activeView": "diagnostics", "namespace": "helm-demo"},
            "messages": [{"role": "user", "content": "Investigate latency"}],
        }

        # 1. Unauthorized IAP caller -> 403 Strict Zero-Trust deny on both endpoints
        intruder = {"X-Goog-Authenticated-User-Email": "accounts.google.com:intruder@gmail.com"}
        assert (await async_client.get("/api/mcp/tools", headers=intruder)).status_code == 403
        assert (await async_client.post("/api/clusters/obs-sbx/ai/ag-ui", headers=intruder, json=body)).status_code == 403

        # 2. Authorized Viewer -> 200 SSE stream, but HITL card marked allowed=False
        viewer = {"X-Goog-Authenticated-User-Email": "accounts.google.com:viewer@corp.com"}
        r_viewer = await async_client.post("/api/clusters/obs-sbx/ai/ag-ui", headers=viewer, json=body)
        assert r_viewer.status_code == 200
        assert r_viewer.headers.get("x-ag-ui-protocol") == "1.0"
        assert "text/event-stream" in r_viewer.headers.get("content-type", "")
        viewer_events = _parse_sse_events(r_viewer.text)
        viewer_hitl = [e for e in viewer_events if e["type"] == "CUSTOM" and e["name"] == "kubeorbit:hitl_approval"]
        assert viewer_hitl[0]["value"]["allowed"] is False

        # 3. Authorized Operator -> 200 SSE stream and HITL card marked allowed=True
        operator = {"X-Goog-Authenticated-User-Email": "accounts.google.com:ops@corp.com"}
        r_ops = await async_client.post("/api/clusters/obs-sbx/ai/ag-ui", headers=operator, json=body)
        assert r_ops.status_code == 200
        ops_events = _parse_sse_events(r_ops.text)
        ops_hitl = [e for e in ops_events if e["type"] == "CUSTOM" and e["name"] == "kubeorbit:hitl_approval"]
        assert ops_hitl[0]["value"]["allowed"] is True
        assert ops_events[-1]["type"] == "RUN_FINISHED"

    def test_portal_quick_prompts_are_generalized_without_hardcoded_demo_names(self):
        from pathlib import Path

        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "static" / "index.html").read_text(encoding="utf-8")
        app_js = (root / "static" / "app.js").read_text(encoding="utf-8")

        marker = 'id="agui-quick-prompts"'
        assert marker in html
        start = html.index(marker)
        snippet = html[start : html.index("</div>", start) + 6]
        assert "helm-demo" not in snippet
        assert "kubeorbit-demo" not in snippet

        assert "function buildPortalQuickPrompts()" in app_js
        assert "function renderAGUIQuickPrompts()" in app_js
        fn_start = app_js.index("function buildPortalQuickPrompts()")
        fn_end = app_js.index("function renderAGUIQuickPrompts()")
        fn_body = app_js[fn_start:fn_end]
        assert "helm-demo" not in fn_body
        assert "kubeorbit-demo" not in fn_body
        assert "List Namespaces" in fn_body
        assert "Services & Ingress" in fn_body
        assert "Helm Releases" in fn_body

    def test_interactive_mcp_tool_renderer_module_wiring(self):
        from pathlib import Path

        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        agui_views = (root / "static" / "agui_views.js").read_text(encoding="utf-8")
        app_js = (root / "static" / "app.js").read_text(encoding="utf-8")
        index_html = (root / "static" / "index.html").read_text(encoding="utf-8")

        for sym in [
            "export function summarizeToolResult",
            "export function renderInteractiveToolBody",
            "get_namespaces",
            "get_namespace_resources",
            "get_helm_releases",
            "get_events",
            "diagnose_cluster_issues",
        ]:
            assert sym in agui_views

        assert "agui_views.js" in app_js
        assert "renderInteractiveToolBody" in app_js
        assert "summarizeToolResult" in app_js
        assert "agui_views.js" in index_html

    def test_ui_open_manifest_blocks_secrets_and_allows_configmaps(self):
        import ai_agent

        for secret_kind in ("Secret", "secret", "secrets", "secrets.v1", "v1/Secret"):
            blocked = ai_agent.ui_open_manifest(secret_kind, "app-credentials", "default")
            assert blocked["ok"] is False
            assert "disabled" in blocked["error"].lower()

        allowed = ai_agent.ui_open_manifest("ConfigMap", "app-config", "default")
        assert allowed["ok"] is True
        assert allowed["action"] == "open_manifest"
        assert allowed["kind"] == "ConfigMap"

    @pytest.mark.asyncio
    async def test_claude_and_openai_placeholder_providers(self, monkeypatch: pytest.MonkeyPatch):
        import ai_agent

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("KUBEORBIT_AI_MODEL", raising=False)
        monkeypatch.delenv("KUBEORBIT_AI_PROVIDER", raising=False)

        providers = {p["id"]: p for p in ai_agent.get_supported_ai_providers()}
        assert providers["vertex_ai"]["status"] == "active"
        assert providers["claude"]["status"] == "placeholder"
        assert providers["openai"]["status"] == "placeholder"

        claude_cfg = ai_agent.ensure_vertex_ai_env(provider_override="claude")
        assert claude_cfg["provider"] == "claude"
        assert claude_cfg["placeholder"] is True
        assert claude_cfg["configured"] is False
        assert claude_cfg["model"].startswith("anthropic/")

        openai_cfg = ai_agent.ensure_vertex_ai_env(provider_override="openai")
        assert openai_cfg["provider"] == "openai"
        assert openai_cfg["placeholder"] is True
        assert openai_cfg["configured"] is False
        assert openai_cfg["model"].startswith("openai/")

        # AG-UI stream gracefully returns AI_PROVIDER_PLACEHOLDER when UI selects claude/openai
        for prov in ("claude", "openai"):
            req = ai_agent.RunAgentInput(
                messages=[{"role": "user", "content": "check pods"}],
                state={"aiProvider": prov},
            )
            events = _parse_sse_events("".join([c async for c in ai_agent.stream_ag_ui_run("obs-sbx", req)]))
            assert events[-1]["type"] == "RUN_ERROR"
            assert events[-1]["code"] == "AI_PROVIDER_PLACEHOLDER"

        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        index_html = (root / "static" / "index.html").read_text(encoding="utf-8")
        assert 'id="ai-provider-select"' in index_html
        assert 'value="claude"' in index_html
        assert 'value="openai"' in index_html
