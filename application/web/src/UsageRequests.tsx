import { useCallback, useEffect, useState } from "react";
import * as api from "./api";
import type { UsageRequest } from "./api";
import { ago } from "./Explore";

const TABS: { s: "PENDING" | "APPROVED" | "REJECTED"; label: string }[] = [
  { s: "PENDING", label: "대기" }, { s: "APPROVED", label: "승인됨" }, { s: "REJECTED", label: "거절됨" },
];

/** MCP 사용 신청 처리. 관리자는 전부, reviewer 는 자기에게 지정된 MCP 의 신청만 보인다. */
export default function UsageRequests() {
  const [tab, setTab] = useState<"PENDING" | "APPROVED" | "REJECTED">("PENDING");
  const [rows, setRows] = useState<UsageRequest[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(""); const [msg, setMsg] = useState("");
  const [rejecting, setRejecting] = useState<UsageRequest | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setLoading(true); setErr("");
    api.listUsageRequests(tab).then((r) => setRows(r.requests)).catch((e) => setErr(e.message)).finally(() => setLoading(false));
  }, [tab]);
  useEffect(load, [load]);

  async function decide(r: UsageRequest, decision: "APPROVED" | "REJECTED", why = "") {
    setBusy(true); setErr(""); setMsg("");
    try { await api.decideUsage(r.item_id, r.email, decision, why); setMsg(`${r.email} 님의 사용 신청을 ${decision === "APPROVED" ? "승인" : "거절"}했습니다.`); setRejecting(null); setReason(""); load(); }
    catch (e: any) { setErr(e.message); }
    setBusy(false);
  }

  return (
    <section className="card" aria-labelledby="ur-h">
      <h2 id="ur-h">MCP 사용 승인</h2>
      <p className="sub">등록된 MCP 를 쓰겠다는 신청입니다. 승인하면 신청한 사람이 그 MCP 를 테스트·연결할 수 있습니다.</p>
      <div className="tabs" role="tablist">{TABS.map((t) => (
        <button key={t.s} role="tab" aria-selected={tab === t.s} className={`tab ${tab === t.s ? "on" : ""}`} onClick={() => setTab(t.s)}>{t.label}</button>))}</div>
      {loading ? <p className="sub">불러오는 중…</p> : rows.length === 0 ? <div className="empty-box"><p>{tab === "PENDING" ? "대기 중인 사용 신청이 없습니다." : "해당하는 신청이 없습니다."}</p></div> : (
        <div className="tablewrap"><table>
          <thead><tr><th>MCP</th><th>신청자</th><th>신청</th>{tab !== "PENDING" && <th>처리</th>}<th /></tr></thead>
          <tbody>{rows.map((r) => (
            <tr key={`${r.item_id}:${r.email}`}>
              <td><strong>{r.mcp.name}</strong>{r.mcp.description && <div className="hint clip">{r.mcp.description}</div>}</td>
              <td className="mono sm">{r.email}</td>
              <td>{ago(r.requested_at)}</td>
              {tab !== "PENDING" && <td className="hint">{r.reason || (r.decided_by && `by ${r.decided_by}`)}</td>}
              <td>{tab === "PENDING" && (<div className="acts">
                <button type="button" className="btn" disabled={busy} onClick={() => decide(r, "APPROVED")}>승인</button>
                <button type="button" className="btn danger" disabled={busy} onClick={() => { setRejecting(r); setReason(""); }}>거절</button></div>)}</td>
            </tr>))}</tbody>
        </table></div>)}
      {rejecting && (
        <div className="overlay top" onMouseDown={(e) => e.target === e.currentTarget && setRejecting(null)}>
          <div className="modal confirm" role="dialog" aria-modal="true" aria-label="사용 신청 거절">
            <h2>{rejecting.email} 님의 신청을 거절할까요?</h2>
            <label className="field"><span className="lbl">거절 사유 <span className="hint">필수 · {reason.length}/200</span></span>
              <textarea rows={2} maxLength={200} autoFocus value={reason} onChange={(e) => setReason(e.target.value)} /></label>
            <div className="actions"><button className="btn ghost" onClick={() => setRejecting(null)}>취소</button>
              <button className="btn danger" disabled={busy || !reason.trim()} onClick={() => decide(rejecting, "REJECTED", reason)}>거절</button></div>
          </div>
        </div>)}
      {msg && <p className="note ok" role="status">{msg}</p>}
      {err && <p className="note error" role="alert">{err}</p>}
    </section>
  );
}
