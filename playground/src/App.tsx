import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "./api";
import type { AgentMeta, Message, StreamEvent, Task, ToolEvent, User } from "./api";
import { AlertDialog } from "./AlertDialog";
import { AgentIcon, CloseIcon, LogoutIcon, MenuIcon, NewTaskIcon, ThemeIcon } from "./icons";
import GoogleButton, { disableAutoSelect } from "./GoogleButton";
import { MessageBubble } from "./MessageBubble";
import { ChatInput, type QueuedMessage } from "./ChatInput";
import { TaskListItem } from "./TaskListItem";
import { useTheme } from "./theme";

const CREATING_ALERT = "현재 Agent 생성중입니다. 생성이 완료된 후에 다시 시도하세요.";

const urlAgent = () => { const m = location.pathname.match(/^\/playground\/(\d+)\/?$/); return m ? Number(m[1]) : null; };
const SCROLL_THRESHOLD_PX = 64;

/** harness-work 와 같이: tool_start 때 카드를 올리고, tool_call 때 같은 블록(i)으로 입력을 upsert 한다. */
function toolUseIdOf(e: StreamEvent): string | undefined {
  if (e.i !== undefined && e.i !== null) return String(e.i);
  if (e.id != null) return String(e.id);
  return undefined;
}

function parseToolInput(input: unknown): unknown {
  if (typeof input === "string" && input.trim().startsWith("{")) {
    try { return JSON.parse(input); } catch { /* */ }
  }
  return input ?? "";
}

function upsertTool(events: ToolEvent[], tool: string, input: unknown, toolUseId?: string): ToolEvent[] {
  const next: ToolEvent = { type: "tool", tool, input, toolUseId };
  if (toolUseId !== undefined) {
    const idx = events.findIndex((ev) => ev.type === "tool" && ev.toolUseId === toolUseId);
    if (idx >= 0) {
      const copy = events.slice();
      copy[idx] = { ...copy[idx], ...next };
      return copy;
    }
  }
  return [...events, next];
}

/** 스트림 조각을 시간순 항목의 끝에 반영한다. 텍스트는 이어 붙이고, 도구는 start→call upsert, 결과는 새 항목. */
function applyEvent(events: ToolEvent[], e: StreamEvent): ToolEvent[] {
  if (e.t) {
    const tail = events[events.length - 1];
    if (tail?.type === "text") return [...events.slice(0, -1), { ...tail, data: (tail.data ?? "") + e.t }];
    return [...events, { type: "text", data: e.t }];
  }
  if (e.tool_start) return upsertTool(events, e.tool_start, "…", toolUseIdOf(e));
  if (e.tool_call) return upsertTool(events, e.tool_call, parseToolInput(e.input), toolUseIdOf(e));
  if (e.tool_result !== undefined) {
    const tid = toolUseIdOf(e);
    const idx = tid !== undefined
      ? events.findIndex((ev) => ev.type === "tool_result" && ev.toolUseId === tid)
      : -1;
    const next: ToolEvent = {
      type: "tool_result",
      data: e.tool_result,
      tool: e.tool,
      toolUseId: tid,
      mcpApp: e.mcpApp,
    };
    if (idx >= 0) {
      const copy = events.slice();
      const prev = copy[idx];
      copy[idx] = {
        ...prev,
        ...next,
        data: `${prev.data ?? ""}${e.tool_result}`,
        mcpApp: e.mcpApp ?? prev.mcpApp,
      };
      return copy;
    }
    return [...events, next];
  }
  return events;
}

