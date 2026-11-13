import type { ToolEvent } from "./api";
import { McpAppFrame } from "./McpAppFrame";

function formatInput(input: unknown): string {
  if (input === undefined || input === null || input === "") return "(매개변수 없음)";
  if (input === "…") return "…";
  if (typeof input === "object" && !Array.isArray(input) && Object.keys(input as object).length === 0) {
    return "(매개변수 없음 — 기본값 사용)";
  }
  return typeof input === "string" ? input : JSON.stringify(input, null, 2);
}

/** 결과 payload 를 읽기 쉽게: [{type:"text",text}] 는 본문만, JSON 은 들여쓰기, 그 외는 이스케이프된 줄바꿈을 푼다. */
function formatPayload(data: string | undefined): string {
  if (!data) return "";
  try {
    const parsed = JSON.parse(data) as unknown;
    if (Array.isArray(parsed)) {
      const texts = parsed
        .filter(
          (b): b is { type: string; text: string } =>
            !!b &&
            typeof b === "object" &&
            (b as { type?: unknown }).type === "text" &&
            typeof (b as { text?: unknown }).text === "string",
        )
        .map((b) => b.text);
      if (texts.length > 0) return texts.join("\n\n");
    }
    return JSON.stringify(parsed, null, 2);
  } catch {
    return data.replace(/\\n/g, "\n").replace(/\\t/g, "\t");
  }
}

/** Gateway 의 `server___Tool` 형태면 서버 이름을 괄호로 덧붙인다. */
function serverOf(tool?: string): string | undefined {
  if (!tool || !tool.includes("___")) return undefined;
  return tool.split("___")[0]?.trim() || undefined;
}

export function ToolCallCard({ event }: { event: ToolEvent }) {
  const srv = serverOf(event.tool);
  if (event.type === "tool") {
    return (
      <details className="tool-card">
        <summary>{event.tool ? `Tools: ${event.tool}${srv ? ` (${srv})` : ""}` : "Tools"}</summary>
        <pre>{formatInput(event.input)}</pre>
      </details>
    );
  }
  if (event.type === "tool_result") {
    const hasApp = Boolean(event.mcpApp?.html);
    return (
      <div className="tool-card-stack">
        {hasApp && event.mcpApp ? <McpAppFrame app={event.mcpApp} /> : null}
        <details className="tool-card">
          <summary>
            {event.tool ? `Tool result: ${event.tool}${srv ? ` (${srv})` : ""}` : "Tool result"}
          </summary>
          <pre>{formatPayload(event.data)}</pre>
        </details>
      </div>
    );
  }
  return (
    <details className="tool-card">
      <summary>Info</summary>
      <pre>{formatPayload(event.data)}</pre>
    </details>
  );
}
