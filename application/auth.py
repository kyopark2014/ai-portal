"""Google ID token 검증 + HMAC 서명 세션 쿠키 + 역할 판정.

권한 판단은 브라우저가 아니라 서버가 한다. 역할은 쿠키에 저장하지 않고
요청마다 이메일로 계산하므로 ADMIN_EMAILS 변경이 즉시 반영된다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request
from google.auth.transport import requests as g_requests
from google.oauth2 import id_token as g_id_token

COOKIE_NAME = "portal_session"
COOKIE_VERSION = "v1"
_KEY_FILE = Path(__file__).resolve().parent / "data" / ".session_signing_key"
_key: Optional[bytes] = None


class AuthError(Exception):
    """토큰/세션이 유효하지 않다 (401)."""


@dataclass(frozen=True)
class User:
    email: str
    name: str
    picture: str
    role: str  # "admin" | "user"


def google_client_id() -> str:
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip()


def admin_emails() -> set[str]:
    raw = os.environ.get("ADMIN_EMAILS", "e2npark@gmail.com")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def role_for(email: str) -> str:
    """관리자 = 환경 변수의 최초 관리자 + 화면에서 추가한 관리자(SSM). 매 요청마다 계산하므로 추가/제거가 바로 반영된다."""
    from . import admins
    return "admin" if admins.is_admin(email) else "user"


def max_age() -> int:
    try:
        v = int(os.environ.get("SESSION_MAX_AGE_SECONDS", "") or 86400)
    except ValueError:
        return 86400
    return v if v > 0 else 86400


def _signing_key() -> bytes:
    global _key
    if _key:
        return _key
    env = os.environ.get("SESSION_SIGNING_KEY", "").strip()
    if env:
        _key = env.encode()
        return _key
    if _KEY_FILE.exists():
        _key = _KEY_FILE.read_bytes().strip()
    else:
        _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _key = secrets.token_urlsafe(48).encode()
        _KEY_FILE.write_bytes(_key)
        _KEY_FILE.chmod(0o600)
    return _key


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload: str) -> str:
    return _b64e(hmac.new(_signing_key(), payload.encode(), hashlib.sha256).digest())


def make_session(email: str, name: str, picture: str) -> str:
    payload = _b64e(json.dumps(
        {"e": email.lower(), "n": name, "p": picture, "x": int(time.time()) + max_age()},
        separators=(",", ":"),
    ).encode())
    return f"{COOKIE_VERSION}.{payload}.{_sign(payload)}"


def read_session(cookie: Optional[str]) -> User:
    if not cookie:
        raise AuthError("no session")
    parts = cookie.split(".")
    if len(parts) != 3 or parts[0] != COOKIE_VERSION:
        raise AuthError("malformed session")
    if not hmac.compare_digest(_sign(parts[1]), parts[2]):
        raise AuthError("bad signature")
    try:
        d = json.loads(_b64d(parts[1]))
    except Exception as e:  # noqa: BLE001
        raise AuthError("bad payload") from e
    if int(d.get("x", 0)) < time.time():
        raise AuthError("expired")
    email = str(d["e"])
    return User(email=email, name=str(d.get("n") or email), picture=str(d.get("p") or ""), role=role_for(email))


def verify_google_credential(credential: str) -> dict:
    """Google ID token(JWT)의 서명·iss·aud·exp 를 검증하고 claims 를 돌려준다."""
    cid = google_client_id()
    if not cid:
        raise AuthError("GOOGLE_CLIENT_ID not configured")
    try:
        claims = g_id_token.verify_oauth2_token(credential, g_requests.Request(), cid)
    except ValueError as e:
        raise AuthError(f"invalid google token: {e}") from e
    if not claims.get("email") or claims.get("email_verified") is not True:
        raise AuthError("email not verified")
    return claims


def current_user(request: Request) -> User:
    try:
        return read_session(request.cookies.get(COOKIE_NAME))
    except AuthError:
        raise HTTPException(401, "unauthenticated")
