"""관리자 «로그 확인»: MCP 요청 로그를 Athena 로 조회하고 내려받는다.

저장: interceptor → Firehose → s3://<bucket>/logs/kind=mcp/target=<gateway-id>/dt=YYYY-MM-DD/*.json.gz (installer.ensure_log_pipeline).
조회: Athena 는 느리므로(수 초~수십 초) 비동기다 — 조회 시작(POST) → 상태 확인(GET) → 결과(GET). 다운로드는 CSV·JSON.
한 호출은 REQUEST·RESPONSE 두 행으로 기록되어 있어, 여기서 (rpc_id, req_hash) 로 묶는다(가장 가까운 뒤쪽 응답 하나).
한계: RESPONSE 이벤트에는 호출자 정보가 없을 수 있어 호출자까지 키로 쓰지 못한다. 같은 gateway 에서 같은 id·method·tool 의 호출이
거의 동시에 겹치면 응답이 서로 바뀔 수 있다(요청자·요청 내용은 REQUEST 행에서 오므로 영향이 없고, 결과·지연 시간만 해당).
Agent 는 아직 수집하지 않는다(kind=agent 는 비어 있음).
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import auth, aws, catalog, db

log = logging.getLogger("logs")
router = APIRouter(prefix="/api/admin/logs")

MAX_ROWS = 10000                      # 한 조회가 돌려주는 최대 행(다운로드 포함)
SHOW_ROWS = 500                       # 화면에 표시하는 행
_SAFE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_QID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
COLUMNS = ["time", "user_email", "caller", "method", "mcp_target", "tool", "arguments", "blocked", "block_reason",
           "status", "is_error", "error_message", "resp_bytes", "latency_ms"]


def require_admin(user: auth.User = Depends(auth.current_user)) -> auth.User:
    if user.role != "admin":
        raise HTTPException(403, "admin only")
    return user


def _cfg(env: str, key: str, default: str = "") -> str:
    import os
    return os.environ.get(env, "").strip() or catalog.config_value(key) or default


def database() -> str:
    return _cfg("LOG_GLUE_DB", "log_glue_database", "ai_portal")


def table() -> str:
    return _cfg("LOG_GLUE_TABLE", "log_glue_table", "request_logs")


def workgroup() -> str:
    return _cfg("LOG_ATHENA_WORKGROUP", "log_athena_workgroup", "ai-portal-logs")


def retention_days() -> int:
    try:
        return int(_cfg("LOG_RETENTION_DAYS", "log_retention_days", "90"))
    except ValueError:
        return 90


def athena():
    return aws.client("athena")


def _err(e: Exception) -> HTTPException:
    if isinstance(e, ClientError):
        c = e.response.get("Error", {})
        return HTTPException(502, f"Athena 오류: {c.get('Code', '')} {str(c.get('Message', ''))[:300]}")
    return HTTPException(502, f"Athena 오류: {type(e).__name__}")


class QueryIn(BaseModel):
    kind: str = Field(pattern="^(mcp|agent)$")
    target: str                                   # MCP: gateway id
    start: date
    end: date
    user: str = Field("", max_length=200)         # 요청자(이메일 일부) — 비우면 전체
    tool: str = Field("", max_length=200)         # tool 이름(일부) — 비우면 전체
    all_methods: bool = False                     # False 면 tools/call 만, True 면 tools/list·initialize 등도
    only_problems: bool = False                   # 차단됐거나 오류인 호출만


def build_sql(b: QueryIn) -> tuple[str, list[str]]:
    """Athena SQL 과 ExecutionParameters. target·날짜는 검증한 값만 리터럴로 넣고(파티션 프로젝션이 리터럴을 요구한다),
    사용자가 입력한 문자열은 모두 파라미터(?)로 넘긴다."""
    if not _SAFE.match(b.target):
        raise HTTPException(422, "target 형식이 올바르지 않습니다")
    today = datetime.now(timezone.utc).date()
    if b.end < b.start:
        raise HTTPException(422, "종료일이 시작일보다 빠릅니다")
    if (b.end - b.start).days > retention_days():
        raise HTTPException(422, f"조회 기간은 최대 {retention_days()}일입니다")
    if b.start < today - timedelta(days=retention_days()) or b.end > today + timedelta(days=1):
        raise HTTPException(422, f"보존 기간({retention_days()}일) 안의 날짜만 조회할 수 있습니다")
    tbl = f'"{database()}"."{table()}"'
    where = f"kind = '{b.kind}' AND target = '{b.target}' AND dt BETWEEN '{b.start.isoformat()}' AND '{b.end.isoformat()}'"
    params: list[str] = []
    cond = []
    if not b.all_methods:
        cond.append("q.method = 'tools/call'")
    if b.user.strip():
        cond.append("(lower(q.user_email) LIKE ? OR lower(q.caller) LIKE ?)")
        params += [f"%{b.user.strip().lower()}%"] * 2
    if b.tool.strip():
        cond.append("lower(q.tool) LIKE ?")
        params.append(f"%{b.tool.strip().lower()}%")
    outer = ["rn = 1"]
    if b.only_problems:
        outer.append("(blocked OR is_error)")
    sql = f"""
