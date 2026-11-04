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

        <RegistryCard />
      </>)}
    </section>
  );
}

/** 이 포털이 쓰는 Registry(읽기 전용). 생성·변경 UI 는 두지 않는다 — 설치기가 만든 `ai-portal` 하나로 고정이다. */
function RegistryCard() {
  const [r, setR] = useState<api.RegistryInfo | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => { api.registryInfo().then(setR).catch((e) => setErr(e.message)); }, []);
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
          <div><dt>Auto-approval</dt><dd>{r.auto_approval ? "켜짐" : "꺼짐 (수동 승인)"}</dd></div>
          <div className="wide-row"><dt>Registry ID</dt><dd className="mono sm arn">{r.registry_id}</dd></div>
        </dl>) : <p className="sub">연결된 Registry가 없습니다. <code>python installer.py</code> 로 만드세요.</p>)}
      <p className="hint">이 포털은 설치 때 만든 Registry 하나만 사용합니다. 인증 방식과 auto-approval 은 Registry 생성 시 정해지며 여기서 바꾸지 않습니다.</p>
    </section>
  );
}
