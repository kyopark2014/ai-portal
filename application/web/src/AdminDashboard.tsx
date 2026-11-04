import { useEffect, useState } from "react";
import * as api from "./api";
import type { Dashboard, Kind } from "./api";
import { ago } from "./Explore";

const KIND_LABEL: Record<string, string> = { prompt: "Prompt", mcp: "MCP", skill: "Skill", agent: "Agent" };
const KINDS: Kind[] = ["prompt", "mcp", "skill", "agent"];
const STATUS_LABEL: Record<string, string> = {
  APPROVED: "승인됨", PENDING_APPROVAL: "승인 대기", DRAFT: "초안", REJECTED: "거절됨", DEPRECATED: "Deprecated",
  CREATING: "등록 중", UPDATING: "갱신 중", NOT_REGISTERED: "미등록",
};
const STATUS_ORDER = ["APPROVED", "PENDING_APPROVAL", "DRAFT", "REJECTED", "DEPRECATED", "CREATING", "UPDATING", "NOT_REGISTERED"];

const when = (t: number | null) => (t ? new Date(t * 1000).toLocaleString("ko-KR", { hour12: false }) : "-");
const sum = (o?: Record<string, number>) => Object.values(o ?? {}).reduce((a, b) => a + b, 0);

/** 상태별 개수를 작은 배지로. 개수가 0 인 상태는 그리지 않는다. */
function Counts({ c }: { c?: Record<string, number> }) {
  const keys = STATUS_ORDER.filter((s) => c?.[s]);
  if (!keys.length) return <span className="hint">-</span>;
  return <span className="counts">{keys.map((s) => <span key={s} className={`badge st-${s === "NOT_REGISTERED" ? "" : s}`} title={STATUS_LABEL[s]}>{STATUS_LABEL[s]} {c![s]}</span>)}</span>;
}

export default function AdminDashboard() {
  const [d, setD] = useState<Dashboard | null>(null);
  const [err, setErr] = useState("");
  const [q, setQ] = useState("");
  const [loading, setLoading] = useState(true);

  const load = () => { setLoading(true); setErr(""); api.adminDashboard().then(setD).catch((e) => setErr(e.message)).finally(() => setLoading(false)); };
  useEffect(load, []);

  const users = (d?.users ?? []).filter((u) => !q.trim() || u.email.includes(q.trim().toLowerCase()) || u.name.toLowerCase().includes(q.trim().toLowerCase()));

  return (
    <section aria-labelledby="ad-h">
      <div className="page-head"><h1 id="ad-h">관리</h1>
        <button type="button" className="btn ghost" onClick={load} disabled={loading}>새로고침</button></div>
      {err && <p className="note error" role="alert">{err}</p>}
      {!d && loading && <p className="sub">불러오는 중…</p>}

      {d && (<>
        <div className="stats">
          <div className="stat"><span className="lbl">접속한 사용자</span><strong>{d.totals.unique_users}</strong></div>
          <div className="stat"><span className="lbl">최근 7일 활성</span><strong>{d.totals.active_7d}</strong></div>
          <div className="stat"><span className="lbl">총 로그인</span><strong>{d.totals.total_logins}</strong></div>
          <div className="stat"><span className="lbl">Registry record</span><strong>{d.registry.total}</strong></div>
        </div>

        <section className="card" aria-labelledby="ad-reg">
          <h2 id="ad-reg">Registry 등록 현황 <span className="hint">type별 · 승인/진행 상태</span></h2>
          {d.registry.error && <p className="note error" role="alert">Registry 를 조회하지 못했습니다: {d.registry.error}</p>}
          <div className="tablewrap"><table>
            <thead><tr><th>종류</th><th className="n">합계</th><th>상태별</th></tr></thead>
            <tbody>{KINDS.map((k) => (
              <tr key={k}><td><strong>{KIND_LABEL[k]}</strong></td><td className="n">{sum(d.registry.by_type[k])}</td><td><Counts c={d.registry.by_type[k]} /></td></tr>))}</tbody>
          </table></div>
          <p className="hint">AWS Registry 를 직접 조회한 값입니다(포털 밖에서 등록된 record 포함).</p>
        </section>

        <section className="card" aria-labelledby="ad-users">
          <div className="head"><h2 id="ad-users">사용자 <span className="hint">{users.length}명</span></h2>
            <input className="search" type="search" placeholder="이메일·이름 검색" aria-label="사용자 검색" value={q} onChange={(e) => setQ(e.target.value)} /></div>
          <div className="tablewrap"><table>
            <thead><tr><th>사용자</th><th className="n">로그인</th><th>마지막 접속</th><th className="n">등록</th>{KINDS.map((k) => <th key={k}>{KIND_LABEL[k]}</th>)}</tr></thead>
            <tbody>{users.length === 0 ? <tr><td className="empty" colSpan={5 + KINDS.length}>해당하는 사용자가 없습니다.</td></tr> : users.map((u) => (
              <tr key={u.email}>
                <td><strong>{u.name || u.email.split("@")[0]}</strong>{u.is_admin && <span className="badge admin">관리자</span>}<div className="hint">{u.email}</div></td>
                <td className="n">{u.login_count || "-"}</td>
                <td>{u.last_login ? <span title={when(u.last_login)}>{ago(u.last_login)}</span> : <span className="hint">접속 기록 없음</span>}</td>
                <td className="n">{u.total}</td>
                {KINDS.map((k) => <td key={k}><Counts c={u.kinds[k]} /></td>)}
              </tr>))}</tbody>
          </table></div>
          <p className="hint">등록 수는 소유자 기준입니다. Registry 에서 복원되었거나 AWS 에서 동기화된 항목은 소유자가 <code>registry</code> / <code>aws</code> 로 보입니다. 접속 기록에는 이메일·이름·시각만 저장하며 재배포하면 초기화됩니다.</p>
        </section>

        <section className="card" aria-labelledby="ad-recent">
          <h2 id="ad-recent">최근 접속</h2>
          {d.recent_logins.length === 0 ? <p className="sub">아직 접속 기록이 없습니다.</p> : (
            <ul className="adm-list">{d.recent_logins.map((l, i) => (
              <li key={i}><span className="mono">{l.email}</span>{l.name && <span className="hint">{l.name}</span>}<span className="hint" style={{ marginLeft: "auto" }} title={when(l.logged_at)}>{ago(l.logged_at)}</span></li>))}</ul>)}
        </section>

        <RegistryDefaults />
      </>)}
    </section>
  );
}

