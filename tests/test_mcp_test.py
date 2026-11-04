import pytest
from fastapi.testclient import TestClient

from application import auth, aws, catalog, db, mcp_test, server


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_SIGNING_KEY", "k")
    monkeypatch.setenv("PORTAL_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("HARNESS_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h")
    monkeypatch.setattr(auth, "_key", None)
    monkeypatch.setattr(catalog, "start_sync", lambda force=False: False)
    monkeypatch.setattr(aws, "account_id", lambda: "123456789012")


def client(email="a@x.com"):
    c = TestClient(server.app)
    c.cookies.set(auth.COOKIE_NAME, auth.make_session(email, "N", ""))
    return c


def gw(authorizer="AWS_IAM", region="us-east-1"):
    return {"id": 1, "name": "gateway-websearch", "data": {"kind": "gateway", "authorizer": authorizer,
            "gateway_id": "gw-abc", "region": region, "endpoint": "https://x/mcp"}}


def test_gateway_becomes_agentcore_gateway_tool_with_region_arn(env):
    t = mcp_test.tool_for(gw())
    assert t["type"] == "agentcore_gateway" and t["name"] == "gateway-websearch"
    assert t["config"]["agentCoreGateway"]["gatewayArn"] == "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/gw-abc"


def test_jwt_gateway_is_rejected(env):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        mcp_test.tool_for(gw("CUSTOM_JWT"))
    assert e.value.status_code == 422


def test_direct_mcp_becomes_remote_mcp_with_headers(env):
    t = mcp_test.tool_for({"name": "my mcp!", "data": {"endpoint": "https://e/mcp", "headers": [{"key": "Authorization", "value": "Bearer S"}]}})
    assert t["type"] == "remote_mcp" and t["name"] == "my_mcp_"
    assert t["config"]["remoteMcp"] == {"url": "https://e/mcp", "headers": {"Authorization": "Bearer S"}}


def test_actor_id_matches_memory_pattern_and_hides_email():
    import re
    a = mcp_test.actor_id("E2NPark@Gmail.com")
    assert re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9\-_/]*", a) and "@" not in a and "gmail" not in a
    assert a == mcp_test.actor_id("e2npark@gmail.com")          # 대소문자 무관, 안정적


def test_requires_login_and_validates_input(env):
    assert TestClient(server.app).post("/api/mcp/1/chat", json={}).status_code == 401
    c = client()
    iid = db.insert_item("mcp", "m", "", "a@x.com", {"endpoint": "https://e/mcp", "headers": []})
    ok = {"session_id": "a" * 36}
    assert c.post(f"/api/mcp/{iid}/chat", json={**ok, "messages": [{"role": "assistant", "content": "x"}]}).status_code == 422
    assert c.post(f"/api/mcp/{iid}/chat", json={"session_id": "short", "messages": [{"role": "user", "content": "x"}]}).status_code == 422
    assert c.post("/api/mcp/999/chat", json={**ok, "messages": [{"role": "user", "content": "x"}]}).status_code == 404


