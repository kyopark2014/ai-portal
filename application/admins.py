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


# ── record 별 reviewer ───────────────────────────────────────────────────
# 관리자는 항상 모든 record 를 승인할 수 있다. 관리자가 record 마다 reviewer 를 추가하면 그 사람도 **그 record 만** 승인할 수 있다.
# Registry 는 record 에 reviewer 를 붙일 자리가 없고 컨테이너 DB 는 재배포 때 초기화되므로 SSM 에 둔다.
REVIEWERS_PARAM = os.environ.get("REVIEWERS_PARAM", "/ai-portal/reviewers")
MAX_REVIEWERS_PER_RECORD = 20
_acache: tuple[float, dict[str, list[str]]] | None = None


def reviewers_map() -> dict[str, list[str]]:
    """{recordId: [reviewer 이메일…]}. 읽을 수 없으면 빈 매핑 → reviewer 권한은 인정하지 않는다(fail closed: 관리자만 승인)."""
    global _acache
    with _lock:
        if _acache and time.monotonic() - _acache[0] < _TTL:
            return {k: list(v) for k, v in _acache[1].items()}
    try:
        raw = json.loads(_ssm().get_parameter(Name=REVIEWERS_PARAM)["Parameter"]["Value"])
        m = {str(k): sorted({str(e).lower() for e in v if isinstance(e, str)}) for k, v in raw.items()} if isinstance(raw, dict) else {}
    except ClientError as e:
        if e.response["Error"]["Code"] != "ParameterNotFound":
            log.warning("reviewers parameter unreadable (%s) — only admins can decide", e.response["Error"]["Code"])
        m = {}
    except (BotoCoreError, ValueError):
        log.warning("reviewers parameter unreadable — only admins can decide")
        m = {}
    with _lock:
        _acache = (time.monotonic(), m)
    return {k: list(v) for k, v in m.items()}


def reviewers_of(record_id: str) -> list[str]:
    return reviewers_map().get(record_id, [])


def _save_reviewers(m: dict[str, list[str]]) -> None:
    global _acache
    m = {k: v for k, v in m.items() if v}
    _ssm().put_parameter(Name=REVIEWERS_PARAM, Type="String", Overwrite=True, Value=json.dumps(m, sort_keys=True),
                         Description="ai-portal: record -> additional reviewers")
    with _lock:
        _acache = (time.monotonic(), m)


def add_reviewer(record_id: str, email: str) -> list[str]:
    email = email.strip().lower()
    if not valid_email(email):
        raise ValueError("이메일 형식이 올바르지 않습니다")
    m = reviewers_map(); cur = set(m.get(record_id, []))
    if email in all_admins():
        raise ValueError("관리자는 이미 모든 record 를 승인할 수 있습니다")
    if email in cur:
        raise ValueError("이미 이 record 의 reviewer 입니다")
    if len(cur) >= MAX_REVIEWERS_PER_RECORD:
        raise ValueError(f"reviewer 는 record 당 {MAX_REVIEWERS_PER_RECORD}명까지 지정할 수 있습니다")
    m[record_id] = sorted(cur | {email})
    _save_reviewers(m)
    return m[record_id]


def set_owner_as_reviewer(record_id: str, owner: str | None) -> None:
    """등록 승인(APPROVED)된 record 의 소유자를 reviewer 로 둔다(그 record 의 사용 신청을 처리할 수 있다).
    관리자이거나 이미 reviewer 이면 할 일이 없다. best effort — 실패해도 승인 자체에는 영향이 없다."""
    if not owner or not valid_email(owner.strip().lower()):
        return
    try:
        add_reviewer(record_id, owner)
    except ValueError:
        pass                                                      # 관리자 / 이미 reviewer / 상한
    except Exception:  # noqa: BLE001
        log.warning("could not add owner as reviewer for %s", record_id)


def remove_reviewer(record_id: str, email: str) -> list[str]:
    email = email.strip().lower()
    m = reviewers_map(); cur = set(m.get(record_id, []))
    if email not in cur:
        raise ValueError("이 record 의 reviewer 가 아닙니다")
    m[record_id] = sorted(cur - {email})
    _save_reviewers(m)
    return m[record_id]


def clear_reviewers(record_id: str) -> None:
    """record 가 사라졌을 때 그 reviewer 정보를 정리한다."""
    m = reviewers_map()
    if m.pop(record_id, None) is None:
        return
    _save_reviewers(m)


def is_reviewer(record_id: str, email: str) -> bool:
    return email.lower() in reviewers_of(record_id)


def can_decide(record_id: str, email: str) -> bool:
    """이 record 를 승인/거절할 수 있는가: 관리자(전체) 또는 이 record 의 reviewer."""
    return is_admin(email) or is_reviewer(record_id, email)


def reviewing(email: str) -> list[str]:
    """이 사람이 reviewer 로 지정된 record id 들."""
    e = email.lower()
    return sorted(r for r, v in reviewers_map().items() if e in v)


def reset_cache() -> None:
    global _cache, _acache
    with _lock:
        _cache = None
        _acache = None
