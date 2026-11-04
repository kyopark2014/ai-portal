import { useEffect, useRef, useState } from "react";
import * as api from "./api";
import type { Item, PickUser } from "./api";

/** record 의 reviewer 를 관리하는 창(관리자 전용). 전체 사용자 중에서 이메일·이름으로 검색해 고른다. */
export default function ReviewerPicker({ item, onClose }: { item: Item; onClose: () => void }) {
  const recordId = item.registry_record_id;
  const [reviewers, setReviewers] = useState<string[]>([]);
  const [q, setQ] = useState("");
  const [results, setResults] = useState<PickUser[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(""); const [msg, setMsg] = useState("");
  const box = useRef<HTMLDivElement>(null);
  const seq = useRef(0);

  useEffect(() => {
    box.current?.focus();
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k); return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  useEffect(() => { if (recordId) api.getReviewers(recordId).then((r) => setReviewers(r.reviewers)).catch((e) => setErr(e.message)); }, [recordId]);

  // 입력할 때마다 서버에서 검색한다(사용자가 많아도 한 번에 일부만 받는다). 늦게 도착한 이전 응답이 최신 결과를 덮지 않게 순번을 둔다.
  useEffect(() => {
    const my = ++seq.current; setLoading(true);
    const t = setTimeout(() => {
      api.searchUsers(q.trim()).then((r) => { if (my === seq.current) { setResults(r.users); setTotal(r.total); } })
        .catch((e) => { if (my === seq.current) setErr(e.message); })
        .finally(() => { if (my === seq.current) setLoading(false); });
    }, 200);
    return () => clearTimeout(t);
  }, [q]);

  async function act(fn: () => Promise<{ reviewers: string[] }>, done: string) {
    setBusy(true); setErr(""); setMsg("");
    try { setReviewers((await fn()).reviewers); setMsg(done); } catch (e: any) { setErr(e.message); }
    setBusy(false);
  }

  if (!recordId) return null;
  return (
    <div className="overlay top" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label="Reviewer 추가" tabIndex={-1} ref={box}>
        <div className="modal-head"><h2>Reviewer 추가 <span className="hint">{item.name}</span></h2><button className="x" aria-label="닫기" onClick={onClose}>×</button></div>
        <p className="hint">관리자는 모든 record 를 승인할 수 있습니다. 여기서 고른 reviewer 는 <b>이 record 만</b> 승인·거절할 수 있습니다.</p>

        <h3>현재 reviewer <span className="hint">{reviewers.length}명</span></h3>
        {reviewers.length === 0 ? <p className="sub">아직 없습니다. 관리자만 승인할 수 있습니다.</p> : (
          <ul className="adm-list">{reviewers.map((e) => (
            <li key={e}><span className="mono">{e}</span>
              <button type="button" className="link del" disabled={busy} onClick={() => act(() => api.removeReviewer(recordId, e), `${e} 님을 제거했습니다.`)}>제거</button></li>))}</ul>)}

        <h3>사용자에서 고르기</h3>
        <input className="search" type="search" placeholder="이메일 또는 이름으로 검색" aria-label="사용자 검색" autoFocus value={q} onChange={(e) => setQ(e.target.value)} />
        <p className="hint" role="status">{loading ? "검색 중…" : total === 0 ? "일치하는 사용자가 없습니다. (로그인한 적이 있는 사용자만 나옵니다)" : `${total}명 중 ${results.length}명 표시${total > results.length ? " — 검색어를 더 입력해 좁혀 보세요" : ""}`}</p>
        <ul className="adm-list pick" aria-label="검색 결과">
          {results.map((u) => {
            const already = reviewers.includes(u.email);
            return (
              <li key={u.email}>
                <span><strong>{u.name || u.email.split("@")[0]}</strong><div className="hint">{u.email}</div></span>
                {u.is_admin ? <span className="badge admin" title="관리자는 이미 모든 record 를 승인할 수 있습니다">관리자</span>
                  : already ? <span className="badge user">reviewer</span>
                  : <button type="button" className="btn" disabled={busy} onClick={() => act(() => api.addReviewer(recordId, u.email), `${u.email} 님을 추가했습니다.`)}>추가</button>}
              </li>);
          })}
        </ul>
        {msg && <p className="note ok" role="status">{msg}</p>}
        {err && <p className="note error" role="alert">{err}</p>}
        <div className="actions"><button type="button" className="btn ghost" onClick={onClose}>닫기</button></div>
      </div>
    </div>
  );
}