export default function App() {
  const [cfg, setCfg] = useState<api.PublicConfig | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");
  const [agents, setAgents] = useState<AgentMeta[] | null>(null);
  const [agentId, setAgentId] = useState<number | null>(urlAgent());
  const [tasks, setTasks] = useState<Task[]>([]);
  const [taskId, setTaskId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [streamEvents, setStreamEvents] = useState<ToolEvent[]>([]);
  const [chatErr, setChatErr] = useState("");
  const [alertMsg, setAlertMsg] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [agentMenu, setAgentMenu] = useState(false);
  const { toggle: toggleTheme } = useTheme();
  // 응답 중에 보낸 메시지는 task 별 대기열에 쌓이고, 응답이 끝나면 하나씩 이어서 보낸다(cde-pilot 과 같은 동작).
  const [queued, setQueued] = useState<Record<string, QueuedMessage[]>>({});
  const [paused, setPaused] = useState<Record<string, boolean>>({});
  const queuedRef = useRef(queued);                 // 비동기 콜백이 최신 대기열을 읽도록 ref 로도 든다
  const pendingSteer = useRef<{ taskId: string; text: string } | null>(null);
  const sending = useRef(false);                    // 응답 중 여부(상태는 한 박자 늦어 연속 입력이 새는 것을 막는다)
  const messagesRef = useRef<Message[]>([]);        // 최신 화면의 메시지(비동기 콜백에서 읽는다)
  const viewTask = useRef<string | null>(null);     // 지금 화면에 열려 있는 task(응답이 끝났을 때 아직 그 task 를 보고 있는지 확인)
  const abort = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const agentPickerRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const creatingAlertedFor = useRef<number | null>(null); // CREATING 알림을 agent 당 한 번만 띄운다
  const agent = agents?.find((a) => a.id === agentId) ?? null;
  const active = tasks.find((t) => t.id === taskId) ?? null;
  const agentCreating = agent?.status === "CREATING";
  viewTask.current = taskId;
  messagesRef.current = messages;

  useEffect(() => {
    (async () => {
      try { setCfg(await api.getConfig()); try { setUser(await api.getMe()); } catch { /* 미로그인 */ } }
      catch { setError("서버에 연결할 수 없습니다."); }
      setReady(true);
    })();
  }, []);
  useEffect(() => {
    if (!user) return;
    api.listAgents().then((list) => {
      setAgents(list);
      setAgentId((cur) => (cur !== null && list.some((a) => a.id === cur) ? cur : list[0]?.id ?? null));
    }).catch((e) => { setAgents([]); setError(e.message); });
  }, [user]);
  useEffect(() => { const f = () => setAgentId(urlAgent()); addEventListener("popstate", f); return () => removeEventListener("popstate", f); }, []);

  // listAgents 는 DB 의 stale status 만 준다. 목록이 올라온 뒤 getAgent(→ refresh_agent_status)로 AWS 상태를 맞춘다.
  // CREATING 이면 채팅 진입을 막고, 백그라운드에서 상태가 READY 가 될 때까지 갱신한다.
  const agentsLoaded = agents !== null;
  useEffect(() => {
    if (!user || agentId === null || !agentsLoaded) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const apply = (meta: api.AgentMeta) => {
      setAgents((list) => {
        if (!list) return list;
        const i = list.findIndex((a) => a.id === meta.id);
        if (i < 0) return list;
        const prev = list[i];
        if (prev.status === meta.status && prev.name === meta.name && prev.description === meta.description) return list;
        const copy = list.slice();
        copy[i] = { ...prev, ...meta };
        return copy;
      });
    };
    const refresh = () => {
      api.getAgent(agentId).then((meta) => {
        if (!alive) return;
        apply(meta);
        // AWS 에서 CREATING 이 확인된 뒤에만 알림(목록의 stale CREATING 으로는 띄우지 않음).
        if (meta.status === "CREATING" && creatingAlertedFor.current !== agentId) {
          creatingAlertedFor.current = agentId;
          setAlertMsg(CREATING_ALERT);
        }
        if (meta.status !== "CREATING") creatingAlertedFor.current = null;
        if (meta.status === "CREATING" || meta.status === "UPDATING") {
          timer = setTimeout(refresh, 5000);
        }
      }).catch(() => {});
    };
    refresh();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [agentId, user, agentsLoaded]);

  // agent 를 고르면 그 agent 의 task 목록을 읽고, 가장 최근 task 를 연다(없으면 빈 화면에서 시작).
  // CREATING 중에는 채팅 진입을 하지 않는다.
  useEffect(() => {
    abort.current?.abort(); pendingSteer.current = null; sending.current = false; setStreaming(false); setStreamEvents([]); setChatErr("");
    queuedRef.current = {}; setQueued({}); setPaused({});
    setTasks([]); setTaskId(null); setMessages([]);
    if (agentId === null || !user || agentCreating) return;
    let alive = true;
    api.listTasks(agentId).then(async (list) => {
      if (!alive) return;
      setTasks(list);
      const first = [...list].sort((a, b) => b.updated_at - a.updated_at)[0];
      if (first) await openTask(agentId, first.id, alive);
    }).catch((e) => alive && setChatErr(e.message));
    return () => { alive = false; };
  }, [agentId, user, agentCreating]); // eslint-disable-line react-hooks/exhaustive-deps

  function leaveTask() {
    // 보던 task 를 떠난다: 진행 중인 응답은 중단하되, 그 task 의 대기열은 일시정지로 둔다(돌아와서 Resume 으로 이어 보낼 수 있다).
    const cur = viewTask.current;
    if (sending.current && cur && (queuedRef.current[cur] ?? []).length > 0) setPaused((p) => ({ ...p, [cur]: true }));
    pendingSteer.current = null;
    const ctl = abort.current;
    abort.current = null; ctl?.abort();
    sending.current = false; setStreaming(false); setStreamEvents([]); setChatErr("");
  }
  async function openTask(aid: number, id: string, alive = true) {
    leaveTask();
    try {
      const t = await api.getTask(aid, id);
      if (!alive) return;
      setTaskId(id); setMessages(t.messages); stick.current = true; setSidebarOpen(false);
    } catch (e: any) { setChatErr(e.message); }
  }

  // 자동 스크롤: 맨 아래 근처에 있을 때만 따라간다.
  useEffect(() => {
    const el = scrollRef.current; if (!el) return;
    const f = () => { stick.current = el.scrollHeight - el.scrollTop - el.clientHeight <= SCROLL_THRESHOLD_PX; };
    el.addEventListener("scroll", f, { passive: true }); return () => el.removeEventListener("scroll", f);
  }, []);
  useEffect(() => { if (stick.current) bottomRef.current?.scrollIntoView({ behavior: streaming ? "auto" : "smooth" }); }, [messages, streamEvents, streaming]);
  // agent 선택 메뉴: 바깥(바탕·다른 UI) 클릭 시 현재 선택 유지한 채 닫는다.
  useEffect(() => {
    if (!agentMenu) return;
    const onPointerDown = (e: MouseEvent) => {
      const t = e.target;
      if (!(t instanceof Node)) return;
      if (agentPickerRef.current?.contains(t)) return;
      setAgentMenu(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [agentMenu]);

  function pickAgent(id: number | null) {
    history.pushState(null, "", id ? `/playground/${id}` : "/playground");
    setAgentId(id); setAgentMenu(false);
  }
  async function newTask() {
    if (agentId === null) return;
    leaveTask();
    // 아직 비어 있는 task 가 있으면 새로 만들지 않고 그것을 연다(빈 task 가 쌓이지 않게).
    const empty = tasks.find((t) => t.title === "New task" && t.id !== taskId);
    if (empty) { await openTask(agentId, empty.id); return; }
    if (active && messages.length === 0) { setSidebarOpen(false); return; }
    try {
      const t = await api.createTask(agentId);
      setTasks((cur) => [t, ...cur]); setTaskId(t.id); setMessages([]); setSidebarOpen(false);
    } catch (e: any) { setChatErr(e.message); }
  }
  async function renameTask(t: Task, title: string) {
    try { const n = await api.patchTask(t.agent_id, t.id, { title }); setTasks((cur) => cur.map((x) => (x.id === t.id ? n : x))); } catch (e: any) { setChatErr(e.message); }
  }
  async function pinTask(t: Task) {
    try {
      const n = await api.patchTask(t.agent_id, t.id, { pinned: !t.pinned });
      setTasks((cur) => sortTasks(cur.map((x) => (x.id === t.id ? n : x))));
    } catch (e: any) { setChatErr(e.message); }
  }
  async function removeTask(t: Task) {
    try {
      await api.deleteTask(t.agent_id, t.id);
      if (t.id in queuedRef.current) { const n = { ...queuedRef.current }; delete n[t.id]; queuedRef.current = n; setQueued(n); }
      clearPaused(t.id);
      const rest = tasks.filter((x) => x.id !== t.id);
      setTasks(rest);
      if (t.id === taskId) {
        const next = [...rest].sort((a, b) => b.updated_at - a.updated_at)[0];
        if (next) await openTask(t.agent_id, next.id); else { setTaskId(null); setMessages([]); }
      }
    } catch (e: any) { setChatErr(e.message); }
  }

  function setQueue(taskId: string, list: QueuedMessage[]) {
    const next = { ...queuedRef.current, [taskId]: list };
    queuedRef.current = next; setQueued(next);
  }
  function clearPaused(taskId: string) {
    setPaused((p) => { if (!p[taskId]) return p; const n = { ...p }; delete n[taskId]; return n; });
  }

  /** 질문 하나를 보내고 스트림을 받는다. 끝나면 서버가 저장한 대화로 화면을 맞추고, 중지가 아니면 대기열의 다음 메시지를 이어서 보낸다. */
  async function dispatch(tid: string, text: string) {
    if (agentId === null) return;
    sending.current = true; setChatErr("");
    stick.current = true;
    const baseline = messagesRef.current.filter((m) => m.seq !== undefined).length;      // 서버에 저장돼 있는 메시지 수(낙관적 항목 제외)
    setMessages((cur) => [...cur, { role: "user", content: text, tool_events: [] }]);
    setStreaming(true); setStreamEvents([]);
    const ctl = new AbortController(); abort.current = ctl;
    let acc: ToolEvent[] = [], stopped = false;
    try {
      await api.streamChat(agentId, tid, text, (e) => {
        if (e.error) setChatErr(e.error);
        acc = applyEvent(acc, e); setStreamEvents(acc);
      }, ctl.signal);
    } catch (x: any) {
      if (x.name === "AbortError") stopped = true; else setChatErr(x.message);
    }
    if (ctl.signal.aborted) stopped = true;
    // 서버가 저장한 대화를 다시 읽어 화면과 맞춘다. 중지는 연결을 끊는 것이라 서버가 받은 만큼 저장하는 시점이 이 읽기보다 늦을 수 있다
    // → 중지했다면 이번 질문이 저장되어 메시지 수가 늘어날 때까지 잠깐 다시 읽는다. 끝내 안 늘면(저장 실패) 화면의 내용을 그대로 둔다.
    try {
      const before = baseline;
      let t = await api.getTask(agentId, tid);
      for (let i = 0; stopped && i < 10 && t.messages.length <= before; i++) {
        await new Promise((r) => setTimeout(r, 200));
        t = await api.getTask(agentId, tid);
      }
      if (!stopped || t.messages.length > before) {
        if (viewTask.current === tid) setMessages(t.messages);          // 다른 task 로 옮겨 갔다면 그 화면은 건드리지 않는다
        setTasks(await api.listTasks(agentId));
      }
    } catch { /* 목록 갱신 실패는 화면에 이미 있는 내용으로 둔다 */ }
    if (abort.current === ctl) { setStreamEvents([]); setStreaming(false); abort.current = null; sending.current = false; }

    if (stopped) {
      const steer = pendingSteer.current;
      if (steer && steer.taskId === tid) {                 // «이 메시지로 전환»: 멈춘 뒤 바로 그 메시지를 보낸다
        pendingSteer.current = null; clearPaused(tid);
        await dispatch(tid, steer.text);
        return;
      }
      if ((queuedRef.current[tid] ?? []).length > 0) setPaused((p) => ({ ...p, [tid]: true }));   // 직접 멈추면 대기열도 멈춘다
      return;
    }
    clearPaused(tid);
    await drain(tid);
  }
  async function drain(tid: string) {
    const q = queuedRef.current[tid] ?? [];
    if (q.length === 0) return;
    const [next, ...rest] = q;
    setQueue(tid, rest);
    await dispatch(tid, next.text);
  }

  const send = async (text: string) => {
    if (agentId === null) return;
    // 응답 중이면 대기열에 쌓는다(진행 중인 응답은 멈추지 않는다).
    if (sending.current && taskId) {
      setQueue(taskId, [...(queuedRef.current[taskId] ?? []), { id: crypto.randomUUID?.() ?? String(Date.now() + Math.random()), text }]);
      return;
    }
    setChatErr("");
    let tid = taskId;
    sending.current = true;                                // task 를 만드는 동안 들어온 입력도 대기열로 가도록 먼저 표시
    try {
      if (!tid) { const created = await api.createTask(agentId); tid = created.id; setTasks((cur) => [created, ...cur]); setTaskId(tid); }
    } catch (e: any) { sending.current = false; setChatErr(e.message); return; }
    await dispatch(tid, text);
  };
  const removeQueued = (id: string) => {
    if (!taskId) return;
    const rest = (queuedRef.current[taskId] ?? []).filter((m) => m.id !== id);
    setQueue(taskId, rest);
    if (rest.length === 0) clearPaused(taskId);
  };
  const resumeQueue = async () => {
    if (!taskId || sending.current) return;
    clearPaused(taskId); await drain(taskId);
  };
  /** 진행 중인 응답을 멈추고 이 대기 메시지를 바로 보낸다. 응답 중이 아니면 그냥 보낸다. */
  const steerQueued = async (id: string) => {
    if (!taskId) return;
    const q = queuedRef.current[taskId] ?? [];
    const item = q.find((m) => m.id === id);
    if (!item) return;
    setQueue(taskId, q.filter((m) => m.id !== id)); clearPaused(taskId);
    if (sending.current) { pendingSteer.current = { taskId, text: item.text }; abort.current?.abort(); return; }
    await dispatch(taskId, item.text);
  };

  async function onCredential(credential: string) {
    setError("");
    try { setUser(await api.loginWithGoogle(credential)); } catch { setError("로그인에 실패했습니다. 다시 시도하세요."); }
  }
  async function onLogout() {
    abort.current?.abort(); pendingSteer.current = null; sending.current = false;
    queuedRef.current = {}; setQueued({}); setPaused({});
    try { await api.logout(); } catch { /* 무시 */ }
    disableAutoSelect(); setUser(null); setAgents(null); setTasks([]); setMessages([]); setTaskId(null);
  }

  if (!ready) return <div className="boot-loading">Loading…</div>;
  if (!user) {
    return (
      <div className="auth-screen">
        <div className="auth-card">
          <h1>AI Portal <span>Playground</span></h1>
          <p>AI Portal에서 사용 승인을 받은 agent를 대화로 시험해 봅니다. Google 계정으로 로그인하세요.</p>
          {error && <p className="modal-error" role="alert">{error}</p>}
          {cfg?.googleClientId ? <GoogleButton clientId={cfg.googleClientId} onCredential={onCredential} onError={setError} />
            : <p className="modal-error">GOOGLE_CLIENT_ID 가 설정되지 않았습니다.</p>}
        </div>
      </div>);
  }

  const pinned = tasks.filter((t) => t.pinned), others = tasks.filter((t) => !t.pinned);
  const row = (t: Task) => (
    <TaskListItem key={t.id} task={t} active={t.id === taskId} onSelect={() => agentId !== null && openTask(agentId, t.id)}
      onDelete={() => removeTask(t)} onRename={(title) => renameTask(t, title)} onTogglePin={() => pinTask(t)} />);
  const lastIsUser = messages[messages.length - 1]?.role === "user";

  return (
    <div className={`app-shell${sidebarOpen ? " sidebar-open" : ""}`}>
      {sidebarOpen && <button type="button" className="sidebar-backdrop" aria-label="메뉴 닫기" onClick={() => setSidebarOpen(false)} />}
      <aside className={`sidebar${sidebarOpen ? " sidebar-panel-open" : ""}`}>
        <div className="sidebar-header">
          <div className="brand-row">
            <span className="brand" title={user.email}>{agent?.name ?? "Playground"} ({user.email})</span>
            <div className="sidebar-header-actions">
              <button type="button" className="sidebar-close-btn" aria-label="메뉴 닫기" onClick={() => setSidebarOpen(false)}><CloseIcon className="sidebar-icon" /></button>
              <button type="button" className="brand-logout-btn" aria-label="Logout" title="Logout" onClick={onLogout}><LogoutIcon className="sidebar-icon" /></button>
            </div>
          </div>
        </div>
        <button type="button" className="sidebar-menu-btn" disabled={agentId === null || agentCreating} onClick={newTask}>
          <NewTaskIcon className="sidebar-icon" /><span>New task</span>
        </button>
        <div className="task-list">
          {agents !== null && agents.length === 0 && (
            <p className="sidebar-note">사용할 수 있는 agent가 없습니다. AI Portal에서 agent를 선택해 사용 신청을 하고 승인을 받으세요.</p>)}
          {pinned.length > 0 && <div className="task-list-section"><div className="section-label">Pinned</div>{pinned.map(row)}</div>}
          {others.length > 0 && <div className="task-list-section">{pinned.length > 0 && <div className="section-label">Tasks</div>}{others.map(row)}</div>}
        </div>
        <div className="agent-picker" ref={agentPickerRef}>
          <button type="button" className={`sidebar-menu-btn${agentMenu ? " is-active" : ""}`} aria-expanded={agentMenu}
            disabled={!agents || agents.length === 0} onClick={() => setAgentMenu((o) => !o)}>
            <AgentIcon className="sidebar-icon" /><span>{agent ? agent.name : "Agent 선택"}</span>
          </button>
          {agentMenu && agents && (
            <div className="agent-menu" role="menu">
              {agents.map((a) => (
                <button key={a.id} type="button" role="menuitemradio" aria-checked={a.id === agentId} className={a.id === agentId ? "on" : ""} onClick={() => pickAgent(a.id)}>
                  <b>{a.name}</b><small>{a.agent_type}{a.status && a.status !== "READY" ? ` · ${a.status}` : ""}</small></button>))}
            </div>)}
        </div>
        <button type="button" className="sidebar-menu-btn" onClick={toggleTheme}><ThemeIcon className="sidebar-icon" /><span>Appearance</span></button>
        <a className="sidebar-menu-btn" href="/"><span style={{ marginLeft: 26 }}>AI Portal로 돌아가기</span></a>
      </aside>

      <div className="main-panel">
        <header className="main-header">
          <button type="button" className="menu-btn" aria-label="메뉴 열기" onClick={() => setSidebarOpen(true)}><MenuIcon className="sidebar-icon" /></button>
          <span className="main-header-title">{active && active.title !== "New task" ? active.title : agent ? agent.name : "Playground"}</span>
        </header>
        {agentCreating ? (
          <div className="chat-scroll">
            <div className="chat-thread">
              <div className="empty-state" role="alert">
                <p>{agent?.name}</p>
                <p>현재 Agent 생성중입니다. 생성이 완료된 후에 다시 시도하세요.</p>
                <p><a href="/">AI Portal로 돌아가기</a></p>
              </div>
            </div>
          </div>
        ) : (
          <>
            <div className="chat-scroll" ref={scrollRef}>
              <div className="chat-thread">
                {messages.length === 0 && !streaming && (
                  <div className="empty-state">
                    {agent ? <><p>{agent.name}</p>{agent.description && <p>{agent.description}</p>}<p>메시지를 입력해 대화를 시작하세요.</p></>
                      : <p>{agents === null ? "불러오는 중…" : "사이드바에서 agent를 선택하세요."}</p>}
                  </div>)}
                {messages.map((m, i) => <MessageBubble key={m.seq ?? `m${i}`} role={m.role} content={m.content} toolEvents={m.tool_events} />)}
                {streaming && (streamEvents.length > 0
                  ? <MessageBubble role="assistant" content="" toolEvents={streamEvents} />
                  : lastIsUser && <div className="message-row assistant"><div className="streaming-indicator" aria-label="Thinking">Thinking<span className="thinking-ellipsis" aria-hidden="true" /></div></div>)}
                {chatErr && <p className="modal-error" role="alert">{chatErr}</p>}
                <div ref={bottomRef} />
              </div>
            </div>
            <ChatInput disabled={agentId === null} waiting={streaming} onSend={send}
              onStop={() => abort.current?.abort()} focusKey={`${agentId}:${taskId}`}
              queuedMessages={taskId ? queued[taskId] ?? [] : []} queuePaused={Boolean(taskId && paused[taskId])}
              onRemoveQueued={removeQueued} onSteerQueued={steerQueued} onResumeQueue={resumeQueue} />
          </>
        )}
      </div>
      {alertMsg && (
        <AlertDialog message={alertMsg} onClose={() => setAlertMsg(null)} />
      )}
    </div>);
}

function sortTasks(list: Task[]): Task[] {
  return [...list].sort((a, b) => (a.pinned === b.pinned ? b.updated_at - a.updated_at : a.pinned ? -1 : 1));
}
