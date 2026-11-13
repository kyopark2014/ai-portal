import { useEffect, useState } from "react";
import * as api from "./api";
import type { Dashboard, Kind } from "./api";
import { ago } from "./Explore";
import ConfirmDialog, { type ConfirmSpec } from "./ConfirmDialog";
import LogViewer from "./LogViewer";

const KIND_LABEL: Record<string, string> = { prompt: "Prompt", mcp: "MCP", skill: "Skill", agent: "Agent" };
const KINDS: Kind[] = ["prompt", "mcp", "skill", "agent"];
const STATUS_LABEL: Record<string, string> = {
  APPROVED: "등록", PENDING_APPROVAL: "승인 대기", DRAFT: "초안", REJECTED: "거절됨", DEPRECATED: "Deprecated",
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
  const [tab, setTab] = useState<"stats" | "logs">("stats");
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
      <div className="seg" role="tablist" aria-label="관리 메뉴" style={{ marginBottom: ".8rem" }}>
        <button type="button" role="tab" aria-selected={tab === "stats"} className={tab === "stats" ? "on" : ""} onClick={() => setTab("stats")}>현황</button>
        <button type="button" role="tab" aria-selected={tab === "logs"} className={tab === "logs" ? "on" : ""} onClick={() => setTab("logs")}>로그 확인</button>
      </div>
      {tab === "logs" && <LogViewer />}
      {tab === "stats" && <>
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

        <RegistryCard />
      </>)}
      </>}
    </section>
  );
}

/** 이 포털이 쓰는 Registry(읽기 전용) + 승인 자동화 스위치. Registry 자체는 설치 때 정해진 하나로 고정이다. */
function RegistryCard() {
  const [r, setR] = useState<api.RegistryInfo | null>(null);
  const [pol, setPol] = useState<api.Policy | null>(null);
  const [err, setErr] = useState(""); const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [ask, setAsk] = useState<ConfirmSpec | null>(null);
  useEffect(() => { api.registryInfo().then(setR).catch((e) => setErr(e.message)); api.getPolicy().then(setPol).catch((e) => setErr(e.message)); }, []);

  async function change(patch: Partial<Pick<api.Policy, "auto_approval" | "auto_review">>, done: string) {
    setBusy(true); setErr(""); setMsg("");
    try { setPol(await api.setPolicy(patch)); setMsg(done); } catch (e: any) { setErr(e.message); }
    setBusy(false);
  }
  // 끄는 것은 바로, 켜는 것은 확인을 거친다(승인 절차가 줄어드는 방향이므로)
  const toggle = (key: "auto_approval" | "auto_review", label: string, risk: string) => (on: boolean) => {
    if (!on) return change({ [key]: false }, `${label}을(를) 껐습니다.`);
    setAsk({ title: `${label}을(를) 켤까요?`, lines: [risk, "켠 사람(" + "지금 로그인한 관리자" + ")이 기록에 남습니다.", "언제든 다시 끌 수 있습니다."],
             confirmLabel: "켜기", danger: true, onConfirm: () => change({ [key]: true }, `${label}을(를) 켰습니다.`) });
  };
  const onApproval = toggle("auto_approval", "Auto-approval", "등록하면 승인 요청까지 자동으로 진행됩니다. (Auto-review 가 꺼져 있으면 reviewer 의 승인은 여전히 필요합니다.)");
  const onReview = toggle("auto_review", "Auto-review", "승인 요청이 들어오면 reviewer·관리자의 허락 없이 바로 승인되어, 요청한 사람이 즉시 사용할 수 있게 됩니다. 검토 절차가 사실상 사라집니다.");

  return (
    <section className="card" aria-labelledby="ad-regmgr">
      <h2 id="ad-regmgr">연결된 Registry</h2>
      {err && <p className="note error" role="alert">{err}</p>}
      {!r && !err && <p className="sub">불러오는 중…</p>}
      {r && (r.registry_id ? (
        <dl className="kv">
          <div><dt>이름</dt><dd>{r.name}</dd></div>
          <div><dt>상태</dt><dd>{r.status}</dd></div>
          <div><dt>인증 방식</dt><dd>{r.authorizer === "AWS_IAM" ? "IAM" : r.authorizer}</dd></div>
          <div className="wide-row"><dt>Registry ID</dt><dd className="mono sm arn">{r.registry_id}</dd></div>
        </dl>) : <p className="sub">연결된 Registry가 없습니다. <code>python installer.py</code> 로 만드세요.</p>)}
      <p className="hint">이 포털은 설치 때 만든 Registry 하나만 사용합니다. 인증 방식은 Registry 생성 시 정해지며 여기서 바꾸지 않습니다.</p>

      <h3>승인 자동화</h3>
      {pol ? (<div className="switches">
        <label className="switch"><input type="checkbox" role="switch" checked={pol.auto_approval} disabled={busy} onChange={(e) => onApproval(e.target.checked)} />
          <span><strong>Auto-approval</strong> — Registry에 등록 시 자동 승인<span className="hint">등록하면 승인 요청까지 자동으로 보냅니다.</span></span></label>
        <label className="switch"><input type="checkbox" role="switch" checked={pol.auto_review} disabled={busy} onChange={(e) => onReview(e.target.checked)} />
          <span><strong>Auto-review</strong> — Registry 사용 신청시 자동 승인<span className="hint">사용 신청(승인 요청)이 오면 reviewer 허락 없이 바로 승인되어 요청자가 즉시 쓸 수 있습니다.</span></span></label>
        {(pol.auto_approval || pol.auto_review) && <p className="note error" role="status">
          {pol.auto_review ? "검토 없이 승인되는 상태입니다. " : ""}{pol.auto_approval && pol.auto_review ? "등록만 하면 곧바로 사용할 수 있습니다. " : ""}
          {pol.changed_by && `(마지막 변경: ${pol.changed_by}${pol.changed_at ? ", " + new Date(pol.changed_at * 1000).toLocaleString("ko-KR", { hour12: false }) : ""})`}</p>}
      </div>) : !err && <p className="sub">불러오는 중…</p>}
      {msg && <p className="note ok" role="status">{msg}</p>}
      {ask && <ConfirmDialog spec={ask} onClose={() => setAsk(null)} />}
    </section>
  );
}
