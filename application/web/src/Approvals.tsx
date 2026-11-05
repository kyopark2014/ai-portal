import { useCallback, useEffect, useState } from "react";
import * as api from "./api";
import type { RecStatus, RegDetail, RegRecord } from "./api";
import AdminManager from "./AdminManager";
import UsageRequests from "./UsageRequests";

type Section = "REG" | "USAGE" | "ADMINS";
const SECTIONS: { s: Section; label: string }[] = [
  { s: "REG", label: "등록 승인" }, { s: "USAGE", label: "사용 승인" }, { s: "ADMINS", label: "승인자 관리" },
];
const TABS: { s: RecStatus; label: string }[] = [
  { s: "PENDING_APPROVAL", label: "대기" }, { s: "APPROVED", label: "승인됨" },
  { s: "REJECTED", label: "거절됨" }, { s: "DEPRECATED", label: "Deprecated" },
];
const TYPE: Record<string, string> = { MCP: "MCP", A2A: "Agent", AGENT_SKILLS: "Skill", CUSTOM: "Prompt/Custom" };

/** 목록용: 날짜와 시간(분까지)만. 예) 2026. 11. 4. 22:15 */
function short(iso: string | null) {   // eslint-disable-line
  if (!iso) return "-";
  const d = new Date(iso), p = (n: number) => String(n).padStart(2, "0");
  return <><span className="nw">{`${d.getFullYear()}. ${d.getMonth() + 1}. ${d.getDate()}.`}</span> <span className="nw">{`${p(d.getHours())}:${p(d.getMinutes())}`}</span></>;   // 좁으면 날짜 / 시간 두 줄
}
function when(iso: string | null) { return iso ? new Date(iso).toLocaleString("ko-KR", { hour12: false }) : "-"; }

export default function Approvals({ openId, onOpened, isAdmin = false }: { openId: string | null; onOpened: () => void; isAdmin?: boolean }) {
  const [section, setSection] = useState<Section>(isAdmin ? "REG" : "USAGE");
  const [tab, setTab] = useState<RecStatus>("PENDING_APPROVAL");
  const [rows, setRows] = useState<RegRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [sel, setSel] = useState<string | null>(openId);

  const load = useCallback(async () => {
    setLoading(true); setErr("");
    if (section !== "REG") { setLoading(false); return; }
    try { setRows((await api.listApprovals(tab)).records); } catch (e: any) { setErr(e.message); }
    setLoading(false);
  }, [tab, section]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { if (openId) { setSel(openId); onOpened(); } }, [openId]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <section aria-labelledby="ap-h">
      <div className="page-head"><h1 id="ap-h">승인</h1>
        <button type="button" className="btn ghost" onClick={load} disabled={loading}>새로고침</button></div>
      <p className="sub"><b>등록 승인</b>(관리자 전용)은 Registry에 승인 요청된 record를, <b>사용 승인</b>은 등록된 MCP를 쓰겠다는 사용 신청을 검토합니다. reviewer 는 자기가 맡은 MCP 의 사용 신청만 보입니다(관리자는 전체).</p>
      <div className="tabs" role="tablist" aria-label="승인 종류">
        {SECTIONS.filter((t) => isAdmin || t.s === "USAGE").map((t) => <button key={t.s} role="tab" aria-selected={section === t.s} className={`tab ${section === t.s ? "on" : ""}`} onClick={() => setSection(t.s)}>{t.label}</button>)}
      </div>
      {err && <p className="note error" role="alert">{err}</p>}
      {section === "ADMINS" ? <AdminManager /> : section === "USAGE" ? <UsageRequests /> : (
        <section className="card" aria-labelledby="rg-h">
          <h2 id="rg-h">등록 승인</h2>
          <p className="sub">Registry에 승인 요청된 record입니다. 승인하면 Registry 에 등록되어 검색에 노출됩니다.</p>
          <div className="tabs" role="tablist" aria-label="상태">
            {TABS.map((t) => <button key={t.s} role="tab" aria-selected={tab === t.s} className={`tab ${tab === t.s ? "on" : ""}`} onClick={() => setTab(t.s)}>{t.label}</button>)}
          </div>
          {loading ? <p className="sub">불러오는 중…</p> : rows.length === 0 ? (
            <div className="empty-box"><p>{tab === "PENDING_APPROVAL" ? "대기 중인 등록 요청이 없습니다." : "해당하는 record가 없습니다."}</p></div>
          ) : (
            <div className="tablewrap"><table className="reg-table">
              <thead><tr><th>이름</th><th>종류</th><th>요청자</th><th>요청</th><th /></tr></thead>
              <tbody>{rows.map((r) => (
                <tr key={r.recordId}>
                  <td><strong>{r.name}</strong>{r.description && <div className="hint clip" title={r.description}>{r.description}</div>}</td>
                  <td>{TYPE[r.descriptorType] ?? r.descriptorType}</td>
                  <td className="mono sm nw">{r.portal ? r.portal.owner : "포털 외 등록"}
                    {(r.reviewers?.length ?? 0) > 0 && <span className="badge" title={r.reviewers!.join(", ")}>reviewer {r.reviewers!.length}명</span>}</td>
                  <td>{short(r.updatedAt)}</td>
                  <td className="nw"><div className="acts"><button type="button" className="btn nw" onClick={() => setSel(r.recordId)}>검토</button></div></td>
                </tr>))}</tbody>
            </table></div>)}
        </section>)}
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
  const [resetDone, setResetDone] = useState(false);
  async function reset() {
    if (!window.confirm("Deprecated 된 record 를 삭제하고 등록 전 상태로 되돌릴까요?")) return;
    setBusy(true); setErr("");
    try { await api.resetDeprecated(id); setResetDone(true); }
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
              {d.status === "DEPRECATED" && !resetDone && <button className="btn" disabled={busy} title="등록 전(미등록) 상태로 되돌립니다" onClick={reset}>취소</button>}
              {(done || resetDone) && <button className="btn" onClick={onDone}>목록으로</button>}</div>)}
          {resetDone && <p className="note ok" role="status">취소했습니다. 등록 전 상태로 되돌렸습니다.</p>}
          {done && <p className="note ok" role="status">{done === "APPROVED" ? "승인했습니다." : done === "REJECTED" ? "거절했습니다." : "Deprecated 처리했습니다."}</p>}
        </div>)}
        {err && <p className="note error" role="alert">{err}</p>}
      </div>
    </div>
  );
}
