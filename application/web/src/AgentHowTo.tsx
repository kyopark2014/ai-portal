import { useEffect, useState } from "react";
import * as api from "./api";
import type { Item } from "./api";

/** Agent 사용 방법: AgentCore harness / runtime 을 Python(boto3)으로 호출하는 예제. (Claude Code·Codex 는 제공하지 않는다) */
/** harness 스트림 출력: 텍스트 + tool 요청/입력/결과 + 종료 사유 + 토큰 사용량. (test_hr_agent.py 와 같은 처리) */
const STREAM_LOOP = String.raw`# 텍스트 + tool 요청/응답 출력
# harness 는 메시지마다 contentBlockIndex 를 다시 쓰므로 toolUseId 로 추적한다.
block_tool_use, block_tool_result = {}, {}          # contentBlockIndex → toolUseId
tool_names, tool_inputs, tool_results = {}, {}, {}   # toolUseId → 이름 / 입력 JSON 조각 / 결과 텍스트
in_text = False


def end_text():
    global in_text
    if in_text:
        print()
        in_text = False


def fmt_json(s: str) -> str:
    try:
        return json.dumps(json.loads(s), ensure_ascii=False, indent=2) if s.strip() else "{}"
    except json.JSONDecodeError:
        return s


def result_text(part) -> str:
    items = part if isinstance(part, list) else [part]
    out = ""
    for it in items:
        if isinstance(it, dict):
            out += it["text"] if "text" in it else json.dumps(it.get("json", it), ensure_ascii=False)
        elif it is not None:
            out += str(it)
    return out


for ev in r["stream"]:
    if "messageStart" in ev:
        block_tool_use.clear()
        block_tool_result.clear()
    elif "contentBlockStart" in ev:
        cbs = ev["contentBlockStart"]
        idx, st = cbs.get("contentBlockIndex", 0), cbs.get("start") or {}
        if "toolUse" in st:                              # tool 호출 시작
            end_text()
            tid, name = (st["toolUse"].get("toolUseId") or f"idx-{idx}").strip(), st["toolUse"].get("name") or ""
            block_tool_use[idx], tool_names[tid], tool_inputs[tid] = tid, name, ""
            print(f"\n[tool_start] {name}  (id={tid})", flush=True)
        elif "toolResult" in st:                         # tool 결과 시작
            end_text()
            tid = (st["toolResult"].get("toolUseId") or "").strip()
            if tid:
                block_tool_result[idx] = tid
                tool_results.setdefault(tid, "")
                print(f"\n[tool_result_start] {tool_names.get(tid, tid)}", flush=True)
    elif "contentBlockDelta" in ev:
        cbd = ev["contentBlockDelta"]
        idx, dl = cbd.get("contentBlockIndex", 0), cbd.get("delta") or {}
        if dl.get("text"):
            in_text = True
            print(dl["text"], end="", flush=True)
        if "toolUse" in dl and block_tool_use.get(idx):  # tool 입력은 JSON 조각으로 나뉘어 온다
            tool_inputs[block_tool_use[idx]] += (dl["toolUse"] or {}).get("input") or ""
        if "toolResult" in dl and block_tool_result.get(idx):
            tool_results[block_tool_result[idx]] += result_text(dl["toolResult"])
    elif "contentBlockStop" in ev:
        idx = ev["contentBlockStop"].get("contentBlockIndex", 0)
        if idx in block_tool_use:                        # tool 입력 완성
            end_text()
            tid = block_tool_use[idx]
            print(f"\n[tool_call] {tool_names.get(tid, '')}\n{fmt_json(tool_inputs.get(tid, ''))}", flush=True)
        if idx in block_tool_result:                     # tool 결과 완성
            end_text()
            tid = block_tool_result[idx]
            print(f"\n[tool_result] {tool_names.get(tid, tid)}\n{tool_results.get(tid, '')[:4000]}", flush=True)
    elif "messageStop" in ev:
        end_text()
        print(f"\n[stop] {ev['messageStop'].get('stopReason')}", flush=True)
    elif "metadata" in ev:
        u = (ev["metadata"] or {}).get("usage") or {}
        print(f"\n[usage] input={u.get('inputTokens')} output={u.get('outputTokens')}", flush=True)
    else:
        for k in ("internalServerException", "validationException", "runtimeClientError"):
            if k in ev:
                end_text()
                print(f"\n[error] {ev[k].get('message', k)}", flush=True)
                break
end_text()`;

