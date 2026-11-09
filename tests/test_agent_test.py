import json

import pytest
from fastapi.testclient import TestClient

from application import agent_test, auth, aws, catalog, db, server

RT = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/agentic_work-AbC123"
HN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:harness/ai_portal-XyZ789"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_SIGNING_KEY", "k")
    monkeypatch.setenv("PORTAL_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(auth, "_key", None)
    monkeypatch.setattr(catalog, "start_sync", lambda force=False: False)


def client(email="a@x.com"):
    c = TestClient(server.app)
    c.cookies.set(auth.COOKIE_NAME, auth.make_session(email, "N", ""))
    return c


def agent(data, name="ag", grant=True):
    iid = db.insert_item("agent", name, "", "a@x.com", data)
    if grant:                                           # 소유자도 사용 신청이 승인되어야 대화할 수 있다
        db.put_usage_request(iid, "a@x.com", "PENDING"); db.put_usage_request(iid, "a@x.com", "APPROVED", decided_by="t")
    return iid


BODY = {"session_id": "s" * 36, "messages": [{"role": "user", "content": "hi"}]}


def events(r):
    return [json.loads(l[5:]) for l in r.text.split("\n\n") if l.startswith("data:")]


def test_target_selection_and_refusals(env):
    from fastapi import HTTPException
    assert agent_test.target_of({"data": {"arn": RT, "agent_type": "runtime", "protocol": "HTTP"}}) == ("runtime", RT)
    assert agent_test.target_of({"data": {"arn": HN, "agent_type": "harness"}}) == ("harness", HN)
    for data in ({}, {"arn": "not-an-arn"}, {"arn": "arn:aws:s3:::bucket"},
                 {"arn": RT, "agent_type": "runtime", "protocol": "MCP"},
                 {"arn": "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/harness_ai_portal-R1"}):
        with pytest.raises(HTTPException) as e:
            agent_test.target_of({"data": data})
        assert e.value.status_code == 422


def test_requires_login_and_validates(env):
    assert TestClient(server.app).post("/api/agents/1/chat", json=BODY).status_code == 401
    c, iid = client(), agent({"arn": RT, "agent_type": "runtime"})
    assert c.post(f"/api/agents/{iid}/chat", json={**BODY, "messages": [{"role": "assistant", "content": "x"}]}).status_code == 422
    assert c.post("/api/agents/999/chat", json=BODY).status_code == 404
    mcp = db.insert_item("mcp", "m", "", "a@x.com", {})
    assert c.post(f"/api/agents/{mcp}/chat", json=BODY).status_code == 404          # agent 가 아닌 항목
    assert c.post(f"/api/agents/{agent({})}/chat", json=BODY).status_code == 422    # ARN 없음


def test_runtime_streams_text_and_tools_with_agentic_work_payload(env, monkeypatch):
    seen = {}
    class Body:
        def iter_lines(self):
            yield b'data: {"data": "he"}'
            yield b'data: {"data": "llo"}'
            yield b'data: {"toolResult": "12:00", "tool": "get_current_time", "toolUseId": "t"}'
            yield b'data: {"result": {"messages": [{"role": "assistant", "content": "hello"}]}}'
    class RTc:
        def invoke_agent_runtime(self, **kw):
            seen.update(kw)
            return {"contentType": "text/event-stream; charset=utf-8", "response": Body()}
    monkeypatch.setattr(aws.boto3, "client", lambda *a, **k: RTc())
    iid = agent({"arn": RT, "agent_type": "runtime", "protocol": "HTTP"})
    evs = events(client().post(f"/api/agents/{iid}/chat", json={**BODY, "messages": [
        {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]}))
    assert "".join(e.get("t", "") for e in evs) == "hello"              # 최종 result 와 중복되지 않는다
    assert {"tool_call": "get_current_time", "input": ""} in evs and {"tool_result": "12:00"} in evs
    p = json.loads(seen["payload"])
    assert p["prompt"].endswith("c") and "[이전 대화]" in p["prompt"] and "사용자: a" in p["prompt"]
    assert p["guardrail_enabled"] is False and p["memory_enabled"] is False
    assert "model_name" not in p                                        # agent 별 기본 모델을 쓴다
    assert "@" not in p["user_id"] and "@" not in seen["runtimeUserId"] and seen["qualifier"] == "DEFAULT"


def test_runtime_result_only_and_non_sse(env, monkeypatch):
    class Body:
        def __init__(self, lines): self.lines = lines
        def iter_lines(self): yield from self.lines
        def read(self): return b'{"result": {"messages": [{"content": "plain"}]}}'
    class RTc:
        def __init__(self, ct, body): self.ct, self.body = ct, body
        def invoke_agent_runtime(self, **kw): return {"contentType": self.ct, "response": self.body}
    iid = agent({"arn": RT, "agent_type": "runtime"})
    monkeypatch.setattr(aws.boto3, "client", lambda *a, **k: RTc("text/event-stream", Body(
        [b'data: {"result": {"messages": [{"content": "only-result"}]}}'])))
    assert "".join(e.get("t", "") for e in events(client().post(f"/api/agents/{iid}/chat", json=BODY))) == "only-result"
    monkeypatch.setattr(aws.boto3, "client", lambda *a, **k: RTc("application/json", Body([])))
    assert "".join(e.get("t", "") for e in events(client().post(f"/api/agents/{iid}/chat", json=BODY))) == "plain"


def test_harness_uses_invoke_harness_without_forced_tools(env, monkeypatch):
    seen = {}
    class RTc:
        def invoke_harness(self, **kw):
            seen.update(kw)
            return {"stream": iter([{"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "yo"}}},
                                    {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 2}}}])}
    monkeypatch.setattr(aws.boto3, "client", lambda *a, **k: RTc())
    iid = agent({"arn": HN, "agent_type": "harness"})
    evs = events(client().post(f"/api/agents/{iid}/chat", json=BODY))
    assert evs[0] == {"t": "yo"} and any(e.get("done") for e in evs)
    assert "tools" not in seen and seen["harnessArn"] == HN and "@" not in seen["actorId"]


def test_sessions_are_isolated_per_user_and_errors_are_events(env, monkeypatch):
    seen = []
    class RTc:
        def invoke_agent_runtime(self, **kw):
            seen.append(kw["runtimeSessionId"]); raise RuntimeError("boom")
    monkeypatch.setattr(aws.boto3, "client", lambda *a, **k: RTc())
    iid = agent({"arn": RT, "agent_type": "runtime"})
    db.update_item(iid, registry_status="APPROVED")   # 다른 사용자가 보려면 승인된 항목이어야 한다
    db.put_usage_request(iid, "b@x.com", "PENDING"); db.put_usage_request(iid, "b@x.com", "APPROVED", decided_by="t")   # 사용 승인
    r = client("a@x.com").post(f"/api/agents/{iid}/chat", json=BODY)
    client("b@x.com").post(f"/api/agents/{iid}/chat", json=BODY)
    assert r.status_code == 200 and '"error": "boom"' in r.text and seen[0] != seen[1]


def test_internal_harness_runtimes_are_not_synced(env, monkeypatch):
    monkeypatch.setattr(aws, "list_all_prompts", lambda: [])
    monkeypatch.setattr(aws, "list_harnesses", lambda: [])
    monkeypatch.setattr(aws, "list_gateways", lambda reg=None: [])
    monkeypatch.setattr(aws, "runtime_info", lambda rid: {"protocol": "HTTP", "auth": "IAM"})
    monkeypatch.setattr(aws, "list_runtimes", lambda: [
        {"agentRuntimeId": "agentic_work-A1", "agentRuntimeName": "agentic_work", "agentRuntimeArn": RT, "status": "READY"},
        {"agentRuntimeId": "harness_ai_portal-R1", "agentRuntimeName": "harness_ai_portal", "agentRuntimeArn": "x", "status": "READY"}])
    catalog.sync_all()
    names = [i["name"] for i in db.list_items("agent")]
    assert names == ["agentic_work"] and db.list_items("agent")[0]["data"]["protocol"] == "HTTP"


def test_jwt_harness_is_called_with_users_bearer_token(env, monkeypatch):
    from application import mcp_token
    seen = {}
    monkeypatch.setattr(mcp_token, "issue", lambda email: {"access_token": "TOK-" + email})
    monkeypatch.setattr(aws, "get_harness", lambda hid: {"status": "READY"})
    monkeypatch.setattr(agent_test, "_harness_events", lambda arn, history, user, session, system=None, bearer=None: (seen.update(b=bearer), iter([{"t": "ok"}]))[1])
    iid = agent({"arn": HN, "agent_type": "harness", "auth": "JWT"})
    r = client().post(f"/api/agents/{iid}/chat", json=BODY)
    assert r.status_code == 200 and seen["b"] == "TOK-a@x.com" and "TOK-" not in r.text


def test_jwt_harness_unsigned_request_carries_authorization_header(env, monkeypatch):
    sent = {}

    class FakeClient:
        class meta:
            class events:
                @staticmethod
                def register(name, fn):
                    p = {"headers": {}}; fn(p); sent["headers"] = p["headers"]; sent["event"] = name
        def invoke_harness(self, **kw):
            sent["kw"] = kw
            return {"stream": iter([])}
    monkeypatch.setattr(aws.boto3, "client", lambda *a, **k: (sent.update(cfg=k["config"]), FakeClient())[1])
    list(agent_test._harness_events(HN, [{"role": "user", "content": "hi"}], auth.User("a@x.com", "N", "", "user"), "s" * 40, None, "TOK"))
    assert sent["headers"] == {"Authorization": "Bearer TOK"} and sent["event"].endswith("InvokeHarness")
    assert sent["cfg"].signature_version is not None and "runtimeUserId" not in sent["kw"]


def test_empty_assistant_turns_are_dropped_and_empty_user_turns_refused(env, monkeypatch):
    seen = []
    monkeypatch.setattr(agent_test, "_harness_events", lambda arn, history, *a, **k: (seen.append(history), iter([{"t": "ok"}]))[1])
    monkeypatch.setattr(aws, "get_harness", lambda hid: {"status": "READY"})
    iid = agent({"arn": HN, "agent_type": "harness"})
    msgs = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": ""}, {"role": "user", "content": "q2"}]
    assert client().post(f"/api/agents/{iid}/chat", json={**BODY, "messages": msgs}).status_code == 200
    assert [m["content"] for m in seen[0]] == ["q1", "q2"]                       # 에러로 빈 응답은 이력에서 빠진다
    bad = [{"role": "user", "content": " "}]
    assert client().post(f"/api/agents/{iid}/chat", json={**BODY, "messages": bad}).status_code == 422


def test_harness_still_creating_gives_a_clear_409(env, monkeypatch):
    monkeypatch.setattr(aws, "get_harness", lambda hid: {"status": "CREATING"})
    iid = agent({"arn": HN, "agent_type": "harness"})
    r = client().post(f"/api/agents/{iid}/chat", json=BODY)
    assert r.status_code == 409 and "준비되지 않았습니다" in r.json()["detail"]
