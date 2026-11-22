// AG-UI (Agent-User Interaction) Protocol v1.0 SSE Client for KubeOrbit Portal.
// Connects the browser UI directly to Google ADK + MCP tool runs over Server-Sent Events.

import { getStoredApiKey } from "./api.js";

export const PORTAL_FRONTEND_TOOLS = [
  {
    name: "ui_navigate_portal",
    description: "Switch the KubeOrbit portal view and optional namespace filter.",
    parameters: {
      type: "object",
      properties: {
        view: { type: "string" },
        namespace: { type: "string" },
      },
      required: ["view"],
    },
  },
  {
    name: "ui_filter_12step_machine",
    description: "Open the 12-Step kubectl apply Machine and filter by namespace/step.",
    parameters: {
      type: "object",
      properties: {
        namespace: { type: "string" },
        step_number: { type: "integer" },
      },
    },
  },
  {
    name: "ui_inspect_pod",
    description: "Open the Pod Inspector modal for a specific pod.",
    parameters: {
      type: "object",
      properties: {
        pod_name: { type: "string" },
        namespace: { type: "string" },
      },
      required: ["pod_name"],
    },
  },
  {
    name: "ui_open_pod_logs",
    description: "Open the Pod Logs modal for a specific pod.",
    parameters: {
      type: "object",
      properties: {
        pod_name: { type: "string" },
        namespace: { type: "string" },
      },
      required: ["pod_name"],
    },
  },
  {
    name: "ui_open_manifest",
    description: "Open the live YAML Manifest viewer modal for a Kubernetes object.",
    parameters: {
      type: "object",
      properties: {
        kind: { type: "string" },
        name: { type: "string" },
        namespace: { type: "string" },
      },
      required: ["kind", "name"],
    },
  },
  {
    name: "ui_propose_rollback",
    description: "Stage an interactive HITL rollback approval card (requires Operator/Admin confirmation).",
    parameters: {
      type: "object",
      properties: {
        namespace: { type: "string" },
        workload_type: { type: "string" },
        name: { type: "string" },
        reason: { type: "string" },
        to_revision: { type: "integer" },
      },
      required: ["namespace", "workload_type", "name", "reason"],
    },
  },
];

function parseSSEFrame(rawFrame) {
  if (!rawFrame) return null;
  const lines = rawFrame.split(/\r?\n/);
  const dataLines = [];
  for (const line of lines) {
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (!dataLines.length) return null;
  const payload = dataLines.join("\n").trim();
  if (!payload || payload === "[DONE]") return null;
  return JSON.parse(payload);
}

export class AGUIStreamClient {
  constructor(basePath = "/api/clusters") {
    this.basePath = basePath;
  }

  async run({
    clusterId,
    threadId = "",
    runId = "",
    state = {},
    messages = [],
    tools = PORTAL_FRONTEND_TOOLS,
    context = [],
    forwardedProps = {},
    signal = undefined,
    onEvent = () => {},
  }) {
    if (!clusterId) {
      throw new Error("clusterId is required for AG-UI run");
    }

    const headers = {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    };
    const apiKey = getStoredApiKey();
    if (apiKey) headers["X-API-Key"] = apiKey;

    const url = `${this.basePath}/${encodeURIComponent(clusterId)}/ai/ag-ui`;
    const body = JSON.stringify({
      threadId: threadId || "",
      runId: runId || `run-${Date.now().toString(36)}`,
      state: state || {},
      messages: messages || [],
      tools: tools || PORTAL_FRONTEND_TOOLS,
      context: context || [],
      forwardedProps: forwardedProps || {},
    });

    const response = await fetch(url, {
      method: "POST",
      headers,
      body,
      signal,
    });

    if (!response.ok) {
      let detail = `AG-UI request failed (${response.status})`;
      try {
        const errText = await response.text();
        if (errText) {
          const parsed = JSON.parse(errText);
          detail = parsed.detail || parsed.message || detail;
        }
      } catch {
        // ignore JSON parse failure on error body
      }
      throw new Error(detail);
    }

    if (!response.body || typeof response.body.getReader !== "function") {
      const fullText = await response.text();
      for (const frame of fullText.split(/\r?\n\r?\n/)) {
        const evt = parseSSEFrame(frame);
        if (evt) await onEvent(evt);
      }
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let boundary;
        while ((boundary = buffer.search(/\r?\n\r?\n/)) !== -1) {
          const rawFrame = buffer.slice(0, boundary);
          const sepMatch = buffer.slice(boundary).match(/^\r?\n\r?\n/);
          const sepLen = sepMatch ? sepMatch[0].length : 2;
          buffer = buffer.slice(boundary + sepLen);
          const evt = parseSSEFrame(rawFrame);
          if (evt) await onEvent(evt);
        }
      }
      buffer += decoder.decode();
      if (buffer.trim()) {
        for (const frame of buffer.split(/\r?\n\r?\n/)) {
          const evt = parseSSEFrame(frame);
          if (evt) await onEvent(evt);
        }
      }
    } finally {
      try {
        reader.releaseLock();
      } catch {
        // ignore if already released/cancelled
      }
    }
  }
}
