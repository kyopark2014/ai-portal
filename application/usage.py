"""MCP 사용 신청. Registry 의 승인은 'Registry 에 등록해도 되는가' 이고, 사용 신청은 '이 MCP 를 내가 써도 되는가' 이다.

  - 신청은 **등록이 끝난 MCP**(Registry 상태 APPROVED)에만 할 수 있다.
  - 승인은 관리자 또는 그 record 의 reviewer 가 한다(등록 승인과 같은 사람들).
  - 실제로 쓰는 동작(연결 테스트·harness 테스트)은 소유자, 관리자, 승인을 받은 사람만 할 수 있다.
    목록과 상세는 등록된 MCP 라면 누구나 볼 수 있다.
  - Auto-review 가 켜져 있으면 신청 즉시 승인된다(사유에 [auto-review] 표시).
"""
from __future__ import annotations

from typing import Literal

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import admins, auth, catalog, db, policy

router = APIRouter(prefix="/api")
REASON_MAX = 255


def can_use(item: dict, user: auth.User) -> bool:
    """이 사용자가 이 항목을 실제로 호출해도 되는가. MCP 만 사용 신청이 필요하고 나머지는 지금처럼 열려 있다."""
    if item["kind"] != "mcp":
        return True
    if user.role == "admin" or item["owner"] == user.email:
        return True
    r = db.get_usage_request(item["id"], user.email)
    return bool(r and r["status"] == "APPROVED")


def require_use(item: dict, user: auth.User) -> None:
    if not can_use(item, user):
        raise HTTPException(403, "이 MCP 를 사용하려면 사용 신청이 승인되어야 합니다")


def state_for(item: dict, user: auth.User) -> dict:
    """화면용: 이 사용자의 사용 상태. 등록이 끝나지 않았으면 신청할 수 없다."""
    registered = item["registry_status"] == "APPROVED"
    if item["kind"] != "mcp":
        return {"applicable": False}
    base = {"applicable": True, "registered": registered, "can_use": can_use(item, user)}
    if user.role == "admin" or item["owner"] == user.email:
        return {**base, "status": "OWNER", "can_request": False}
    r = db.get_usage_request(item["id"], user.email)
    return {**base, "status": r["status"] if r else "NONE", "reason": (r or {}).get("reason", ""),
            "can_request": registered and (r is None or r["status"] == "REJECTED")}


class DecisionIn(BaseModel):
    decision: Literal["APPROVED", "REJECTED"]
    reason: str = Field("", max_length=200)


def _item(item_id: int, user: auth.User) -> dict:
    it = catalog.visible_item(item_id, user, "mcp")
    return it


@router.get("/mcp/{item_id}/usage")
def my_usage(item_id: int, user: auth.User = Depends(auth.current_user)):
    return state_for(_item(item_id, user), user)


@router.post("/mcp/{item_id}/usage")
def request_usage(item_id: int, user: auth.User = Depends(auth.current_user)):
    """사용 신청. 등록이 끝난 MCP 에만 가능하고, 이미 승인됐거나 대기 중이면 다시 보내지 않는다."""
    it = _item(item_id, user)
    if it["registry_status"] != "APPROVED":
        raise HTTPException(409, "등록이 완료된 MCP 에만 사용 신청할 수 있습니다")
    if user.role == "admin" or it["owner"] == user.email:
        raise HTTPException(409, "소유자와 관리자는 신청 없이 사용할 수 있습니다")
    cur = db.get_usage_request(it["id"], user.email)
    if cur and cur["status"] == "PENDING":
        raise HTTPException(409, "이미 사용 신청 중입니다")
    if cur and cur["status"] == "APPROVED":
        raise HTTPException(409, "이미 사용 승인된 MCP 입니다")
    db.put_usage_request(it["id"], user.email, "PENDING")
    if policy.get()["auto_review"]:                                   # 신청 즉시 자동 승인 (읽지 못하면 꺼짐)
        pol = policy.get()
        db.put_usage_request(it["id"], user.email, "APPROVED", decided_by="auto-review",
                             reason=f"[auto-review] 자동 검토 (설정: {pol['changed_by'] or 'unknown'})"[:REASON_MAX])
    return state_for(it, user)


def _can_review(record_id: str | None, user: auth.User) -> bool:
    return bool(record_id) and admins.can_decide(record_id, user.email)


@router.get("/usage-requests")
def pending_requests(status: Literal["PENDING", "APPROVED", "REJECTED"] = "PENDING", user: auth.User = Depends(auth.current_user)):
    """내가 처리할 수 있는 사용 신청 목록(관리자는 전부, reviewer 는 자기에게 지정된 MCP 의 것만)."""
    out = []
    for r in db.list_usage_requests(status=status):
        it = db.get_item(r["item_id"])
        if not it or it["kind"] != "mcp" or not _can_review(it["registry_record_id"], user):
            continue
        out.append({**r, "mcp": {"id": it["id"], "name": it["name"], "owner": it["owner"], "description": it["description"]}})
    if user.role != "admin" and not out and not admins.reviewing(user.email):
        raise HTTPException(403, "승인 권한이 없습니다")
    return {"requests": out}


@router.post("/usage-requests/{item_id}/{email}/decision")
def decide_usage(item_id: int, email: str, body: DecisionIn, user: auth.User = Depends(auth.current_user)):
    it = db.get_item(item_id)
    if not it or it["kind"] != "mcp" or not _can_review(it["registry_record_id"], user):
        raise HTTPException(404, "not found")                          # 처리 권한이 없는 신청은 존재 여부도 알리지 않는다
    cur = db.get_usage_request(item_id, email)
    if not cur:
        raise HTTPException(404, "not found")
    if cur["status"] != "PENDING":
        raise HTTPException(409, f"이미 처리된 신청입니다 (현재: {cur['status']})")
    if body.decision == "REJECTED" and not body.reason.strip():
        raise HTTPException(422, "거절 사유를 입력하세요")
    by = f" [by {user.email}]"
    reason = (body.reason.strip().replace("\n", " ")[: REASON_MAX - len(by)] + by).strip()
    return db.put_usage_request(item_id, email, body.decision, decided_by=user.email, reason=reason)
