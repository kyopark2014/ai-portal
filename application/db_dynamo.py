"""DynamoDB 저장소. SQLite 와 같은 함수·정수 id 를 제공하므로 호출부는 바뀌지 않는다.

테이블 하나(`<project>`), 키 pk(S)/sk(S):
  ITEM#<kind>   | <id 10자리 0패딩>     항목(prompt/mcp/skill/agent)
  LOGIN#<email> | <ts 20자리>           로그인 기록(이메일·이름·시각만)
  SETTING       | <key>                 설정
  COUNTER       | items                 정수 id 발급용 원자 카운터

규모(수백 건 이하)에서는 Query/Scan 후 서버에서 필터·집계한다. 정수 id 는 UpdateItem ADD 의 원자 증가로 발급해
동시 요청에서도 겹치지 않고, 항목 수정·삭제는 조건부 쓰기(존재할 때만)로 해서 지워진 항목을 되살리지 않는다.
"""
from __future__ import annotations

import json
import os
import threading
import time
from decimal import Decimal
from typing import Any, Optional

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.config import Config
from botocore.exceptions import ClientError

KINDS = ("prompt", "mcp", "agent", "skill")
_cfg = Config(retries={"max_attempts": 5, "mode": "standard"}, connect_timeout=5, read_timeout=15)
_local = threading.local()          # boto3 resource 는 스레드 간 공유가 안전하지 않다 → 스레드마다 따로 만든다
_override = None                    # 테스트가 주입하는 테이블


def table_name() -> str:
    return os.environ.get("DDB_TABLE", "ai-portal")


def region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"


def table():
    if _override is not None:
        return _override
    key = (region(), table_name())
    if getattr(_local, "key", None) != key:
        _local.table = boto3.resource("dynamodb", region_name=region(), config=_cfg).Table(table_name())
        _local.key = key
    return _local.table


def reset_for_tests(tbl=None) -> None:
    global _override
    _override = tbl
    _local.__dict__.clear()


# ── 값 변환: DynamoDB 는 float 를 받지 않고 숫자를 Decimal 로 돌려준다 ──
def _num(v: Any) -> Any:
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, dict):
        return {k: _num(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_num(x) for x in v]
    return v


def _plain(v: Any) -> Any:
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    return v


def _row(r: dict) -> dict:
    d = {k: _plain(v) for k, v in r.items() if k not in ("pk", "sk")}
    d["id"] = int(d["id"])
    d["data"] = json.loads(d.get("data") or "{}")
    d["desc_edited"] = bool(d.get("desc_edited", 0))
    for k in ("ext_id", "registry_id", "registry_record_id", "registry_status"):
        d[k] = d.get(k) or None
    return d


def _key(kind: str, item_id: int) -> dict:
    return {"pk": f"ITEM#{kind}", "sk": f"{item_id:010d}"}


def is_visible(item: dict, email: str, is_admin: bool) -> bool:
    return is_admin or item["owner"] == email or item["registry_status"] == "APPROVED"


def _query_all(pk: str, **kw) -> list[dict]:
    out, start = [], None
    while True:
        r = table().query(KeyConditionExpression=Key("pk").eq(pk), **({"ExclusiveStartKey": start} if start else {}), **kw)
        out += r["Items"]
        start = r.get("LastEvaluatedKey")
        if not start:
            return out


def _all_items() -> list[dict]:
    return [_row(r) for k in KINDS for r in _query_all(f"ITEM#{k}")]


def _next_id() -> int:
    r = table().update_item(Key={"pk": "COUNTER", "sk": "items"}, UpdateExpression="ADD n :one",
                            ExpressionAttributeValues={":one": 1}, ReturnValues="UPDATED_NEW")
    return int(r["Attributes"]["n"])


# ── 항목 ──
def list_items(kind: str, q: str = "", viewer: Optional[tuple[str, bool]] = None) -> list[dict]:
    rows = [_row(r) for r in _query_all(f"ITEM#{kind}")]
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in r["name"].lower() or ql in (r["description"] or "").lower()]
    if viewer and not viewer[1]:
        rows = [r for r in rows if r["owner"] == viewer[0] or r["registry_status"] == "APPROVED"]
    return sorted(rows, key=lambda r: r["updated_at"], reverse=True)


def list_owned(email: str) -> list[dict]:
    return sorted((r for r in _all_items() if r["owner"] == email), key=lambda r: r["updated_at"], reverse=True)


def get_item(item_id: int) -> Optional[dict]:
    """kind 를 모르는 조회라 4개 종류를 차례로 본다(각각 단일 키 GetItem)."""
    for k in KINDS:
        r = table().get_item(Key=_key(k, int(item_id))).get("Item")
        if r:
            return _row(r)
    return None


def insert_item(kind: str, name: str, description: str, owner: str, data: dict,
                source: str = "portal", ext_id: Optional[str] = None) -> int:
    if kind not in KINDS:
        raise ValueError(kind)
    now = time.time()
    item_id = _next_id()
    table().put_item(
        Item=_num({**_key(kind, item_id), "id": item_id, "kind": kind, "name": name, "description": description, "owner": owner,
                   "source": source, "ext_id": ext_id or "", "data": json.dumps(data), "desc_edited": 0,
                   "registry_id": "", "registry_record_id": "", "registry_status": "", "created_at": now, "updated_at": now}),
        ConditionExpression=Attr("pk").not_exists())                       # id 충돌 시 덮어쓰지 않는다
    return item_id


