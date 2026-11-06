"""MCP 테스트: 선택한 MCP(직접 등록 / AgentCore Gateway)를 harness agent 의 도구로 붙여 대화한다.

harness `ai_portal`(installer.py --harness-only)의 InvokeHarness 를 호출하고, 호출마다 `tools` 로
테스트 대상 MCP 를 지정한다. 스트림의 텍스트와 도구 호출/결과를 SSE 로 중계한다.

  - Gateway(IAM)   → agentcore_gateway 도구 (harness 실행 역할이 SigV4 로 호출)
  - 직접 등록 MCP  → remote_mcp 도구 (url + 사용자가 입력한 헤더)
Gateway 중 CUSTOM_JWT 인증은 지원하지 않는다(토큰이 필요).
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Iterator

from botocore.config import Config
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import auth, aws, catalog, db

log = logging.getLogger("mcp_test")
router = APIRouter(prefix="/api")
MAX_TURNS = 20
MAX_CHARS = 30000
_TOOLNAME = re.compile(r"[^A-Za-z0-9_-]+")


def actor_id(email: str) -> str:
    """메모리 actorId 형식 [a-zA-Z0-9][a-zA-Z0-9-_/]* 에 맞춘 안정적인 사용자 식별자(이메일 원문은 쓰지 않는다)."""
    return "u-" + uuid.uuid5(uuid.NAMESPACE_URL, email.lower()).hex[:24]


def harness_arn() -> str:
    import os
    return os.environ.get("HARNESS_ARN") or catalog.config_value("harness_arn")


def tool_for(item: dict, bearer: str | None = None) -> dict:
    """MCP 항목 → InvokeHarness tools 항목.

    JWT(Cognito) 인증 Gateway 는 harness 의 IAM 서명으로 부를 수 없으므로, 포털이 이 사용자의 액세스 토큰을 발급해(bearer)
    `Authorization: Bearer` 헤더를 붙인 remote MCP 로 연결한다. 토큰은 호출마다 새로 만들고 저장·로그하지 않는다."""
    d = item["data"]
    name = _TOOLNAME.sub("_", item["name"])[:40] or "mcp"
    if d.get("kind") == "gateway" and d.get("authorizer") == "CUSTOM_JWT":
        if not bearer:
            raise HTTPException(422, "JWT 인증 Gateway 를 테스트하려면 접속 토큰이 필요합니다")
        if not d.get("endpoint"):
            raise HTTPException(422, "endpoint 가 없습니다")
        return {"type": "remote_mcp", "name": name, "config": {"remoteMcp": {"url": d["endpoint"], "headers": {"Authorization": f"Bearer {bearer}"}}}}
    if d.get("kind") == "gateway":
        if d.get("authorizer") != "AWS_IAM":
            raise HTTPException(422, f"{d.get('authorizer')} 인증 Gateway 는 테스트할 수 없습니다 (AWS_IAM, CUSTOM_JWT 만 지원)")
        gid = d["gateway_id"]
        reg = d.get("region") or aws.region()
        arn = f"arn:aws:bedrock-agentcore:{reg}:{aws.account_id()}:gateway/{gid}"
        return {"type": "agentcore_gateway", "name": name, "config": {"agentCoreGateway": {"gatewayArn": arn}}}
    if not d.get("endpoint"):
        raise HTTPException(422, "endpoint 가 없습니다")
    cfg: dict[str, Any] = {"url": d["endpoint"]}
    if d.get("headers"):
        cfg["headers"] = {h["key"]: h["value"] for h in d["headers"]}
    return {"type": "remote_mcp", "name": name, "config": {"remoteMcp": cfg}}


def relay_harness_stream(stream) -> Iterator[dict]:
    """InvokeHarness 이벤트 스트림 → 정규화 이벤트(t / tool_start / tool_call / tool_result / stop / done / error)."""
    tools: dict[int, dict] = {}
    result_len = 0
    for ev in stream:
        if "contentBlockStart" in ev:
            st = ev["contentBlockStart"].get("start", {})
            idx = ev["contentBlockStart"].get("contentBlockIndex", 0)
            if "toolUse" in st:
                tools[idx] = {"name": st["toolUse"].get("name", ""), "input": ""}
                yield {"tool_start": tools[idx]["name"], "i": idx}
        elif "contentBlockDelta" in ev:
            idx = ev["contentBlockDelta"].get("contentBlockIndex", 0)
            dl = ev["contentBlockDelta"].get("delta", {})
            if dl.get("text"):
                yield {"t": dl["text"]}
            elif "toolUse" in dl and idx in tools:
                tools[idx]["input"] += dl["toolUse"].get("input", "")
            elif "toolResult" in dl:
                out = "".join(b.get("text", "") for b in dl["toolResult"] if isinstance(b, dict))
                room = max(0, 8000 - result_len)          # 화면/브라우저 보호: 결과 전체를 8000자로 제한
                result_len += len(out)
                if room:
                    yield {"tool_result": out[:room], "i": idx}
        elif "contentBlockStop" in ev:
            idx = ev["contentBlockStop"].get("contentBlockIndex", 0)
            if idx in tools and tools[idx]["input"]:
                yield {"tool_call": tools[idx]["name"], "input": tools[idx]["input"][:2000], "i": idx}
        elif "messageStop" in ev:
            yield {"stop": ev["messageStop"].get("stopReason")}
        elif "metadata" in ev:
            u = ev["metadata"].get("usage", {})
            yield {"done": True, "usage": {"input_tokens": u.get("inputTokens"), "output_tokens": u.get("outputTokens")}}
        else:
            for k in ("internalServerException", "validationException", "runtimeClientError"):
                if k in ev:
                    yield {"error": ev[k].get("message", k)}
                    return


_WEB = re.compile(r"^(?P<repo>https://[^/]+/[^/]+/[^/]+?)(?:\.git)?/(?:tree|blob)/(?P<ref>[^/]+)/(?P<path>.+?)/?$")


def skill_source(item: dict) -> dict:
    """Skill 항목 → InvokeHarness `skills` 항목.

    Git 은 harness 가 `git clone` 하므로 GitHub 웹 주소(.../tree/<ref>/<path>)를 그대로 줄 수 없다.
    repo URL 과 하위 경로로 분리해서 넘긴다. 사용자가 subdirectory 를 따로 적었으면 그것이 우선한다.
    """
    d = item["data"]
    if d.get("source_type") == "s3":
        uri = d.get("s3_uri", "")
        if not uri.startswith("s3://"):
            raise HTTPException(422, "S3 skill 의 URI 가 올바르지 않습니다")
        return {"s3": {"uri": uri if uri.endswith("/") else uri + "/"}}
    url = (d.get("git_url") or "").strip()
    path = (d.get("subdirectory") or "").strip().strip("/")
    m = _WEB.match(url)
    if m:                                    # 웹 주소 → repo + 하위 경로
        url, path = m["repo"], path or m["path"]
    elif url.endswith("/") or url.endswith(".git"):
        url = url[:-4] if url.endswith(".git") else url.rstrip("/")
    if not url.startswith("https://"):
        raise HTTPException(422, "Git skill 은 https:// 주소만 테스트할 수 있습니다")
    git: dict[str, Any] = {"url": url}
    if path:
        git["path"] = path
    if d.get("credential_provider_arn"):
        git["auth"] = {"credentialArn": d["credential_provider_arn"],
                       **({"username": d["git_username"]} if d.get("git_username") else {})}
    return {"git": git}


class Msg(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=8000)        # 에러·중단으로 비어 버린 assistant 턴은 허용하고 아래에서 걸러낸다


def clean_history(msgs: list[Msg]) -> list[dict]:
    """빈 assistant 턴을 버린다. 그 외 빈 메시지(사용자)는 거절한다."""
    out = [m.model_dump() for m in msgs if not (m.role == "assistant" and not m.content.strip())]
    if not out or any(not m["content"].strip() for m in out):
        raise HTTPException(422, "빈 메시지는 보낼 수 없습니다")
    return out


class McpChatIn(BaseModel):
    prompt_id: int | None = None          # 선택: harness 의 system prompt 로 쓸 prompt. 없으면 기본(harness_prompt)
    messages: list[Msg] = Field(min_length=1, max_length=MAX_TURNS)
    session_id: str = Field(min_length=33, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


@router.get("/mcp/test-status")
def status(user: auth.User = Depends(auth.current_user)):
    return {"ready": bool(harness_arn()), "harness_arn": harness_arn()}


# harness 기본 이미지(Amazon Linux 2023)에는 node/npm 이 없다. harness-work(220e457)처럼 호출 전에 세션마다 한 번 설치한다.
# 단, 이 이미지는 apt-get 이 아니라 microdnf(`dnf` 로 연결됨, -q 옵션 미지원)라 dnf 를 먼저 쓰고 apt-get 은 폴백으로 둔다.
NODE_INSTALL_COMMAND = (
    "command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1 "
    "|| dnf install -y nodejs npm "
    "|| apt-get install -y -qq --no-install-recommends nodejs npm "
    "|| (apt-get update -qq && apt-get install -y -qq --no-install-recommends nodejs npm)"
)
_node_ready: set[str] = set()


def ensure_node_installed(rt, arn: str, session: str) -> None:
    """세션에 nodejs/npm 을 설치한다(세션당 1회, best effort — 실패해도 대화는 계속한다).

    InvokeAgentRuntimeCommand 는 harness 가 관리하는 runtime ARN 이 아니라 **harness ARN** 을 받는다.
    """
    if session in _node_ready:
        return
    try:
        r = rt.invoke_agent_runtime_command(agentRuntimeArn=arn, runtimeSessionId=session,
                                            body={"command": NODE_INSTALL_COMMAND, "timeout": 300})
        code, err = None, []
        for ev in r.get("stream") or []:
            ch = ev.get("chunk") or {}
            if ch.get("contentStop"):
                code = ch["contentStop"].get("exitCode")
            e = (ch.get("contentDelta") or {}).get("stderr")
            if e:
                err.append(e)
        if code == 0:
            _node_ready.add(session)
        else:
            log.warning("node/npm install exit=%s: %s", code, "".join(err)[-300:])
    except Exception as e:  # noqa: BLE001
        log.warning("node/npm install failed (continuing): %s", type(e).__name__)


def _stream_harness(arn: str, user: auth.User, session_id: str, history: list[dict], *, node: bool = False,
                    system: list[dict] | None = None, **extra: Any) -> StreamingResponse:
    """harness `ai_portal` 을 호출해 SSE 로 중계한다. extra 는 tools / skills 등 호출별 구성."""
    msgs = [{"role": m["role"], "content": [{"text": m["content"]}]} for m in history]
    # 세션 ID 에 사용자를 섞어 다른 사용자의 harness 세션을 재사용하지 못하게 한다
    session = f"{uuid.uuid5(uuid.NAMESPACE_URL, user.email)}-{session_id}"[:100]

    def sse() -> Iterator[bytes]:
        def enc(e: dict) -> bytes:
            return f"data: {json.dumps(e, ensure_ascii=False)}\n\n".encode()
        try:
            rt = aws.boto3.client("bedrock-agentcore", region_name=aws.region(),
                                  config=Config(read_timeout=300, connect_timeout=10, retries={"max_attempts": 0}))
            if node and session not in _node_ready:
                yield enc({"status": "실행 환경에 Node.js 를 준비하는 중…"})     # 첫 호출에 설치로 20초 안팎 걸린다
                ensure_node_installed(rt, arn, session)
            r = rt.invoke_harness(harnessArn=arn, runtimeSessionId=session, runtimeUserId=actor_id(user.email),
                                  actorId=actor_id(user.email), messages=msgs,
                                  **({"systemPrompt": system} if system else {}), **extra)
            yield from (enc(ev) for ev in relay_harness_stream(r["stream"]))
        except Exception as e:  # noqa: BLE001 — botocore 오류를 사용자 메시지로
            msg = getattr(e, "response", {}).get("Error", {}).get("Message") if hasattr(e, "response") else None
            yield enc({"error": (msg or str(e) or type(e).__name__)[:400]})

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


def _prepare(item_id: int, kind: str, body: McpChatIn, user: auth.User) -> tuple[dict, str, list[dict]]:
    history = clean_history(body.messages)
    if history[-1]["role"] != "user":
        raise HTTPException(422, "마지막 메시지는 user 여야 합니다")
    if sum(len(m["content"]) for m in history) > MAX_CHARS:
        raise HTTPException(422, "대화가 너무 깁니다. 새 대화를 시작하세요")
    it = catalog.visible_item(item_id, user, kind)
    from . import usage
    usage.require_use(it, user)                                        # MCP 는 사용 신청이 승인된 사람만 harness 로 호출해 볼 수 있다
    arn = harness_arn()
    if not arn:
        raise HTTPException(503, "테스트용 harness 가 없습니다. `python installer.py --harness-only` 를 실행하세요")
    return it, arn, history


@router.post("/mcp/{item_id}/chat")
def chat(item_id: int, body: McpChatIn, user: auth.User = Depends(auth.current_user)):
    it, arn, history = _prepare(item_id, "mcp", body, user)
    bearer = None
    if (it["data"] or {}).get("kind") == "gateway" and it["data"].get("authorizer") == "CUSTOM_JWT":
        from . import mcp_token
        gid = it["data"].get("gateway_id", "")
        mcp_token.grant(user.email, gid)                              # 사용 승인된 사람(또는 소유자)만 여기까지 온다 → 그룹을 맞춘다
        bearer = mcp_token.issue(user.email)["access_token"]          # 이 요청에만 쓸 토큰(저장·로그하지 않는다)
    return _stream_harness(arn, user, body.session_id, history, system=catalog.harness_system_prompt(user, body.prompt_id),
                           tools=[tool_for(it, bearer)])


@router.post("/skills/{item_id}/chat")
def chat_skill(item_id: int, body: McpChatIn, user: auth.User = Depends(auth.current_user)):
    """선택한 skill 을 harness `ai_portal` 의 skills 로 붙여 대화한다 (MCP 테스트와 같은 방식)."""
    it, arn, history = _prepare(item_id, "skill", body, user)
    return _stream_harness(arn, user, body.session_id, history, node=True,
                           system=catalog.harness_system_prompt(user, body.prompt_id), skills=[skill_source(it)])
