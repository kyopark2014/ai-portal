"""프롬프트 테스트 REST: Gateway 모델 목록 + SSE 스트리밍 채팅."""
from __future__ import annotations

import json
from typing import Iterator, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import auth, aws, catalog, gateway

router = APIRouter(prefix="/api")
MAX_TURNS = 40
MAX_CHARS = 60000


class Msg(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20000)


class ChatIn(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    messages: list[Msg] = Field(min_length=1, max_length=MAX_TURNS)
    prompt_id: int | None = None          # 선택한 prompt 가 system 메시지가 된다
    values: dict[str, str] = {}           # {{변수}} 값
    max_tokens: int = Field(1024, ge=1, le=gateway.MAX_TOKENS_CAP)


def _gw(e: gateway.GatewayError) -> HTTPException:
    return HTTPException(e.status if 400 <= e.status < 600 else 502, e.message)


@router.get("/gateway/models")
def models(refresh: bool = False, user: auth.User = Depends(auth.current_user)):
    try:
        return {"models": gateway.list_models(force=refresh), "gateway": gateway.base_url()}
    except gateway.GatewayError as e:
        raise _gw(e)


@router.post("/chat")
def chat(body: ChatIn, user: auth.User = Depends(auth.current_user)):
    if body.messages[-1].role != "user":
        raise HTTPException(422, "마지막 메시지는 user 여야 합니다")
    if sum(len(m.content) for m in body.messages) > MAX_CHARS:
        raise HTTPException(422, "대화가 너무 깁니다. 새 대화를 시작하세요")
    system = ""
    if body.prompt_id is not None:
        it = catalog.visible_item(body.prompt_id, user, "prompt")
        system = aws.render_prompt(it["data"].get("text", ""), body.values)
    msgs = [m.model_dump() for m in body.messages]

    # 첫 이벤트 전에 검증 오류(모델/키/권한)를 HTTP 상태로 돌려주기 위해 미리 한 번 당겨 본다.
    it_ = gateway.stream_chat(body.model, system, msgs, body.max_tokens) if gateway.configured() else None
    if it_ is None:
        raise HTTPException(503, "LLM_GATEWAY_KEY 가 설정되지 않았습니다")
    try:
        first = next(it_, None)
    except gateway.GatewayError as e:
        raise _gw(e)

    def sse() -> Iterator[bytes]:
        def enc(ev: dict) -> bytes:
            return f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode()
        try:
            if first is not None:
                yield enc(first)
            for ev in it_:
                yield enc(ev)
        except gateway.GatewayError as e:
            yield enc({"error": e.message})

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
