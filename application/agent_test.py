"""Agent 테스트: 선택한 agent 에 프롬프트를 보내고 응답/도구 호출을 SSE 로 중계한다.

  - harness                → InvokeHarness  (messages 대화, 도구는 harness 에 설정된 것)
  - runtime (HTTP/…)       → InvokeAgentRuntime. agentic-work 와 같은 payload 로 호출한다.
  - runtime (MCP 프로토콜) → 대화형 agent 가 아니라 MCP 서버라서 지원하지 않는다 (MCP 탭에서 테스트).
  - harness 가 만든 내부 runtime(harness_*) 은 직접 호출할 수 없다 (동기화에서 제외).

runtime 의 payload 규약은 agent 마다 다르다. 알려진 형식(`prompt` + agentic-work 필드)을 보내고,
응답은 SSE 의 data/result/toolResult 이벤트를 해석한다. 규약이 다른 agent 는 응답이 비어 있을 수 있다.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Iterator

from botocore import UNSIGNED
from botocore.config import Config
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import auth, aws, catalog, mcp_test, mcp_token, usage

router = APIRouter(prefix="/api")
MAX_TURNS = 20
MAX_CHARS = 30000
_ARN = re.compile(r"^arn:aws:bedrock-agentcore:[a-z0-9-]+:\d{12}:(runtime|harness)/[A-Za-z0-9_-]+$")
_cfg = Config(read_timeout=300, connect_timeout=10, retries={"max_attempts": 0})


class Msg(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=8000)        # 에러·중단으로 비어 버린 assistant 턴은 허용하고 아래에서 걸러낸다


def clean_history(msgs: list[Msg]) -> list[dict]:
    """빈 assistant 턴을 버린다. 그 외 빈 메시지(사용자)는 거절한다."""
    out = [m.model_dump() for m in msgs if not (m.role == "assistant" and not m.content.strip())]
    if not out or any(not m["content"].strip() for m in out):
        raise HTTPException(422, "빈 메시지는 보낼 수 없습니다")
    return out


class AgentChatIn(BaseModel):
    prompt_id: int | None = None          # 선택: harness agent 의 system prompt. 없으면 기본(harness_prompt)
    messages: list[Msg] = Field(min_length=1, max_length=MAX_TURNS)
    session_id: str = Field(min_length=33, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


def _region_of(arn: str) -> str:
    return arn.split(":")[3]


def target_of(item: dict) -> tuple[str, str]:
    """agent 항목 → (종류, ARN). 테스트할 수 없으면 사유와 함께 422."""
    d = item["data"]
    arn = d.get("arn", "")
    if not arn or not _ARN.match(arn):
        raise HTTPException(422, "호출할 수 있는 ARN 이 없는 agent 입니다 (AgentCore harness/runtime 만 테스트할 수 있습니다)")
    if arn.split(":")[5].startswith("runtime/harness_"):
        raise HTTPException(422, "harness 가 관리하는 내부 runtime 은 직접 호출할 수 없습니다. harness 항목으로 테스트하세요")
    if d.get("agent_type") == "runtime" and (d.get("protocol") or "").upper() == "MCP":
        raise HTTPException(422, "MCP 프로토콜 runtime 은 대화형 agent 가 아니라 MCP 서버입니다. MCP 탭에서 테스트하세요")
    return ("harness" if ":harness/" in arn else "runtime"), arn


def _runtime_payload(prompt: str, history: list[dict], user: auth.User, session: str) -> bytes:
    """agentic-work 의 payload 규약. 모르는 필드는 무시되고, 대화 이력은 prompt 에 이어 붙인다."""
    if len(history) > 1:
        convo = "\n".join(f"{'사용자' if m['role'] == 'user' else '어시스턴트'}: {m['content']}" for m in history[:-1])
        prompt = f"[이전 대화]\n{convo}\n\n[현재 요청]\n{prompt}"
    return json.dumps({
        # model_name 은 보내지 않는다: agent 마다 형식이 달라(표시 이름 vs 모델 ID) 강제하면 깨진다.
        # 생략하면 각 agent 가 자기 기본 모델을 쓴다.
        "prompt": prompt, "mcp_servers": [], "skill_list": [],
        "user_id": mcp_test.actor_id(user.email), "runtime_session_id": session,
        "guardrail_enabled": False, "memory_enabled": False, "files": [], "task_id": "",
    }).encode()


def _runtime_events(arn: str, payload: bytes, session: str, user: auth.User) -> Iterator[dict]:
    rt = aws.boto3.client("bedrock-agentcore", region_name=_region_of(arn), config=_cfg)
    r = rt.invoke_agent_runtime(agentRuntimeArn=arn, runtimeSessionId=session, payload=payload, qualifier="DEFAULT",
                                runtimeUserId=mcp_test.actor_id(user.email))
    ct = r.get("contentType", "")
    if "text/event-stream" not in ct:           # SSE 가 아니면 본문 전체를 한 번에 낸다
        body = r["response"].read().decode("utf-8", "replace")
        try:
            j = json.loads(body)
            body = j.get("result", {}).get("messages", [{}])[-1].get("content") if isinstance(j, dict) and "result" in j else body
        except ValueError:
            pass
        yield {"t": str(body)[:20000]}
        return
    streamed = False
    for line in r["response"].iter_lines():
        l = line.decode("utf-8", "replace")
        if not l.startswith("data:"):
            continue
        try:
            e = json.loads(l[5:])
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        if isinstance(e.get("data"), str):
            streamed = True
            yield {"t": e["data"]}
        elif "toolResult" in e:
            yield {"tool_call": e.get("tool", "tool"), "input": ""}
            yield {"tool_result": str(e["toolResult"])[:4000]}
        elif "result" in e and not streamed:      # 스트리밍 조각 없이 최종 결과만 온 경우
            msgs = (e["result"] or {}).get("messages") or []
            if msgs:
                yield {"t": str(msgs[-1].get("content", ""))}
        elif "error" in e:
            yield {"error": str(e["error"])[:400]}
            return


def _harness_events(arn: str, history: list[dict], user: auth.User, session: str,
                    system: list[dict] | None = None, bearer: str | None = None) -> Iterator[dict]:
    """bearer 가 있으면 JWT 인증 harness: SigV4 서명을 끄고 Authorization: Bearer 헤더로 호출한다."""
    region = _region_of(arn)
    if bearer:
        rt = aws.boto3.client("bedrock-agentcore", region_name=region, config=Config(
            signature_version=UNSIGNED, read_timeout=300, connect_timeout=10, retries={"max_attempts": 0}))
        rt.meta.events.register("before-call.bedrock-agentcore.InvokeHarness",
                                lambda params, **kw: params["headers"].__setitem__("Authorization", f"Bearer {bearer}"))
    else:
        rt = aws.boto3.client("bedrock-agentcore", region_name=region, config=_cfg)
    msgs = [{"role": m["role"], "content": [{"text": m["content"]}]} for m in history]
    extra: dict[str, Any] = {"systemPrompt": system} if system else {}
    if not bearer:                                  # JWT 호출의 사용자는 토큰이 정한다 (IAM 호출에서만 runtimeUserId 를 지정)
        extra["runtimeUserId"] = mcp_test.actor_id(user.email)
    r = rt.invoke_harness(harnessArn=arn, runtimeSessionId=session, actorId=mcp_test.actor_id(user.email),
                          messages=msgs, **extra)
    yield from mcp_test.relay_harness_stream(r["stream"])


@router.post("/agents/{item_id}/chat")
def chat(item_id: int, body: AgentChatIn, user: auth.User = Depends(auth.current_user)):
    history = clean_history(body.messages)
    if history[-1]["role"] != "user":
        raise HTTPException(422, "마지막 메시지는 user 여야 합니다")
    if sum(len(m["content"]) for m in history) > MAX_CHARS:
        raise HTTPException(422, "대화가 너무 깁니다. 새 대화를 시작하세요")
    it = catalog.visible_item(item_id, user, "agent")
    usage.require_use(it, user)                                   # 사용 신청이 승인된 사람(소유자 포함)만 대화할 수 있다
    kind, arn = target_of(it)
    jwt = kind == "harness" and (it["data"] or {}).get("auth") == "JWT"
    bearer = mcp_token.issue(user.email)["access_token"] if jwt else None   # 이 요청에만 쓸 토큰 (저장·로그하지 않는다)
    if kind == "harness":
        try:
            st = aws.get_harness(arn.split("/")[-1])["status"]
        except Exception:  # noqa: BLE001 — 상태를 못 읽으면 호출을 시도해 본다
            st = "READY"
        if st != "READY":
            raise HTTPException(409, f"harness 가 아직 준비되지 않았습니다 (상태: {st}). 잠시 뒤 다시 시도하세요" if st in ("CREATING", "UPDATING")
                                else f"harness 를 호출할 수 없는 상태입니다 ({st})")
    system = catalog.harness_system_prompt(user, body.prompt_id) if kind == "harness" else None   # runtime 에는 해당 없음
    # 사용자별로 세션을 분리해 다른 사용자의 agent 세션/메모리를 재사용하지 못하게 한다
    session = f"{uuid.uuid5(uuid.NAMESPACE_URL, user.email)}-{body.session_id}"[:100]

    def sse() -> Iterator[bytes]:
        def enc(e: dict) -> bytes:
            return f"data: {json.dumps(e, ensure_ascii=False)}\n\n".encode()
        try:
            src = (_harness_events(arn, history, user, session, system, bearer) if kind == "harness"
                   else _runtime_events(arn, _runtime_payload(history[-1]["content"], history, user, session), session, user))
            for ev in src:
                yield enc(ev)
            yield enc({"done": True})
        except Exception as e:  # noqa: BLE001 — botocore 오류를 사용자 메시지로
            msg = getattr(e, "response", {}).get("Error", {}).get("Message") if hasattr(e, "response") else None
            yield enc({"error": (msg or str(e) or type(e).__name__)[:400]})

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
