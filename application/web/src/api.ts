export type Role = "admin" | "user";
export interface User { email: string; name: string; picture: string; role: Role; can_review?: boolean; reviewing?: number }
export interface PublicConfig { googleClientId: string; projectName: string; cognitoClientId?: string; cognitoDomain?: string }

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { credentials: "same-origin", ...init });
  if (!res.ok) throw Object.assign(new Error(`HTTP ${res.status}`), { status: res.status });
  return res.json() as Promise<T>;
}

export const getConfig = () => call<PublicConfig>("/api/config");
export const getMe = () => call<User>("/api/me");
export const loginWithGoogle = (credential: string) =>
  call<User>("/api/auth/google", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ credential }),
  });
export const logout = () => call<{ ok: boolean }>("/api/auth/logout", { method: "POST" });

// ── catalog ──
export type Kind = "prompt" | "mcp" | "agent" | "skill";
export interface Item {
  id: number; kind: Kind; name: string; description: string; owner: string; source: "portal" | "aws";
  ext_id: string | null; data: Record<string, any>; desc_edited: boolean; can_edit: boolean; usable?: boolean;
  registry_id: string | null; registry_record_id: string | null; registry_status: string | null;
  created_at: number; updated_at: number; status_at?: number | null; registry_error?: string;
}
export interface RegistryInfo { registry_id: string; name: string; status: string; authorizer: string; auto_approval: boolean | null }
const json = (method: string, body?: unknown): RequestInit => ({
  method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body),
});
async function callErr<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { credentials: "same-origin", ...init });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { const b = await res.json(); if (typeof b.detail === "string") msg = b.detail; else if (b.detail) msg = b.detail.map((d: any) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; "); } catch { /* */ }
    throw Object.assign(new Error(msg), { status: res.status });
  }
  return res.json() as Promise<T>;
}
export const listItems = (kind: Kind, q = "") => callErr<{ items: Item[] }>(`/api/catalog/${kind}?q=${encodeURIComponent(q)}`).then((r) => r.items);
export const create = (path: string, body: unknown) => callErr<Item>(`/api/${path}`, json("POST", body));
export const updatePrompt = (id: number, body: unknown) => callErr<Item>(`/api/prompts/${id}`, json("PUT", body));
export const refreshPrompt = (id: number) => callErr<Item>(`/api/prompts/${id}/refresh`, json("POST"));
export const testMcp = (id: number) => callErr<{ status: number; tools_status: number; body: string }>(`/api/mcp/${id}/test`, json("POST"));
export const setDescription = (kind: Kind, id: number, description: string) => callErr<Item>(`/api/catalog/${kind}/${id}/description`, json("PATCH", { description }));
export const remove = (kind: Kind, id: number) => callErr<{ ok: boolean }>(`/api/catalog/${kind}/${id}`, json("DELETE"));
export const getSync = () => callErr<{ running: boolean; last: number | null; error: string; added: Record<string, number> }>("/api/sync");
export const syncNow = () => callErr<{ started: boolean }>("/api/sync", json("POST"));
export const registerItem = (kind: Kind, id: number) => callErr<Item>(`/api/catalog/${kind}/${id}/register`, json("POST"));
export const refreshRegistry = (kind: Kind, id: number) => callErr<Item & { registry_reason: string }>(`/api/catalog/${kind}/${id}/registry/refresh`, json("POST"));
export const submitRegistry = (kind: Kind, id: number) => callErr<Item>(`/api/catalog/${kind}/${id}/registry/submit`, json("POST"));

// ── prompt 테스트 (Gateway) ──
export interface GwModel { id: string; provider: string; endpoint: "chat" | "messages" | null; usable: boolean }
export interface ChatMsg { role: "user" | "assistant"; content: string }
export const getGatewayModels = (refresh = false) =>
  callErr<{ models: GwModel[]; gateway: string }>(`/api/gateway/models${refresh ? "?refresh=true" : ""}`);

