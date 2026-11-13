"""AI Portal — FastAPI (REST API + 빌드된 React SPA 서빙)."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import admins, agent_create, agent_test, approvals, auth, catalog, chat, db, mcp_apps, mcp_test, mcp_token, playground_api, usage

DIST = Path(__file__).resolve().parent / "web" / "dist"
_docs = os.environ.get("ENABLE_API_DOCS") == "1"
app = FastAPI(
    title="AI Portal",
    docs_url="/docs" if _docs else None,
    redoc_url=None,
    openapi_url="/openapi.json" if _docs else None,
)


# script-src 에 'unsafe-inline' 이 필요하다:
# - MCP Apps(weather 등)는 sandboxed srcdoc iframe 으로 렌더되며 부모 문서 CSP 를 상속한다.
# - 'unsafe-inline' 이 없으면 iframe UI 스크립트·bootstrap 이 막혀 예보가 비어 보인다
#   (agentic-work b19eda4 와 동일).
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://accounts.google.com https://apis.google.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "img-src 'self' data: blob: https:; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "connect-src 'self' https://accounts.google.com https://oauth2.googleapis.com "
    "https://www.googleapis.com; "
    "frame-src 'self' https://accounts.google.com; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "object-src 'none'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    # no-referrer 는 localhost HTTP 에서 Google Identity Services 로그인을 깨뜨린다.
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    # MCP Apps srcdoc 이 부모 CSP 를 상속하므로 script-src 'unsafe-inline' 을 반드시 포함한다.
    resp.headers["Content-Security-Policy"] = _CONTENT_SECURITY_POLICY
    if request.url.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


current_user = auth.current_user


def require_admin(user: auth.User = Depends(current_user)) -> auth.User:
    if user.role != "admin":
        raise HTTPException(403, "admin only")
    return user


def _user_json(u: auth.User) -> dict:
    # can_review: 관리자이거나 한 record 라도 reviewer 로 지정됨 → 화면의 "승인" 메뉴 표시 여부. 실제 권한은 서버가 요청마다 판단한다.
    try:
        reviewing = len(admins.reviewing(u.email))
    except Exception:  # noqa: BLE001 — 저장소 장애여도 로그인/조회는 막지 않는다(fail closed: reviewer 로 보지 않음)
        reviewing = 0
    return {"email": u.email, "name": u.name, "picture": u.picture, "role": u.role,
            "can_review": u.role == "admin" or reviewing > 0, "reviewing": reviewing}


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/config")
def public_config():
    """브라우저에 필요한 공개 값만 반환한다."""
    # cognitoClientId: 비밀이 아닌 공개 앱 클라이언트 ID(시크릿 없음). MCP '사용 방법' 예제에 들어간다.
    return {"googleClientId": auth.google_client_id(), "projectName": "AI Portal",
            "cognitoClientId": mcp_token.settings()["client"], "cognitoDomain": mcp_token.settings().get("domain", "")}


class GoogleLogin(BaseModel):
    credential: str


@app.post("/api/auth/google")
def login_google(body: GoogleLogin, request: Request, response: Response):
    try:
        claims = auth.verify_google_credential(body.credential)
    except auth.AuthError as e:
        logging.getLogger("uvicorn.error").warning("google login failed: %s", e)
        raise HTTPException(401, "invalid google credential")
    email = claims["email"]
    cookie = auth.make_session(email, claims.get("name") or email, claims.get("picture") or "")
    secure = os.environ.get("COOKIE_SECURE") == "1" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        auth.COOKIE_NAME, cookie, max_age=auth.max_age(),
        httponly=True, secure=secure, samesite="lax", path="/",
    )
    try:    # 접속 기록(통계용)은 로그인을 막지 않는다. 이메일·이름만 저장한다.
        db.record_login(email, claims.get("name") or "")
    except Exception:  # noqa: BLE001
        pass
    try:    # MCP 접속용 Cognito 사용자도 만든다(신원은 위의 Google 검증). 실패해도 로그인은 막지 않는다.
        mcp_token.ensure_user(email)
    except Exception:  # noqa: BLE001
        pass
    catalog.start_sync()  # 최초 로그인 시 AWS 정보를 백그라운드로 갱신
    return _user_json(auth.read_session(cookie))


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(user: auth.User = Depends(current_user)):
    return _user_json(user)


@app.get("/api/admin/ping")
def admin_ping(user: auth.User = Depends(require_admin)):
    """관리자 전용 엔드포인트의 골격(이후 승인 큐 등이 이 의존성을 쓴다)."""
    return {"ok": True, "admin": user.email}


app.include_router(catalog.router)
app.include_router(chat.router)
app.include_router(mcp_test.router)
app.include_router(mcp_apps.router)
app.include_router(agent_create.router)
app.include_router(agent_test.router)
app.include_router(approvals.router)
app.include_router(approvals.admins_router)
app.include_router(approvals.reviewers_router)
app.include_router(usage.router)
app.include_router(mcp_token.router)
app.include_router(approvals.dashboard_router)
app.include_router(playground_api.router)


# ── Playground (/playground): 같은 컨테이너·같은 도메인·같은 세션 쿠키. 별도 React 앱(playground/)을 빌드해 둔 것을 서빙한다 ──
PG_DIST = Path(__file__).resolve().parent / "playground_web" / "dist"
if (PG_DIST / "assets").is_dir():
    app.mount("/playground/assets", StaticFiles(directory=PG_DIST / "assets"), name="playground-assets")


@app.get("/playground", include_in_schema=False)
@app.get("/playground/{path:path}", include_in_schema=False)
def playground_spa(path: str = ""):
    target = (PG_DIST / path).resolve()
    if path and target.is_file() and PG_DIST.resolve() in target.parents:     # favicon 같은 정적 파일
        return FileResponse(target)
    index = PG_DIST / "index.html"
    if not index.is_file():
        raise HTTPException(503, "playground not built (run ./run_local.sh)")
    return FileResponse(index, headers={"Cache-Control": "no-cache"})


# ── SPA ──
if (DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    if path.startswith("api/"):
        raise HTTPException(404)
    target = (DIST / path).resolve()
    if path and target.is_file() and DIST.resolve() in target.parents:
        return FileResponse(target)
    index = DIST / "index.html"
    if not index.is_file():
        raise HTTPException(503, "frontend not built (run ./run_local.sh)")
    return FileResponse(index, headers={"Cache-Control": "no-cache"})
