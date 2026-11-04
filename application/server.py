"""AI Portal — FastAPI (REST API + 빌드된 React SPA 서빙)."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent_test, approvals, auth, catalog, chat, db, mcp_test

DIST = Path(__file__).resolve().parent / "web" / "dist"
_docs = os.environ.get("ENABLE_API_DOCS") == "1"
app = FastAPI(
    title="AI Portal",
    docs_url="/docs" if _docs else None,
    redoc_url=None,
    openapi_url="/openapi.json" if _docs else None,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    if request.url.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


current_user = auth.current_user


def require_admin(user: auth.User = Depends(current_user)) -> auth.User:
    if user.role != "admin":
        raise HTTPException(403, "admin only")
    return user


def _user_json(u: auth.User) -> dict:
    return {"email": u.email, "name": u.name, "picture": u.picture, "role": u.role}


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/config")
def public_config():
    """브라우저에 필요한 공개 값만 반환한다."""
    return {"googleClientId": auth.google_client_id(), "projectName": "AI Portal"}


class GoogleLogin(BaseModel):
    credential: str


@app.post("/api/auth/google")
def login_google(body: GoogleLogin, request: Request, response: Response):
    try:
        claims = auth.verify_google_credential(body.credential)
    except auth.AuthError:
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
app.include_router(agent_test.router)
app.include_router(approvals.router)
app.include_router(approvals.admins_router)
app.include_router(approvals.dashboard_router)


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
