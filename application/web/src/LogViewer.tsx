import { useEffect, useRef, useState } from "react";
import * as api from "./api";
import type { LogQuery, LogRow, LogTarget } from "./api";

const ymd = (d: Date) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
const daysAgo = (n: number) => { const d = new Date(); d.setDate(d.getDate() - n); return ymd(d); };
const kb = (n: number | null) => (n == null ? "-" : n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`);
const local = (iso: string) => {
  const t = new Date(iso);
  if (isNaN(+t)) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${t.getFullYear()}. ${t.getMonth() + 1}. ${t.getDate()}. ${p(t.getHours())}:${p(t.getMinutes())}:${p(t.getSeconds())}`;
};

/** 관리자 «로그 확인»: MCP/Agent → 대상 → 기간·요청자 → Athena 조회(비동기) → 표 + CSV/JSON 다운로드. */
export default function LogViewer() {
  const [kind, setKind] = useState<"mcp" | "agent">("mcp");
  const [targets, setTargets] = useState<LogTarget[]>([]);
  const [retention, setRetention] = useState(90);
  const [note, setNote] = useState("");
  const [f, setF] = useState({ target: "", start: daysAgo(6), end: ymd(new Date()), user: "", tool: "", all_methods: false, only_problems: false });
  const [phase, setPhase] = useState<"idle" | "running" | "done" | "failed">("idle");
  const [err, setErr] = useState("");
  const [qid, setQid] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const [rows, setRows] = useState<LogRow[]>([]);
  const [meta, setMeta] = useState<{ truncated: boolean; scanned: number | null }>({ truncated: false, scanned: null });
  const [open, setOpen] = useState<number | null>(null);
  const alive = useRef(0);                                  // 조회를 새로 시작하거나 화면을 떠나면 이전 폴링을 멈춘다
  const set = (k: string, v: any) => setF((p) => ({ ...p, [k]: v }));

  useEffect(() => () => { alive.current++; }, []);
  useEffect(() => {
    setTargets([]); setNote(""); setPhase("idle"); setRows([]); setF((p) => ({ ...p, target: "" }));
    api.logTargets(kind).then((r) => {
      setTargets(r.targets); setRetention(r.retention_days); setNote(r.note);
      setF((p) => ({ ...p, target: r.targets[0]?.id ?? "" }));
    }).catch((e) => setErr(e.message));
  }, [kind]);

  async function run(e: React.FormEvent) {
    e.preventDefault();
    const my = ++alive.current;
    setErr(""); setRows([]); setOpen(null); setPhase("running"); setElapsed(0);
    const t0 = Date.now();
    try {
      const { query_id } = await api.logStart({ kind, ...f } as LogQuery);
      setQid(query_id);
      for (;;) {
        await new Promise((r) => setTimeout(r, 1500));
        if (alive.current !== my) return;
        setElapsed(Math.round((Date.now() - t0) / 1000));
        const st = await api.logStatus(query_id);
        if (st.state === "SUCCEEDED") break;
        if (st.state === "FAILED" || st.state === "CANCELLED") throw new Error(st.reason || `조회가 ${st.state} 되었습니다`);
      }
      const r = await api.logResults(query_id);
      if (alive.current !== my) return;
      setRows(r.rows); setMeta({ truncated: r.truncated, scanned: r.scanned_bytes }); setPhase("done");
    } catch (x: any) {
      if (alive.current !== my) return;
      setErr(x.message); setPhase("failed");
    }
  }

  const who = (r: LogRow) => r.user_email || r.caller || "-";
  const blockedN = rows.filter((r) => r.blocked).length, errN = rows.filter((r) => r.is_error && !r.blocked).length;
  const byUser = Object.entries(rows.reduce<Record<string, number>>((m, r) => { m[who(r)] = (m[who(r)] ?? 0) + 1; return m; }, {}))
    .sort((a, b) => b[1] - a[1]).slice(0, 5);

  return (
    <section className="card" aria-labelledby="lv-h">
      <h2 id="lv-h">로그 확인</h2>
      <p className="sub">누가 어떤 요청을 보냈는지 Athena 로 조회합니다. 몇 초에서 수십 초 걸릴 수 있습니다. 로그는 {retention}일 보관됩니다.</p>
      <form onSubmit={run} className="form">
        <div className="seg wide" role="group" aria-label="종류">
          <button type="button" aria-pressed={kind === "mcp"} className={kind === "mcp" ? "on" : ""} onClick={() => setKind("mcp")}>MCP</button>
          <button type="button" aria-pressed={kind === "agent"} className={kind === "agent" ? "on" : ""} onClick={() => setKind("agent")}>Agent</button>
        </div>
        {kind === "agent" ? <p className="note">{note || "Agent 로그는 아직 수집하지 않습니다(추후 적용)."}</p> : (<>
          <label className="field"><span className="lbl">MCP (Gateway)</span>
            <select required value={f.target} onChange={(e) => set("target", e.target.value)}>
              {targets.length === 0 && <option value="">로그가 쌓이는 MCP 가 없습니다</option>}
              {targets.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}</select>
            <span className="hint">AgentCore Gateway 를 거치는 MCP 만 기록됩니다.</span></label>
          <div className="row2">
            <label className="field"><span className="lbl">시작일</span><input type="date" required value={f.start} min={daysAgo(retention)} max={f.end} onChange={(e) => set("start", e.target.value)} /></label>
            <label className="field"><span className="lbl">종료일</span><input type="date" required value={f.end} min={f.start} max={ymd(new Date())} onChange={(e) => set("end", e.target.value)} /></label>
          </div>
          <div className="row2">
            <label className="field"><span className="lbl">요청자 (선택)</span><input placeholder="이메일 일부" value={f.user} onChange={(e) => set("user", e.target.value)} /></label>
            <label className="field"><span className="lbl">Tool (선택)</span><input placeholder="tool 이름 일부" value={f.tool} onChange={(e) => set("tool", e.target.value)} /></label>
          </div>
          <label className="check"><input type="checkbox" checked={f.only_problems} onChange={(e) => set("only_problems", e.target.checked)} /> 차단되었거나 오류가 난 호출만</label>
          <label className="check"><input type="checkbox" checked={f.all_methods} onChange={(e) => set("all_methods", e.target.checked)} /> tool 호출 외 요청(tools/list, initialize 등)도 포함</label>
          <div className="actions"><button className="btn" disabled={phase === "running" || !f.target}>{phase === "running" ? `조회 중… ${elapsed}초` : "조회"}</button></div>
        </>)}
      </form>

      {err && <p className="note error" role="alert">{err}</p>}
      {phase === "done" && (
        <div className="lv-results">
          <div className="page-head">
            <h3 style={{ flex: 1, margin: 0 }}>결과 {rows.length}건{meta.truncated ? "+" : ""}</h3>
            <a className="btn ghost" href={api.logDownloadUrl(qid, "csv")}>CSV 다운로드</a>
            <a className="btn ghost" href={api.logDownloadUrl(qid, "json")}>JSON 다운로드</a>
          </div>
          <p className="hint">
            차단 {blockedN}건 · 오류 {errN}건 · 요청자 상위: {byUser.map(([u, n]) => `${u} (${n})`).join(", ") || "-"} · 스캔 {kb(meta.scanned)}
            {meta.truncated ? " · 화면에는 최근 500건만 표시합니다. 다운로드에는 전체(최대 1만 건)가 들어갑니다." : ""}
          </p>
          <div className="tablewrap"><table className="lv-table">
            <thead><tr><th>시간</th><th>요청자</th><th>Tool</th><th>요청 내용</th><th>결과</th><th className="n">지연(ms)</th></tr></thead>
            <tbody>
              {rows.length === 0 && <tr><td colSpan={6} className="empty">조건에 맞는 로그가 없습니다. 기록은 5분 단위로 저장되므로 방금 한 호출은 잠시 뒤에 보입니다.</td></tr>}
              {rows.map((r, i) => (
                <tr key={i} onClick={() => setOpen(open === i ? null : i)} style={{ cursor: "pointer" }}>
                  <td className="lv-time">{local(r.time)}</td>
                  <td>{who(r)}</td>
                  <td>{r.tool || r.method}</td>
                  <td style={{ maxWidth: 360, wordBreak: "break-all" }}>{open === i ? <pre style={{ whiteSpace: "pre-wrap", margin: 0 }}>{r.arguments ?? "-"}</pre> : (r.arguments ?? "-").slice(0, 80)}</td>
                  <td className="lv-result">{r.blocked ? <span className="badge st-REJECTED" title={r.block_reason ?? ""}>차단</span>
                    : r.is_error ? <span className="badge st-REJECTED" title={r.error_message ?? ""}>오류</span>
                    : r.status == null ? <span className="hint">응답 없음</span> : <span className="badge st-APPROVED">성공</span>}</td>
                  <td className="n">{r.latency_ms ?? "-"}</td>
                </tr>))}
            </tbody></table></div>
        </div>
      )}
    </section>
  );
}
