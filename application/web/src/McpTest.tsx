import { useEffect, useRef, useState } from "react";
import * as api from "./api";
import type { Item, McpEvent } from "./api";
import Markdown from "./Markdown";

// 한 턴을 "텍스트 / 도구 호출 / 도구 결과" 의 시간순 목록으로 둔다. 도구와 텍스트를 따로 모으면
// 중간 답변이 전부 맨 아래로 밀려 도구 호출과의 선후 관계가 사라진다(harness-work 의 tool_events 와 같은 방식).
type Part = { kind: "text"; text: string } | { kind: "call"; name?: string; text: string } | { kind: "result"; text: string };
interface Turn { role: "user" | "assistant"; content: string; parts?: Part[]; meta?: string }

/** 스트림 조각을 시간순 목록 끝에 반영한다. 같은 종류가 연달아 오면 이어 붙이고, 종류가 바뀌면 새 항목을 만든다. */
function pushPart(parts: Part[], next: Part): Part[] {
  const tail = parts[parts.length - 1];
  if (tail && tail.kind === next.kind && next.kind !== "call") {      // 텍스트·결과는 조각으로 오므로 이어 붙인다
    return [...parts.slice(0, -1), { ...tail, text: tail.text + next.text } as Part];
  }
  return [...parts, next];
}

