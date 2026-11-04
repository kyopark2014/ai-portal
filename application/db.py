"""SQLite 저장소. 카탈로그 항목(prompt/mcp/agent/skill)을 kind 컬럼으로 구분해 한 테이블에 둔다.

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
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def db_path() -> Path:
    p = os.environ.get("PORTAL_DB_PATH")
    return Path(p) if p else Path(__file__).resolve().parent / "data" / "portal.db"


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
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


def get_setting(key: str, default: str = "") -> str:
    with conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key: str, value: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))
