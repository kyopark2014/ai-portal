import { useEffect, useState } from "react";
import * as api from "./api";
import { DetailModal } from "./Modals";

/** "에이전트 생성" 탭: 이름 → 모델·프롬프트 → MCP → Skill → 인증 → 생성 (AgentCore harness). */
const NAME_RE = /^[A-Za-z][A-Za-z0-9_]{0,39}$/;
const STEPS = ["이름", "모델·프롬프트", "MCP", "Skill", "인증"] as const;

function Pick({ items, sel, toggle, empty, sub }: {
  items: api.Item[]; sel: number[]; toggle: (id: number) => void; empty: string; sub: (i: api.Item) => string;
}) {
  if (items.length === 0) return <p className="hint">{empty}</p>;
  return (
    <ul className="pick" style={{ listStyle: "none", padding: 0, margin: 0, display: "grid", gap: ".4rem" }}>
      {items.map((i) => (
        <li key={i.id}><label style={{ display: "flex", gap: ".6rem", alignItems: "flex-start", cursor: "pointer" }}>
          <input type="checkbox" checked={sel.includes(i.id)} onChange={() => toggle(i.id)} />
          <span><b>{i.name}</b> <span className="hint">{sub(i)}</span>
            {i.description && <span className="hint" style={{ display: "block" }}>{i.description.slice(0, 120)}</span>}</span>
        </label></li>
      ))}
    </ul>
  );
}