/** SSE 스트리밍 채팅. 서버 오류는 HTTP 상태로, 스트림 중 오류는 onEvent({error}) 로 전달된다. */
export async function streamChat(
  body: { model: string; messages: ChatMsg[]; prompt_id?: number | null; values?: Record<string, string>; max_tokens?: number },
  onEvent: (e: { t?: string; done?: boolean; error?: string; usage?: Record<string, number>; ms?: number }) => void,
  signal?: AbortSignal,
) {
  const res = await fetch("/api/chat", { ...json("POST", body), credentials: "same-origin", signal });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { const b = await res.json(); if (typeof b.detail === "string") msg = b.detail; } catch { /* */ }
    throw new Error(msg);
  }
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
export const cancelApproval = (kind: Kind, id: number) => callErr<Item>(`/api/catalog/${kind}/${id}/registry/cancel`, json("POST"));

// ── 승인 큐 (관리자) ──
export type RecStatus = "PENDING_APPROVAL" | "APPROVED" | "REJECTED" | "DEPRECATED" | "DRAFT";
export interface RegRecord {
  recordId: string; name: string; description: string; descriptorType: string; status: string;
  recordVersion: string; createdAt: string | null; updatedAt: string | null;
  portal: { id: number; kind: Kind; name: string; owner: string } | null;
  reviewers?: string[]; can_decide?: boolean;
}
export interface RegDetail extends RegRecord { statusReason: string; content: unknown; registryId: string }
export const listApprovals = (status: RecStatus = "PENDING_APPROVAL") =>
  callErr<{ records: RegRecord[]; registryId: string }>(`/api/approvals?status=${status}`);
export const getApproval = (id: string) => callErr<RegDetail>(`/api/approvals/${encodeURIComponent(id)}`);
export const resetDeprecated = (id: string) =>
  callErr<unknown>(`/api/approvals/${encodeURIComponent(id)}/reset`, json("POST", {}));
export const decide = (id: string, decision: "APPROVED" | "REJECTED" | "DEPRECATED", reason: string) =>
  callErr<{ recordId: string; status: string; statusReason: string }>(
    `/api/approvals/${encodeURIComponent(id)}/decision`, json("POST", { decision, reason }));

// ── MCP 테스트 (harness agent) ──
export interface McpEvent {
  t?: string; tool_start?: string; tool_call?: string; input?: string; tool_result?: string; i?: number;
  stop?: string; done?: boolean; error?: string; status?: string; usage?: Record<string, number | null>;
}
export const mcpTestStatus = () => callErr<{ ready: boolean; harness_arn: string }>("/api/mcp/test-status");
export async function streamMcpChat(
  id: number, body: { messages: ChatMsg[]; session_id: string; prompt_id?: number | null }, onEvent: (e: McpEvent) => void, signal?: AbortSignal,
  path: "mcp" | "skills" = "mcp",
) {
  const res = await fetch(`/api/${path}/${id}/chat`, { ...json("POST", body), credentials: "same-origin", signal });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { const b = await res.json(); if (typeof b.detail === "string") msg = b.detail; } catch { /* */ }
    throw new Error(msg);
  }
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

// ── Agent 테스트 ──
export async function streamAgentChat(
  id: number, body: { messages: ChatMsg[]; session_id: string; prompt_id?: number | null }, onEvent: (e: McpEvent) => void, signal?: AbortSignal,
) {
  const res = await fetch(`/api/agents/${id}/chat`, { ...json("POST", body), credentials: "same-origin", signal });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { const b = await res.json(); if (typeof b.detail === "string") msg = b.detail; } catch { /* */ }
    throw new Error(msg);
  }
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
export const streamSkillChat: typeof streamMcpChat = (id, body, onEvent, signal) => streamMcpChat(id, body, onEvent, signal, "skills");

// ── 내 레코드 ──
export const myRecords = () => callErr<{ items: Item[]; counts: Record<string, number>; total: number }>("/api/my/records");

// ── 관리자(승인 가능한 사람) 관리 ──
export interface AdminEntry { email: string; removable: boolean }
export const listAdmins = () => callErr<{ admins: AdminEntry[]; me: string }>("/api/admins");
export const addAdmin = (email: string) => callErr<{ admins: AdminEntry[]; me: string }>("/api/admins", json("POST", { email }));
export const removeAdmin = (email: string) => callErr<{ admins: AdminEntry[]; me: string }>(`/api/admins?email=${encodeURIComponent(email)}`, json("DELETE"));

