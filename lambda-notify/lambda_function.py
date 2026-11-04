"""승인 요청 알림 메일 — EventBridge(Registry Record State changed to Pending Approval) → 이 Lambda → SES.

수신자: 모든 관리자 + 이 record 에 관리자가 추가한 reviewer(SSM /ai-portal/reviewers). 관리자는 항상 모든 record 를
승인할 수 있으므로 항상 포함하고, reviewer 는 자기에게 지정된 record 의 알림만 받는다(다른 record 의 알림은 받지 않는다).
메일 발송에 실패해도 승인 요청 자체에는 영향이 없다(예외는 로그로 남기고 재시도는 EventBridge 가 한다).
"""
import json
import os
import re
import urllib.parse

import boto3

SSM = boto3.client("ssm")
SES = boto3.client("sesv2")
ADMINS_PARAM = os.environ.get("ADMINS_PARAM", "/ai-portal/admins")
REVIEWERS_PARAM = os.environ.get("REVIEWERS_PARAM", "/ai-portal/reviewers")
SEED_ADMINS = [e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()]
SENDER = os.environ["SENDER"]
BASE = os.environ["PORTAL_ORIGIN"].rstrip("/")
_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


def _param(name, default):
    try:
        return json.loads(SSM.get_parameter(Name=name)["Parameter"]["Value"])
    except SSM.exceptions.ParameterNotFound:
        return default
    except Exception as e:  # noqa: BLE001 — 읽지 못하면 seed 관리자 기준으로 동작
        print("ssm read failed:", name, type(e).__name__)
        return default


def recipients(record_id):
    """(관리자 목록, reviewer 목록). 둘 다 정규화·중복 제거하고, 관리자와 겹치는 reviewer 는 reviewer 쪽에서 뺀다."""
    admins = set(SEED_ADMINS) | {e.lower() for e in _param(ADMINS_PARAM, []) if isinstance(e, str)}
    raw = (_param(REVIEWERS_PARAM, {}) or {}).get(record_id) or []
    reviewers = {e.lower() for e in raw if isinstance(e, str)} - admins
    return sorted(admins), sorted(reviewers)


def _mail(rec, role, link, when):
    who = "reviewer(이 record 의 검토 담당)" if role == "reviewer" else "관리자"
    return (f"승인 요청이 들어왔습니다.\n\nRecord ID : {rec}\n받는 사람 : {who}\n요청 시각 : {when}\n\n"
            f"아래 링크에서 로그인한 뒤 검토하고 승인/거절하세요.\n{link}\n")


def handler(event, _ctx):
    rec = (event.get("detail") or {}).get("registryRecordId", "")
    if not _ID.match(rec):
        print("ignored: bad record id")
        return {"sent": 0}
    admins, reviewers = recipients(rec)
    link = f"{BASE}/approvals/{urllib.parse.quote(rec)}"
    sent = 0
    for role, group in (("admin", admins), ("reviewer", reviewers)):
        for addr in group:                          # 한 명이 실패해도 나머지는 보낸다
            try:
                SES.send_email(FromEmailAddress=SENDER, Destination={"ToAddresses": [addr]},
                               Content={"Simple": {"Subject": {"Data": "[AI Portal] 승인 요청", "Charset": "UTF-8"},
                                                   "Body": {"Text": {"Data": _mail(rec, role, link, event.get("time", "")), "Charset": "UTF-8"}}}})
                sent += 1
            except Exception as e:  # noqa: BLE001
                print("send failed:", addr.split("@")[-1], type(e).__name__, str(e)[:120])
    print(json.dumps({"record": rec, "admins": len(admins), "reviewers": len(reviewers), "sent": sent}))
    return {"sent": sent}
