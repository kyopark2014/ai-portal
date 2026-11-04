import { useEffect, useState } from "react";
import * as api from "./api";
import Approvals from "./Approvals";
import Explore from "./Explore";
import AdminDashboard from "./AdminDashboard";
import MyRecords from "./MyRecords";
import { DetailModal } from "./Modals";
import GoogleButton, { disableAutoSelect } from "./GoogleButton";

const ROLE_LABEL: Record<api.Role, string> = { admin: "관리자", user: "일반 사용자" };

export default function App() {
  const [cfg, setCfg] = useState<api.PublicConfig | null>(null);
  const [user, setUser] = useState<api.User | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");
  const [opened, setOpened] = useState<api.Item | null>(null);
  const [mineKey, setMineKey] = useState(0);        // 상세에서 바꾼 뒤 목록을 다시 읽기 위한 키

  useEffect(() => {
    (async () => {
      try {
        setCfg(await api.getConfig());
        try { setUser(await api.getMe()); } catch { /* 미로그인 */ }
      } catch { setError("서버에 연결할 수 없습니다."); }
      setReady(true);
    })();
  }, []);

  async function onCredential(credential: string) {
    setError("");
    try { setUser(await api.loginWithGoogle(credential)); }
    catch { setError("로그인에 실패했습니다. 다시 시도하세요."); }
  }
  async function onLogout() {
    try { await api.logout(); } catch { /* 무시 */ }
    disableAutoSelect();
    setUser(null);
  }

  const [page, setPage] = useState<"home" | "explore" | "approvals" | "admin">("explore")
  // 메일 링크(/approvals/<recordId>)로 들어오면 로그인 후 해당 record 검토 화면을 연다
  const [openRec, setOpenRec] = useState<string | null>(() => {
    const m = location.pathname.match(/^\/approvals\/([A-Za-z0-9_-]{6,64})$/);
    return m ? m[1] : null;
  });;

  return (
    <main className="site">
      <header className="brand-bar">
        <p className="brand">AI <span>Portal</span></p>
        {user && (
          <div className="who">
            {user.picture && <img src={user.picture} alt="" referrerPolicy="no-referrer" className="avatar" />}
            <span>{user.email}</span>
            <button type="button" className="btn ghost" onClick={onLogout}>로그아웃</button>
          </div>
        )}
      </header>

      {user && (
        <nav className="nav" aria-label="주 메뉴">
          <button className={page === "explore" ? "on" : ""} onClick={() => setPage("explore")}>레지스트리 탐색</button>
          {user.can_review && <button className={page === "approvals" ? "on" : ""} onClick={() => setPage("approvals")}>승인</button>}
          {user.role === "admin" && <button className={page === "admin" ? "on" : ""} onClick={() => setPage("admin")}>관리</button>}
          <button className={page === "home" ? "on" : ""} onClick={() => setPage("home")}>내 정보</button>
        </nav>
      )}

      {error && <p className="note error" role="alert">{error}</p>}

      {!ready ? null : !user ? (
        <section className="hero">
          <h1>에이전트와 도구를<br />한곳에서 찾고 관리</h1>
          <p>Google 계정으로 로그인하면 AWS Agent Registry의 MCP 서버, 에이전트, 스킬을 탐색하고 등록할 수 있습니다.</p>
          {cfg?.googleClientId
            ? <GoogleButton clientId={cfg.googleClientId} onCredential={onCredential} onError={setError} />
            : <p className="note error">GOOGLE_CLIENT_ID 가 설정되지 않았습니다.</p>}
        </section>
      ) : page === "approvals" || openRec ? (
        user.can_review
          ? <Approvals isAdmin={user.role === "admin"} openId={openRec} onOpened={() => { setOpenRec(null); setPage("approvals"); history.replaceState(null, "", "/"); }} />
          : <section className="card"><h2>권한이 없습니다</h2><p className="sub">승인은 관리자와, 관리자가 reviewer 로 지정한 사람만 할 수 있습니다. 해당 계정으로 다시 로그인하세요.</p>
              <button className="btn ghost" onClick={onLogout}>로그아웃</button></section>
      ) : page === "admin" && user.role === "admin" ? (
        <AdminDashboard />
      ) : page === "explore" ? (
        <Explore user={user} />

      ) : (
        <>
          <section className="card">
            <h2>내 정보</h2>
            <dl className="kv">
              <div><dt>이름</dt><dd>{user.name}</dd></div>
              <div><dt>이메일</dt><dd>{user.email}</dd></div>
              <div><dt>역할</dt><dd><span className={`badge ${user.role}`}>{ROLE_LABEL[user.role]}</span></dd></div>
            </dl>
          </section>
          <MyRecords key={mineKey} onOpen={setOpened} isAdmin={user.role === "admin"} />
          {opened && user && <DetailModal item={opened} user={user} onClose={() => setOpened(null)}
            onChanged={(it) => { setOpened(it); setMineKey((k) => k + 1); }}
            onDeleted={() => { setOpened(null); setMineKey((k) => k + 1); }} />}
        </>
      )}
    </main>
  );
}