// ── 관리자 대시보드 / 레지스트리 관리 ──
export interface UserStat {
  email: string; name: string; login_count: number; first_login: number | null; last_login: number | null;
  kinds: Record<string, Record<string, number>>; statuses: Record<string, number>; total: number; is_admin: boolean;
}
export interface Dashboard {
  totals: { total_logins: number; unique_users: number; active_7d: number };
  users: UserStat[]; recent_logins: { email: string; name: string; logged_at: number }[];
  registry: { by_type: Record<string, Record<string, number>>; total: number; error: string }; admins: string[];
}
export const adminDashboard = () => callErr<Dashboard>("/api/admin/dashboard");
export const myRecordsAll = () => callErr<{ items: Item[]; counts: Record<string, number>; total: number }>("/api/my/records?scope=all");

// ── record 별 reviewer (관리자 전용) ──
export interface PickUser { email: string; name: string; is_admin: boolean; last_login: number | null }
export const searchUsers = (q: string, limit = 20) =>
  callErr<{ users: PickUser[]; total: number }>(`/api/reviewers/users?q=${encodeURIComponent(q)}&limit=${limit}`);
export const getReviewers = (recordId: string) => callErr<{ reviewers: string[] }>(`/api/reviewers/${encodeURIComponent(recordId)}`);
export const addReviewer = (record_id: string, email: string) => callErr<{ reviewers: string[] }>("/api/reviewers", json("POST", { record_id, email }));
export const removeReviewer = (record_id: string, email: string) =>
  callErr<{ reviewers: string[] }>(`/api/reviewers?record_id=${encodeURIComponent(record_id)}&email=${encodeURIComponent(email)}`, json("DELETE"));

export const registryInfo = () => callErr<RegistryInfo>("/api/registry");

// ── 승인 자동화 정책 ──
export interface Policy { auto_approval: boolean; auto_review: boolean; changed_by?: string; changed_at?: number }
export const getPolicy = () => callErr<Policy>("/api/policy");
export const setPolicy = (body: Partial<Pick<Policy, "auto_approval" | "auto_review">>) => callErr<Policy>("/api/policy", json("PUT", body));

// ── MCP 사용 신청 ──
export interface UsageState {
  applicable: boolean; registered?: boolean; can_use?: boolean; can_request?: boolean;
  status?: "OWNER" | "NONE" | "PENDING" | "APPROVED" | "REJECTED"; reason?: string;
}
export interface UsageRequest {
  item_id: number; email: string; status: "PENDING" | "APPROVED" | "REJECTED"; requested_at: number;
  decided_at: number | null; decided_by: string; reason: string;
  mcp: { id: number; name: string; owner: string; description: string };
}
export const myUsage = (id: number) => callErr<UsageState>(`/api/mcp/${id}/usage`);
export const cancelUsage = (id: number) => callErr<UsageState>(`/api/mcp/${id}/usage`, json("DELETE"));
export const requestUsage = (id: number) => callErr<UsageState>(`/api/mcp/${id}/usage`, json("POST"));
export const listUsageRequests = (status: "PENDING" | "APPROVED" | "REJECTED" = "PENDING") =>
  callErr<{ requests: UsageRequest[] }>(`/api/usage-requests?status=${status}`);
export const decideUsage = (itemId: number, email: string, decision: "APPROVED" | "REJECTED", reason = "") =>
  callErr<unknown>(`/api/usage-requests/${itemId}/${encodeURIComponent(email)}/decision`, json("POST", { decision, reason }));

// ── 에이전트 생성 (harness) ──
export interface AgentCreateBody {
  name: string; model: string; system_prompt: string; prompt_id: number | null;
  mcp_ids: number[]; skill_ids: number[]; auth: "IAM" | "JWT"; description?: string;
}
export const createAgent = (body: AgentCreateBody) => callErr<Item & { registry_error?: string }>("/api/agents/create", json("POST", body));
