"""Gateway MCP 를 Cognito OAuth(브라우저 로그인, Authorization Code + PKCE)로 인증해 Strands Agent 로 호출한다.
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

URL = "https://gw-ai-portal-us-east-1-exhuakgukh.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
COGNITO_DOMAIN = "ai-portal-262976740991.auth.us-east-1.amazoncognito.com"   # Cognito 도메인
CLIENT_ID = "7efrabcf551hnavv2dnpnb4ehj"                                      # 앱 클라이언트 ID (공개 값)
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
            self.wfile.write("<h3>Authentication successful. 이 창을 닫아도 됩니다.</h3>".encode())

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
