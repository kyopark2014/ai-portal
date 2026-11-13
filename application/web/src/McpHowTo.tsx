import { useEffect, useState } from "react";
import * as api from "./api";
import type { Item } from "./api";

type Tool = "claude" | "codex" | "agent";
const TOOLS: { id: Tool; label: string }[] = [{ id: "claude", label: "Claude Code" }, { id: "codex", label: "Codex" }, { id: "agent", label: "Agent" }];

/** JWT(Cognito) Gateway 용 전체 예제: 브라우저 OAuth 로 토큰을 받아 Strands Agent 로 호출한다. */
const JWT_AGENT = String.raw`"""Gateway MCP 를 Cognito OAuth(브라우저 로그인, Authorization Code + PKCE)로 인증해 Strands Agent 로 호출한다.
"""
import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request
import webbrowser

from strands import Agent
from strands.tools.mcp import MCPClient

URL = "__URL__"
COGNITO_DOMAIN = "__DOMAIN__"   # Cognito 도메인
CLIENT_ID = "__CLIENT_ID__"                                      # 앱 클라이언트 ID (공개 값)
PORT = 8484                                                                   # 앱 클라이언트에 등록된 콜백 포트
REDIRECT = f"http://localhost:{PORT}/callback"
SCOPE = "openid email profile"


def _token_request(**form) -> dict:
    req = urllib.request.Request(f"https://{COGNITO_DOMAIN}/oauth2/token", method="POST",
                                 data=urllib.parse.urlencode({"client_id": CLIENT_ID, **form}).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def _browser_login() -> dict:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state, got = secrets.token_urlsafe(16), {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if urllib.parse.urlparse(self.path).path != "/callback":
                self.send_response(404); self.end_headers(); return
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
            self.wfile.write("<h3>Authentication successful.<br>이 창을 닫아도 됩니다.</h3>".encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.handle_request, daemon=True).start()   # 콜백 한 번만 받는다
    url = f"https://{COGNITO_DOMAIN}/oauth2/authorize?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT, "scope": SCOPE, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256", "identity_provider": "Google"})
    print("브라우저에서 로그인하세요. 열리지 않으면 아래 주소를 여세요:\n" + url)
    webbrowser.open(url)
    for _ in range(300):                                               # 최대 5분 대기
        if got:
            break
        time.sleep(1)
    srv.server_close()
    if got.get("state") != state or "code" not in got:
        raise SystemExit(f"로그인 실패: {got.get('error_description') or got.get('error') or '응답 없음/state 불일치'}")
    return _token_request(grant_type="authorization_code", code=got["code"], redirect_uri=REDIRECT, code_verifier=verifier)


HEADERS = {"Authorization": f"Bearer {_browser_login()['access_token']}"}

try:  # mcp 2.x
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client   # MCP 기본 타임아웃(read 300s)·리다이렉트 설정

    def transport():
        return streamable_http_client(URL, http_client=create_mcp_http_client(headers=HEADERS))
except ImportError:  # mcp 1.x
    from mcp.client.streamable_http import streamablehttp_client

    def transport():
        return streamablehttp_client(URL, headers=HEADERS)

mcp = MCPClient(transport)

with mcp:
    agent = Agent(tools=mcp.list_tools_sync())
    agent("사용 가능한 도구를 알려줘")
`;

