"""저장소 선택 계층. 호출부는 항상 `db.xxx` 를 쓰고, 실제 구현은 환경 변수로 고른다.

  기본(PORTAL_STORE 가 비어 있거나 dynamodb) → db_dynamo (배포·로컬 모두. 재배포해도 데이터가 유지된다)
  PORTAL_STORE=sqlite                        → db_sqlite  (테스트 전용: 파일 하나, AWS 불필요)

기본값이 DynamoDB 인 이유: 환경 변수를 빠뜨렸을 때 조용히 컨테이너 SQLite 로 떨어지면 다음 재배포에서 데이터가 사라진다.

두 구현은 같은 함수와 정수 id 를 제공한다(테스트가 같은 시나리오를 양쪽에 돌려 확인한다).
"""
from __future__ import annotations

import importlib
import os
from typing import Any

KINDS = ("prompt", "mcp", "agent", "skill")
_FUNCS = ("is_visible", "list_items", "list_owned", "get_item", "insert_item", "update_item", "delete_item", "upsert_synced",
          "prune_synced", "find_by_record", "get_setting", "set_setting", "record_login", "user_stats", "login_totals", "recent_logins",
          "get_usage_request", "put_usage_request", "list_usage_requests", "delete_usage_requests", "delete_usage_request")


def backend():
    """호출 시점의 환경 변수로 구현을 고른다(테스트에서 바꿀 수 있도록 매번 확인). 알 수 없는 값은 오류로 알린다."""
    store = (os.environ.get("PORTAL_STORE") or "dynamodb").strip().lower()
    if store not in ("dynamodb", "sqlite"):
        raise RuntimeError(f"PORTAL_STORE={store!r} 는 지원하지 않습니다 (dynamodb | sqlite)")
    return importlib.import_module("application.db_dynamo" if store == "dynamodb" else "application.db_sqlite")


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
