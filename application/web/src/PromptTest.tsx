import { useEffect, useRef, useState } from "react";
import * as api from "./api";
import type { ChatMsg, GwModel, Item } from "./api";
import Markdown from "./Markdown";

const PREFERRED = ["claude-sonnet-5-5", "claude-sonnet-4-6", "claude-opus-5-5"];  // 이 Gateway에서 실제 호출이 되는 모델

export default function PromptTest({ item }: { item: Item }) {
  const [models, setModels] = useState<GwModel[]>([]);
  const [model, setModel] = useState("");
  const [vals, setVals] = useState<Record<string, string>>({});
  const [msgs, setMsgs] = useState<(ChatMsg & { meta?: string })[]>([]);
  const [input, setInput] = useState("");
  const [maxTokens, setMaxTokens] = useState(1024);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const abort = useRef<AbortController | null>(null);
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api.getGatewayModels().then((r) => {
      setModels(r.models);
      const ok = r.models.filter((m) => m.usable);
      setModel((ok.find((m) => PREFERRED.includes(m.id)) ?? ok[0])?.id ?? "");
    }).catch((e) => setErr(e.message));
    return () => abort.current?.abort();
  }, []);
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [msgs]);

  const vars: string[] = item.data.variables ?? [];
  const missing = vars.filter((v) => !(vals[v] ?? "").trim());

  async function send() {
    const text = input.trim();
    if (!text || busy || !model) return;
    setErr(""); setInput(""); setBusy(true);
    const history: ChatMsg[] = [...msgs.map(({ role, content }) => ({ role, content })), { role: "user", content: text }];
    setMsgs([...history, { role: "assistant", content: "" }]);
    const ctl = new AbortController(); abort.current = ctl;
    const patch = (fn: (m: ChatMsg & { meta?: string }) => ChatMsg & { meta?: string }) =>
      setMsgs((cur) => cur.map((m, i) => (i === cur.length - 1 ? fn(m) : m)));
    try {
      await api.streamChat({ model, messages: history, prompt_id: item.id, values: vals, max_tokens: maxTokens },
        (e) => {
          if (e.t) patch((m) => ({ ...m, content: m.content + e.t }));
          if (e.error) setErr(e.error);
          if (e.done) patch((m) => ({ ...m, meta: `${model} · ${e.ms ?? 0}ms${e.usage ? ` · in ${e.usage.input_tokens ?? "?"} / out ${e.usage.output_tokens ?? e.usage.completion_tokens ?? "?"}` : ""}` }));
        }, ctl.signal);
    } catch (e: any) {
      if (e.name !== "AbortError") { setErr(e.message); setMsgs(history); setInput(text); }
    }
    setBusy(false); abort.current = null;
  }

  return (
    <div className="ptest">
      <div className="pt-opts">
        <label className="field"><span className="lbl">모델</span>
          <select value={model} onChange={(e) => setModel(e.target.value)} disabled={busy}>
            {models.length === 0 && <option value="">모델 목록 없음</option>}
            {models.map((m) => <option key={m.id} value={m.id} disabled={!m.usable}>{m.id}{m.usable ? "" : " — 지원 불가"}</option>)}
          </select></label>
        <label className="field"><span className="lbl">최대 출력 토큰</span>
          <input type="number" min={1} max={4096} value={maxTokens} onChange={(e) => setMaxTokens(Math.max(1, Math.min(4096, Number(e.target.value) || 1)))} disabled={busy} /></label>
        {vars.map((v) => (
          <label className="field" key={v}><span className="lbl">{`{{${v}}}`}</span>
            <input value={vals[v] ?? ""} onChange={(e) => setVals({ ...vals, [v]: e.target.value })} disabled={busy} /></label>))}
      </div>

      <div className={`chatbox${msgs.length ? "" : " empty"}`} aria-live="polite">
        {msgs.map((m, i) => (
          <div key={i} className={`bubble ${m.role}`} aria-label={m.role === "user" ? "내 메시지" : "모델 응답"}>
            {m.role === "assistant" && m.content
              ? <Markdown>{m.content}</Markdown>
              : <div className="txt">{m.content || (busy && i === msgs.length - 1 ? "…" : "")}</div>}
            {m.meta && <span className="meta">{m.meta}</span>}
          </div>))}
        <div ref={end} />
      </div>

      {err && <p className="note error" role="alert">{err}</p>}
      {missing.length > 0 && <p className="hint">변수 {missing.map((v) => `{{${v}}}`).join(", ")} 값이 비어 있으면 그대로 전달됩니다.</p>}
      <div className="composer">
        <textarea rows={2} placeholder="이 prompt를 system 메시지로 적용해 대화합니다 (Enter 전송, Shift+Enter 줄바꿈)" value={input} aria-label="메시지"
          onChange={(e) => setInput(e.target.value)} disabled={busy}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); } }} />
        <div className="col">
          {busy ? <button type="button" className="btn ghost" onClick={() => abort.current?.abort()}>중단</button>
                : <button type="button" className="btn" disabled={!input.trim() || !model} onClick={send}>보내기</button>}
          <button type="button" className="btn ghost" disabled={busy || msgs.length === 0} onClick={() => { setMsgs([]); setErr(""); }}>새 대화</button>
        </div>
      </div>
    </div>
  );
}
