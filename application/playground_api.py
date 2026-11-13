"""Playground(/playground) API. ai-portal 에서 만든 agent 를 시험하는 최소 기능의 web UI 가 쓴다.

  - 포털과 같은 세션 쿠키로 로그인한 사용자 기준이다.
  - **사용 신청이 승인된 agent 만** 쓸 수 있다. 포털 상세 화면의 테스트와 달리 IAM 인증이어도 예외가 없다.
  - agent 만 순수하게 호출한다: 포털의 기본 system prompt 를 얹지 않고(agent 자체 prompt 사용), skill·MCP·memory 선택이 없다.
  - 대화는 task 로 저장한다(사용자 · agent 별). 이력은 서버가 가진 task 의 메시지에서 읽으므로 새로고침해도 이어서 대화할 수 있다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import uuid
from typing import Iterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import agent_test, auth, catalog, db, playground_store as store, usage

log = logging.getLogger("playground")
router = APIRouter(prefix="/api/playground")
MAX_REPLY = 60000                      # 저장할 응답 길이 상한(스트림이 끝없이 길어져도 저장소가 커지지 않게)


def _meta(it: dict) -> dict:
    d = it["data"] or {}
    return {"id": it["id"], "name": it["name"], "description": it["description"], "agent_type": d.get("agent_type", ""),
            "auth": d.get("auth", ""), "status": d.get("status", "")}


def usable_agent(item_id: int, user: auth.User) -> dict:
    """사용 승인된 agent 를 돌려준다. 보이지 않으면 404, 승인 전이면 403, 호출할 수 없는 종류면 422."""
    it = catalog.visible_item(item_id, user, "agent")
    usage.require_use(it, user)
    agent_test.target_of(it)
    return it


def _my_task(user: auth.User, agent_id: int, task_id: str) -> dict:
    t = store.get_task(user.email, task_id)
    if t is None or t["agent_id"] != agent_id:            # 남의 task·다른 agent 의 task 는 없는 것과 같다
        raise HTTPException(404, "task not found")
    return t


@router.get("/agents")
def agents(user: auth.User = Depends(auth.current_user)):
    out = []
    for it in db.list_items("agent", "", (user.email, user.role == "admin")):
        if not usage.can_use(it, user):
            continue
        try:
            agent_test.target_of(it)
        except HTTPException:
            continue                                   # ARN 이 없거나 MCP runtime 등 대화할 수 없는 항목
        out.append(_meta(it))
    return {"agents": sorted(out, key=lambda a: a["name"].lower())}


@router.get("/agents/{item_id}")
def agent(item_id: int, user: auth.User = Depends(auth.current_user)):
    return _meta(catalog.refresh_agent_status(usable_agent(item_id, user)))


# ── task ──
class TaskPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    pinned: bool | None = None


@router.get("/agents/{item_id}/tasks")
def tasks(item_id: int, user: auth.User = Depends(auth.current_user)):
    usable_agent(item_id, user)
    return {"tasks": store.list_tasks(user.email, item_id)}


@router.post("/agents/{item_id}/tasks")
def new_task(item_id: int, user: auth.User = Depends(auth.current_user)):
    usable_agent(item_id, user)
    return store.create_task(user.email, item_id)


@router.get("/agents/{item_id}/tasks/{task_id}")
def task(item_id: int, task_id: str, user: auth.User = Depends(auth.current_user)):
    usable_agent(item_id, user)
    t = _my_task(user, item_id, task_id)
    return {**t, "messages": store.list_messages(user.email, task_id) or []}


@router.patch("/agents/{item_id}/tasks/{task_id}")
def patch_task(item_id: int, task_id: str, body: TaskPatch, user: auth.User = Depends(auth.current_user)):
    usable_agent(item_id, user)
    _my_task(user, item_id, task_id)
    return store.update_task(user.email, task_id, title=body.title, pinned=body.pinned)


@router.delete("/agents/{item_id}/tasks/{task_id}")
def remove_task(item_id: int, task_id: str, user: auth.User = Depends(auth.current_user)):
    usable_agent(item_id, user)
    _my_task(user, item_id, task_id)
    store.delete_task(user.email, task_id)
    return {"ok": True}


# ── 대화 ──
class ChatIn(BaseModel):
    content: str = Field(min_length=1, max_length=8000)


def _history(user: auth.User, task_id: str, text: str) -> list[dict]:
    """저장된 대화 + 이번 질문. 에러로 비어 버린 assistant 턴은 뺀다."""
    prev = [{"role": m["role"], "content": m["content"]} for m in (store.list_messages(user.email, task_id) or [])]
    return agent_test.clean_history([agent_test.Msg(**m) for m in prev] + [agent_test.Msg(role="user", content=text)])


def _tool_use_id(ev: dict) -> str | None:
    """harness 정규화 이벤트의 블록 index(i) 또는 id 를 timeline 키로 쓴다."""
    if "i" in ev and ev["i"] is not None:
        return str(ev["i"])
    if ev.get("id") is not None:
        return str(ev["id"])
    return None


def _tool_events(ev: dict) -> dict | None:
    """agent 스트림 이벤트 → 저장·표시용 timeline 항목(harness-work 와 같은 모양).

    tool_start(블록 시작) 때 카드를 올리고, tool_call(블록 종료·입력 확정) 때 같은 toolUseId 로 upsert 한다.
    """
    tid = _tool_use_id(ev)
    if "tool_start" in ev:
        return {"type": "tool", "tool": ev["tool_start"], "input": "…", "toolUseId": tid}
    if "tool_call" in ev:
        return {"type": "tool", "tool": ev["tool_call"], "input": _maybe_json(ev.get("input", "")), "toolUseId": tid}
    if "tool_result" in ev:
        out = {"type": "tool_result", "data": ev["tool_result"], "toolUseId": tid}
        if ev.get("tool"):
            out["tool"] = ev["tool"]
        if ev.get("mcpApp"):
            out["mcpApp"] = ev["mcpApp"]
        return out
    return None


def _upsert_tool_event(events: list[dict], te: dict) -> None:
    """같은 toolUseId 의 tool 항목은 갱신하고, tool_result 는 누적·mcpApp 최신화, 그 외는 뒤에 붙인다."""
    if te["type"] == "tool" and te.get("toolUseId") is not None:
        for i, existing in enumerate(events):
            if existing.get("type") == "tool" and existing.get("toolUseId") == te["toolUseId"]:
                events[i] = {**existing, **te}
                return
    if te["type"] == "tool_result" and te.get("toolUseId") is not None:
        for i, existing in enumerate(events):
            if existing.get("type") == "tool_result" and existing.get("toolUseId") == te["toolUseId"]:
                events[i] = {
                    **existing,
                    **te,
                    "data": f"{existing.get('data', '')}{te.get('data', '')}",
                    "mcpApp": te.get("mcpApp") or existing.get("mcpApp"),
                }
                return
    events.append(te)


def _maybe_json(s):
    if isinstance(s, str) and s.strip()[:1] in "{[":
        try:
            return json.loads(s)
        except ValueError:
            pass
    return s


@router.post("/agents/{item_id}/tasks/{task_id}/chat")
def chat(item_id: int, task_id: str, body: ChatIn, user: auth.User = Depends(auth.current_user)):
    it = usable_agent(item_id, user)
    _my_task(user, item_id, task_id)
    text = body.content.strip()
    if not text:
        raise HTTPException(422, "빈 메시지는 보낼 수 없습니다")
    history = _history(user, task_id, text)
    if sum(len(m["content"]) for m in history) > agent_test.MAX_CHARS:
        raise HTTPException(422, "대화가 너무 깁니다. 새 task 를 시작하세요")
    src = agent_test.agent_events(it, history, task_id.ljust(33, "0"), user, with_system=False)

    q: "queue.Queue[bytes | None]" = queue.Queue()
    stop = threading.Event()                          # 클라이언트가 끊거나 «중지» 를 누르면 켜진다

    def pump() -> None:
        """agent 이벤트를 소비해 SSE 조각을 큐로 넘기고, **끝나면(중지돼도) 받은 만큼 저장한다.**

        저장을 응답 제너레이터의 finally 에 두면 안 된다: 클라이언트가 연결을 끊을 때 Starlette 은 스레드풀에서 돌던 동기
        제너레이터를 취소만 하고 닫지 않아 finally 가 실행되지 않는다(중지한 턴이 통째로 사라진다). 그래서 소비·저장을 이 스레드가 맡는다."""
        reply, events, failed = "", [], False

        def tap() -> Iterator[dict]:
            nonlocal reply, failed
            for ev in src:
                if ev.get("t"):
                    reply += ev["t"]; events.append({"type": "text", "data": ev["t"]})
                elif (te := _tool_events(ev)):
                    _upsert_tool_event(events, te)
                elif ev.get("error"):
                    failed = True
                yield ev

        gen = agent_test.sse_bytes(tap())
        try:
            for chunk in gen:
                if stop.is_set():
                    break
                q.put(chunk)
        finally:
            gen.close()                                # agent 스트림(boto)도 닫는다
            try:
                if reply.strip() or events:
                    store.add_messages(user.email, task_id, [
                        {"role": "user", "content": text},
                        {"role": "assistant", "content": reply[:MAX_REPLY], "tool_events": _merge_text(events)}])
                elif not failed:
                    store.add_messages(user.email, task_id, [{"role": "user", "content": text}])
            except Exception:  # noqa: BLE001 — 저장 실패가 스트림 종료를 막지 않게
                log.exception("playground: saving the turn failed")
            q.put(None)

    threading.Thread(target=pump, daemon=True, name="playground-pump").start()

    async def body():
        try:
            while True:
                chunk = await asyncio.to_thread(q.get)
                if chunk is None:
                    break
                yield chunk
        finally:
            stop.set()                                  # 끊겼다면 pump 가 다음 조각에서 멈추고 저장한다

    return StreamingResponse(body(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


def _merge_text(events: list[dict]) -> list[dict]:
    """스트림 조각(text)을 이어 붙여 도구 호출 사이의 구간별 텍스트로 만든다."""
    out: list[dict] = []
    for e in events:
        if e["type"] == "text" and out and out[-1]["type"] == "text":
            out[-1] = {"type": "text", "data": out[-1]["data"] + e["data"]}
        else:
            out.append(e)
    return out


def new_session_id() -> str:                           # (테스트용 보조) 33자 이상 세션 id
    return uuid.uuid4().hex + "0"