const JWT_HARNESS = String.raw`"""JWT 인증 harness 를 Cognito OAuth(브라우저 로그인, Authorization Code + PKCE)로 받은 토큰으로 호출한다."""
import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser

import boto3
from botocore import UNSIGNED
from botocore.config import Config

ARN = "__ARN__"
REGION = "__REGION__"
COGNITO_DOMAIN = "__DOMAIN__"     # Cognito 도메인
CLIENT_ID = "__CLIENT_ID__"       # 앱 클라이언트 ID (공개 값)
PORT = 8484                       # 앱 클라이언트에 등록된 콜백 포트
REDIRECT = f"http://localhost:{PORT}/callback"


def browser_login() -> str:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state, got = secrets.token_urlsafe(16), {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path != "/callback":
                self.send_response(404); self.end_headers(); return
            got.update({k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()})
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
            self.wfile.write("<h3>Authentication successful.<br>이 창을 닫아도 됩니다.</h3>".encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.handle_request, daemon=True).start()   # 콜백 한 번만 받는다
    url = f"https://{COGNITO_DOMAIN}/oauth2/authorize?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT, "scope": "openid email profile",
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "identity_provider": "Google"})
    print("브라우저에서 로그인하세요. 열리지 않으면 아래 주소를 여세요:\n" + url)
    webbrowser.open(url)
    for _ in range(300):                                               # 최대 5분 대기
        if got:
            break
        time.sleep(1)
    srv.server_close()
    if got.get("state") != state or "code" not in got:
        raise SystemExit(f"로그인 실패: {got.get('error_description') or got.get('error') or '응답 없음/state 불일치'}")
    req = urllib.request.Request(f"https://{COGNITO_DOMAIN}/oauth2/token", method="POST",
        data=urllib.parse.urlencode({"grant_type": "authorization_code", "client_id": CLIENT_ID, "code": got["code"],
                                     "redirect_uri": REDIRECT, "code_verifier": verifier}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)["access_token"]


TOKEN = browser_login()

# JWT harness 는 SigV4 가 아니라 Bearer 토큰으로 호출한다: 서명을 끄고 Authorization 헤더를 붙인다
client = boto3.client("bedrock-agentcore", region_name=REGION, config=Config(signature_version=UNSIGNED))
client.meta.events.register("before-call.bedrock-agentcore.InvokeHarness",
                            lambda params, **kw: params["headers"].__setitem__("Authorization", f"Bearer {TOKEN}"))

r = client.invoke_harness(
    harnessArn=ARN,
    runtimeSessionId=f"{uuid.uuid4()}-{uuid.uuid4()}",   # 33자 이상, 같은 값을 쓰면 대화가 이어집니다
    messages=[{"role": "user", "content": [{"text": "안녕하세요, 무엇을 할 수 있나요?"}]}],
)
${STREAM_LOOP}
`;

