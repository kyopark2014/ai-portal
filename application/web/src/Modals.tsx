import { useEffect, useRef, useState, type ReactNode } from "react";
import * as api from "./api";
import type { Item, Kind, RegistryInfo, User } from "./api";
import ConfirmDialog, { type ConfirmSpec } from "./ConfirmDialog";
import McpTest from "./McpTest";
import { statusText } from "./Explore";
import PromptTest from "./PromptTest";
import ReviewerPicker from "./ReviewerPicker";

function Modal({ title, onClose, children, wide }: { title: string; onClose: () => void; children: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    ref.current?.focus();
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => { window.removeEventListener("keydown", k); prev?.focus(); };
  }, [onClose]);
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal ${wide ? "wide" : ""}`} role="dialog" aria-modal="true" aria-label={title} tabIndex={-1} ref={ref}>
        <div className="modal-head"><h2>{title}</h2><button className="x" aria-label="닫기" onClick={onClose}>×</button></div>
        {children}
      </div>
    </div>
  );
}

function Field({ label, hint, children, count }: { label: string; hint?: string; children: ReactNode; count?: string }) {
  return (
    <label className="field"><span className="lbl">{label}</span>
      {hint && <span className="hint">{hint}</span>}{children}{count && <span className="hint">{count}</span>}</label>
  );
}

const MODELS = ["", "anthropic.claude-sonnet-4-5-20250929-v1:0", "us.anthropic.claude-haiku-4-5-20251001-v1:0", "amazon.nova-pro-v1:0"];

export function CreateModal({ kind, onClose, onDone }: { kind: Kind; onClose: () => void; onDone: (r: Item) => void }) {
  const [f, setF] = useState<Record<string, any>>({ headers: [], source_type: "git", agent_type: "custom" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const set = (k: string, v: any) => setF((p) => ({ ...p, [k]: v }));
  const path = { prompt: "prompts", mcp: "mcp", agent: "agents", skill: "skills" }[kind];

  async function submit(e: React.FormEvent) {
    e.preventDefault(); setBusy(true); setErr("");
    try {
      const body: any = { ...f };
      if (kind === "mcp") body.headers = (f.headers as any[]).filter((h) => h.key.trim());
      onDone(await api.create(path, body));
    } catch (x: any) { setErr(x.message); }
    setBusy(false);
  }

  const title = { prompt: "Prompt 등록", mcp: "MCP 서버 등록", agent: "Agent 등록", skill: "Skill 등록" }[kind];
  return (
    <Modal title={title} onClose={onClose}>
      <form onSubmit={submit} className="form">
        <Field label="이름" hint={kind === "prompt" ? "영문, 숫자, _ - 만 사용" : undefined}>
          <input required value={f.name ?? ""} onChange={(e) => set("name", e.target.value)} maxLength={100} />
        </Field>

        {kind === "prompt" && (<>
          <Field label="설명 (선택)"><input value={f.description ?? ""} onChange={(e) => set("description", e.target.value)} /></Field>
          <Field label="System prompt" hint="변수는 {{name}} 형태로 쓰면 자동 인식됩니다." count={`${(f.text ?? "").length}/20000 characters`}>
            <textarea required rows={8} maxLength={20000} value={f.text ?? ""} placeholder="You are a helpful assistant." onChange={(e) => set("text", e.target.value)} />
          </Field>
          <Field label="모델 (선택)"><select value={f.model_id ?? ""} onChange={(e) => set("model_id", e.target.value)}>
            {MODELS.map((m) => <option key={m} value={m}>{m || "지정 안 함"}</option>)}</select></Field>
          <p className="hint">등록하면 Amazon Bedrock Prompt Management에 저장하고, 버전을 게시해 Registry에도 한 번에 등록합니다.</p>
        </>)}

        {kind === "mcp" && (<>
          <Field label="설명 (선택)"><input value={f.description ?? ""} onChange={(e) => set("description", e.target.value)} /></Field>
          <Field label="Endpoint URL" hint="원격 MCP 서버의 URL" count={`${(f.endpoint ?? "").length}/16383 characters`}>
            <input required type="url" placeholder="https://…/mcp" value={f.endpoint ?? ""} onChange={(e) => set("endpoint", e.target.value)} maxLength={16383} />
          </Field>
          <fieldset className="grp"><legend>HTTP headers <em>optional</em></legend>
            <p className="hint">API 키나 bearer token 같은 인증 정보. 저장 후에는 값이 마스킹되어 표시됩니다.</p>
            {(f.headers as any[]).map((h, i) => (
              <div className="row" key={i}>
                <input aria-label="헤더 이름" placeholder="Authorization" value={h.key} onChange={(e) => set("headers", f.headers.map((x: any, j: number) => j === i ? { ...x, key: e.target.value } : x))} />
                <input aria-label="헤더 값" type="password" autoComplete="off" placeholder="Bearer …" value={h.value} onChange={(e) => set("headers", f.headers.map((x: any, j: number) => j === i ? { ...x, value: e.target.value } : x))} />
                <button type="button" className="btn ghost" onClick={() => set("headers", f.headers.filter((_: any, j: number) => j !== i))}>삭제</button>
              </div>))}
            <button type="button" className="btn ghost" onClick={() => set("headers", [...f.headers, { key: "", value: "" }])}>Add new header</button>
          </fieldset>
          <p className="hint">등록하면 Registry에도 함께 등록됩니다. 사용 승인 절차는 추후 구현됩니다.</p>
        </>)}

        {kind === "skill" && (<>
          <div className="seg wide" role="group" aria-label="소스 종류">
            <button type="button" aria-pressed={f.source_type === "git"} className={f.source_type === "git" ? "on" : ""} onClick={() => set("source_type", "git")}>Git</button>
            <button type="button" aria-pressed={f.source_type === "s3"} className={f.source_type === "s3" ? "on" : ""} onClick={() => set("source_type", "s3")}>S3</button>
          </div>
          {f.source_type === "git" ? (<>
            <Field label="Git URL"><input required type="url" placeholder="https://github.com/org/repo" value={f.git_url ?? ""} onChange={(e) => set("git_url", e.target.value)} /></Field>
            <div className="row2">
              <Field label="Subdirectory (선택)"><input value={f.subdirectory ?? ""} onChange={(e) => set("subdirectory", e.target.value)} /></Field>
              <Field label="User name (선택)"><input value={f.git_username ?? ""} onChange={(e) => set("git_username", e.target.value)} /></Field>
            </div>
            <Field label="Credential provider ARN (비공개 repo 필수)"><input value={f.credential_provider_arn ?? ""} onChange={(e) => set("credential_provider_arn", e.target.value)} /></Field>
          </>) : (<>
            <Field label="S3 URI"><input required placeholder="s3://bucket/path/skill.zip" value={f.s3_uri ?? ""} onChange={(e) => set("s3_uri", e.target.value)} /></Field>
            <Field label="Object version (선택)"><input value={f.object_version ?? ""} onChange={(e) => set("object_version", e.target.value)} /></Field>
          </>)}
          <Field label="설명" hint="비워 두면 Git/S3 정보가 기본 설명으로 들어갑니다. 등록 후에도 수정할 수 있습니다.">
            <textarea rows={3} value={f.description ?? ""} placeholder={f.source_type === "git" ? `GIT skill: ${f.git_url ?? ""}` : `S3 skill: ${f.s3_uri ?? ""}`} onChange={(e) => set("description", e.target.value)} />
          </Field>
        </>)}

        {kind === "agent" && (<>
          <p className="hint">AgentCore harness·runtime은 로그인 시 자동으로 가져옵니다. 여기서는 그 외 agent를 직접 추가합니다.</p>
          <Field label="종류"><select value={f.agent_type} onChange={(e) => set("agent_type", e.target.value)}>
            <option value="custom">custom</option><option value="harness">harness</option><option value="runtime">runtime</option></select></Field>
          <Field label="ARN (선택)"><input value={f.arn ?? ""} onChange={(e) => set("arn", e.target.value)} /></Field>
          <Field label="Invoke URL (선택)"><input type="url" value={f.invoke_url ?? ""} onChange={(e) => set("invoke_url", e.target.value)} /></Field>
          <Field label="설명"><textarea rows={3} value={f.description ?? ""} onChange={(e) => set("description", e.target.value)} /></Field>
        </>)}

        {err && <p className="note error" role="alert">{err}</p>}
        <div className="actions"><button type="button" className="btn ghost" onClick={onClose}>취소</button>
          <button className="btn" disabled={busy}>{busy ? "등록 중…" : "등록"}</button></div>
      </form>
    </Modal>
  );
}

export function DetailModal({ item, user, onClose, onChanged, onDeleted }: {
  item: Item; user: User; onClose: () => void; onChanged: (i: Item) => void; onDeleted: () => void;
}) {
  const [desc, setDesc] = useState(item.description);
  const [msg, setMsg] = useState(""); const [err, setErr] = useState(""); const [busy, setBusy] = useState(false);
  const [out, setOut] = useState("");
  const [reg, setReg] = useState<RegistryInfo | null>(null);
  const [ask, setAsk] = useState<ConfirmSpec | null>(null);
  const [pickReviewer, setPickReviewer] = useState(false);
  const [usage, setUsage] = useState<api.UsageState | null>(null);
  const d = item.data;

  useEffect(() => { api.registryInfo().then(setReg).catch(() => setReg({ registry_id: "", name: "", status: "", authorizer: "", auto_approval: null })); }, []);

  async function run<T>(fn: () => Promise<T>, ok?: (r: T) => void) {
    setBusy(true); setErr(""); setMsg("");
    try { const r = await fn(); ok?.(r); } catch (e: any) { setErr(e.message); }
    setBusy(false);
  }
  const upd = (i: Item) => { onChanged(i); setDesc(i.description); };
  const loadUsage = () => { if (item.kind === "mcp") api.myUsage(item.id).then(setUsage).catch(() => setUsage(null)); };
  useEffect(loadUsage, [item.id, item.registry_status]); // eslint-disable-line react-hooks/exhaustive-deps

  // 등록 직후 record 는 CREATING 이고 1~2초 뒤 DRAFT 가 된다. 포털 상태가 낡은 채 굳지 않도록
  // 화면이 열려 있는 동안 실제 상태를 확인해 반영한다(그래야 "승인 요청" 버튼이 나타난다).
  const transient = item.registry_status === "CREATING" || item.registry_status === "UPDATING";
  useEffect(() => {
    if (!item.registry_record_id || !transient) return;
    let alive = true, n = 0;
    const tick = async () => {
      try { const r = await api.refreshRegistry(item.kind, item.id); if (alive && r.registry_status !== item.registry_status) { upd(r); return; } } catch { /* 다음 주기에 재시도 */ }
      if (alive && ++n < 20) setTimeout(tick, 2000);
    };
    const t = setTimeout(tick, 1000);
    return () => { alive = false; clearTimeout(t); };
  }, [item.id, item.registry_record_id, item.registry_status]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <Modal title={item.name} onClose={onClose} wide>
      <div className="detail">
        <p className="by">{item.kind.toUpperCase()} · By {item.owner}{item.source === "aws" && <span className="badge">AWS 동기화</span>}</p>

        <h3>설명</h3>
        {item.can_edit ? (<div className="row">
          <textarea rows={2} value={desc} onChange={(e) => setDesc(e.target.value)} aria-label="설명" />
          <button className="btn" disabled={busy || desc === item.description} onClick={() => run(() => api.setDescription(item.kind, item.id, desc), (i) => { upd(i); setMsg("설명을 저장했습니다."); })}>저장</button>
        </div>) : <p>{item.description || "설명 없음"}</p>}

        {item.kind === "prompt" && (<>
          <h3>Prompt <span className="mono sm">{d.prompt_id} · {d.version}</span></h3>
          <pre className="code">{d.text}</pre>
          <div className="actions left">
            <button className="btn ghost" disabled={busy} onClick={() => run(() => api.refreshPrompt(item.id), (i) => { upd(i); setMsg("Bedrock에서 최신 내용을 가져왔습니다."); })}>Bedrock에서 새로고침</button>
          </div>
          <h3>테스트 <span className="hint">AWSome AI Gateway</span></h3>
          <PromptTest key={item.id + ":" + d.version + ":" + d.text.length} item={item} />
        </>)}

        {item.kind === "mcp" && usage?.applicable && (
          <div className="usage" role="region" aria-label="사용 신청">
            <h3>사용 신청</h3>
            {usage.status === "OWNER" ? <p className="hint">소유자·관리자는 신청 없이 사용할 수 있습니다.</p>
              : usage.status === "APPROVED" ? <p className="note ok">사용이 승인되었습니다. 아래에서 테스트할 수 있습니다.</p>
              : usage.status === "PENDING" ? <p className="note" role="status">사용 신청 중입니다. reviewer 가 승인하면 사용할 수 있습니다.</p>
              : (<>
                  {usage.status === "REJECTED" && <p className="note error" role="status">사용 신청이 거절되었습니다{usage.reason ? `: ${usage.reason}` : ""}. 다시 신청할 수 있습니다.</p>}
                  {!usage.registered && <p className="hint">등록이 완료된 후에 사용 신청할 수 있습니다.</p>}
                  <div className="actions left"><button type="button" className="btn" disabled={busy || !usage.can_request}
                    title={usage.can_request ? "" : "등록이 완료된 MCP 에만 신청할 수 있습니다"}
                    onClick={() => run(() => api.requestUsage(item.id), (u) => { setUsage(u); setMsg(u.status === "APPROVED" ? "사용 신청이 바로 승인되었습니다." : "사용 신청을 보냈습니다. reviewer 가 승인하면 사용할 수 있습니다."); })}>사용 신청</button></div>
                </>)}
          </div>)}

        {item.kind === "mcp" && d.kind === "gateway" && (<>
          <h3>AgentCore Gateway</h3>
          <dl className="kv">
            <div><dt>Gateway ID</dt><dd className="mono sm">{d.gateway_id}</dd></div>
            <div><dt>상태</dt><dd>{d.status}</dd></div>
            <div><dt>인증</dt><dd>{d.authorizer}</dd></div>
            <div><dt>프로토콜</dt><dd>{d.protocol}</dd></div>
            <div><dt>리전</dt><dd>{d.region}</dd></div>
            <div className="wide-row"><dt>MCP endpoint</dt><dd className="mono sm arn">{d.endpoint || "-"}</dd></div>
          </dl>
          {(usage?.can_use ?? true) && (<>
            <h3>테스트 <span className="hint">harness agent</span></h3>
            <McpTest key={item.id} item={item} /></>)}
        </>)}

        {item.kind === "mcp" && usage && !usage.can_use && <p className="hint">사용 신청이 승인되면 이 MCP 를 테스트할 수 있습니다.</p>}

        {item.kind === "mcp" && d.kind !== "gateway" && (usage?.can_use ?? true) && (<>
          <h3>Endpoint</h3><p className="mono">{d.endpoint}</p>
          {d.headers?.length > 0 && <p className="hint">헤더: {d.headers.map((h: any) => h.key).join(", ")} (값은 숨김)</p>}
          <div className="actions left"><button className="btn ghost" disabled={busy} onClick={() => run(() => api.testMcp(item.id), (r) => setOut(`initialize: HTTP ${r.status}\ntools/list: HTTP ${r.tools_status}\n\n${r.body}`))}>연결 테스트 (tools/list)</button></div>
          {out && <pre className="code out">{out}</pre>}
          <h3>테스트 <span className="hint">harness agent</span></h3>
          <McpTest key={item.id} item={item} />
        </>)}

        {item.kind === "skill" && (<><h3>소스</h3>
          <p className="mono">{d.source_type === "git" ? d.git_url : d.s3_uri}</p>
          {d.subdirectory && <p className="hint">subdirectory: {d.subdirectory}</p>}
          <h3>테스트 <span className="hint">harness agent</span></h3>
          <McpTest key={item.id} item={item} /></>)}

        {item.kind === "agent" && (<><h3>정보</h3>
          <dl className="kv"><div><dt>종류</dt><dd>{d.agent_type}</dd></div>{d.status && <div><dt>상태</dt><dd>{d.status}</dd></div>}
            {d.protocol && <div><dt>프로토콜</dt><dd>{d.protocol}</dd></div>}
            {d.arn && <div className="wide-row"><dt>ARN</dt><dd className="mono sm arn">{d.arn}</dd></div>}</dl>
          {d.arn && !(d.agent_type === "runtime" && d.protocol === "MCP") && (<>
            <h3>테스트 <span className="hint">{d.agent_type === "harness" ? "harness" : "AgentCore runtime"}</span></h3>
            <McpTest key={item.id} item={item} /></>)}
          {d.agent_type === "runtime" && d.protocol === "MCP" && <p className="hint">MCP 프로토콜 runtime은 대화형 agent가 아니라 MCP 서버입니다. MCP 탭에서 테스트하세요.</p>}
          </>)}

        <h3>Agent Registry</h3>
        {item.registry_record_id ? (<div>
          <p>Record <span className="mono sm">{item.registry_record_id}</span> <span className={`badge st-${item.registry_status}`}>{statusText(item.registry_status)}</span>
            {transient && <span className="hint"> 등록 처리 중… 완료되면 승인 요청 버튼이 나타납니다.</span>}</p>
          <div className="actions left">
            <button className="btn ghost" disabled={busy} onClick={() => run(() => api.refreshRegistry(item.kind, item.id), (i) => { upd(i); setMsg(i.registry_reason ? `상태 사유: ${i.registry_reason}` : "상태를 갱신했습니다."); })}>상태 확인</button>
            {item.can_edit && item.registry_status === "PENDING_APPROVAL" && <button className="btn danger" disabled={busy}
              onClick={() => setAsk({
                title: "승인 요청을 취소할까요?",
                lines: ["Registry의 record가 삭제되고, 다시 등록해야 승인 요청할 수 있습니다.",
                  ...(item.kind === "prompt" ? ["Prompt Management의 prompt는 그대로 유지됩니다."] : [])],
                confirmLabel: "승인 요청 취소", cancelLabel: "닫기", danger: true,
                onConfirm: () => run(() => api.cancelApproval(item.kind, item.id), (i) => { upd(i); setMsg("승인 요청을 취소했습니다. 필요하면 다시 등록하세요."); }),
              })}>승인 요청 취소</button>}
            {item.can_edit && item.registry_status === "DRAFT" && <button className="btn" disabled={busy}
              onClick={() => run(() => api.submitRegistry(item.kind, item.id), (i) => { upd(i); setMsg("승인 요청을 보냈습니다. 관리자와 이 record 의 reviewer 가 승인할 수 있습니다."); })}>승인 요청</button>}
          </div></div>
        ) : reg && !reg.registry_id ? (
          <p className="hint">연결된 Registry가 없습니다. 설치(<code>installer.py</code>)로 Registry를 먼저 만들어야 합니다.</p>
        ) : item.can_edit ? (
          <button className="btn" disabled={busy || !reg} onClick={() => run(() => api.registerItem(item.kind, item.id), (i) => { upd(i); setMsg("Registry에 DRAFT로 등록했습니다."); })}>Registry 등록 다시 시도</button>
        ) : null}

        {user.role === "admin" && item.registry_record_id && (
          <div className="actions left"><button type="button" className="btn ghost" onClick={() => setPickReviewer(true)}>Add reviewer</button>
            <span className="hint">이 record 만 승인할 수 있는 사람을 추가합니다.</span></div>)}
        {pickReviewer && <ReviewerPicker item={item} onClose={() => setPickReviewer(false)} />}



        {msg && <p className="note ok" role="status">{msg}</p>}
        {err && <p className="note error" role="alert">{err}</p>}
        <div className="actions">
          <button type="button" className="btn ghost" onClick={onClose}>취소</button>
          {item.can_edit && <button type="button" className="btn danger" disabled={busy}
            onClick={() => setAsk({
              title: `"${item.name}" 을(를) 삭제할까요?`,
              lines: [...(item.kind === "prompt" && item.source === "portal" ? ["Bedrock Prompt Management의 prompt도 함께 삭제됩니다."] : []),
                ...(item.registry_record_id ? ["Registry record도 함께 삭제됩니다."] : []), "이 작업은 되돌릴 수 없습니다."],
              confirmLabel: "삭제", danger: true,
              onConfirm: () => run(() => api.remove(item.kind, item.id), onDeleted),
            })}>삭제</button>}
        </div>
      </div>
      {ask && <ConfirmDialog spec={ask} onClose={() => setAsk(null)} />}
    </Modal>
  );
}