/** 서버 이름: 영문·숫자·_- 만 (CLI 인자로 안전하게). */
const slug = (n: string) => n.replace(/[^A-Za-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || "mcp-server";

/** runtime MCP → 승인 필요면 JWT Gateway, 불필요면 자신의 runtime endpoint 기준으로 가이드 항목을 만든다. */
function resolveGuide(item: Item): { item: Item; blocked?: string; lead?: string } {
  const d = item.data;
  if (d.kind !== "runtime") return { item };
  if (d.approval_required) {
    if (!d.approval_gateway_id || !d.approval_gateway_endpoint) {
      return { item, blocked: "승인 프로세스용 JWT Gateway 를 먼저 연결하세요. 연결 후 그 Gateway 를 MCP 로 등록하는 방법이 표시됩니다." };
    }
    return {
      item: {
        ...item,
        name: d.approval_gateway_name || item.name,
        data: { ...d, kind: "gateway", authorizer: "CUSTOM_JWT", endpoint: d.approval_gateway_endpoint, gateway_id: d.approval_gateway_id },
      },
      lead: `승인 필요 — JWT Gateway "${d.approval_gateway_name || d.approval_gateway_id}" 를 MCP 로 등록하세요.`,
    };
  }
  // 승인 불필요: 자신의 runtime MCP endpoint 를 직접 등록
  if (d.auth === "JWT") {
    return {
      item: { ...item, data: { ...d, kind: "gateway", authorizer: "CUSTOM_JWT", endpoint: d.endpoint } },
      lead: "승인 불필요 — 이 runtime MCP 를 직접 등록하세요 (Cognito JWT).",
    };
  }
  return {
    item: { ...item, data: { ...d, kind: "gateway", authorizer: "AWS_IAM", endpoint: d.endpoint } },
    lead: "승인 불필요 — 이 runtime MCP 를 직접 등록하세요 (IAM SigV4).",
  };
}

function build(tool: Tool, item: Item, clientId: string, domain: string): { code: string; lang: string; notes: string[] } {
  const d = item.data, name = slug(item.name), url = d.endpoint || "<MCP_ENDPOINT>";
  const jwt = d.kind === "gateway" && d.authorizer === "CUSTOM_JWT";
  const iam = d.kind === "gateway" && d.authorizer === "AWS_IAM";
  const hdrs: string[] = (d.headers ?? []).map((h: any) => h.key);
  const cid = clientId || "<COGNITO_APP_CLIENT_ID>";

  if (tool === "claude") {
    if (jwt) return { lang: "bash", code: `claude mcp add ${name} \\\n  ${url} \\\n  --transport http \\\n  --client-id ${cid} \\\n  --callback-port 8484`,
      notes: ["등록한 뒤 Claude Code 안에서 /mcp → 해당 서버 → Authenticate 를 선택하면 브라우저에서 Google 로그인합니다.", "이 MCP 의 사용 신청이 승인되어 있어야 도구가 보입니다."] };
    if (iam) return { lang: "bash", code: `# AWS_IAM 인증 Gateway 는 SigV4 서명이 필요합니다 (AWS 자격 증명이 설정되어 있어야 함)\nclaude mcp add ${name} -- uvx mcp-proxy-for-aws@latest \\\n  ${url} \\\n  --region ${d.region || "us-west-2"}`,
      notes: ["Claude Code 가 직접 서명하지 못하므로 mcp-proxy-for-aws 가 SigV4 서명을 대신합니다."] };
    return { lang: "bash", code: `claude mcp add ${name} ${url} --transport http${hdrs.map((k) => ` \\\n  --header "${k}: <VALUE>"`).join("")}`,
      notes: hdrs.length ? ["헤더 값은 보안상 숨겨져 있습니다. 등록 시 사용한 값을 넣으세요."] : [] };
  }
  if (tool === "codex") {
    if (jwt) return { lang: "bash", code: `codex mcp add ${name} \\\n  --url ${url} \\\n  --oauth-client-id ${cid}`,
      notes: [`등록한 뒤 codex mcp login ${name} 을 실행하면 브라우저에서 Google 로그인(OAuth)합니다.`,
              "이 MCP 의 사용 신청이 승인되어 있어야 도구가 보입니다."] };
    if (iam) return { lang: "bash", code: `codex mcp add ${name} -- uvx mcp-proxy-for-aws@latest \\\n  ${url} \\\n  --region ${d.region || "us-west-2"}`,
      notes: ["AWS_IAM 인증 Gateway 는 SigV4 서명이 필요해 mcp-proxy-for-aws 를 거칩니다 (AWS 자격 증명 필요)."] };
    return { lang: "bash", code: `codex mcp add ${name} --url ${url}`, notes: hdrs.length ? ["이 서버는 헤더(" + hdrs.join(", ") + ")가 필요합니다. ~/.codex/config.toml 의 http_headers 에 추가하세요."] : [] };
  }
  if (iam) return { lang: "python", code: `from mcp import StdioServerParameters, stdio_client\nfrom strands import Agent\nfrom strands.tools.mcp import MCPClient\n\n# AWS_IAM Gateway: mcp-proxy-for-aws 가 SigV4 서명\nmcp = MCPClient(lambda: stdio_client(StdioServerParameters(\n    command="uvx", args=["mcp-proxy-for-aws@latest", "${url}", "--region", "${d.region || "us-west-2"}"])))\n\nwith mcp:\n    agent = Agent(tools=mcp.list_tools_sync())\n    agent("사용 가능한 도구를 알려줘")`, notes: ["pip install strands-agents mcp"] };
  if (jwt) return { lang: "python", code: JWT_AGENT.replace("__URL__", url).replace("__DOMAIN__", domain || "<COGNITO_DOMAIN>").replace("__CLIENT_ID__", cid),
    notes: ["pip install strands-agents mcp  (mcp 1.x·2.x 모두 동작)", "test.py 로 저장해 실행하세요. 실행할 때마다 브라우저에서 Google 로그인합니다. 토큰은 메모리에만 있고 파일에 저장하지 않습니다.",
            "콜백 포트 8484 를 사용합니다(앱 클라이언트에 등록된 값). 이 MCP 의 사용 신청이 승인된 계정으로 로그인해야 도구가 보입니다."] };
  const hdr = jwt ? `{"Authorization": f"Bearer {os.environ['MCP_TOKEN']}"}` : hdrs.length ? `{${hdrs.map((k) => `"${k}": "<VALUE>"`).join(", ")}}` : "{}";
  return { lang: "python", code: `import os\nfrom strands import Agent\nfrom strands.tools.mcp import MCPClient\n\nURL = "${url}"\nHEADERS = ${hdr}\n\ntry:  # mcp 2.x\n    from mcp.client.streamable_http import streamable_http_client\n    from mcp.shared._httpx_utils import create_mcp_http_client\n\n    def transport():\n        return streamable_http_client(URL, http_client=create_mcp_http_client(headers=HEADERS))\nexcept ImportError:  # mcp 1.x\n    from mcp.client.streamable_http import streamablehttp_client\n\n    def transport():\n        return streamablehttp_client(URL, headers=HEADERS)\n\nmcp = MCPClient(transport)\n\nwith mcp:\n    agent = Agent(tools=mcp.list_tools_sync())\n    agent("사용 가능한 도구를 알려줘")`,
    notes: ["pip install strands-agents mcp  (mcp 1.x·2.x 모두 동작: 2.x 는 streamable_http_client + create_mcp_http_client, 1.x 는 streamablehttp_client)", ...(jwt ? ["MCP_TOKEN 은 Cognito 가 발급한 액세스 토큰입니다. 이 MCP 의 사용 신청이 승인되어 있어야 합니다."] : [])] };
}

export default function McpHowTo({ item }: { item: Item }) {
  const [tool, setTool] = useState<Tool | null>(null);
  const [clientId, setClientId] = useState("");
  const [domain, setDomain] = useState("");
  const [copied, setCopied] = useState(false);
  useEffect(() => { api.getConfig().then((c) => { setClientId(c.cognitoClientId ?? ""); setDomain(c.cognitoDomain ?? ""); }).catch(() => {}); }, []);
  const guide = resolveGuide(item);
  const ex = tool && !guide.blocked ? build(tool, guide.item, clientId, domain) : null;
  return (
    <>
      <h3>사용 방법</h3>
      {guide.lead && <p className="hint">{guide.lead}</p>}
      {guide.blocked ? (
        <p className="note" role="status">{guide.blocked}</p>
      ) : (<>
        <div className="actions left" role="group" aria-label="사용 방법 선택">
          {TOOLS.map((t) => (
            <button key={t.id} type="button" className={`btn ${tool === t.id ? "" : "ghost"}`} aria-pressed={tool === t.id}
                    onClick={() => { setTool(tool === t.id ? null : t.id); setCopied(false); }}>{t.label}</button>
          ))}
        </div>
        {ex && (<>
          <pre className="code">{ex.code}</pre>
          <div className="actions left"><button type="button" className="btn ghost" onClick={() => {
            navigator.clipboard?.writeText(ex.code).then(() => setCopied(true)).catch(() => {});
          }}>{copied ? "복사했습니다" : "복사"}</button></div>
          {ex.notes.map((n, i) => <p key={i} className="hint">{n}</p>)}
        </>)}
      </>)}
    </>
  );
}
