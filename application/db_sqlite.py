"""SQLite 저장소(로컬 개발·테스트 기본값). 배포 환경은 db_dynamo.py(DynamoDB)를 쓴다. 카탈로그 항목(prompt/mcp/agent/skill)을 kind 컬럼으로 구분해 한 테이블에 둔다.

종류별 필드는 data(JSON)에 둔다. AWS 에서 동기화한 항목은 (kind, ext_id)로 upsert 한다.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

import threading
_ready: set = set()
_ready_lock = threading.Lock()

KINDS = ("prompt", "mcp", "agent", "skill")

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  owner TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'portal',   -- portal | aws
  ext_id TEXT,                             -- prompt id / harness id / runtime arn
  data TEXT NOT NULL DEFAULT '{}',
  desc_edited INTEGER NOT NULL DEFAULT 0,  -- 사용자가 설명을 고쳤으면 동기화가 덮어쓰지 않는다
  registry_id TEXT,
  registry_record_id TEXT,
  registry_status TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_items_ext ON items(kind, ext_id) WHERE ext_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_items_kind ON items(kind, updated_at DESC);
CREATE TABLE IF NOT EXISTS login_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  logged_at REAL NOT NULL            -- 프로필 사진 URL 은 저장하지 않는다(최소 수집)
);
CREATE INDEX IF NOT EXISTS ix_login_email ON login_events(email, logged_at DESC);
CREATE INDEX IF NOT EXISTS ix_login_at ON login_events(logged_at DESC);
CREATE TABLE IF NOT EXISTS usage_requests (
  item_id INTEGER NOT NULL,
  email TEXT NOT NULL,
  status TEXT NOT NULL,                -- PENDING | APPROVED | REJECTED
  requested_at REAL NOT NULL,
  decided_at REAL,
  decided_by TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (item_id, email)
);
CREATE INDEX IF NOT EXISTS ix_usage_status ON usage_requests(status, requested_at DESC);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def db_path() -> Path:
    p = os.environ.get("PORTAL_DB_PATH")
    return Path(p) if p else Path(__file__).resolve().parent / "data" / "portal.db"


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")                 # 쓰기 잠금을 기다린다(동시 요청이 서로 '잠김' 오류를 내지 않게)
    if path not in _ready:                                  # 스키마·WAL 설정은 파일마다 한 번만. 매 연결마다 하면 동시 요청이 서로 잠근다
        with _ready_lock:
            if path not in _ready:
                c.execute("PRAGMA journal_mode=WAL")
                c.executescript(SCHEMA)
                _ready.add(path)
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["data"] = json.loads(d["data"] or "{}")
    d["desc_edited"] = bool(d["desc_edited"])
    return d


def is_visible(item: dict, email: str, is_admin: bool) -> bool:
    """관리자는 전부. 일반 사용자는 자기가 만든 것(owner)과 Registry 승인(APPROVED)된 것만."""
    return is_admin or item["owner"] == email or item["registry_status"] == "APPROVED"


def list_items(kind: str, q: str = "", viewer: tuple[str, bool] | None = None) -> list[dict]:
    """viewer=(email, is_admin) 을 주면 그 사용자에게 보이는 항목만. None 이면 전부(내부 동기화용)."""
    sql, args = "SELECT * FROM items WHERE kind=?", [kind]
    if q:
        like = f"%{q.lower()}%"
        sql += " AND (lower(name) LIKE ? OR lower(description) LIKE ?)"
        args += [like, like]
    if viewer and not viewer[1]:
        sql += " AND (owner=? OR registry_status='APPROVED')"
        args.append(viewer[0])
    with conn() as c:
        rows = c.execute(sql + " ORDER BY updated_at DESC", args).fetchall()
    return [_row(r) for r in rows]


def list_owned(email: str) -> list[dict]:
    """한 사용자가 소유한 모든 종류의 항목(최근 변경순)."""
    with conn() as c:
        rows = c.execute("SELECT * FROM items WHERE owner=? ORDER BY updated_at DESC", (email,)).fetchall()
    return [_row(r) for r in rows]


def get_item(item_id: int) -> Optional[dict]:
    with conn() as c:
        r = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    return _row(r) if r else None


def insert_item(kind: str, name: str, description: str, owner: str, data: dict,
                source: str = "portal", ext_id: Optional[str] = None) -> int:
    now = time.time()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO items(kind,name,description,owner,source,ext_id,data,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (kind, name, description, owner, source, ext_id, json.dumps(data), now, now))
        return int(cur.lastrowid)


def update_item(item_id: int, **fields: Any) -> None:
    allowed = {"name", "description", "data", "desc_edited", "registry_id",
               "registry_record_id", "registry_status", "ext_id", "owner"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(k)
        sets.append(f"{k}=?")
        vals.append(json.dumps(v) if k == "data" else v)
    sets.append("updated_at=?")
    vals.append(time.time())
    with conn() as c:
        c.execute(f"UPDATE items SET {','.join(sets)} WHERE id=?", (*vals, item_id))


def delete_item(item_id: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM items WHERE id=?", (item_id,))


def upsert_synced(kind: str, ext_id: str, name: str, description: str, data: dict, owner: str = "aws") -> bool:
    """AWS 에서 가져온 항목을 반영한다. 사용자가 고친 설명은 보존. 새로 추가됐으면 True."""
    now = time.time()
    with conn() as c:
        r = c.execute("SELECT id, desc_edited, data FROM items WHERE kind=? AND ext_id=?", (kind, ext_id)).fetchone()
        if r is None:
            c.execute(
                "INSERT INTO items(kind,name,description,owner,source,ext_id,data,created_at,updated_at) "
                "VALUES(?,?,?,?,'aws',?,?,?,?)", (kind, name, description, owner, ext_id, json.dumps(data), now, now))
            return True
        merged = {**json.loads(r["data"] or "{}"), **data}  # 사용자가 추가한 키(예: tags)는 유지
        if r["desc_edited"]:
            c.execute("UPDATE items SET name=?, data=?, updated_at=? WHERE id=?",
                      (name, json.dumps(merged), now, r["id"]))
        else:
            c.execute("UPDATE items SET name=?, description=?, data=?, updated_at=? WHERE id=?",
                      (name, description, json.dumps(merged), now, r["id"]))
        return False


def prune_synced(kind: str, keep_ext_ids: set[str]) -> int:
    """AWS 에서 사라진 동기화 항목을 지운다(포털에서 만든 항목 중 ext_id 가 있는 것은 제외)."""
    with conn() as c:
        rows = c.execute("SELECT id, ext_id FROM items WHERE kind=? AND source='aws' AND ext_id IS NOT NULL",
                         (kind,)).fetchall()
        gone = [r["id"] for r in rows if r["ext_id"] not in keep_ext_ids]
        for i in gone:
            c.execute("DELETE FROM items WHERE id=?", (i,))
        return len(gone)


def find_by_record(record_id: str) -> Optional[dict]:
    with conn() as c:
        r = c.execute("SELECT * FROM items WHERE registry_record_id=?", (record_id,)).fetchone()
    return _row(r) if r else None


def get_setting(key: str, default: str = "") -> str:
    with conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key: str, value: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))


def record_login(email: str, name: str) -> bool:
    """로그인 한 번을 기록한다. 이 사용자의 첫 로그인이면 True."""
    with conn() as c:
        first = c.execute("SELECT 1 FROM login_events WHERE email=? LIMIT 1", (email.lower(),)).fetchone() is None
        c.execute("INSERT INTO login_events(email,name,logged_at) VALUES(?,?,?)", (email.lower(), name[:200], time.time()))
    return first


def user_stats() -> list[dict]:
    """사용자별 로그인 요약 + 그 사용자가 만든 항목의 종류별·상태별 개수. 로그인한 적 없는 소유자(registry/aws)도 포함한다."""
    with conn() as c:
        logins = {r["email"]: dict(r) for r in c.execute(
            "SELECT email, COUNT(*) AS login_count, MIN(logged_at) AS first_login, MAX(logged_at) AS last_login, "
            "(SELECT name FROM login_events l2 WHERE l2.email=l.email ORDER BY logged_at DESC LIMIT 1) AS name "
            "FROM login_events l GROUP BY email")}
        items = c.execute("SELECT owner, kind, registry_status AS st, COUNT(*) AS n FROM items GROUP BY owner, kind, registry_status").fetchall()
    users: dict[str, dict] = {e: {**v, "kinds": {}, "statuses": {}, "total": 0} for e, v in logins.items()}
    for r in items:
        u = users.setdefault(r["owner"].lower(), {"email": r["owner"].lower(), "login_count": 0, "first_login": None,
                                                  "last_login": None, "name": "", "kinds": {}, "statuses": {}, "total": 0})
        u["kinds"].setdefault(r["kind"], {})[r["st"] or "NOT_REGISTERED"] = u["kinds"].get(r["kind"], {}).get(r["st"] or "NOT_REGISTERED", 0) + r["n"]
        u["statuses"][r["st"] or "NOT_REGISTERED"] = u["statuses"].get(r["st"] or "NOT_REGISTERED", 0) + r["n"]
        u["total"] += r["n"]
    return sorted(users.values(), key=lambda u: (u["last_login"] or 0, u["total"]), reverse=True)


def login_totals() -> dict:
    with conn() as c:
        return dict(c.execute(
            "SELECT COUNT(*) AS total_logins, COUNT(DISTINCT email) AS unique_users, "
            "COUNT(DISTINCT CASE WHEN logged_at >= ? THEN email END) AS active_7d FROM login_events",
            (time.time() - 7 * 86400,)).fetchone())


def recent_logins(limit: int = 30) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT email, name, logged_at FROM login_events ORDER BY logged_at DESC LIMIT ?", (min(max(limit, 1), 200),))]


# ── 사용 신청 (MCP 를 쓰겠다는 신청. Registry 의 '등록 승인' 과는 별개로 포털이 관리한다) ──
def _ur(r: sqlite3.Row) -> dict:
    return {"item_id": int(r["item_id"]), "email": r["email"], "status": r["status"], "requested_at": r["requested_at"],
            "decided_at": r["decided_at"], "decided_by": r["decided_by"] or "", "reason": r["reason"] or ""}


def get_usage_request(item_id: int, email: str) -> Optional[dict]:
    with conn() as c:
        r = c.execute("SELECT * FROM usage_requests WHERE item_id=? AND email=?", (item_id, email.lower())).fetchone()
    return _ur(r) if r else None


def put_usage_request(item_id: int, email: str, status: str, decided_by: str = "", reason: str = "") -> dict:
    """신청을 만들거나(PENDING) 처리 결과를 기록한다. 같은 (항목, 사용자)는 한 건만 있고 다시 신청하면 덮어쓴다."""
    now, email = time.time(), email.lower()
    with conn() as c:
        if status == "PENDING":
            c.execute("INSERT INTO usage_requests(item_id,email,status,requested_at,decided_at,decided_by,reason) VALUES(?,?,?,?,NULL,'','') "
                      "ON CONFLICT(item_id,email) DO UPDATE SET status='PENDING', requested_at=excluded.requested_at, decided_at=NULL, decided_by='', reason=''",
                      (item_id, email, status, now))
        else:
            c.execute("UPDATE usage_requests SET status=?, decided_at=?, decided_by=?, reason=? WHERE item_id=? AND email=?",
                      (status, now, decided_by, reason[:255], item_id, email))
    return get_usage_request(item_id, email) or {}


def list_usage_requests(status: Optional[str] = None, item_id: Optional[int] = None) -> list[dict]:
    sql, args = "SELECT * FROM usage_requests WHERE 1=1", []
    if status: sql += " AND status=?"; args.append(status)
    if item_id is not None: sql += " AND item_id=?"; args.append(item_id)
    with conn() as c:
        rows = c.execute(sql + " ORDER BY requested_at DESC", args).fetchall()
    return [_ur(r) for r in rows]


def delete_usage_request(item_id: int, email: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM usage_requests WHERE item_id=? AND email=?", (item_id, email))


def delete_usage_requests(item_id: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM usage_requests WHERE item_id=?", (item_id,))
