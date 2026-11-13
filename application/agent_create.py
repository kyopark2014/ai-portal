"""에이전트 생성: AgentCore harness 를 만든다.

  이름 → 모델·시스템 프롬프트 → MCP → Skill → 인증(IAM / JWT) → 생성

  - 모델: Amazon Bedrock Claude(추론 프로파일).
  - 시스템 프롬프트: 직접 입력 또는 포털(Registry)의 prompt 한 개.
  - MCP:
      · IAM Gateway / 승인 없는 IAM runtime / 직접 등록 MCP → create_harness 에 bake (SigV4·remote_mcp).
      · JWT Gateway / 승인 프로세스에 묶인 runtime → create 시점에는 bake 하지 않고 mcp_ids 만 저장.
        호출(InvokeHarness) 때 포털이 실행자 Cognito 토큰을 발급해 remote_mcp 로 붙인다.
        (Harness 는 호출자 JWT 를 Gateway tool 로 passthrough 하지 않아, bake 하면 401 이 난다.)
  - Skill: 사용자에게 보이는 skill.
  - 인증: AWS_IAM(SigV4) 또는 Cognito JWT. JWT Gateway 를 하나라도 고르면 JWT 인증이 필요하다.
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Literal

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import auth, aws, catalog, db, gateway, mcp_test, mcp_token, usage

log = logging.getLogger("agent_create")
router = APIRouter(prefix="/api")

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")      # CreateHarness harnessName 규칙
MAX_TOOLS, MAX_SKILLS = 10, 10
_TOOLNAME = re.compile(r"[^A-Za-z0-9_-]+")


class AgentCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    model: str = Field(min_length=1, max_length=200)
    system_prompt: str = Field("", max_length=20000)       # 직접 입력
    prompt_id: int | None = None                           # 또는 포털 prompt 한 개 (둘 다 주면 prompt_id 가 우선)
    mcp_ids: list[int] = Field([], max_length=MAX_TOOLS)
    skill_ids: list[int] = Field([], max_length=MAX_SKILLS)
    auth: Literal["IAM", "JWT"] = "IAM"
    description: str = Field("", max_length=1000)


def bedrock_models() -> list[dict]:
    """에이전트에 쓸 수 있는 모델: Bedrock 의 Claude(추론 프로파일 ID). 목록은 Gateway 의 모델 목록에서 고른다."""
    return [m for m in gateway.list_models() if m["usable"] and m["provider"] == "BEDROCK"
            and m["id"].startswith(("global.anthropic.", "us.anthropic."))]


def role_arn() -> str:
    """installer 가 만든 harness 실행 역할. 기본 이름 규칙을 따른다(HARNESS_ROLE_ARN 으로 바꿀 수 있다)."""
    import os
    return (os.environ.get("HARNESS_ROLE_ARN") or catalog.config_value("harness_role_arn")
            or f"arn:aws:iam::{aws.account_id()}:role/role-harness-for-ai-portal-{aws.region()}")


def _authorizer() -> dict:
    s = mcp_token.settings()
    if not s["pool"] or not s["client"]:
        raise HTTPException(503, "JWT 인증을 쓸 수 없습니다(Cognito 가 설치되지 않았습니다). IAM 을 선택하세요")
    return {"customJWTAuthorizer": {
        "discoveryUrl": f"https://cognito-idp.{s['region']}.amazonaws.com/{s['pool']}/.well-known/openid-configuration",
        "allowedClients": [s["client"]]}}


def _tool_name(item: dict) -> str:
    return _TOOLNAME.sub("_", item["name"])[:40] or "mcp"


def _gateway_arn_tool(name: str, gateway_id: str, region: str) -> dict:
    arn = f"arn:aws:bedrock-agentcore:{region}:{aws.account_id()}:gateway/{gateway_id}"
    return {"type": "agentcore_gateway", "name": name, "config": {"agentCoreGateway": {"gatewayArn": arn}}}


def _is_jwt_gateway_mcp(d: dict) -> bool:
    """호출 시 실행자 Cognito 토큰이 필요한 MCP 인가."""
    if d.get("kind") == "gateway" and d.get("authorizer") == "CUSTOM_JWT":
        return True
    if d.get("kind") == "runtime" and d.get("approval_required") and d.get("approval_gateway_id"):
        return True
    return False


def mcp_needs_caller_jwt(ids: list[int], user: auth.User) -> bool:
    """선택한 MCP 중 JWT Gateway(또는 승인용 JWT Gateway 에 묶인 runtime)가 있으면 True."""
    for i in dict.fromkeys(ids):
        it = catalog.visible_item(i, user, "mcp")
        if _is_jwt_gateway_mcp(it.get("data") or {}):
            return True
    return False


def jwt_gateway_ids_for(ids: list[int], user: auth.User) -> list[str]:
    """호출 전 Cognito 그룹에 넣을 Gateway ID 목록."""
    out: list[str] = []
    for i in dict.fromkeys(ids):
        it = catalog.visible_item(i, user, "mcp")
        d = it.get("data") or {}
        if d.get("kind") == "gateway" and d.get("authorizer") == "CUSTOM_JWT" and d.get("gateway_id"):
            out.append(d["gateway_id"])
        elif d.get("kind") == "runtime" and d.get("approval_required") and d.get("approval_gateway_id"):
            out.append(d["approval_gateway_id"])
    return list(dict.fromkeys(out))


def gateway_targets(it: dict) -> list[str]:
    """Gateway MCP 항목의 target 이름 목록."""
    d = it.get("data") or {}
    gid = d.get("gateway_id") or ""
    if not gid:
        return []
    try:
        return aws.list_gateway_target_names(gid, d.get("region"))
    except Exception:  # noqa: BLE001
        log.warning("list_gateway_targets(%s) failed", gid, exc_info=True)
        return [i["name"] for i in db.list_items("mcp")
                if (i.get("data") or {}).get("kind") == "runtime"
                and (i.get("data") or {}).get("approval_required")
                and (i.get("data") or {}).get("approval_gateway_id") == gid]


def selectable_mcps(user: auth.User) -> list[dict]:
    """에이전트 생성 MCP 목록. «사용 가능»인 것만. 승인 필요 runtime 은 Gateway target 이므로 제외."""
    out = []
    for it in db.list_items("mcp", "", (user.email, user.role == "admin")):
        ok, _ = usage.usable_since(it, user)
        if not ok:
            continue
        d = it.get("data") or {}
        if d.get("kind") == "runtime" and d.get("approval_required"):
            continue
        row = catalog._out(it, user)
        if d.get("kind") == "gateway" and d.get("gateway_id"):
            row = {**row, "data": {**row["data"], "targets": gateway_targets(it)}}
        out.append(row)
    return out


def _load_mcps(ids: list[int], user: auth.User) -> list[dict]:
    items = []
    for i in dict.fromkeys(ids):
        it = catalog.visible_item(i, user, "mcp")
        usage.require_use(it, user)
        d = it.get("data") or {}
        if d.get("kind") == "runtime" and d.get("approval_required"):
            gw = d.get("approval_gateway_name") or d.get("approval_gateway_id") or "Gateway"
            raise HTTPException(422, f"'{it['name']}' 은 승인 프로세스 MCP 라 Gateway target 입니다. "
                                     f"에이전트에는 '{gw}' Gateway 를 선택하세요")
        if d.get("kind") == "gateway" and d.get("authorizer") not in ("AWS_IAM", "CUSTOM_JWT"):
            raise HTTPException(422, f"'{it['name']}' 은 {d.get('authorizer')} 인증 Gateway 라 에이전트에 붙일 수 없습니다")
        items.append(it)
    names = [_tool_name(it) for it in items]
    if len(set(names)) != len(names):
        raise HTTPException(422, "이름이 같은 MCP 가 여러 개입니다. 하나만 고르세요")
    return items


def bake_tools(ids: list[int], user: auth.User) -> list[dict]:
    """create_harness 에 넣을 도구. JWT Gateway 는 빼고(호출 시 remote_mcp+Bearer 로 붙임), IAM·직접 MCP 만 bake."""
    out = []
    for it in _load_mcps(ids, user):
        d = it.get("data") or {}
        if _is_jwt_gateway_mcp(d):
            continue
        if d.get("kind") == "gateway" and d.get("authorizer") == "AWS_IAM":
            out.append(_gateway_arn_tool(_tool_name(it), d["gateway_id"], d.get("region") or aws.region()))
            continue
        out.append(mcp_test.tool_for(it))
    return out


def invoke_tools(ids: list[int], user: auth.User, bearer: str | None) -> list[dict]:
    """InvokeHarness 호출마다 넘기는 전체 도구 목록. JWT Gateway 는 bearer 로 remote_mcp."""
    out = []
    for it in _load_mcps(ids, user):
        d = it.get("data") or {}
        if _is_jwt_gateway_mcp(d):
            if not bearer:
                raise HTTPException(422, "JWT Gateway 를 쓰려면 실행자 Cognito 토큰이 필요합니다")
            out.append(mcp_test.tool_for(it, bearer))
            continue
        if d.get("kind") == "gateway" and d.get("authorizer") == "AWS_IAM":
            out.append(_gateway_arn_tool(_tool_name(it), d["gateway_id"], d.get("region") or aws.region()))
            continue
        out.append(mcp_test.tool_for(it))
    return out


def _skills(ids: list[int], user: auth.User) -> list[dict]:
    return [mcp_test.skill_source(catalog.visible_item(i, user, "skill")) for i in dict.fromkeys(ids)]


@router.get("/agents/models")
def models(user: auth.User = Depends(auth.current_user)):
    try:
        return {"models": bedrock_models()}
    except gateway.GatewayError as e:
        raise HTTPException(502, e.message)


@router.get("/agents/mcps")
def mcps(user: auth.User = Depends(auth.current_user)):
    return {"items": selectable_mcps(user)}


@router.post("/agents/create")
def create(body: AgentCreateIn, user: auth.User = Depends(auth.current_user)):
    if not NAME_RE.match(body.name):
        raise HTTPException(422, "이름은 영문으로 시작하고 a-z, A-Z, 0-9, _ 만 쓸 수 있습니다 (최대 40자)")
    try:
        usable = {m["id"] for m in bedrock_models()}
    except gateway.GatewayError as e:
        raise HTTPException(502, e.message)
    if body.model not in usable:
        raise HTTPException(422, "선택할 수 없는 모델입니다")

    if body.prompt_id is not None:
        system = catalog.harness_system_prompt(user, body.prompt_id)
    else:
        system = [{"text": body.system_prompt.strip()}] if body.system_prompt.strip() else None

    need_jwt = mcp_needs_caller_jwt(body.mcp_ids, user)
    auth_kind = "JWT" if need_jwt else body.auth
    if need_jwt and body.auth != "JWT":
        log.info("forcing JWT auth for agent %s (JWT Gateway MCP selected)", body.name)

    tools, skills = bake_tools(body.mcp_ids, user), _skills(body.skill_ids, user)
    authorizer = _authorizer() if auth_kind == "JWT" else None

    if any(i["name"] == body.name for i in db.list_items("agent")):
        raise HTTPException(409, "같은 이름의 에이전트가 이미 있습니다")
    try:
        params: dict = dict(
            harnessName=body.name, executionRoleArn=role_arn(), clientToken=str(uuid.uuid4()),
            model={"bedrockModelConfig": {"modelId": body.model, "maxTokens": 4096}},
            maxIterations=10, maxTokens=8192, timeoutSeconds=300, tags={"Project": "ai-portal", "Owner": "portal"})
        if system:
            params["systemPrompt"] = system
        if tools:
            params["tools"] = tools
        if skills:
            params["skills"] = skills
        if authorizer:
            params["authorizerConfiguration"] = authorizer
        h = aws.client("bedrock-agentcore-control").create_harness(**params)["harness"]
    except (ClientError, BotoCoreError) as e:
        raise catalog.aws_err(e)

    data = {"agent_type": "harness", "arn": h.get("arn", ""), "status": h.get("status", "CREATING"), "model": body.model,
            "auth": auth_kind, "mcp_ids": body.mcp_ids, "skill_ids": body.skill_ids,
            "jwt_mcp": need_jwt}
    iid = db.insert_item("agent", body.name, body.description, user.email, data, ext_id=f"harness:{h['harnessId']}")
    if body.description.strip():
        db.update_item(iid, desc_edited=1)
    return catalog._created(iid, user)