/** Registry 관리: 새 Registry 의 기본값(IAM 인증, auto-approval 꺼짐)을 서버에서 받아 폼 초기값으로 쓴다. */
function RegistryDefaults() {
  const [def, setDef] = useState<{ authorizer: string; auto_approval: boolean } | null>(null);
  const [regs, setRegs] = useState<api.Registry[]>([]);
  const [selected, setSelected] = useState("");
  const [f, setF] = useState({ name: "", authorizer: "AWS_IAM", auto: false, url: "", aud: "", clients: "" });
  const [busy, setBusy] = useState(false); const [err, setErr] = useState(""); const [msg, setMsg] = useState("");

  const reload = () => api.listRegistries().then((r) => { setRegs(r.registries); setSelected(r.selected); }).catch((e) => setErr(e.message));
  useEffect(() => {
    api.registryDefaults().then((x) => { setDef(x); setF((p) => ({ ...p, authorizer: x.authorizer, auto: x.auto_approval })); }).catch(() => {});
    reload();
  }, []);
  const csv = (v: string) => v.split(",").map((x) => x.trim()).filter(Boolean);

  async function create(e: React.FormEvent) {
    e.preventDefault(); setBusy(true); setErr(""); setMsg("");
    try {
      await api.createRegistry({ name: f.name, authorizer: f.authorizer, auto_approval: f.auto, jwt_discovery_url: f.url,
        jwt_allowed_audience: csv(f.aud), jwt_allowed_clients: csv(f.clients) });
      setMsg(`Registry "${f.name}" 을(를) 만들었습니다.`); setF((p) => ({ ...p, name: "" })); reload();
    } catch (x: any) { setErr(x.message); }
    setBusy(false);
  }

  return (
    <section className="card" aria-labelledby="ad-regmgr">
      <h2 id="ad-regmgr">레지스트리 관리</h2>
      <label className="field"><span className="lbl">사용할 Registry</span>
        <select value={selected} disabled={busy} onChange={(e) => api.selectRegistry(e.target.value).then(() => setSelected(e.target.value)).catch((x) => setErr(x.message))}>
          <option value="">선택…</option>{regs.map((r) => <option key={r.registryId} value={r.registryId}>{r.name} · {r.authorizerType} · {r.status}</option>)}</select></label>

      <form className="form" onSubmit={create}>
        <h3>새 Registry 만들기</h3>
        <p className="hint">기본값: 인증 <b>{def?.authorizer === "AWS_IAM" ? "IAM" : def?.authorizer ?? "IAM"}</b>, auto-approval <b>{def?.auto_approval ? "켜짐" : "꺼짐"}</b> (수동 승인).</p>
        <label className="field"><span className="lbl">이름</span><input required pattern="[A-Za-z][A-Za-z0-9_]*" placeholder="영문으로 시작, 영문/숫자/_" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></label>
        <div className="row2">
          <label className="field"><span className="lbl">인증 방식</span>
            <select value={f.authorizer} onChange={(e) => setF({ ...f, authorizer: e.target.value })}>
              <option value="AWS_IAM">IAM (기본)</option><option value="CUSTOM_JWT">JWT (OIDC)</option></select></label>
          <label className="field"><span className="lbl">Auto-approval</span>
            <select value={String(f.auto)} onChange={(e) => setF({ ...f, auto: e.target.value === "true" })}>
              <option value="false">끔 — 수동 승인 (기본)</option><option value="true">켬 — 승인 없이 바로 검색 가능</option></select></label>
        </div>
        {f.auto && <p className="note error">auto-approval 을 켜면 등록 즉시 검색에 노출됩니다. 검토 절차가 사라지니 신중히 선택하세요.</p>}
        {f.authorizer === "CUSTOM_JWT" && (<fieldset className="grp"><legend>JWT 설정</legend>
          <label className="field"><span className="lbl">OIDC Discovery URL (필수)</span><input required type="url" placeholder="https://…/.well-known/openid-configuration" value={f.url} onChange={(e) => setF({ ...f, url: e.target.value })} /></label>
          <label className="field"><span className="lbl">Allowed audience (쉼표 구분, 선택)</span><input value={f.aud} onChange={(e) => setF({ ...f, aud: e.target.value })} /></label>
          <label className="field"><span className="lbl">Allowed clients (쉼표 구분, 선택)</span><input value={f.clients} onChange={(e) => setF({ ...f, clients: e.target.value })} /></label></fieldset>)}
        <div className="actions"><button className="btn" disabled={busy || !f.name}>{busy ? "만드는 중…" : "Registry 생성"}</button></div>
      </form>
      {msg && <p className="note ok" role="status">{msg}</p>}
      {err && <p className="note error" role="alert">{err}</p>}
    </section>
  );
}
