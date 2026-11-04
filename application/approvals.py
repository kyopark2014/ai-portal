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

from . import admins, auth, aws, catalog, db

router = APIRouter(prefix="/api/approvals")
_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
REASON_MAX = 255          # statusReason 의 AWS 한도


def require_admin(user: auth.User = Depends(auth.current_user)) -> auth.User:
    if user.role != "admin":
        raise HTTPException(403, "관리자만 사용할 수 있습니다")
    return user


def require_reviewer(user: auth.User = Depends(auth.current_user)) -> auth.User:
    """승인 화면에 들어올 수 있는 사람: 관리자 + 한 record 라도 reviewer 로 지정된 사람. 처리 가능 범위는 record 마다 따로 판단한다."""
    if user.role != "admin" and not admins.reviewing(user.email):
        raise HTTPException(403, "승인 권한이 없습니다")
    return user


def _visible_to(user: auth.User, record_id: str) -> bool:
    """관리자는 전부, reviewer 는 자기에게 지정된 record 만 본다."""
    return user.role == "admin" or admins.is_reviewer(record_id, user.email)


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
    it = db.find_by_record(record_id)
    return {"id": it["id"], "kind": it["kind"], "name": it["name"], "owner": it["owner"]} if it else None


def _summary(r: dict) -> dict:
    return {"recordId": r["recordId"], "name": r["name"], "description": r.get("description", ""),
            "descriptorType": r["descriptorType"], "status": r["status"], "recordVersion": r.get("recordVersion", ""),
            "createdAt": _iso(r.get("createdAt")), "updatedAt": _iso(r.get("updatedAt"))}


@router.get("")
def queue(status: Literal["PENDING_APPROVAL", "APPROVED", "REJECTED", "DEPRECATED", "DRAFT"] = "PENDING_APPROVAL",
          user: auth.User = Depends(require_reviewer)):
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
    out = [x for x in out if _visible_to(user, x["recordId"])]
    out.sort(key=lambda x: x.get("updatedAt") or 0, reverse=True)
    rv = admins.reviewers_map()
    return {"records": [{**_summary(x), "portal": _portal_item(x["recordId"]), "reviewers": rv.get(x["recordId"], []),
                         "can_decide": admins.can_decide(x["recordId"], user.email)} for x in out], "registryId": rid}


@router.get("/{record_id}")
def detail(record_id: str, user: auth.User = Depends(require_reviewer)):
    rid = _registry_id()
    if not _visible_to(user, _record_id(record_id)):
        raise HTTPException(404, "not found")          # reviewer 가 아닌 record 는 존재 여부도 알리지 않는다
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
            "portal": _portal_item(record_id), "registryId": rid,
            "reviewers": admins.reviewers_of(record_id), "can_decide": admins.can_decide(record_id, user.email)}


class DecisionIn(BaseModel):
    decision: Literal["APPROVED", "REJECTED", "DEPRECATED"]
    reason: str = Field("", max_length=200)


@router.post("/{record_id}/decision")
def decide(record_id: str, body: DecisionIn, user: auth.User = Depends(require_reviewer)):
    rid, rec = _registry_id(), _record_id(record_id)
    if not _visible_to(user, rec):
        raise HTTPException(404, "not found")
    if not admins.can_decide(rec, user.email):
        raise HTTPException(403, "이 record 를 처리할 권한이 없습니다")
    if body.decision == "DEPRECATED" and user.role != "admin":
        raise HTTPException(403, "Deprecate 는 관리자만 할 수 있습니다")
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
    try:
        admins.clear_reviewers(rec)
    except Exception:  # noqa: BLE001 — 정리는 best effort
        pass
    item = _portal_item(rec)
    if item:    # 포털 항목의 표시 상태를 맞춘다
        db.update_item(item["id"], registry_status=body.decision)
    return {"recordId": rec, "status": body.decision, "statusReason": reason}


class AdminIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)


admins_router = APIRouter(prefix="/api/admins")


@admins_router.get("")
def list_admins(user: auth.User = Depends(require_admin)):
    return {"admins": admins.listing(), "me": user.email}


@admins_router.post("")
def add_admin(body: AdminIn, user: auth.User = Depends(require_admin)):
    try:
        admins.add(body.email)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except (ClientError, BotoCoreError) as e:
        raise catalog.aws_err(e)
    return {"admins": admins.listing(), "me": user.email}


