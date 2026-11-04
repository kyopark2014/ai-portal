import { useState } from "react";
import * as api from "./api";
import type { Item, McpToken } from "./api";

/** 한 번 복사해서 쓸 수 있게 보여 준다. 토큰은 비밀이므로 처음엔 가리고, 이 화면을 닫으면 지운다(서버에도 저장하지 않는다). */
function Secret({ label, value, hint }: { label: string; value: string; hint?: string }) {
  const [show, setShow] = useState(false);
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try { await navigator.clipboard.writeText(value); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { /* 선택해서 복사 */ }
  };
  return (
    <div className="secret">
      <div className="secret-head"><strong>{label}</strong>{hint && <span className="hint">{hint}</span>}
        <span className="acts">
          <button type="button" className="link" onClick={() => setShow(!show)}>{show ? "숨기기" : "보기"}</button>
          <button type="button" className="link" onClick={copy}>{copied ? "복사됨" : "복사"}</button></span></div>
      <pre className="code out secret-body" aria-label={label}>{show ? value : "•".repeat(24) + `  (${value.length}자)`}</pre>
    </div>
  );
}

/** MCP(AgentCore Gateway)의 접속 토큰. 사용이 승인된 사람(또는 소유자)만 받을 수 있다. */
export default function McpTokenBox({ item, canUse }: { item: Item; canUse: boolean }) {
  const [tok, setTok] = useState<McpToken | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const url = item.data.endpoint as string;
  if (item.data.authorizer !== "CUSTOM_JWT") return null;          // 토큰은 JWT 인증 Gateway 에만 해당한다

  async function issue() {
    setBusy(true); setErr("");
    try { setTok(await api.getMcpToken(item.id)); } catch (e: any) { setErr(e.message); setTok(null); }
    setBusy(false);
  }
  const hours = tok ? Math.round(tok.expires_in / 3600) : 0;
  const cmd = tok ? `claude mcp add --transport http ${item.name} ${url} --header "Authorization: Bearer <액세스 토큰>"` : "";

  return (
    <div className="usage" role="region" aria-label="접속 토큰">
      <h3>접속 토큰 <span className="hint">Claude Code 등에서 이 Gateway 를 쓸 때</span></h3>
      {!canUse ? <p className="hint">사용 신청이 승인되면 접속 토큰을 받을 수 있습니다.</p> : (<>
        <div className="actions left"><button type="button" className="btn" disabled={busy} onClick={issue}>{tok ? "토큰 다시 받기" : "토큰 받기"}</button></div>
        {err && <p className="note error" role="alert">{err}</p>}
        {tok && (<>
          <Secret label="액세스 토큰" value={tok.access_token} hint={`${hours}시간 유효 · Authorization: Bearer 로 보냅니다`} />
          <Secret label="리프레시 토큰" value={tok.refresh_token} hint="약 10년 유효 · 새 액세스 토큰을 받을 때 씁니다. 외부에 공개하지 마세요" />
          <dl className="kv">
            <div><dt>Client ID</dt><dd className="mono sm">{tok.client_id}</dd></div>
            <div><dt>그룹(사용 승인)</dt><dd className="mono sm">{tok.group}</dd></div>
            <div className="wide-row"><dt>MCP endpoint</dt><dd className="mono sm arn">{url}</dd></div>
          </dl>
          <p className="hint">Claude Code 예: <code className="mono">{cmd}</code></p>
          <p className="note" role="status">액세스 토큰은 {hours}시간 뒤 만료됩니다. 사용 승인이 취소되면 리프레시 토큰이 폐기되어 갱신할 수 없습니다.</p>
        </>)}
      </>)}
    </div>
  );
}