_UPDATABLE = {"name", "description", "data", "desc_edited", "registry_id", "registry_record_id", "registry_status", "ext_id", "owner"}


def update_item(item_id: int, **fields: Any) -> None:
    for k in fields:
        if k not in _UPDATABLE:
            raise ValueError(k)
    cur = get_item(item_id)
    if cur is None:
        return                                                             # 이미 지워진 항목: 조용히 무시(SQLite UPDATE 와 동일)
    sets, names, vals = [], {}, {}
    for i, (k, v) in enumerate({**fields, "updated_at": time.time()}.items()):
        names[f"#f{i}"], vals[f":v{i}"] = k, _num(json.dumps(v) if k == "data" else ("" if v is None and k != "desc_edited" else v))
        sets.append(f"#f{i} = :v{i}")
    try:
        table().update_item(Key=_key(cur["kind"], cur["id"]), UpdateExpression="SET " + ", ".join(sets),
                            ConditionExpression=Attr("pk").exists(),       # 그 사이 삭제됐다면 되살리지 않는다
                            ExpressionAttributeNames=names, ExpressionAttributeValues=vals)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


def delete_item(item_id: int) -> None:
    cur = get_item(item_id)
    if cur:
        table().delete_item(Key=_key(cur["kind"], cur["id"]))


def upsert_synced(kind: str, ext_id: str, name: str, description: str, data: dict, owner: str = "aws") -> bool:
    existing = next((r for r in _query_all(f"ITEM#{kind}") if r.get("ext_id") == ext_id), None)
    if existing is None:
        insert_item(kind, name, description, owner, data, "aws", ext_id)
        return True
    row = _row(existing)
    merged = {**row["data"], **data}
    if row["desc_edited"]:
        update_item(row["id"], name=name, data=merged)
    else:
        update_item(row["id"], name=name, description=description, data=merged)
    return False


def prune_synced(kind: str, keep_ext_ids: set[str]) -> int:
    gone = [r for r in (_row(x) for x in _query_all(f"ITEM#{kind}"))
            if r["source"] == "aws" and r["ext_id"] and r["ext_id"] not in keep_ext_ids]
    for r in gone:
        delete_item(r["id"])
    return len(gone)


def find_by_record(record_id: str) -> Optional[dict]:
    return next((r for r in _all_items() if r["registry_record_id"] == record_id), None)


# ── 설정 ──
def get_setting(key: str, default: str = "") -> str:
    r = table().get_item(Key={"pk": "SETTING", "sk": key}).get("Item")
    return r["value"] if r else default


def set_setting(key: str, value: str) -> None:
    table().put_item(Item={"pk": "SETTING", "sk": key, "value": value})


# ── 로그인 기록(이메일·이름·시각만) ──
def record_login(email: str, name: str) -> bool:
    email = email.lower()
    first = not table().query(KeyConditionExpression=Key("pk").eq(f"LOGIN#{email}"), Limit=1)["Items"]
    now = time.time()
    table().put_item(Item=_num({"pk": f"LOGIN#{email}", "sk": f"{int(now * 1000):020d}", "email": email,
                                "name": name[:200], "logged_at": now}))
    return first


def _logins() -> list[dict]:
    out, start = [], None
    while True:
        r = table().scan(FilterExpression=Attr("pk").begins_with("LOGIN#"), **({"ExclusiveStartKey": start} if start else {}))
        out += [{k: _plain(v) for k, v in x.items()} for x in r["Items"]]
        start = r.get("LastEvaluatedKey")
        if not start:
            return out


def user_stats() -> list[dict]:
    by: dict[str, dict] = {}
    for l in sorted(_logins(), key=lambda x: x["logged_at"]):
        u = by.setdefault(l["email"], {"email": l["email"], "login_count": 0, "first_login": l["logged_at"], "last_login": None, "name": ""})
        u["login_count"] += 1
        u["last_login"], u["name"] = l["logged_at"], l.get("name", "")
    users = {e: {**v, "kinds": {}, "statuses": {}, "total": 0} for e, v in by.items()}
    for it in _all_items():
        st = it["registry_status"] or "NOT_REGISTERED"
        u = users.setdefault(it["owner"].lower(), {"email": it["owner"].lower(), "login_count": 0, "first_login": None, "last_login": None,
                                                   "name": "", "kinds": {}, "statuses": {}, "total": 0})
        u["kinds"].setdefault(it["kind"], {})
        u["kinds"][it["kind"]][st] = u["kinds"][it["kind"]].get(st, 0) + 1
        u["statuses"][st] = u["statuses"].get(st, 0) + 1
        u["total"] += 1
    return sorted(users.values(), key=lambda u: (u["last_login"] or 0, u["total"]), reverse=True)


def login_totals() -> dict:
    ls = _logins()
    week = time.time() - 7 * 86400
    return {"total_logins": len(ls), "unique_users": len({l["email"] for l in ls}),
            "active_7d": len({l["email"] for l in ls if l["logged_at"] >= week})}


def recent_logins(limit: int = 30) -> list[dict]:
    rows = sorted(_logins(), key=lambda x: x["logged_at"], reverse=True)[: min(max(limit, 1), 200)]
    return [{"email": r["email"], "name": r.get("name", ""), "logged_at": r["logged_at"]} for r in rows]
