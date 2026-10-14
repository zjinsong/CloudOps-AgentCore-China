// AG-UI SSE 客户端:POST /api/invocations,解析 data: {...} 事件流
export interface AguiHandlers {
  onText?: (delta: string) => void;
  onReasoning?: (delta: string) => void;
  onToolStart?: (name: string, id: string) => void;
  onToolResult?: (id: string, content: string) => void;
  onRunFinished?: () => void;
  onError?: (msg: string) => void;
}

export async function runAgent(prompt: string, handlers: AguiHandlers): Promise<void> {
  const threadId = "t-" + Date.now();
  const runId = "r-" + Date.now();
  const body = {
    threadId,
    runId,
    messages: [{ id: "m-" + Date.now(), role: "user", content: prompt }],
    tools: [],
    context: [],
    state: {},
    forwardedProps: {},
  };

  const resp = await fetch("/api/invocations", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
  });
  if (!resp.ok || !resp.body) {
    handlers.onError?.(`HTTP ${resp.status}`);
    return;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop() || "";
    for (const line of lines) {
      const t = line.trim();
      if (!t.startsWith("data:")) continue;
      const payload = t.slice(5).trim();
      if (!payload) continue;
      let ev: any;
      try {
        ev = JSON.parse(payload);
      } catch {
        continue;
      }
      switch (ev.type) {
        case "TEXT_MESSAGE_CONTENT":
          if (ev.delta) handlers.onText?.(ev.delta);
          break;
        case "REASONING_MESSAGE_CONTENT":
          if (ev.delta) handlers.onReasoning?.(ev.delta);
          break;
        case "TOOL_CALL_START":
          handlers.onToolStart?.(ev.toolCallName || ev.toolCallId, ev.toolCallId);
          break;
        case "TOOL_CALL_RESULT":
          handlers.onToolResult?.(ev.toolCallId, ev.content || "");
          break;
        case "RUN_FINISHED":
          handlers.onRunFinished?.();
          break;
        case "RUN_ERROR":
          handlers.onError?.(ev.message || "run error");
          break;
      }
    }
  }
}
