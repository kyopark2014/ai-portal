import { useEffect, useState } from "react";
import * as api from "./api";
import type { AdminEntry } from "./api";
import ConfirmDialog, { type ConfirmSpec } from "./ConfirmDialog";

/** 승인할 수 있는 사람(관리자) 목록을 보고 추가·제거한다. 최초 관리자(서버 설정)는 지울 수 없다. */
export default function AdminManager() {
  const [admins, setAdmins] = useState<AdminEntry[]>([]);
  const [me, setMe] = useState("");
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [ask, setAsk] = useState<ConfirmSpec | null>(null);

  useEffect(() => { api.listAdmins().then((r) => { setAdmins(r.admins); setMe(r.me); }).catch((e) => setErr(e.message)); }, []);

  async function run(fn: () => Promise<{ admins: AdminEntry[] }>, done: string) {
    setBusy(true); setErr(""); setMsg("");
    try { setAdmins((await fn()).admins); setMsg(done); } catch (e: any) { setErr(e.message); }
    setBusy(false);
  }

  return (
    <section className="card" aria-labelledby="adm-h">
      <h2 id="adm-h">승인자 관리 <span className="hint">{admins.length}명</span></h2>
      <p className="sub">여기에 있는 사람이 승인 요청을 승인·거절할 수 있고, 승인 요청 때 승인자로 고를 수 있습니다. 추가·제거는 바로 반영됩니다.</p>
      <ul className="adm-list">
        {admins.map((a) => (
          <li key={a.email}>
            <span className="mono">{a.email}</span>
            {a.email === me && <span className="badge user">나</span>}
            {!a.removable && <span className="badge" title="서버 설정(ADMIN_EMAILS)의 최초 관리자라 삭제할 수 없습니다">최초 관리자</span>}
            {a.removable && a.email !== me && (
              <button type="button" className="link del" disabled={busy}
                onClick={() => setAsk({
                  title: `${a.email} 님을 승인자에서 뺄까요?`,
                  lines: ["이후 이 사람은 승인 화면에 접근할 수 없습니다.", "이 사람에게 지정된 대기 중 요청은 다른 관리자가 처리할 수 있게 됩니다."],
                  confirmLabel: "제거", danger: true, onConfirm: () => run(() => api.removeAdmin(a.email), `${a.email} 님을 제거했습니다.`),
                })}>제거</button>)}
          </li>))}
      </ul>
      <form className="row" onSubmit={(e) => { e.preventDefault(); if (email.trim()) run(() => api.addAdmin(email), `${email.trim()} 님을 추가했습니다.`).then(() => setEmail("")); }}>
        <input type="email" required placeholder="추가할 사람의 이메일 (Google 계정)" aria-label="추가할 관리자 이메일" value={email} onChange={(e) => setEmail(e.target.value)} disabled={busy} />
        <button className="btn" disabled={busy || !email.trim()}>추가</button>
      </form>
      <p className="hint">Google 로그인에 쓰는 이메일과 같아야 적용됩니다. 목록은 AWS 에 저장되어 재배포 후에도 유지됩니다.</p>
      {msg && <p className="note ok" role="status">{msg}</p>}
      {err && <p className="note error" role="alert">{err}</p>}
      {ask && <ConfirmDialog spec={ask} onClose={() => setAsk(null)} />}
    </section>
  );
}
