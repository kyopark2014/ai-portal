"""에이전트 생성: AgentCore harness 를 만든다.

  이름 → 모델·시스템 프롬프트 → MCP → Skill → 인증(IAM / JWT) → 생성

  - 모델: AWSome AI Gateway(OpenAI 호환 /v1)를 LiteLLM 모델로 연결한다. Gateway 키는 서버(.env)에만 있고, AgentCore Identity 의
    API key credential provider 에 한 번 저장해 ARN 으로만 참조한다(브라우저·harness 설정에 키 원문이 나가지 않는다).
  - 시스템 프롬프트: 직접 입력 또는 포털(Registry)의 prompt 한 개(생성 시점의 본문을 복사해 넣는다).
  - MCP: 사용할 수 있는 것만(사용 신청 승인·소유자). JWT 인증 Gateway 는 사용자별 토큰이 필요해 harness 에 고정할 수 없으므로 제외한다.
  - Skill: 사용자에게 보이는 skill.
  - 인증: AWS_IAM(SigV4) 또는 Cognito JWT(이 포털의 Cognito 앱 클라이언트가 발급한 토큰).
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


def _tools(ids: list[int], user: auth.User) -> list[dict]:
    out = []
    for i in dict.fromkeys(ids):
        it = catalog.visible_item(i, user, "mcp")
        usage.require_use(it, user)                                   # 사용 신청이 승인된 MCP 만 (소유자 포함)
        d = it["data"] or {}
        if d.get("kind") == "gateway" and d.get("authorizer") == "CUSTOM_JWT":
            raise HTTPException(422, f"'{it['name']}' 은 JWT 인증 Gateway 라 에이전트에 고정할 수 없습니다 (사용자별 토큰 필요)")
        out.append(mcp_test.tool_for(it))
    names = [t["name"] for t in out]
    if len(set(names)) != len(names):
        raise HTTPException(422, "이름이 같은 MCP 가 여러 개입니다. 하나만 고르세요")
    return out


def _skills(ids: list[int], user: auth.User) -> list[dict]:
    return [mcp_test.skill_source(catalog.visible_item(i, user, "skill")) for i in dict.fromkeys(ids)]


@router.get("/agents/models")
def models(user: auth.User = Depends(auth.current_user)):
    try:
        return {"models": bedrock_models()}
    except gateway.GatewayError as e:
        raise HTTPException(502, e.message)


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
        system = catalog.harness_system_prompt(user, body.prompt_id)          # 보이는 prompt 만, 본문은 Prompt Management 에서
    else:
        system = [{"text": body.system_prompt.strip()}] if body.system_prompt.strip() else None
    tools, skills = _tools(body.mcp_ids, user), _skills(body.skill_ids, user)
    authorizer = _authorizer() if body.auth == "JWT" else None

    if any(i["name"] == body.name for i in db.list_items("agent")):
        raise HTTPException(409, "같은 이름의 에이전트가 이미 있습니다")
    try:
        params: dict = dict(
            harnessName=body.name, executionRoleArn=role_arn(), clientToken=str(uuid.uuid4()),
            model={"bedrockModelConfig": {"modelId": body.model, "maxTokens": 4096}},      # MCP 테스트 harness(ai_portal)와 같은 방식: Bedrock 직접 호출
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
            "auth": body.auth, "mcp_ids": body.mcp_ids, "skill_ids": body.skill_ids}
    iid = db.insert_item("agent", body.name, body.description, user.email, data, ext_id=f"harness:{h['harnessId']}")
    if body.description.strip():
        db.update_item(iid, desc_edited=1)
    return catalog._created(iid, user)