def test_missing_harness_gives_actionable_503(env, monkeypatch):
    monkeypatch.delenv("HARNESS_ARN"); monkeypatch.setattr(catalog, "config_value", lambda k: "")
    iid = db.insert_item("mcp", "m", "", "a@x.com", {"endpoint": "https://e/mcp", "headers": []})
    r = client().post(f"/api/mcp/{iid}/chat", json={"session_id": "a" * 36, "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 503 and "--harness-only" in r.json()["detail"]
    assert client().get("/api/mcp/test-status").json()["ready"] is False


def test_stream_relays_text_and_tool_events_and_scopes_session(env, monkeypatch):
    seen = {}
    class RT:
        def invoke_harness(self, **kw):
            seen.update(kw)
            return {"stream": iter([
                {"contentBlockStart": {"contentBlockIndex": 0, "start": {"toolUse": {"name": "ws___Search"}}}},
                {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {"input": '{"q":'}}}},
                {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {"input": '"x"}'}}}},
                {"contentBlockStop": {"contentBlockIndex": 0}},
                {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"toolResult": [{"text": "RES"}]}}},
                {"contentBlockDelta": {"contentBlockIndex": 2, "delta": {"text": "done"}}},
                {"messageStop": {"stopReason": "end_turn"}},
                {"metadata": {"usage": {"inputTokens": 5, "outputTokens": 7}}},
            ])}
    monkeypatch.setattr(aws.boto3, "client", lambda *a, **k: RT())
    iid = db.insert_item("mcp", "m", "", "a@x.com", {"endpoint": "https://e/mcp", "headers": []})
    db.update_item(iid, registry_status="APPROVED")   # 다른 사용자가 보려면 승인된 항목이어야 한다
    r = client("a@x.com").post(f"/api/mcp/{iid}/chat", json={"session_id": "s" * 36, "messages": [{"role": "user", "content": "go"}]})
    import json
    evs = [json.loads(l[5:]) for l in r.text.split("\n\n") if l.startswith("data:")]
    assert {"tool_start": "ws___Search", "i": 0} in evs
    assert any(e.get("tool_call") == "ws___Search" and e["input"] == '{"q":"x"}' for e in evs)
    assert any(e.get("tool_result") == "RES" for e in evs) and any(e.get("t") == "done" for e in evs)
    assert evs[-1]["done"] and evs[-1]["usage"] == {"input_tokens": 5, "output_tokens": 7}
    # 다른 사용자는 같은 session_id 를 써도 다른 harness 세션이 된다
    first = seen["runtimeSessionId"]
    db.put_usage_request(iid, "b@x.com", "PENDING"); db.put_usage_request(iid, "b@x.com", "APPROVED", decided_by="admin")   # 다른 사용자는 사용 승인을 받은 뒤에야 호출할 수 있다
    client("b@x.com").post(f"/api/mcp/{iid}/chat", json={"session_id": "s" * 36, "messages": [{"role": "user", "content": "go"}]})
    assert seen["runtimeSessionId"] != first and "@" not in seen["actorId"]


def test_aws_error_is_reported_as_event(env, monkeypatch):
    class RT:
        def invoke_harness(self, **kw):
            raise RuntimeError("boom")
    monkeypatch.setattr(aws.boto3, "client", lambda *a, **k: RT())
    iid = db.insert_item("mcp", "m", "", "a@x.com", {"endpoint": "https://e/mcp", "headers": []})
    r = client().post(f"/api/mcp/{iid}/chat", json={"session_id": "s" * 36, "messages": [{"role": "user", "content": "go"}]})
    assert r.status_code == 200 and '"error": "boom"' in r.text


def test_harness_stream_keeps_text_and_tools_in_arrival_order_with_reused_block_indexes():
    """실제 harness 는 메시지마다 contentBlockIndex 를 0,1,… 로 다시 쓴다. 그래도 도구 입력이 섞이지 않고
    텍스트·도구 호출·도구 결과가 도착한 순서 그대로 나가야 한다(프런트가 이 순서대로 표시한다)."""
    from application import mcp_test as m
    def tool(idx, inp): return [
        {"contentBlockStart": {"contentBlockIndex": idx, "start": {"toolUse": {"name": "ws"}}}},
        {"contentBlockDelta": {"contentBlockIndex": idx, "delta": {"toolUse": {"input": inp}}}},
        {"contentBlockStop": {"contentBlockIndex": idx}}]
    def result(t): return [
        {"contentBlockStart": {"contentBlockIndex": 0, "start": {"toolResult": {}}}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolResult": [{"text": t}]}}},
        {"contentBlockStop": {"contentBlockIndex": 0}}]
    def text(idx, t): return [{"contentBlockDelta": {"contentBlockIndex": idx, "delta": {"text": t}}}]
    stream = [*text(1, "A먼저"), *tool(2, '{"q":"1"}'), *result("R1"), *text(1, "B중간"), *tool(1, '{"q":"2"}'), *result("R2"), *text(0, "C끝"),
              {"metadata": {"usage": {}}}]
    kinds = []
    for e in m.relay_harness_stream(iter(stream)):
        kinds.append(("t:" + e["t"]) if "t" in e else ("call:" + e["input"]) if "tool_call" in e
                     else ("res:" + e["tool_result"]) if "tool_result" in e else None)
    assert [k for k in kinds if k] == ["t:A먼저", 'call:{"q":"1"}', "res:R1", "t:B중간", 'call:{"q":"2"}', "res:R2", "t:C끝"]
