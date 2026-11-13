"""Playground 대화(task) 저장소. 사용자 · agent 별로 task 목록과 메시지를 보관한다.

배포 환경은 DynamoDB 테이블 `ai-portal-playground`(PORTAL_STORE=dynamodb), 로컬·테스트는 SQLite(PORTAL_STORE=sqlite)를 쓴다.
포털 본 저장소(db.py)와 분리해 둔 이유: 대화 기록은 양이 계속 늘고 포털 항목과 수명·접근 패턴이 다르기 때문이다.

DynamoDB 키 (pk / sk):
  USER#<email>  | TASK#<task_id>              task 한 건 (agent_id, title, pinned, 시각)
  TASK#<task_id>| MSG#<seq 10자리>             메시지 한 건 (role, content, tool_events)

task_id 는 서버가 만드는 uuid 이고, 모든 조회·변경은 **email 이 일치하는 task 만** 대상으로 한다(남의 task 는 없는 것과 같다).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Optional

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError

MAX_TITLE = 50
_cfg = Config(retries={"max_attempts": 5, "mode": "standard"}, connect_timeout=5, read_timeout=15)
_local = threading.local()
_override = None


def backend() -> str:
    s = (os.environ.get("PORTAL_STORE") or "dynamodb").strip().lower()
    if s not in ("dynamodb", "sqlite"):
        raise RuntimeError(f"PORTAL_STORE={s!r} 는 지원하지 않습니다 (dynamodb | sqlite)")
    return s


def table_name() -> str:
    return os.environ.get("PLAYGROUND_DDB_TABLE", "ai-portal-playground")


def _region() -> str:
    return os.environ.get("DDB_REGION") or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"


def _table():
    if _override is not None:
        return _override
    key = (_region(), table_name())
    if getattr(_local, "key", None) != key:
        _local.table = boto3.resource("dynamodb", region_name=_region(), config=_cfg).Table(table_name())
        _local.key = key
    return _local.table


def reset_for_tests(tbl=None) -> None:
    global _override
    _override = tbl
    _local.__dict__.clear()


def _plain(v: Any) -> Any:
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    return v


def _title_of(text: str) -> str:
    return " ".join(text.split())[:MAX_TITLE] or "New task"


# ── SQLite ──
_SCHEMA = """
CREATE TABLE IF NOT EXISTS pg_tasks (
  id TEXT PRIMARY KEY, email TEXT NOT NULL, agent_id INTEGER NOT NULL, title TEXT NOT NULL,
  pinned INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_pg_tasks ON pg_tasks(email, agent_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS pg_messages (
  task_id TEXT NOT NULL, seq INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
  tool_events TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, PRIMARY KEY (task_id, seq));
"""
_ready: set = set()
_ready_lock = threading.Lock()


def _sqlite_path() -> Path:
    p = os.environ.get("PLAYGROUND_DB_PATH") or os.environ.get("PORTAL_DB_PATH")
    return Path(p) if p else Path(__file__).resolve().parent / "data" / "portal.db"


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    path = _sqlite_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    if path not in _ready:
        with _ready_lock:
            if path not in _ready:
                c.executescript(_SCHEMA)
                _ready.add(path)
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _task_from_sql(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "agent_id": r["agent_id"], "title": r["title"], "pinned": bool(r["pinned"]),
            "created_at": r["created_at"], "updated_at": r["updated_at"]}


# ── DynamoDB ──
def _task_from_ddb(r: dict) -> dict:
    return {"id": r["task_id"], "agent_id": int(r["agent_id"]), "title": r["title"], "pinned": bool(r.get("pinned", False)),
            "created_at": _plain(r["created_at"]), "updated_at": _plain(r["updated_at"])}


def _ukey(email: str, task_id: str) -> dict:
    return {"pk": f"USER#{email.lower()}", "sk": f"TASK#{task_id}"}


# ── 공개 함수 (두 저장소가 같은 모양을 돌려준다) ──
def list_tasks(email: str, agent_id: int, limit: int = 100) -> list[dict]:
    """이 사용자의 이 agent task. 고정(pinned)이 먼저, 그다음 최근 갱신순."""
    email = email.lower()
    if backend() == "sqlite":
        with _conn() as c:
            rows = c.execute("SELECT * FROM pg_tasks WHERE email=? AND agent_id=?", (email, agent_id)).fetchall()
        out = [_task_from_sql(r) for r in rows]
    else:
        items, start = [], None
        while True:
            r = _table().query(KeyConditionExpression=Key("pk").eq(f"USER#{email}") & Key("sk").begins_with("TASK#"),
                               **({"ExclusiveStartKey": start} if start else {}))
            items += r["Items"]; start = r.get("LastEvaluatedKey")
            if not start:
                break
        out = [_task_from_ddb(i) for i in items if int(i["agent_id"]) == agent_id]
    out.sort(key=lambda t: (not t["pinned"], -t["updated_at"]))
    return out[:limit]


def get_task(email: str, task_id: str) -> Optional[dict]:
    email = email.lower()
    if backend() == "sqlite":
        with _conn() as c:
            r = c.execute("SELECT * FROM pg_tasks WHERE id=? AND email=?", (task_id, email)).fetchone()
        return _task_from_sql(r) if r else None
    r = _table().get_item(Key=_ukey(email, task_id)).get("Item")
    return _task_from_ddb(r) if r else None


def create_task(email: str, agent_id: int, title: str = "New task") -> dict:
    email, now, tid = email.lower(), time.time(), uuid.uuid4().hex
    title = _title_of(title) if title != "New task" else title
    if backend() == "sqlite":
        with _conn() as c:
            c.execute("INSERT INTO pg_tasks(id,email,agent_id,title,pinned,created_at,updated_at) VALUES(?,?,?,?,0,?,?)",
                      (tid, email, agent_id, title, now, now))
    else:
        _table().put_item(Item={**_ukey(email, tid), "task_id": tid, "agent_id": agent_id, "title": title, "pinned": False,
                                "created_at": Decimal(str(now)), "updated_at": Decimal(str(now))})
    return {"id": tid, "agent_id": agent_id, "title": title, "pinned": False, "created_at": now, "updated_at": now}


def update_task(email: str, task_id: str, *, title: Optional[str] = None, pinned: Optional[bool] = None) -> Optional[dict]:
    """제목·고정 여부를 바꾼다. 내 task 가 아니면 None."""
    cur = get_task(email, task_id)
    if cur is None:
        return None
    new = {"title": _title_of(title) if title is not None else cur["title"],
           "pinned": cur["pinned"] if pinned is None else bool(pinned), "updated_at": time.time()}
    if backend() == "sqlite":
        with _conn() as c:
            c.execute("UPDATE pg_tasks SET title=?, pinned=?, updated_at=? WHERE id=? AND email=?",
                      (new["title"], int(new["pinned"]), new["updated_at"], task_id, email.lower()))
    else:
        try:
            _table().update_item(Key=_ukey(email, task_id), UpdateExpression="SET title=:t, pinned=:p, updated_at=:u",
                                 ConditionExpression="attribute_exists(pk)",
                                 ExpressionAttributeValues={":t": new["title"], ":p": new["pinned"], ":u": Decimal(str(new["updated_at"]))})
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            return None
    return {**cur, **new}


def delete_task(email: str, task_id: str) -> bool:
    if get_task(email, task_id) is None:
        return False
    if backend() == "sqlite":
        with _conn() as c:
            c.execute("DELETE FROM pg_messages WHERE task_id=?", (task_id,))
            c.execute("DELETE FROM pg_tasks WHERE id=? AND email=?", (task_id, email.lower()))
    else:
        t = _table()
        with t.batch_writer() as b:
            for m in _msg_rows(task_id):
                b.delete_item(Key={"pk": m["pk"], "sk": m["sk"]})
            b.delete_item(Key=_ukey(email, task_id))
    return True


def _msg_rows(task_id: str) -> list[dict]:
    rows, start = [], None
    while True:
        r = _table().query(KeyConditionExpression=Key("pk").eq(f"TASK#{task_id}"),
                           **({"ExclusiveStartKey": start} if start else {}))
        rows += r["Items"]; start = r.get("LastEvaluatedKey")
        if not start:
            return rows


def list_messages(email: str, task_id: str) -> Optional[list[dict]]:
    """task 의 메시지를 순서대로. 내 task 가 아니면 None."""
    if get_task(email, task_id) is None:
        return None
    if backend() == "sqlite":
        with _conn() as c:
            rows = c.execute("SELECT * FROM pg_messages WHERE task_id=? ORDER BY seq", (task_id,)).fetchall()
        return [{"seq": r["seq"], "role": r["role"], "content": r["content"], "tool_events": json.loads(r["tool_events"] or "[]"),
                 "created_at": r["created_at"]} for r in rows]
    return [{"seq": int(r["sk"].split("#")[1]), "role": r["role"], "content": r["content"],
             "tool_events": json.loads(r.get("tool_events") or "[]"), "created_at": _plain(r["created_at"])}
            for r in sorted(_msg_rows(task_id), key=lambda x: x["sk"])]


def add_messages(email: str, task_id: str, msgs: list[dict]) -> bool:
    """메시지(role·content·tool_events)를 뒤에 붙이고 task 의 갱신 시각을 올린다. 첫 user 메시지가 오면 제목이 'New task' 일 때 제목으로 쓴다.
    내 task 가 아니면 False."""
    cur = get_task(email, task_id)
    if cur is None or not msgs:
        return cur is not None
    now = time.time()
    if backend() == "sqlite":
        with _conn() as c:
            n = c.execute("SELECT COALESCE(MAX(seq), -1) FROM pg_messages WHERE task_id=?", (task_id,)).fetchone()[0] + 1
            for i, m in enumerate(msgs):
                c.execute("INSERT INTO pg_messages(task_id,seq,role,content,tool_events,created_at) VALUES(?,?,?,?,?,?)",
                          (task_id, n + i, m["role"], m["content"], json.dumps(m.get("tool_events") or [], ensure_ascii=False), now))
    else:
        existing = _msg_rows(task_id)
        n = max((int(r["sk"].split("#")[1]) for r in existing), default=-1) + 1
        with _table().batch_writer() as b:
            for i, m in enumerate(msgs):
                b.put_item(Item={"pk": f"TASK#{task_id}", "sk": f"MSG#{n + i:010d}", "role": m["role"], "content": m["content"],
                                 "tool_events": json.dumps(m.get("tool_events") or [], ensure_ascii=False), "created_at": Decimal(str(now))})
    first_user = next((m["content"] for m in msgs if m["role"] == "user"), None)
    update_task(email, task_id, title=first_user if (first_user and cur["title"] == "New task") else None)
    return True
