"""저장소 선택 계층. 호출부는 항상 `db.xxx` 를 쓰고, 실제 구현은 환경 변수로 고른다.

  PORTAL_STORE=dynamodb  → db_dynamo (배포 환경: 재배포해도 데이터 유지)
  그 외(기본)            → db_sqlite  (로컬 개발·테스트: 파일 하나, AWS 불필요)

두 구현은 같은 함수와 정수 id 를 제공한다(테스트가 같은 시나리오를 양쪽에 돌려 확인한다).
"""
from __future__ import annotations

import importlib
import os
from typing import Any

KINDS = ("prompt", "mcp", "agent", "skill")
_FUNCS = ("is_visible", "list_items", "list_owned", "get_item", "insert_item", "update_item", "delete_item", "upsert_synced",
          "prune_synced", "find_by_record", "get_setting", "set_setting", "record_login", "user_stats", "login_totals", "recent_logins")


def backend():
    """호출 시점의 환경 변수로 구현을 고른다(테스트에서 바꿀 수 있도록 매번 확인)."""
    return importlib.import_module("application.db_dynamo" if os.environ.get("PORTAL_STORE") == "dynamodb" else "application.db_sqlite")


def _make(name: str):
    def call(*a: Any, **k: Any) -> Any:
        return getattr(backend(), name)(*a, **k)
    call.__name__ = name
    return call


for _n in _FUNCS:
    globals()[_n] = _make(_n)


def conn():
    """SQLite 전용(테스트·로컬 진단용). DynamoDB 구현에는 없다."""
    from application import db_sqlite
    return db_sqlite.conn()


def db_path():
    from application import db_sqlite
    return db_sqlite.db_path()