const DEFAULT_PROMPT = "harness_prompt";
const newSession = () => (crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`).replace(/[^A-Za-z0-9_-]/g, "") .padEnd(36, "0");

/** 선택한 MCP 를 harness agent 의 도구로 붙여 대화하며 도구 호출 과정을 보여준다. */
export default function McpTest({ item }: { item: Item }) {
  const d = item.data;
  const isAgent = item.kind === "agent";
  const isSkill = item.kind === "skill";
  const unsupported = !isAgent && d.kind === "gateway" && d.authorizer !== "AWS_IAM";
  const [ready, setReady] = useState<boolean | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [status, setStatus] = useState("");
  // agent 테스트 중 runtime 은 system prompt 를 받지 않는다 — harness 에만 적용
  const usesPrompt = !isAgent || d.agent_type === "harness";
  const [prompts, setPrompts] = useState<Item[]>([]);
  const [pid, setPid] = useState<number | "">("");
  const session = useRef(newSession());
  const abort = useRef<AbortController | null>(null);
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (isAgent) setReady(true); else api.mcpTestStatus().then((s) => setReady(s.ready)).catch(() => setReady(false));
    return () => abort.current?.abort();
    if (usesPrompt) api.listItems("prompt").then(setPrompts).catch(() => {});
  }, [isAgent]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [turns]);

  async function send() {
    const text = input.trim();
    if (!text || busy) return;
    setErr(""); setInput(""); setBusy(true);
    const history = [...turns.map(({ role, content }) => ({ role, content })), { role: "user" as const, content: text }];
    setTurns([...turns, { role: "user", content: text }, { role: "assistant", content: "", parts: [] }]);
    const last = (fn: (t: Turn) => Turn) => setTurns((cur) => cur.map((t, i) => (i === cur.length - 1 ? fn(t) : t)));
    const ctl = new AbortController(); abort.current = ctl;
    const onEvent = (e: McpEvent) => {
      if (e.status) setStatus(e.status);
      if (e.t) { setStatus(""); last((t) => ({ ...t, content: t.content + e.t, parts: pushPart(t.parts ?? [], { kind: "text", text: e.t! }) })); }
      if (e.tool_call) last((t) => ({ ...t, parts: pushPart(t.parts ?? [], { kind: "call", name: e.tool_call, text: e.input ?? "" }) }));
      if (e.tool_result !== undefined) last((t) => ({ ...t, parts: pushPart(t.parts ?? [], { kind: "result", text: e.tool_result! }) }));
      if (e.error) setErr(e.error);
      if (e.done) last((t) => ({ ...t, meta: e.usage ? `in ${e.usage.input_tokens ?? "?"} / out ${e.usage.output_tokens ?? "?"}` : "" }));
    };
    try { await (isAgent ? api.streamAgentChat : isSkill ? api.streamSkillChat : api.streamMcpChat)(item.id, { messages: history, session_id: session.current, prompt_id: pid === "" ? null : pid }, onEvent, ctl.signal); }
    catch (x: any) { if (x.name !== "AbortError") { setErr(x.message); setTurns(turns); setInput(text); } }
    setStatus(""); setBusy(false); abort.current = null;
  }

  if (unsupported) return <p className="hint">{d.authorizer} 인증 Gateway는 테스트할 수 없습니다 (AWS_IAM Gateway와 직접 등록한 MCP만 지원).</p>;
  if (ready === false) return <p className="hint">테스트용 harness agent가 없습니다. <code>python installer.py --harness-only</code>로 만든 뒤 서버를 재시작하세요.</p>;

  return (
    <div className="ptest">
      <p className="hint">{isAgent ? "이 agent를 호출해 프롬프트에 대한 응답과 도구 사용 과정을 확인합니다."
        : isSkill ? <>harness agent <code>ai_portal</code>이 이 skill을 불러와 사용합니다. skill이 다루는 작업을 요청해 보세요. 예: “사용 가능한 skill을 알려줘”</>
        : <>harness agent <code>ai_portal</code>이 이 MCP를 도구로 사용합니다. 예: “사용 가능한 도구를 알려줘”</>}</p>
      <div className={`chatbox${turns.length ? "" : " empty"}`} aria-live="polite">
        {turns.map((t, i) => (
          <div key={i} className={`bubble ${t.role}`} aria-label={t.role === "user" ? "내 메시지" : "agent 응답"}>
            {t.role === "assistant" ? (
              <>
                {(t.parts ?? []).map((p, j) => p.kind === "text"
                  ? <Markdown key={j}>{p.text}</Markdown>
                  : (<details key={j} className={`step ${p.kind}`}>
                      <summary>{p.kind === "call" ? `도구 호출 · ${p.name}` : "도구 결과"}</summary>
                      <pre className="code">{p.text}</pre>
                    </details>))}
                {!(t.parts?.length) && <div className="txt">{busy && i === turns.length - 1 ? "…" : ""}</div>}
              </>
            ) : <div className="txt">{t.content}</div>}
            {t.meta && <span className="meta">{t.meta}</span>}
          </div>))}
        <div ref={end} />
      </div>
      {usesPrompt && (
        <label className="field"><span className="lbl">System prompt</span>
          <select value={pid} onChange={(e) => setPid(e.target.value ? Number(e.target.value) : "")} disabled={busy}>
            <option value="">{prompts.some((p) => p.name === DEFAULT_PROMPT) ? `기본 (${DEFAULT_PROMPT})` : "기본 (harness 기본값)"}</option>
            {prompts.filter((p) => p.name !== DEFAULT_PROMPT).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select></label>)}
      {status && <p className="hint" role="status">{status}</p>}
      {err && <p className="note error" role="alert">{err}</p>}
      <div className="composer">
        <textarea rows={2} placeholder={isAgent ? "agent에게 보낼 프롬프트를 입력하세요 (Enter 전송, Shift+Enter 줄바꿈)" : isSkill ? "skill을 사용하는 요청을 입력하세요 (Enter 전송, Shift+Enter 줄바꿈)" : "MCP 도구를 사용하는 요청을 입력하세요 (Enter 전송, Shift+Enter 줄바꿈)"} value={input} aria-label="메시지"
          onChange={(e) => setInput(e.target.value)} disabled={busy || ready === null}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); } }} />
        <div className="col">
          {busy ? <button type="button" className="btn ghost" onClick={() => abort.current?.abort()}>중단</button>
                : <button type="button" className="btn" disabled={!input.trim() || ready !== true} onClick={send}>보내기</button>}
          <button type="button" className="btn ghost" disabled={busy || turns.length === 0}
            onClick={() => { setTurns([]); setErr(""); session.current = newSession(); }}>새 대화</button>
        </div>
      </div>
    </div>
  );
}
