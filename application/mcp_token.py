"""MCP Gateway 접속 토큰 — Cognito 사용자·그룹·토큰.

신원 확인은 포털의 Google 로그인이 한다(Cognito 와 Google 을 federation 하지 않는다). 포털은 로그인한 사용자를 Cognito 풀에
만들고(AdminCreateUser, 비밀번호는 아무도 모르는 무작위 값), 서버(포털)가 관리자 인증(AdminInitiateAuth)으로 토큰을 발급한다.

  - **서버(Gateway)별 승인 = Cognito 그룹** `mcp:<gateway-id>`. 사용 신청이 승인되면 그룹에 넣고, 취소·거절되면 뺀다(+ 리프레시 토큰 폐기).
  - 토큰은 **그 MCP 의 사용이 승인된 사람에게만** 발급·표시한다. 소유자는 신청 없이 가능하다(관리자도 신청·승인이 필요).
  - 액세스 토큰은 최대 24시간이다. 오래 쓰는 것은 리프레시 토큰(최대 약 10년)이고, 클라이언트(Claude Code)가 이것으로 새 액세스 토큰을 받는다.
  - 토큰은 DB 에 저장하지 않는다. 요청할 때 그때그때 발급한다(리프레시 토큰은 Cognito 가 관리하고 폐기할 수 있다).

Cognito 를 쓸 수 없으면(미설치·권한) 모든 동작은 막히고 이유를 알려준다(fail closed).
"""
from __future__ import annotations

import os
import re
import secrets
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException

from . import auth, catalog, usage

router = APIRouter(prefix="/api")
_GW = re.compile(r"^[A-Za-z0-9_-]{4,100}$")


def settings() -> dict[str, str]:
    """Cognito 설정. 환경 변수가 우선, 없으면 config.json(installer 가 기록)."""
    g = lambda env, key: os.environ.get(env, "") or catalog.config_value(key)
    return {"pool": g("COGNITO_USER_POOL_ID", "cognito_user_pool_id"), "client": g("COGNITO_CLIENT_ID", "cognito_client_id"),
            "region": g("COGNITO_REGION", "cognito_region") or "us-east-1", "domain": g("COGNITO_DOMAIN", "cognito_domain")}


def _cognito():
    return boto3.client("cognito-idp", region_name=settings()["region"])


def _need() -> dict[str, str]:
    s = settings()
    if not s["pool"] or not s["client"]:
        raise HTTPException(503, "MCP 접속 토큰을 쓸 수 없습니다(Cognito 가 설치되지 않았습니다)")
    return s


def _err(e: Exception) -> HTTPException:
    code = e.response["Error"]["Code"] if isinstance(e, ClientError) else type(e).__name__
    return HTTPException(502, f"Cognito 오류: {code}")


def group_name(gateway_id: str) -> str:
    if not _GW.match(gateway_id or ""):
        raise ValueError("bad gateway id")
    return f"mcp:{gateway_id}"                          # Cognito 그룹 이름: 이 gateway 의 사용 승인


def _random_password() -> str:
    return secrets.token_urlsafe(24) + "Aa1!"           # 아무도 모르는 값. 로그인은 서버가 AdminInitiateAuth 로만 한다.


def ensure_user(email: str) -> str:
    """로그인한 사용자를 Cognito 에 만든다(이미 있으면 그대로). 반환: Cognito 사용자 이름(sub)."""
    s, c = _need(), _cognito()
    email = email.lower()
    try:
        u = c.admin_get_user(UserPoolId=s["pool"], Username=email)
        return u["Username"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "UserNotFoundException":
            raise _err(e)
    except BotoCoreError as e:
        raise _err(e)
    try:
        c.admin_create_user(UserPoolId=s["pool"], Username=email, MessageAction="SUPPRESS",
                            UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}])
        c.admin_set_user_password(UserPoolId=s["pool"], Username=email, Password=_random_password(), Permanent=True)
        return c.admin_get_user(UserPoolId=s["pool"], Username=email)["Username"]
    except (ClientError, BotoCoreError) as e:
        raise _err(e)


def ensure_group(gateway_id: str) -> str:
    s, c, g = _need(), _cognito(), group_name(gateway_id)
    try:
        c.create_group(UserPoolId=s["pool"], GroupName=g, Description=f"사용이 승인된 사용자 ({gateway_id})")
    except ClientError as e:
        if e.response["Error"]["Code"] != "GroupExistsException":
            raise _err(e)
    return g


def grant(email: str, gateway_id: str) -> None:
    """사용 승인: 사용자를 만들고 이 gateway 의 그룹에 넣는다."""
    ensure_user(email)
    g = ensure_group(gateway_id)
    s = settings()
    try:
        _cognito().admin_add_user_to_group(UserPoolId=s["pool"], Username=email.lower(), GroupName=g)
    except (ClientError, BotoCoreError) as e:
        raise _err(e)


def revoke(email: str, gateway_id: str) -> None:
    """사용 취소: 그룹에서 빼고, 이미 발급된 리프레시 토큰을 모두 폐기한다(액세스 토큰은 만료까지 남지만 인터셉터가 그룹을 확인한다)."""
    s, c = settings(), None
    if not s["pool"]:
        return
    c = _cognito()
    try:
        c.admin_remove_user_from_group(UserPoolId=s["pool"], Username=email.lower(), GroupName=group_name(gateway_id))
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("UserNotFoundException", "ResourceNotFoundException"):
            raise _err(e)
    try:
        c.admin_user_global_sign_out(UserPoolId=s["pool"], Username=email.lower())
    except ClientError as e:
        if e.response["Error"]["Code"] != "UserNotFoundException":
            raise _err(e)


def issue(email: str) -> dict[str, Any]:
    """토큰 발급. 액세스 토큰(Gateway 호출용) + 리프레시 토큰(갱신용). 호출 전에 사용 승인을 확인해야 한다."""
    s, c = _need(), _cognito()
    ensure_user(email)
    pw = _random_password()
    try:                                                # 비밀번호를 새로 정해 한 번 로그인에만 쓴다(저장하지 않는다)
        c.admin_set_user_password(UserPoolId=s["pool"], Username=email.lower(), Password=pw, Permanent=True)
        r = c.admin_initiate_auth(UserPoolId=s["pool"], ClientId=s["client"], AuthFlow="ADMIN_USER_PASSWORD_AUTH",
                                  AuthParameters={"USERNAME": email.lower(), "PASSWORD": pw})["AuthenticationResult"]
    except (ClientError, BotoCoreError) as e:
        raise _err(e)
    return {"access_token": r["AccessToken"], "refresh_token": r["RefreshToken"], "expires_in": r["ExpiresIn"],
            "token_type": "Bearer", "client_id": s["client"], "region": s["region"]}


@router.get("/mcp/{item_id}/token")
def get_token(item_id: int, user: auth.User = Depends(auth.current_user)):
    """이 MCP(Gateway)의 접속 토큰. 사용이 승인된 사람(또는 소유자)에게만 준다."""
    it = usage._item(item_id, user)                      # 보이지 않는 항목은 404
    if it["kind"] != "mcp":
        raise HTTPException(404, "not found")
    gid = (it["data"] or {}).get("gateway_id")
    if (it["data"] or {}).get("kind") != "gateway" or not gid:
        raise HTTPException(409, "접속 토큰은 AgentCore Gateway 에만 발급합니다")
    usage.require_use(it, user)                          # 승인 전이면 403
    out = issue(user.email)
    grant(user.email, gid)                               # 소유자도 같은 그룹 규칙으로 호출되게 한다
    out["group"] = group_name(gid)
    return out