function build(item: Item, clientId: string, domain: string): { code: string; notes: string[] } | null {
  const d = item.data, arn: string = d.arn || "";
  if (!arn) return null;
  const region = arn.split(":")[3] || "us-west-2";
  if (arn.includes(":harness/") && d.auth === "JWT") return {
    code: JWT_HARNESS.replace("__ARN__", arn).replace("__REGION__", region)
      .replace("__DOMAIN__", domain || "<COGNITO_DOMAIN>").replace("__CLIENT_ID__", clientId || "<COGNITO_APP_CLIENT_ID>"),
    notes: ["pip install -U boto3", "이 harness 는 JWT 인증이라 AWS 자격 증명(SigV4)으로 호출하면 \"requires OAuth Bearer token authentication\" 오류가 납니다. 위 예제는 Cognito 에서 받은 액세스 토큰을 Bearer 로 보냅니다.",
            "test.py 로 저장해 실행하세요. 실행할 때마다 브라우저에서 Google 로그인하며, 토큰은 메모리에만 있습니다. 콜백 포트 8484 를 사용합니다(앱 클라이언트에 등록된 값).",
            "이 Agent 의 사용 신청이 승인된 계정으로 로그인해야 합니다."] };
  if (arn.includes(":harness/")) return {
    code: `import json
import uuid
import boto3

ARN = "${arn}"
client = boto3.client("bedrock-agentcore", region_name="${region}")

session = f"{uuid.uuid4()}-{uuid.uuid4()}"   # 33자 이상, 같은 값을 쓰면 대화가 이어집니다
r = client.invoke_harness(
    harnessArn=ARN,
    runtimeSessionId=session,
    messages=[{"role": "user", "content": [{"text": "안녕하세요, 무엇을 할 수 있나요?"}]}],
)

${STREAM_LOOP}`,
    notes: ["pip install boto3  (최신 버전이 필요합니다: pip install -U boto3)",
            "호출하는 AWS 계정/역할에 bedrock-agentcore:InvokeHarness 권한이 필요합니다.",
            "이 Agent 의 사용 신청이 승인되어 있어야 포털에서 테스트할 수 있습니다."] };
  return {
    code: `import json
import uuid
import boto3

ARN = "${arn}"
client = boto3.client("bedrock-agentcore", region_name="${region}")

session = f"{uuid.uuid4()}-{uuid.uuid4()}"   # 33자 이상
r = client.invoke_agent_runtime(
    agentRuntimeArn=ARN,
    runtimeSessionId=session,
    qualifier="DEFAULT",
    payload=json.dumps({"prompt": "안녕하세요, 무엇을 할 수 있나요?"}).encode(),
)

if "text/event-stream" in r.get("contentType", ""):
    for line in r["response"].iter_lines():
        if line.startswith(b"data:"):
            print(line[5:].decode().strip())
else:
    print(r["response"].read().decode())`,
    notes: ["pip install boto3",
            "호출하는 AWS 계정/역할에 bedrock-agentcore:InvokeAgentRuntime 권한이 필요합니다.",
            "payload 형식은 Agent 마다 다릅니다. 위 예제는 {\"prompt\": ...} 를 받는 Agent 기준이며, 필요한 필드는 Agent 구현에 맞게 바꾸세요.",
            "이 Agent 의 사용 신청이 승인되어 있어야 포털에서 테스트할 수 있습니다."] };
}

export default function AgentHowTo({ item }: { item: Item }) {
  const [show, setShow] = useState(false);
  const [copied, setCopied] = useState(false);
  const [clientId, setClientId] = useState("");
  const [domain, setDomain] = useState("");
  useEffect(() => { api.getConfig().then((c) => { setClientId(c.cognitoClientId ?? ""); setDomain(c.cognitoDomain ?? ""); }).catch(() => {}); }, []);
  const ex = build(item, clientId, domain);
  if (!ex) return null;
  return (
    <>
      <h3>사용 방법</h3>
      <div className="actions left" role="group" aria-label="사용 방법 선택">
        <button type="button" className={`btn ${show ? "" : "ghost"}`} aria-pressed={show}
                onClick={() => { setShow(!show); setCopied(false); }}>Python (boto3)</button>
      </div>
      {show && (<>
        <pre className="code">{ex.code}</pre>
        <div className="actions left"><button type="button" className="btn ghost" onClick={() => {
          navigator.clipboard?.writeText(ex.code).then(() => setCopied(true)).catch(() => {});
        }}>{copied ? "복사했습니다" : "복사"}</button></div>
        {ex.notes.map((n, i) => <p key={i} className="hint">{n}</p>)}
      </>)}
    </>
  );
}
