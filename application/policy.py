"""승인 자동화 정책 (관리자가 화면에서 켜고 끈다). 둘 다 기본은 꺼짐.

  auto_approval : 등록하면 승인 요청까지 자동으로 진행해 곧바로 APPROVED 가 된다 (포털 서버가 처리)
  auto_review   : 승인 요청(PENDING_APPROVAL)이 오면 reviewer/관리자의 허락 없이 자동으로 APPROVED 처리한다 (Lambda 가 처리)

승인 절차를 줄이는 설정이라 SSM 에 영속 저장하고(재배포 후에도 유지), 누가 언제 바꿨는지를 함께 남긴다.
저장소를 읽지 못하면 **꺼짐으로 본다(fail closed)** — 장애 때문에 검토 없이 승인되는 일이 없도록.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger("policy")
PARAM = os.environ.get("POLICY_PARAM", "/ai-portal/policy")
_TTL = 10.0
_lock = threading.Lock()
_cache: tuple[float, dict] | None = None
DEFAULT = {"auto_approval": False, "auto_review": False, "changed_by": "", "changed_at": 0}


def _ssm():
    from . import aws
    return aws.client("ssm")


def get() -> dict[str, Any]:
    global _cache
    with _lock:
        if _cache and time.monotonic() - _cache[0] < _TTL:
            return dict(_cache[1])
    pol = dict(DEFAULT)
    try:
        raw = json.loads(_ssm().get_parameter(Name=PARAM)["Parameter"]["Value"])
        if isinstance(raw, dict):
            pol.update({k: raw[k] for k in DEFAULT if k in raw})
            pol["auto_approval"], pol["auto_review"] = bool(pol["auto_approval"]), bool(pol["auto_review"])
    except ClientError as e:
        if e.response["Error"]["Code"] != "ParameterNotFound":
            log.warning("policy unreadable (%s) — automation stays OFF", e.response["Error"]["Code"])
    except (BotoCoreError, ValueError):
        log.warning("policy unreadable — automation stays OFF")
    with _lock:
        _cache = (time.monotonic(), pol)
    return dict(pol)


def set_(*, auto_approval: bool | None, auto_review: bool | None, by: str) -> dict[str, Any]:
    """변경할 값만 받아 저장한다(None 은 그대로). 저장에 실패하면 예외를 그대로 올려 호출자가 알 수 있게 한다."""
    global _cache
    cur = get()
    new = {"auto_approval": cur["auto_approval"] if auto_approval is None else bool(auto_approval),
           "auto_review": cur["auto_review"] if auto_review is None else bool(auto_review),
           "changed_by": by, "changed_at": int(time.time())}
    _ssm().put_parameter(Name=PARAM, Type="String", Overwrite=True, Value=json.dumps(new),
                         Description="ai-portal: approval automation switches")
    with _lock:
        _cache = (time.monotonic(), new)
    return dict(new)


def reset_cache() -> None:
    global _cache
    with _lock:
        _cache = None
