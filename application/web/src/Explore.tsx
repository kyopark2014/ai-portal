import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as api from "./api";
import type { Item, Kind, User } from "./api";
import ConfirmDialog, { type ConfirmSpec } from "./ConfirmDialog";
import { CreateModal, DetailModal } from "./Modals";
import ReviewerPicker from "./ReviewerPicker";

export const KINDS: { kind: Kind; label: string; icon: string; blurb: string }[] = [
  { kind: "prompt", label: "Prompt", icon: "✎", blurb: "Amazon Bedrock Prompt Management" },
  { kind: "mcp", label: "MCP", icon: "⌁", blurb: "원격 MCP 서버" },
  { kind: "skill", label: "Skill", icon: "✦", blurb: "Git / S3 스킬" },
  { kind: "agent", label: "Agent", icon: "◈", blurb: "AgentCore harness · runtime" },
];

/** 카드 배지: Registry 상태를 사람이 읽는 말로. APPROVED 는 '등록'(등록 승인 완료)이다. */
export function statusText(st: string | null, usable?: boolean): string {
  if (st === "APPROVED" && usable) return "사용 가능";      // MCP: 등록 후 사용이 승인된(또는 소유자·관리자) 경우
  return ({ APPROVED: "등록", PENDING_APPROVAL: "승인 대기", DRAFT: "초안", REJECTED: "거절됨", DEPRECATED: "Deprecated", CREATING: "등록 중", UPDATING: "갱신 중" } as Record<string, string>)[st ?? ""] ?? (st ?? "");
}

export function ago(ts: number): string {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "방금 전";
  if (s < 3600) return `${Math.floor(s / 60)}분 전`;
  if (s < 86400) return `${Math.floor(s / 3600)}시간 전`;
  return `${Math.floor(s / 86400)}일 전`;
}

function subtitle(it: Item): string {
  const d = it.data;
  if (it.kind === "prompt") return d.model_id || "모델 미지정";
  if (it.kind === "mcp") return d.kind === "gateway" ? `Gateway · ${d.region} · ${d.authorizer} · ${d.status}` : d.endpoint;
  if (it.kind === "agent") return `${d.agent_type ?? "custom"}${d.status ? " · " + d.status : ""}`;
  return d.source_label || "";
}

function CardMenu({ name, canDelete, onOpen, onDelete, onReviewer }: { name: string; canDelete: boolean; onOpen: () => void; onDelete: () => void; onReviewer?: () => void }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const btn = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const down = (e: MouseEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false); };
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") { setOpen(false); btn.current?.focus(); } };
    document.addEventListener("mousedown", down); document.addEventListener("keydown", key);
    root.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
    return () => { document.removeEventListener("mousedown", down); document.removeEventListener("keydown", key); };
  }, [open]);
  const move = (e: React.KeyboardEvent) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const items = Array.from(root.current!.querySelectorAll<HTMLElement>('[role="menuitem"]'));
    const i = items.indexOf(document.activeElement as HTMLElement);
    items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length]?.focus();
  };
  const pick = (fn: () => void) => () => { setOpen(false); fn(); };
  return (
    <div className="menu" ref={root} onKeyDown={move}>
      <button ref={btn} type="button" className="kebab" aria-label={`${name} 메뉴`} aria-haspopup="menu" aria-expanded={open}
        onClick={() => setOpen(!open)}>⋮</button>
      {open && (
        <div className="menu-pop" role="menu">
          <button type="button" role="menuitem" onClick={pick(onOpen)}>Open</button>
          {onReviewer && <button type="button" role="menuitem" onClick={pick(onReviewer)}>Add reviewer</button>}
          {canDelete && <button type="button" role="menuitem" className="del" onClick={pick(onDelete)}>Delete</button>}
        </div>
      )}
    </div>
  );
}