WITH req AS (
  SELECT ts, rpc_id, req_hash, method, mcp_target, tool, caller, user_email, arguments, blocked, block_reason
  FROM {tbl} WHERE {where} AND point = 'REQUEST'
), resp AS (
  SELECT ts, rpc_id, req_hash, status, is_error, error_message, resp_bytes
  FROM {tbl} WHERE {where} AND point = 'RESPONSE'
), j AS (
  SELECT q.ts AS "time", q.user_email, q.caller, q.method, q.mcp_target, q.tool, q.arguments,
         coalesce(q.blocked, false) AS blocked, q.block_reason,
         r.status, coalesce(r.is_error, false) AS is_error, r.error_message, r.resp_bytes,
         date_diff('millisecond', from_iso8601_timestamp(q.ts), from_iso8601_timestamp(r.ts)) AS latency_ms,
         row_number() OVER (PARTITION BY q.ts, q.rpc_id, q.req_hash, q.caller ORDER BY r.ts) AS rn
  FROM req q LEFT JOIN resp r
    ON q.rpc_id = r.rpc_id AND (r.req_hash = '' OR q.req_hash = r.req_hash) AND r.ts >= q.ts
   AND NOT coalesce(q.blocked, false)          -- 차단된 요청은 target 에 가지 않아 응답이 없다(남의 응답과 섞이지 않게)
  {"WHERE " + " AND ".join(cond) if cond else ""}
)
SELECT "time", user_email, caller, method, mcp_target, tool, arguments, blocked, block_reason,
       status, is_error, error_message, resp_bytes, latency_ms