@admins_router.delete("")
def remove_admin(email: str, user: auth.User = Depends(require_admin)):
    if email.strip().lower() == user.email.lower():
        raise HTTPException(422, "자기 자신은 삭제할 수 없습니다")
    try:
        admins.remove(email)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except (ClientError, BotoCoreError) as e:
        raise catalog.aws_err(e)
    return {"admins": admins.listing(), "me": user.email}


# ── 관리자 대시보드: 접속한 사람 + 레지스트리 등록 현황 ─────────────────────────
dashboard_router = APIRouter(prefix="/api/admin")
_DESCRIPTOR_KIND = {"MCP": "mcp", "A2A": "agent", "AGENT_SKILLS": "skill", "CUSTOM": "prompt"}


@dashboard_router.get("/dashboard")
def dashboard(user: auth.User = Depends(require_admin)):
    """접속 통계 + 사용자별 등록 현황(종류·상태) + Registry 전체의 종류별·상태별 현황.

    사용자별 수치는 포털 DB(소유자 기준)에서, Registry 현황은 Registry 를 직접 조회해서 낸다
    (포털 밖에서 등록된 record 와 소유자를 알 수 없는 복원 항목도 빠지지 않게).
    """
    users = db.user_stats()
    admin_set = admins.all_admins()
    for u in users:
        u["is_admin"] = u["email"] in admin_set
    by_type: dict[str, dict[str, int]] = {}
    registry_error = ""
    try:
        rid = _registry_id()
        token = None
        while True:
            r = aws.registry_client().list_registry_records(registryId=rid, **({"nextToken": token} if token else {}))
            for rec in r.get("registryRecords", []):
                kind = _DESCRIPTOR_KIND.get(rec["descriptorType"], rec["descriptorType"].lower())
                by_type.setdefault(kind, {})
                by_type[kind][rec["status"]] = by_type[kind].get(rec["status"], 0) + 1
            token = r.get("nextToken")
            if not token:
                break
    except HTTPException as e:
        registry_error = str(e.detail)
    except (ClientError, BotoCoreError) as e:
        registry_error = catalog.aws_err(e).detail
    return {"totals": db.login_totals(), "users": users, "recent_logins": db.recent_logins(30),
            "registry": {"by_type": by_type, "total": sum(sum(v.values()) for v in by_type.values()), "error": registry_error},
            "admins": sorted(admin_set)}


# ── record 별 reviewer 관리 (관리자 전용) ─────────────────────────────────────
reviewers_router = APIRouter(prefix="/api/reviewers")


class ReviewerIn(BaseModel):
    record_id: str = Field(min_length=6, max_length=64)
    email: str = Field(min_length=3, max_length=254)


@reviewers_router.get("/users")
def pick_users(q: str = "", limit: int = 20, user: auth.User = Depends(require_admin)):
    """reviewer 로 고를 수 있는 사용자 검색. 로그인한 적 있는 사람과 항목을 가진 사람 중에서 이메일·이름으로 찾는다."""
    q = q.strip().lower()
    out = []
    for u in db.user_stats():
        if u["email"] in ("aws", "registry") or "@" not in u["email"]:
            continue                                   # 소유자 표시용 가짜 계정은 제외
        if q and q not in u["email"] and q not in (u.get("name") or "").lower():
            continue
        out.append({"email": u["email"], "name": u.get("name") or "", "is_admin": u["email"] in admins.all_admins(),
                    "last_login": u.get("last_login")})
    return {"users": out[: min(max(limit, 1), 50)], "total": len(out)}


@reviewers_router.get("/{record_id}")
def get_reviewers(record_id: str, user: auth.User = Depends(require_admin)):
    return {"record_id": _record_id(record_id), "reviewers": admins.reviewers_of(record_id)}


@reviewers_router.post("")
def add_reviewer(body: ReviewerIn, user: auth.User = Depends(require_admin)):
    rec = _record_id(body.record_id)
    try:
        return {"record_id": rec, "reviewers": admins.add_reviewer(rec, body.email)}
    except ValueError as e:
        raise HTTPException(422, str(e))
    except (ClientError, BotoCoreError) as e:
        raise catalog.aws_err(e)


@reviewers_router.delete("")
def remove_reviewer(record_id: str, email: str, user: auth.User = Depends(require_admin)):
    rec = _record_id(record_id)
    try:
        return {"record_id": rec, "reviewers": admins.remove_reviewer(rec, email)}
    except ValueError as e:
        raise HTTPException(422, str(e))
    except (ClientError, BotoCoreError) as e:
        raise catalog.aws_err(e)
