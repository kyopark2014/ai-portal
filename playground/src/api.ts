export interface User { email: string; name: string; picture: string; role: "admin" | "user" }
export interface PublicConfig { googleClientId: string }
export interface AgentMeta { id: number; name: string; description: string; agent_type: string; auth: string; status: string }
export interface Task { id: string; agent_id: number; title: string; pinned: boolean; created_at: number; updated_at: number }

/** MCP Apps iframe 페이로드(html + 초기 toolResult). */
export interface McpAppPayload {
  resourceUri: string;
  html: string;
  tool: string;
  mcpServer?: string;
  mimeType?: string;
  csp?: Record<string, unknown>;
  toolResult?: {
    content: Array<{ type: string; text?: string; [key: string]: unknown }>;
    isError?: boolean;
  };
}

/** 한 턴을 시간순 항목으로 둔다(cde-pilot 의 ToolEvent 와 같은 모양): 텍스트 · 도구 호출 · 도구 결과. */
export interface ToolEvent {
  type: "text" | "tool" | "tool_result" | "info";
  tool?: string;
  input?: unknown;
  toolUseId?: string;
  data?: string;
  mcpApp?: McpAppPayload;
}
export interface Message { seq?: number; role: "user" | "assistant"; content: string; tool_events: ToolEvent[]; status?: "streaming" | "complete" }
export interface StreamEvent {
  t?: string; tool_start?: string; tool_call?: string; input?: string; tool_result?: string;
  tool?: string; mcpApp?: McpAppPayload;
  i?: number; id?: string; done?: boolean; error?: string; status?: string;
}

const json = (method: string, body?: unknown): RequestInit => ({
  method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body),
});
async function failure(res: Response): Promise<Error> {
  let msg = `HTTP ${res.status}`;
  try { const b = await res.json(); if (typeof b.detail === "string") msg = b.detail; } catch { /* */ }
  return Object.assign(new Error(msg), { status: res.status });
}
async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { credentials: "same-origin", ...init });
  if (!res.ok) throw await failure(res);
  return res.json() as Promise<T>;
}

export const getConfig = () => call<PublicConfig>("/api/config");
export const getMe = () => call<User>("/api/me");
export const loginWithGoogle = (credential: string) => call<User>("/api/auth/google", json("POST", { credential }));
export const logout = () => call<{ ok: boolean }>("/api/auth/logout", json("POST"));

const P = "/api/playground";
export const listAgents = () => call<{ agents: AgentMeta[] }>(`${P}/agents`).then((r) => r.agents);
export const getAgent = (id: number) => call<AgentMeta>(`${P}/agents/${id}`);
export const listTasks = (agent: number) => call<{ tasks: Task[] }>(`${P}/agents/${agent}/tasks`).then((r) => r.tasks);
export const createTask = (agent: number) => call<Task>(`${P}/agents/${agent}/tasks`, json("POST"));
export const getTask = (agent: number, id: string) => call<Task & { messages: Message[] }>(`${P}/agents/${agent}/tasks/${id}`);
export const patchTask = (agent: number, id: string, body: { title?: string; pinned?: boolean }) =>
  call<Task>(`${P}/agents/${agent}/tasks/${id}`, json("PATCH", body));
export const deleteTask = (agent: number, id: string) => call<{ ok: boolean }>(`${P}/agents/${agent}/tasks/${id}`, json("DELETE"));

export async function streamChat(agent: number, task: string, content: string, onEvent: (e: StreamEvent) => void, signal?: AbortSignal) {
  const res = await fetch(`${P}/agents/${agent}/tasks/${task}/chat`, { ...json("POST", { content }), credentials: "same-origin", signal });
  if (!res.ok) throw await failure(res);
  const reader = res.body!.getReader(); const dec = new TextDecoder(); let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i: number;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const line = buf.slice(0, i).trim(); buf = buf.slice(i + 2);
      if (line.startsWith("data:")) { try { onEvent(JSON.parse(line.slice(5))); } catch { /* */ } }
    }
  }
}