FROM j WHERE {" AND ".join(outer)}
ORDER BY "time" DESC LIMIT {MAX_ROWS}"""
    return sql.strip(), params


def _known_target(kind: str, target: str) -> None:
    """포털에 등록된 MCP(gateway) 만 조회할 수 있다."""
    if kind == "agent":
        raise HTTPException(501, "Agent 로그는 아직 수집하지 않습니다(추후 적용)")
    if target not in {t["id"] for t in list_targets("mcp")}:
        raise HTTPException(404, "등록되지 않은 MCP 입니다")


def list_targets(kind: str) -> list[dict]:
    """로그가 쌓이는 대상. MCP 는 gateway 로 연결된 항목(항목 번호·이름·gateway id). Gateway 를 거치지 않는 MCP 는 기록되지 않는다."""
    if kind != "mcp":
        return []
    out = []
    for it in db.list_items("mcp"):
        d = it.get("data") or {}
        gid = d.get("gateway_id") if d.get("kind") == "gateway" else ""
        if gid and _SAFE.match(gid):
            out.append({"id": gid, "name": it["name"], "item_id": it["id"]})
    return sorted(out, key=lambda t: t["name"].lower())


@router.get("/targets")
def targets(kind: str = Query("mcp", pattern="^(mcp|agent)$"), user: auth.User = Depends(require_admin)):
    return {"kind": kind, "targets": list_targets(kind), "retention_days": retention_days(),
            "note": "Agent 로그는 아직 수집하지 않습니다(추후 적용)." if kind == "agent" else ""}


@router.post("/query")
def start_query(body: QueryIn, user: auth.User = Depends(require_admin)):
    _known_target(body.kind, body.target)
    sql, params = build_sql(body)
    try:
        kw: dict[str, Any] = {"QueryString": sql, "WorkGroup": workgroup(), "QueryExecutionContext": {"Database": database()}}
        if params:
            kw["ExecutionParameters"] = params
        qid = athena().start_query_execution(**kw)["QueryExecutionId"]
    except (ClientError, BotoCoreError) as e:
        raise _err(e)
    log.info("log query by %s: kind=%s target=%s %s~%s user=%r tool=%r -> %s", user.email, body.kind, body.target,
             body.start, body.end, body.user, body.tool, qid)     # 누가 로그를 열람했는지 남긴다(감사)
    return {"query_id": qid}


def _execution(qid: str) -> dict:
    if not _QID.match(qid):
        raise HTTPException(422, "query id 형식이 올바르지 않습니다")
    try:
        ex = athena().get_query_execution(QueryExecutionId=qid)["QueryExecution"]
    except (ClientError, BotoCoreError) as e:
        raise _err(e)
    if ex.get("WorkGroup") != workgroup():                     # 이 워크그룹의 쿼리만 다룬다
        raise HTTPException(404, "조회를 찾을 수 없습니다")
    return ex


@router.get("/query/{qid}")
def query_status(qid: str, user: auth.User = Depends(require_admin)):
    ex = _execution(qid)
    st = ex.get("Status", {})
    return {"state": st.get("State", ""), "reason": st.get("StateChangeReason", ""),
            "scanned_bytes": (ex.get("Statistics") or {}).get("DataScannedInBytes"),
            "millis": (ex.get("Statistics") or {}).get("TotalExecutionTimeInMillis")}


def _cell(col: str, v: str | None) -> Any:
    if v is None:
        return None
    if col in ("blocked", "is_error"):
        return v == "true"
    if col in ("status", "resp_bytes", "latency_ms"):
        try:
            return int(float(v))
        except ValueError:
            return v
    return v


def fetch_rows(qid: str, limit: int) -> list[dict]:
    ex = _execution(qid)
    state = ex["Status"]["State"]
    if state != "SUCCEEDED":
        raise HTTPException(409, f"조회가 끝나지 않았습니다({state})")
    rows: list[dict] = []
    token = None
    first = True
    try:
        while len(rows) < limit:
            kw: dict[str, Any] = {"QueryExecutionId": qid, "MaxResults": min(1000, limit - len(rows) + (1 if first else 0))}
            if token:
                kw["NextToken"] = token
            r = athena().get_query_results(**kw)
            data = r["ResultSet"]["Rows"]
            if first:                                           # 첫 페이지의 첫 행은 헤더
                data, first = data[1:], False
            for row in data:
                vals = [c.get("VarCharValue") for c in row["Data"]]
                rows.append({c: _cell(c, v) for c, v in zip(COLUMNS, vals)})
            token = r.get("NextToken")
            if not token:
                break
    except (ClientError, BotoCoreError) as e:
        raise _err(e)
    return rows[:limit]


@router.get("/query/{qid}/results")
def query_results(qid: str, user: auth.User = Depends(require_admin)):
    rows = fetch_rows(qid, SHOW_ROWS + 1)
    ex = _execution(qid)
    return {"columns": COLUMNS, "rows": rows[:SHOW_ROWS], "truncated": len(rows) > SHOW_ROWS,
            "scanned_bytes": (ex.get("Statistics") or {}).get("DataScannedInBytes")}


def _csv_safe(v: Any) -> Any:
    """스프레드시트가 수식으로 실행하지 않도록(CSV injection) 위험한 시작 문자 앞에 '를 붙인다. 요청 내용은 사용자가 정한 값이다."""
    return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


@router.get("/query/{qid}/download")
def query_download(qid: str, format: str = Query("csv", pattern="^(csv|json)$"), user: auth.User = Depends(require_admin)):
    rows = fetch_rows(qid, MAX_ROWS)
    log.info("log download by %s: %s rows=%d format=%s", user.email, qid, len(rows), format)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if format == "json":
        return Response(json.dumps(rows, ensure_ascii=False, indent=1), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="mcp-logs-{stamp}.json"', "Cache-Control": "no-store"})
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLUMNS)
    for r in rows:
        w.writerow([_csv_safe(r.get(c)) for c in COLUMNS])
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",      # BOM: Excel 에서 한글이 깨지지 않게
                    headers={"Content-Disposition": f'attachment; filename="mcp-logs-{stamp}.csv"', "Cache-Control": "no-store"})
