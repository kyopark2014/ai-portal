import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from application import auth, catalog, db, logs, server

TODAY = date.today()
GW = "gw-ai-portal-iam-abc123"
QID = "11111111-2222-3333-4444-555555555555"


class FakeAthena:
    def __init__(self): self.started, self.state = [], "SUCCEEDED"
    def start_query_execution(self, **kw): self.started.append(kw); return {"QueryExecutionId": QID}
    def get_query_execution(self, QueryExecutionId):
        return {"QueryExecution": {"WorkGroup": logs.workgroup(), "Status": {"State": self.state, "StateChangeReason": "r"},
                                   "Statistics": {"DataScannedInBytes": 2048, "TotalExecutionTimeInMillis": 900}}}
    def get_query_results(self, QueryExecutionId, MaxResults, NextToken=None):
        hdr = {"Data": [{"VarCharValue": c} for c in logs.COLUMNS]}
        def row(user, tool, arg, blocked="false"):
            v = ["2026-11-13T01:02:03.004Z", user, "arn:x", "tools/call", "tgt", tool, arg, blocked, None, "200", "false", None, "12", "35"]
            return {"Data": [{"VarCharValue": x} if x is not None else {} for x in v]}
        return {"ResultSet": {"Rows": [hdr, row("a@x.com", "search", '{"q":"=cmd"}'), row("b@x.com", "t2", "{}", "true")]}}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_EMAILS", "admin@x.com")
    monkeypatch.setenv("SESSION_SIGNING_KEY", "k"); monkeypatch.setenv("PORTAL_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(auth, "_key", None); monkeypatch.setattr(catalog, "start_sync", lambda force=False: False)
    fa = FakeAthena(); monkeypatch.setattr(logs, "athena", lambda: fa)
    db.insert_item("mcp", "gw-x", "", "o@x.com", {"kind": "gateway", "gateway_id": GW, "endpoint": "https://e/mcp"})
    return fa


def client(email="admin@x.com", admin=True):
    c = TestClient(server.app); c.cookies.set(auth.COOKIE_NAME, auth.make_session(email, "N", "")); return c


def body(**kw):
    return {"kind": "mcp", "target": GW, "start": str(TODAY - timedelta(days=3)), "end": str(TODAY), **kw}


def test_admin_only(env):
    c = client("user@x.com")
    assert c.get("/api/admin/logs/targets").status_code == 403
    assert c.post("/api/admin/logs/query", json=body()).status_code == 403
    assert c.get(f"/api/admin/logs/query/{QID}/download").status_code == 403


def test_targets_lists_gateway_mcps(env):
    r = client().get("/api/admin/logs/targets?kind=mcp").json()
    assert r["targets"] == [{"id": GW, "name": "gw-x", "item_id": 1}] and r["retention_days"] == 90
    assert client().get("/api/admin/logs/targets?kind=agent").json()["targets"] == []


def test_query_uses_partition_literals_and_parameters_for_user_input(env):
    r = client().post("/api/admin/logs/query", json=body(user="Bob'; DROP", tool="x"))
    assert r.status_code == 200 and r.json() == {"query_id": QID}
    kw = env.started[0]
    assert kw["WorkGroup"] == logs.workgroup() and "DROP" not in kw["QueryString"]       # 사용자 입력은 SQL 에 들어가지 않는다
    assert kw["ExecutionParameters"] == ["%bob'; drop%", "%bob'; drop%", "%x%"]
    assert f"target = '{GW}'" in kw["QueryString"] and "kind = 'mcp'" in kw["QueryString"] and "dt BETWEEN" in kw["QueryString"]


@pytest.mark.parametrize("patch,code", [
    ({"target": "x'; --"}, 404), ({"target": "gw-unknown"}, 404), ({"kind": "agent"}, 501),
    ({"end": str(TODAY - timedelta(days=10))}, 422),                                      # 종료 < 시작
    ({"start": str(TODAY - timedelta(days=200))}, 422),                                   # 보존 기간 밖
])
def test_query_validation(env, patch, code):
    assert client().post("/api/admin/logs/query", json=body(**patch)).status_code == code
    assert env.started == []


def test_status_and_results(env):
    c = client()
    assert c.get(f"/api/admin/logs/query/{QID}").json()["state"] == "SUCCEEDED"
    r = c.get(f"/api/admin/logs/query/{QID}/results").json()
    assert [x["user_email"] for x in r["rows"]] == ["a@x.com", "b@x.com"] and r["rows"][1]["blocked"] is True
    assert r["rows"][0]["status"] == 200 and r["rows"][0]["latency_ms"] == 35 and r["rows"][0]["block_reason"] is None
    assert c.get("/api/admin/logs/query/not-a-uuid").status_code == 422


def test_results_before_finish_is_conflict(env):
    env.state = "RUNNING"
    assert client().get(f"/api/admin/logs/query/{QID}/results").status_code == 409


def test_download_csv_neutralizes_formulas_and_json_roundtrips(env):
    c = client()
    t = c.get(f"/api/admin/logs/query/{QID}/download?format=csv")
    assert t.status_code == 200 and "attachment" in t.headers["content-disposition"]
    assert "'=cmd" in t.text or "\"{\"\"q\"\":\"\"=cmd\"\"}\"" in t.text and t.text.startswith("﻿")
    j = c.get(f"/api/admin/logs/query/{QID}/download?format=json").json()
    assert len(j) == 2 and j[0]["tool"] == "search"


def test_csv_safe():
    assert logs._csv_safe("=1+1") == "'=1+1" and logs._csv_safe("@a") == "'@a" and logs._csv_safe("ok") == "ok" and logs._csv_safe(5) == 5