export default function AgentCreate({ user }: { user: api.User }) {
  const [step, setStep] = useState(0);
  const [name, setName] = useState("");
  const [models, setModels] = useState<api.GwModel[]>([]);
  const [model, setModel] = useState("");
  const [mode, setMode] = useState<"text" | "registry">("text");
  const [text, setText] = useState("");
  const [promptId, setPromptId] = useState<number | null>(null);
  const [prompts, setPrompts] = useState<api.Item[]>([]);
  const [mcps, setMcps] = useState<api.Item[]>([]);
  const [skills, setSkills] = useState<api.Item[]>([]);
  const [mcpSel, setMcpSel] = useState<number[]>([]);
  const [skillSel, setSkillSel] = useState<number[]>([]);
  const [authKind, setAuthKind] = useState<"IAM" | "JWT">("IAM");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [loadErr, setLoadErr] = useState("");
  const [created, setCreated] = useState<api.Item | null>(null);

  useEffect(() => {
    api.getAgentModels().then((r) => {
      const ok = r.models.filter((m) => m.usable);
      setModels(ok); setModel((cur) => cur || (ok.find((m) => m.id.endsWith("claude-sonnet-5-5")) ?? ok[0])?.id || "");
    }).catch((e) => setLoadErr(`모델 목록: ${e.message}`));
    api.listItems("prompt").then(setPrompts).catch(() => {});
    api.listItems("mcp").then((l) => setMcps(l.filter((i) =>
      (i.usable || i.owner === user.email) && !(i.data.kind === "gateway" && i.data.authorizer === "CUSTOM_JWT")))).catch(() => {});
    api.listItems("skill").then(setSkills).catch(() => {});
  }, [user.email]);

  const toggle = (set: (f: (p: number[]) => number[]) => void) => (id: number) =>
    set((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));
  const nameOk = NAME_RE.test(name);
  const promptOk = mode === "text" ? true : promptId !== null;
  const canNext = step === 0 ? nameOk : step === 1 ? !!model && promptOk : true;

  async function submit() {
    setBusy(true); setErr("");
    try {
      setCreated(await api.createAgent({
        name, model, system_prompt: mode === "text" ? text : "", prompt_id: mode === "registry" ? promptId : null,
        mcp_ids: mcpSel, skill_ids: skillSel, auth: authKind,
      }));
    } catch (e: any) { setErr(e.message); }
    setBusy(false);
  }
  function reset() {
    setStep(0); setName(""); setText(""); setPromptId(null); setMode("text"); setMcpSel([]); setSkillSel([]);
    setAuthKind("IAM"); setErr(""); setCreated(null);
  }

  return (
    <section className="card">
      <h2>에이전트 생성</h2>
      <ol className="steps" style={{ listStyle: "none", padding: 0, display: "flex", gap: ".5rem", flexWrap: "wrap" }} aria-label="진행 단계">
        {STEPS.map((s, i) => (
          <li key={s} aria-current={i === step ? "step" : undefined}
              className={i === step ? "badge" : "hint"} style={{ opacity: i <= step ? 1 : .6 }}>{i + 1}. {s}</li>
        ))}
      </ol>
      {loadErr && <p className="note error" role="alert">{loadErr}</p>}

      {step === 0 && (
        <label className="field"><span className="lbl">에이전트 이름</span>
          <span className="hint">영문으로 시작하고 a-z, A-Z, 0-9, _ (underscore) 만 사용할 수 있습니다 (최대 40자)</span>
          <input value={name} maxLength={40} autoFocus onChange={(e) => setName(e.target.value)} placeholder="my_agent" />
          {name && !nameOk && <span className="hint" role="alert">사용할 수 없는 이름입니다. a-z, A-Z, 0-9, _ 만, 첫 글자는 영문.</span>}
        </label>
      )}

      {step === 1 && (<>
        <label className="field"><span className="lbl">모델 <span className="hint">Amazon Bedrock</span></span>
          <select value={model} onChange={(e) => setModel(e.target.value)}>
            {models.length === 0 && <option value="">모델 목록 없음</option>}
            {models.map((m) => <option key={m.id} value={m.id}>{m.id}</option>)}
          </select></label>
        <div className="field"><span className="lbl">시스템 프롬프트</span>
          <div style={{ display: "flex", gap: "1rem" }}>
            <label><input type="radio" checked={mode === "text"} onChange={() => setMode("text")} /> 직접 입력</label>
            <label><input type="radio" checked={mode === "registry"} onChange={() => setMode("registry")} /> 레지스트리의 프롬프트</label>
          </div>
          {mode === "text"
            ? <textarea rows={6} maxLength={20000} value={text} onChange={(e) => setText(e.target.value)} placeholder="에이전트의 역할과 지침 (비워도 됩니다)" />
            : prompts.length === 0 ? <p className="hint">선택할 수 있는 프롬프트가 없습니다.</p>
            : <select value={promptId ?? ""} onChange={(e) => setPromptId(e.target.value ? Number(e.target.value) : null)}>
                <option value="">프롬프트를 선택하세요</option>
                {prompts.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>}
        </div>
      </>)}

      {step === 2 && (<>
        <p className="sub">사용할 수 있는 MCP 를 하나 이상 선택합니다 (선택 사항). 사용 신청이 승인된 MCP 와 내가 등록한 MCP 만 보입니다.</p>
        <Pick items={mcps} sel={mcpSel} toggle={toggle(setMcpSel)} empty="사용 가능한 MCP 가 없습니다."
              sub={(i) => i.data.kind === "gateway" ? `Gateway · ${i.data.authorizer}` : "MCP 서버"} />
      </>)}

      {step === 3 && (<>
        <p className="sub">레지스트리의 Skill 을 하나 이상 선택합니다 (선택 사항).</p>
        <Pick items={skills} sel={skillSel} toggle={toggle(setSkillSel)} empty="선택할 수 있는 Skill 이 없습니다."
              sub={(i) => (i.data.source_type ?? "").toUpperCase()} />
      </>)}

      {step === 4 && (<>
        <div className="field"><span className="lbl">인증</span>
          <label style={{ display: "block", marginBottom: ".35rem" }}><input type="radio" checked={authKind === "IAM"} onChange={() => setAuthKind("IAM")} /> IAM <span className="hint">AWS 자격 증명(SigV4)으로 호출</span></label>
          <label style={{ display: "block" }}><input type="radio" checked={authKind === "JWT"} onChange={() => setAuthKind("JWT")} /> JWT Tokens <span className="hint">Cognito 가 발급한 토큰으로 호출</span></label>
        </div>
        <dl className="kv">
          <div><dt>이름</dt><dd className="mono">{name}</dd></div><div><dt>모델</dt><dd className="mono">{model}</dd></div>
          <div><dt>프롬프트</dt><dd>{mode === "registry" ? prompts.find((p) => p.id === promptId)?.name : text.trim() ? `직접 입력 (${text.trim().length}자)` : "없음"}</dd></div>
          <div><dt>MCP</dt><dd>{mcps.filter((m) => mcpSel.includes(m.id)).map((m) => m.name).join(", ") || "없음"}</dd></div>
          <div><dt>Skill</dt><dd>{skills.filter((m) => skillSel.includes(m.id)).map((m) => m.name).join(", ") || "없음"}</dd></div>
        </dl>
      </>)}

      {err && <p className="note error" role="alert">{err}</p>}
      <div className="actions" style={{ marginTop: "1rem" }}>
        {step > 0 && <button className="btn ghost" disabled={busy} onClick={() => setStep(step - 1)}>이전</button>}
        {step < STEPS.length - 1
          ? <button className="btn" disabled={!canNext} onClick={() => setStep(step + 1)}>다음</button>
          : <button className="btn" disabled={busy} onClick={submit}>{busy ? "생성 중…" : "생성"}</button>}
      </div>

      {created && <DetailModal item={created} user={user} onClose={reset}
        onChanged={setCreated} onDeleted={reset} />}
    </section>
  );
}
