"""승인 요청 알림 메일 — EventBridge(Registry Record State changed to Pending Approval) → 이 Lambda → SES.

수신자: 이 record 에 지정된 승인자(SSM /ai-portal/approvers) 한 명. 지정이 없으면 모든 관리자.
지정된 사람이 이미 관리자가 아니면 모든 관리자에게 보낸다(요청이 아무에게도 안 가는 일이 없게).
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
APPROVERS_PARAM = os.environ.get("APPROVERS_PARAM", "/ai-portal/approvers")
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
    admins = set(SEED_ADMINS) | {e.lower() for e in _param(ADMINS_PARAM, []) if isinstance(e, str)}
    who = (_param(APPROVERS_PARAM, {}) or {}).get(record_id)
    if who and who.lower() in admins:
        return [who.lower()], True
    return sorted(admins), False


def handler(event, _ctx):
    rec = (event.get("detail") or {}).get("registryRecordId", "")
    if not _ID.match(rec):
        print("ignored: bad record id")
        return {"sent": 0}
    to, assigned = recipients(rec)
    link = f"{BASE}/approvals/{urllib.parse.quote(rec)}"
    body = (
        f"승인 요청이 들어왔습니다.\n\n"
        f"Record ID : {rec}\n"
        f"받는 사람 : {'지정된 승인자(당신)' if assigned else '모든 관리자'}\n"
        f"요청 시각 : {event.get('time', '')}\n\n"
        f"아래 링크에서 로그인한 뒤 검토하고 승인/거절하세요.\n{link}\n"
    )
    sent = 0
    for addr in to:                              # 한 명이 실패해도 나머지는 보낸다
        try:
            SES.send_email(FromEmailAddress=SENDER, Destination={"ToAddresses": [addr]},
                           Content={"Simple": {"Subject": {"Data": "[AI Portal] 승인 요청", "Charset": "UTF-8"},
                                               "Body": {"Text": {"Data": body, "Charset": "UTF-8"}}}})
            sent += 1
        except Exception as e:  # noqa: BLE001
            print("send failed:", addr.split("@")[-1], type(e).__name__, str(e)[:120])
    print(json.dumps({"record": rec, "assigned": assigned, "recipients": len(to), "sent": sent}))
    return {"sent": sent}
