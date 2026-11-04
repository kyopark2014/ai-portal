import { useCallback, useEffect, useState } from "react";
import * as api from "./api";
import type { RecStatus, RegDetail, RegRecord } from "./api";
import AdminManager from "./AdminManager";
import UsageRequests from "./UsageRequests";

const TABS: { s: RecStatus | "ADMINS" | "USAGE"; label: string }[] = [
  { s: "PENDING_APPROVAL", label: "승인 대기" }, { s: "APPROVED", label: "승인됨" },
  { s: "REJECTED", label: "거절됨" }, { s: "DEPRECATED", label: "Deprecated" }, { s: "USAGE", label: "사용 신청" }, { s: "ADMINS", label: "승인자 관리" },
];
const TYPE: Record<string, string> = { MCP: "MCP", A2A: "Agent", AGENT_SKILLS: "Skill", CUSTOM: "Prompt/Custom" };

function when(iso: string | null) { return iso ? new Date(iso).toLocaleString("ko-KR", { hour12: false }) : "-"; }

export default function Approvals({ openId, onOpened, isAdmin = false }: { openId: string | null; onOpened: () => void; isAdmin?: boolean }) {
  const [tab, setTab] = useState<RecStatus | "ADMINS" | "USAGE">("PENDING_APPROVAL");
  const [rows, setRows] = useState<RegRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [sel, setSel] = useState<string | null>(openId);

  const load = useCallback(async () => {
    setLoading(true); setErr("");
    if (tab === "ADMINS" || tab === "USAGE") { setLoading(false); return; }
    try { setRows((await api.listApprovals(tab)).records); } catch (e: any) { setErr(e.message); }
    setLoading(false);
  }, [tab]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { if (openId) { setSel(openId); onOpened(); } }, [openId]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <section aria-labelledby="ap-h">
      <div className="page-head"><h1 id="ap-h">승인</h1>
        <button type="button" className="btn ghost" onClick={load} disabled={loading}>새로고침</button></div>
      <p className="sub">Registry에 승인 요청된 record를 검토하고 승인·거절합니다. 승인은 검색에 노출될지를 정할 뿐, 런타임 접근 권한은 별도로 관리됩니다.</p>
      <div className="tabs" role="tablist">
        {TABS.filter((t) => isAdmin || t.s !== "ADMINS").map((t) => <button key={t.s} role="tab" aria-selected={tab === t.s} className={`tab ${tab === t.s ? "on" : ""}`} onClick={() => setTab(t.s)}>{t.label}</button>)}
      </div>
      {err && <p className="note error" role="alert">{err}</p>}
      {tab === "ADMINS" ? <AdminManager /> : tab === "USAGE" ? <UsageRequests /> : loading ? <p className="sub">불러오는 중…</p> : rows.length === 0 ? (
        <div className="empty-box"><p>{tab === "PENDING_APPROVAL" ? "승인 대기 중인 요청이 없습니다." : "해당하는 record가 없습니다."}</p></div>
      ) : (
        <div className="rows">
          {rows.map((r) => (
            <article className="item" key={r.recordId}>
              <header><span className="avatar-l" aria-hidden>{r.name[0]?.toUpperCase()}</span><h3 title={r.name}>{r.name}</h3></header>
              <p className="by">{TYPE[r.descriptorType] ?? r.descriptorType}{r.portal ? ` · ${r.portal.owner.split("@")[0]}` : " · 포털 외 등록"}
                {(r.reviewers?.length ?? 0) > 0 && <span className="badge" title={r.reviewers!.join(", ")}>reviewer {r.reviewers!.length}명</span>}</p>
              <div className="preview"><p className="desc">{r.description || <em>설명 없음</em>}</p></div>
              <footer><span>요청 {when(r.updatedAt)}</span>
                <div className="acts"><button type="button" className="link" onClick={() => setSel(r.recordId)}>검토</button></div></footer>
            </article>))}
        </div>)}
      {sel && <ReviewModal id={sel} onClose={() => setSel(null)} onDone={() => { setSel(null); load(); }} />}
    </section>
  );
}

function ReviewModal({ id, onClose, onDone }: { id: string; onClose: () => void; onDone: () => void }) {
  const [d, setD] = useState<RegDetail | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [done, setDone] = useState("");

  useEffect(() => { api.getApproval(id).then(setD).catch((e) => setErr(e.message)); }, [id]);
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k); return () => window.removeEventListener("keydown", k);
  }, [onClose]);

  async function go(decision: "APPROVED" | "REJECTED" | "DEPRECATED") {
    setBusy(true); setErr("");
    try { await api.decide(id, decision, reason); setDone(decision); setD(await api.getApproval(id)); }
    catch (e: any) { setErr(e.message); }
    setBusy(false);
  }
  const pending = d?.status === "PENDING_APPROVAL";
  const allowed = d?.can_decide !== false;

  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal wide" role="dialog" aria-modal="true" aria-label="승인 검토">
        <div className="modal-head"><h2>{d?.name ?? "승인 검토"}</h2><button className="x" aria-label="닫기" onClick={onClose}>×</button></div>
        {!d && !err && <p className="sub">불러오는 중…</p>}
        {d && (<div className="detail">
          <p className="by">{TYPE[d.descriptorType] ?? d.descriptorType} · v{d.recordVersion} · <span className={`badge st-${d.status}`}>{d.status}</span></p>
          <dl className="kv"><div><dt>Record ID</dt><dd className="mono sm">{d.recordId}</dd></div>
            <div><dt>요청자</dt><dd>{d.portal?.owner ?? "포털 외 등록"}</dd></div>
            <div><dt>갱신</dt><dd>{when(d.updatedAt)}</dd></div></dl>
          <h3>설명</h3><p>{d.description || "설명 없음"}</p>
          <h3>Record 내용</h3>
          <pre className="code">{typeof d.content === "string" ? d.content : JSON.stringify(d.content, null, 2)}</pre>
          {d.statusReason && <p className="hint">처리 사유: {d.statusReason}</p>}
          {(d.reviewers?.length ?? 0) > 0 && <p className="hint">이 record 의 reviewer: {d.reviewers!.join(", ")}</p>}
          {pending ? (<>
            <label className="field"><span className="lbl">사유 <span className="hint">거절 시 필수 · {reason.length}/200</span></span>
              <textarea rows={2} maxLength={200} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="요청자에게 남길 메모" /></label>
            <div className="actions">
              <button className="btn ghost" onClick={onClose}>닫기</button>
              <button className="btn danger" disabled={busy || !allowed || !reason.trim()} title={!allowed ? "처리 권한이 없습니다" : reason.trim() ? "" : "거절 사유를 입력하세요"} onClick={() => go("REJECTED")}>거절</button>
              <button className="btn" disabled={busy || !allowed} title={allowed ? "" : "처리 권한이 없습니다"} onClick={() => go("APPROVED")}>승인</button>
            </div></>) : (
            <div className="actions"><button className="btn ghost" onClick={onClose}>닫기</button>
              {d.status === "APPROVED" && <button className="btn danger" disabled={busy} onClick={() => go("DEPRECATED")}>Deprecate</button>}
              {done && <button className="btn" onClick={onDone}>목록으로</button>}</div>)}
          {done && <p className="note ok" role="status">{done === "APPROVED" ? "승인했습니다." : done === "REJECTED" ? "거절했습니다." : "Deprecated 처리했습니다."}</p>}
        </div>)}
        {err && <p className="note error" role="alert">{err}</p>}
      </div>
    </div>
  );
}
