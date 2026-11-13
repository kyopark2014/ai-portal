import type { ToolEvent } from "./api";
import Markdown from "./Markdown";
import { ToolCallCard } from "./ToolCallCard";

interface Props { role: "user" | "assistant"; content: string; toolEvents?: ToolEvent[] }

const bubble = (role: "user" | "assistant", text: string, key?: string | number) =>
  role === "assistant"
    ? <div key={key} className="message-bubble"><Markdown>{text}</Markdown></div>
    : <div key={key} className="message-bubble">{text}</div>;

/**
 * cde-pilot 과 같은 표시: 사용자는 오른쪽의 회색 말풍선, agent 응답은 말풍선 없이 본문 폭으로 그린다.
 * agent 턴은 텍스트 · 도구 호출 · 도구 결과를 시간순으로 이어서 보여 준다.
 * 도구 카드는 tool_start 시점에 올라오고(tool_call 로 입력 upsert), harness-work 와 같은 타이밍이다.
 */
export function MessageBubble({ role, content, toolEvents = [] }: Props) {
  const hasText = toolEvents.some((e) => e.type === "text" && (e.data ?? "").trim());
  if (hasText || toolEvents.length > 0) {
    return (
      <div className={`message-row ${role}`}>
        <div className="message-timeline">
          {toolEvents.map((e, i) => e.type === "text"
            ? ((e.data ?? "").trim() ? bubble(role, e.data ?? "", `t${i}`) : null)
            : <ToolCallCard key={`${e.type}-${e.toolUseId ?? i}`} event={e} />)}
        </div>
        {!hasText && content.trim() && bubble(role, content)}
      </div>);
  }
  return <div className={`message-row ${role}`}>{content.trim() && bubble(role, content)}</div>;
}
