"""JWT 인증 harness 를 Cognito OAuth(브라우저 로그인, Authorization Code + PKCE)로 받은 토큰으로 호출한다."""
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

ARN = "arn:aws:bedrock-agentcore:us-west-2:262976740991:harness/search_agent_jwt-udbmsFp6LR"
REGION = "us-west-2"
COGNITO_DOMAIN = "ai-portal-262976740991.auth.us-east-1.amazoncognito.com"     # Cognito 도메인
CLIENT_ID = "7efrabcf551hnavv2dnpnb4ehj"       # 앱 클라이언트 ID (공개 값)
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
for ev in r["stream"]:
    delta = ev.get("contentBlockDelta", {}).get("delta", {})
    if delta.get("text"):
        print(delta["text"], end="", flush=True)
print()
