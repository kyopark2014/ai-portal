"""관리자 승인 큐: Registry 의 PENDING_APPROVAL record 를 조회하고 승인/거절/deprecate 한다.

포털 DB 가 아니라 Registry 를 직접 조회한다(포털 밖에서 등록된 record 도 처리해야 하므로).
권한은 서버가 판단한다 — 관리자만 가능하며, 사유에 처리한 관리자 이메일을 남겨 감사 추적을 돕는다.
(CloudTrail 에도 호출이 남지만 IAM 주체가 서비스 역할이라 개인을 구분할 수 없다.)
"""
from __future__ import annotations

import json
import re
from typing import Any, Literal

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import auth, aws, catalog, db

router = APIRouter(prefix="/api/approvals")
_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
REASON_MAX = 255          # statusReason 의 AWS 한도


def require_admin(user: auth.User = Depends(auth.current_user)) -> auth.User:
    if user.role != "admin":
        raise HTTPException(403, "관리자만 사용할 수 있습니다")
    return user


def _registry_id() -> str:
    rid = catalog.selected_registry()
    if not rid:
        raise HTTPException(409, "사용할 Registry 가 선택되지 않았습니다")
    return rid


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _record_id(record_id: str) -> str:
    if not _ID.match(record_id):
        raise HTTPException(422, "record id 형식이 올바르지 않습니다")
    return record_id


def _portal_item(record_id: str) -> dict | None:
    with db.conn() as c:
        r = c.execute("SELECT id, kind, name, owner FROM items WHERE registry_record_id=?", (record_id,)).fetchone()
    return dict(r) if r else None


def _summary(r: dict) -> dict:
    return {"recordId": r["recordId"], "name": r["name"], "description": r.get("description", ""),
            "descriptorType": r["descriptorType"], "status": r["status"], "recordVersion": r.get("recordVersion", ""),
            "createdAt": _iso(r.get("createdAt")), "updatedAt": _iso(r.get("updatedAt"))}


@router.get("")
def queue(status: Literal["PENDING_APPROVAL", "APPROVED", "REJECTED", "DEPRECATED", "DRAFT"] = "PENDING_APPROVAL",
          user: auth.User = Depends(require_admin)):
    rid, out, token = _registry_id(), [], None
    try:
        while True:
            r = aws.registry_client().list_registry_records(
                registryId=rid, status=status, **({"nextToken": token} if token else {}))
            out += r.get("registryRecords", [])
            token = r.get("nextToken")
            if not token:
                break
    except (ClientError, BotoCoreError) as e:
        raise catalog.aws_err(e)
    out.sort(key=lambda x: x.get("updatedAt") or 0, reverse=True)
    return {"records": [{**_summary(x), "portal": _portal_item(x["recordId"])} for x in out], "registryId": rid}


@router.get("/{record_id}")
def detail(record_id: str, user: auth.User = Depends(require_admin)):
    rid = _registry_id()
    try:
        r = aws.registry_client().get_registry_record(registryId=rid, recordId=_record_id(record_id))
    except (ClientError, BotoCoreError) as e:
        if catalog._missing(e):   # 요청자가 취소했거나 삭제한 record 의 오래된 메일 링크
            raise HTTPException(404, "이 record 는 더 이상 존재하지 않습니다. 요청자가 승인 요청을 취소하거나 "
                                     "삭제했을 수 있습니다. 승인 목록에서 현재 대기 중인 요청을 확인하세요.")
        raise catalog.aws_err(e)
    content: Any = None
    d = r.get("descriptors") or {}
    inline = ((d.get("custom") or {}).get("inlineContent")
              or ((d.get("mcp") or {}).get("server") or {}).get("inlineContent")
              or ((d.get("a2a") or {}).get("agentCard") or {}).get("inlineContent")
              or ((d.get("agentSkills") or {}).get("skillMd") or {}).get("inlineContent"))
    if inline:
        try:
            content = json.loads(inline)
        except ValueError:
            content = inline
    return {**_summary(r), "statusReason": r.get("statusReason", ""), "content": content,
            "portal": _portal_item(record_id), "registryId": rid}


class DecisionIn(BaseModel):
    decision: Literal["APPROVED", "REJECTED", "DEPRECATED"]
    reason: str = Field("", max_length=200)


@router.post("/{record_id}/decision")
def decide(record_id: str, body: DecisionIn, user: auth.User = Depends(require_admin)):
    rid, rec = _registry_id(), _record_id(record_id)
    if body.decision == "REJECTED" and not body.reason.strip():
        raise HTTPException(422, "거절 사유를 입력하세요")
    client = aws.registry_client()
    try:   # 낡은 화면/이중 클릭 방지: AWS 의 현재 상태를 보고 전이가 유효한지 판단한다
        cur = client.get_registry_record(registryId=rid, recordId=rec)["status"]
    except (ClientError, BotoCoreError) as e:
        raise catalog.aws_err(e)
    allowed = {"PENDING_APPROVAL": ("APPROVED", "REJECTED"), "APPROVED": ("DEPRECATED",)}
    if body.decision not in allowed.get(cur, ()):
        raise HTTPException(409, f"현재 상태({cur})에서는 {body.decision} 로 바꿀 수 없습니다. 이미 처리되었을 수 있습니다")
    # 사유 한도(255)를 넘지 않게 사유 쪽을 줄이고 처리자 이메일은 항상 남긴다
    by = f" [by {user.email}]"
    reason = (body.reason.strip().replace("\n", " ")[: REASON_MAX - len(by)] + by).strip()
    try:
        client.update_registry_record_status(registryId=rid, recordId=rec, status=body.decision, statusReason=reason)
    except (ClientError, BotoCoreError) as e:
        raise catalog.aws_err(e)
    item = _portal_item(rec)
    if item:    # 포털 항목의 표시 상태를 맞춘다
        db.update_item(item["id"], registry_status=body.decision)
    return {"recordId": rec, "status": body.decision, "statusReason": reason}