export default function Explore({ user }: { user: User }) {
  const [kind, setKind] = useState<Kind>("prompt");
  const [items, setItems] = useState<Item[]>([]);
  const [q, setQ] = useState("");
  const [mine, setMine] = useState(false);
  const [view, setView] = useState<"grid" | "list">("grid");
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [creating, setCreating] = useState(false);
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null);
  const [open, setOpen] = useState<Item | null>(null);
  const [ask, setAsk] = useState<ConfirmSpec | null>(null);
  const [reviewerFor, setReviewerFor] = useState<Item | null>(null);
  const isAdmin = user.role === "admin";
  const [sync, setSync] = useState<{ running: boolean; error: string } | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setErr("");
    try { setItems(await api.listItems(kind, q)); } catch (e: any) { setErr(e.message); }
    setLoading(false);
  }, [kind, q]);

  useEffect(() => { const t = setTimeout(load, q ? 250 : 0); return () => clearTimeout(t); }, [load, q]);

  // 로그인 직후 백그라운드 동기화 진행 상황을 확인하고, 끝나면 목록을 새로 읽는다.
  useEffect(() => {
    let alive = true, was = false;
    const tick = async () => {
      try {
        const s = await api.getSync();
        if (!alive) return;
        setSync(s);
        if (was && !s.running) load();
        was = s.running;
        if (s.running) setTimeout(tick, 1500);
      } catch { /* 무시 */ }
    };
    tick();
    return () => { alive = false; };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const shown = useMemo(() => (mine ? items.filter((i) => i.owner === user.email) : items), [items, mine, user.email]);
  const meta = KINDS.find((k) => k.kind === kind)!;

  function onDelete(it: Item) {
    setAsk({
      title: `"${it.name}" 을(를) 삭제할까요?`,
      lines: [
        ...(it.kind === "prompt" && it.source === "portal" ? ["Bedrock Prompt Management의 prompt도 함께 삭제됩니다."] : []),
        ...(it.source === "aws" ? ["AWS에서 가져온 항목이라 포털 목록에서만 지워지며, 다음 동기화 때 다시 나타날 수 있습니다."] : []),
        ...(it.registry_record_id ? ["Agent Registry의 record도 함께 삭제됩니다."] : []),
        "이 작업은 되돌릴 수 없습니다."],
      confirmLabel: "삭제", danger: true,
      onConfirm: async () => {
        try { await api.remove(it.kind, it.id); setNotice({ ok: true, text: `"${it.name}" 을(를) 삭제했습니다.` }); load(); }
        catch (e: any) { setNotice({ ok: false, text: `삭제 실패: ${e.message}` }); }
      },
    });
  }

  async function resync() {
    await api.syncNow();
    setSync({ running: true, error: "" });
    const poll = async () => {
      const s = await api.getSync(); setSync(s);
      if (s.running) setTimeout(poll, 1500); else load();
    };
    setTimeout(poll, 800);
  }

  return (
    <section aria-labelledby="explore-h">
      <div className="page-head">
        <h1 id="explore-h">레지스트리 탐색</h1>
        <span className={`sync ${sync?.running ? "on" : ""}`} role="status">
          {sync?.running ? "AWS 정보 동기화 중…" : sync?.error ? "동기화 실패" : "동기화됨"}
        </span>
        <button type="button" className="btn ghost" onClick={resync} disabled={sync?.running}>새로고침</button>
      </div>

      <div className="tabs" role="tablist">
        {KINDS.map((k) => (
          <button key={k.kind} role="tab" aria-selected={k.kind === kind} className={`tab ${k.kind === kind ? "on" : ""}`}
            onClick={() => { setKind(k.kind); setQ(""); }}>
            <span aria-hidden>{k.icon}</span> {k.label}
          </button>
        ))}
      </div>
      <p className="sub">{meta.blurb}</p>

      <div className="toolbar">
        <input className="search" type="search" placeholder={`${meta.label} 검색`} aria-label="검색"
          value={q} onChange={(e) => setQ(e.target.value)} />
        <button type="button" className={`chip ${mine ? "on" : ""}`} aria-pressed={mine} onClick={() => setMine(!mine)}>내가 만든 것</button>
        <div className="seg" role="group" aria-label="보기 방식">
          <button type="button" aria-pressed={view === "list"} className={view === "list" ? "on" : ""} onClick={() => setView("list")}>목록</button>
          <button type="button" aria-pressed={view === "grid"} className={view === "grid" ? "on" : ""} onClick={() => setView("grid")}>카드</button>
        </div>
        <button type="button" className="btn" onClick={() => setCreating(true)}>+ 등록</button>
      </div>

      {notice && <p className={`note ${notice.ok ? "ok" : "error"}`} role="status">{notice.text}
        <button type="button" className="link" onClick={() => setNotice(null)}> 닫기</button></p>}
      {err && <p className="note error" role="alert">{err}</p>}
      {loading ? <p className="sub">불러오는 중…</p> : shown.length === 0 ? (
        <div className="empty-box">
          <p>{q || mine ? "조건에 맞는 항목이 없습니다." : `등록된 ${meta.label}이(가) 없습니다.`}</p>
        </div>
      ) : (
      view === "grid" ? (
        <div className="cards">
          {shown.map((it) => (
            <article className="item" key={it.id}>
              <header>
                <span className={`avatar-l k-${it.kind}`} aria-hidden>{it.name[0]?.toUpperCase()}</span>
                <h3 title={it.name}><button type="button" className="title-link" onClick={() => setOpen(it)}>{it.name}</button></h3>
                <CardMenu name={it.name} canDelete={it.can_edit} onOpen={() => setOpen(it)} onDelete={() => onDelete(it)}
                  onReviewer={isAdmin && it.registry_record_id ? () => setReviewerFor(it) : undefined} />
              </header>
              <p className="by">By {it.owner.split("@")[0]}{it.source === "aws" && <span className="badge">AWS</span>}</p>
              <div className="preview">
                <p className="desc">{it.description || <em>설명 없음</em>}</p>
                <p className="mono sm">{subtitle(it)}</p>
              </div>
              <footer>
                <span>Modified {ago(it.updated_at)}</span>
                {it.registry_status && <span className={`badge st-${it.registry_status}`}>{statusText(it.registry_status, it.usable)}</span>}
              </footer>
            </article>
          ))}
        </div>
        ) : (
        <div className="tablewrap"><table className="list-table">
          <thead><tr><th>이름</th><th>작성자</th><th>설명</th><th>수정</th><th>상태</th><th /></tr></thead>
          <tbody>{shown.map((it) => (
            <tr key={it.id}>
              <td className="nm"><span className={`avatar-l k-${it.kind}`} aria-hidden>{it.name[0]?.toUpperCase()}</span>
                <button type="button" className="title-link" title={it.name} onClick={() => setOpen(it)}>{it.name}</button></td>
              <td className="nw">{it.owner.split("@")[0]}{it.source === "aws" && <span className="badge">AWS</span>}</td>
              <td className="ds"><div className="clip2" title={it.description}>{it.description || <em>설명 없음</em>}</div>
                <div className="hint mono sm clip">{subtitle(it)}</div></td>
              <td className="nw">{ago(it.updated_at)}</td>
              <td className="nw">{it.registry_status && <span className={`badge st-${it.registry_status}`}>{statusText(it.registry_status, it.usable)}</span>}</td>
              <td className="nw"><div className="acts">
                <button type="button" className="link" onClick={() => setOpen(it)}>Open</button>
                {isAdmin && it.registry_record_id && <button type="button" className="link" onClick={() => setReviewerFor(it)}>Add reviewer</button>}
                {it.can_edit && <button type="button" className="link del" onClick={() => onDelete(it)}>Delete</button>}
              </div></td>
            </tr>))}</tbody>
        </table></div>
        )
      )}

      {ask && <ConfirmDialog spec={ask} onClose={() => setAsk(null)} />}
      {reviewerFor && <ReviewerPicker item={reviewerFor} onClose={() => setReviewerFor(null)} />}
      {creating && <CreateModal kind={kind} onClose={() => setCreating(false)} onDone={(r) => {
        setCreating(false); load();
        setNotice(r.registry_error
          ? { ok: false, text: `"${r.name}" 을(를) 저장했지만 Registry 등록은 실패했습니다: ${r.registry_error}. 상세 화면에서 다시 시도할 수 있습니다.` }
          : { ok: true, text: `"${r.name}" 을(를) 등록했습니다${r.kind === "prompt" ? " (Prompt Management 저장 + 버전 게시)" : ""} · Registry ${r.registry_status ?? ""}` });
      }} />}
      {open && <DetailModal item={open} user={user} onClose={() => setOpen(null)}
        onChanged={(it) => { setOpen(it); load(); }} onDeleted={() => { setOpen(null); load(); }} />}
    </section>
  );
}
