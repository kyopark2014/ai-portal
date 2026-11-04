"""관리자(= 승인 가능한 사람) 목록.

- 최초 관리자(seed): 환경 변수 ADMIN_EMAILS. 화면에서 지울 수 없다 → 실수로 전원을 지워도 복구 가능.
- 추가 관리자: SSM Parameter Store(/ai-portal/admins). 컨테이너 로컬 DB 에 두면 재배포 때 초기화되어
  승인 권한이 조용히 바뀌므로, 권한 정보는 영속 저장소에 둔다. SSM 을 읽지 못하면 seed 만 관리자로 본다(fail closed).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger("admins")
PARAM = os.environ.get("ADMINS_PARAM", "/ai-portal/admins")
_EMAIL = re.compile(r"^[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+$")
_TTL = 15.0                      # 다른 태스크/요청에서 바꾼 목록이 늦지 않게 반영되도록 짧게 캐시
_lock = threading.Lock()
_cache: tuple[float, set[str]] | None = None


def seed() -> set[str]:
    raw = os.environ.get("ADMIN_EMAILS", "e2npark@gmail.com")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _ssm():
    from . import aws
    return aws.client("ssm")


def valid_email(e: str) -> bool:
    return bool(_EMAIL.match(e)) and len(e) <= 254


def extra() -> set[str]:
    """SSM 에 저장된 추가 관리자. 읽을 수 없으면 빈 집합(= seed 만 관리자)."""
    global _cache
    with _lock:
        if _cache and time.monotonic() - _cache[0] < _TTL:
            return set(_cache[1])
    try:
        v = _ssm().get_parameter(Name=PARAM)["Parameter"]["Value"]
        emails = {e.lower() for e in json.loads(v) if isinstance(e, str) and valid_email(e)}
    except ClientError as e:
        if e.response["Error"]["Code"] != "ParameterNotFound":
            log.warning("admins parameter unreadable (%s) — only seed admins apply", e.response["Error"]["Code"])
        emails = set()
    except (BotoCoreError, ValueError):
        log.warning("admins parameter unreadable — only seed admins apply")
        emails = set()
    with _lock:
        _cache = (time.monotonic(), emails)
    return set(emails)


def all_admins() -> set[str]:
    return seed() | extra()


def is_admin(email: str) -> bool:
    return email.lower() in all_admins()


def listing() -> list[dict]:
    s = seed()
    return sorted(({"email": e, "removable": e not in s} for e in all_admins()), key=lambda x: (x["removable"], x["email"]))


def _save(emails: set[str]) -> None:
    global _cache
    _ssm().put_parameter(Name=PARAM, Type="String", Overwrite=True, Value=json.dumps(sorted(emails)),
                         Description="ai-portal additional approvers (JSON list of emails)")
    with _lock:
        _cache = (time.monotonic(), set(emails))


def add(email: str) -> None:
    email = email.strip().lower()
    if not valid_email(email):
        raise ValueError("이메일 형식이 올바르지 않습니다")
    if email in all_admins():
        raise ValueError("이미 관리자입니다")
    _save(extra() | {email})


def remove(email: str) -> None:
    email = email.strip().lower()
    if email in seed():
        raise ValueError("최초 관리자는 삭제할 수 없습니다")
    cur = extra()
    if email not in cur:
        raise ValueError("관리자 목록에 없습니다")
    _save(cur - {email})


def reset_cache() -> None:
    global _cache, _acache
    with _lock:
        _cache = None
        _acache = None


# ── record 별 지정 승인자 ────────────────────────────────────────────────
# Registry 는 승인 요청에 승인자를 붙일 자리가 없다. 컨테이너 DB 는 재배포 때 초기화되므로 같은 영속 저장소에 둔다.
ASSIGN_PARAM = os.environ.get("APPROVERS_PARAM", "/ai-portal/approvers")
_acache: tuple[float, dict[str, str]] | None = None


def assignments() -> dict[str, str]:
    """{recordId: 지정된 승인자 이메일}. 읽을 수 없으면 빈 매핑 → 지정이 없는 것으로 보고 모든 관리자가 처리 가능(fail open: 지정은 편의 기능)."""
    global _acache
    with _lock:
        if _acache and time.monotonic() - _acache[0] < _TTL:
            return dict(_acache[1])
    try:
        raw = json.loads(_ssm().get_parameter(Name=ASSIGN_PARAM)["Parameter"]["Value"])
        m = {str(k): str(v).lower() for k, v in raw.items()} if isinstance(raw, dict) else {}
    except ClientError as e:
        if e.response["Error"]["Code"] != "ParameterNotFound":
            log.warning("approvers parameter unreadable (%s)", e.response["Error"]["Code"])
        m = {}
    except (BotoCoreError, ValueError):
        m = {}
    with _lock:
        _acache = (time.monotonic(), m)
    return dict(m)


def assign(record_id: str, email: str) -> None:
    """record 의 승인자를 지정한다. 지정할 수 있는 사람은 현재 관리자뿐이다."""
    global _acache
    email = email.strip().lower()
    if email not in all_admins():
        raise ValueError("관리자가 아닌 사람은 승인자로 지정할 수 없습니다")
    m = assignments(); m[record_id] = email
    _ssm().put_parameter(Name=ASSIGN_PARAM, Type="String", Overwrite=True, Value=json.dumps(m),
                         Description="ai-portal: record -> assigned approver")
    with _lock:
        _acache = (time.monotonic(), m)


def clear_assignment(record_id: str) -> None:
    global _acache
    m = assignments()
    if m.pop(record_id, None) is None:
        return
    _ssm().put_parameter(Name=ASSIGN_PARAM, Type="String", Overwrite=True, Value=json.dumps(m))
    with _lock:
        _acache = (time.monotonic(), m)


def can_decide(record_id: str, email: str) -> bool:
    """이 record 를 승인/거절할 수 있는가: 관리자이고, 지정된 승인자가 있다면 그 사람이어야 한다."""
    email = email.lower()
    if email not in all_admins():
        return False
    who = assignments().get(record_id)
    return who is None or who == email or who not in all_admins()   # 지정된 사람이 더 이상 관리자가 아니면 막히지 않게 풀어 준다
