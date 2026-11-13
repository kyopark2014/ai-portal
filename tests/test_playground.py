"""Playground(/playground): 사용 승인된 agent 만, 사용자·agent 별 task 로 대화를 저장한다. agent 만 순수하게 호출한다(포털 기본 system prompt 없음)."""
import json

import pytest
from fastapi.testclient import TestClient

from application import agent_test, auth, aws, catalog, db, playground_store as store, server

RT = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/rt_a-AbC123"
HN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:harness/hr_agent-MopJObRjow"
P = "/api/playground"


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


def agent(arn, name, auth_="IAM", owner="a@x.com", approved=True, who="a@x.com", t="runtime", **extra):
    iid = db.insert_item("agent", name, "d", owner, {"agent_type": t, "arn": arn, "auth": auth_, "status": "READY", **extra})
    if approved:
        db.put_usage_request(iid, who, "PENDING"); db.put_usage_request(iid, who, "APPROVED", decided_by="t")
    return iid


def events(r):
    return [json.loads(l[5:]) for l in r.text.split("\n\n") if l.startswith("data:")]


def fake_harness(monkeypatch, evs, seen=None):
    monkeypatch.setattr(aws, "get_harness", lambda hid: {"status": "READY"})
    def f(arn, history, user, session, system=None, bearer=None):
        if seen is not None: seen.update(system=system, arn=arn, history=history, session=session)
        return iter(evs)
    monkeypatch.setattr(agent_test, "_harness_events", f)


# ── 접근 ──
def test_requires_login(env):
    assert TestClient(server.app).get(f"{P}/agents").status_code == 401


def test_lists_only_usage_approved_agents_even_when_iam(env):
    agent(RT, "ok")
    agent(HN, "pending", approved=False, t="harness")                       # IAM 이어도 승인 전에는 playground 에 없다
    agent(RT, "mcp-rt", protocol="MCP")                                    # 대화할 수 없는 MCP runtime
    assert [a["name"] for a in client().get(f"{P}/agents").json()["agents"]] == ["ok"]


def test_other_users_approval_does_not_leak(env):
    agent(RT, "mine")
    assert client("b@x.com").get(f"{P}/agents").json()["agents"] == []


def test_unapproved_agent_has_no_tasks_or_chat(env):
    iid = agent(RT, "x", approved=False)
    c = client()
    assert c.get(f"{P}/agents/{iid}").status_code == 403
    assert c.post(f"{P}/agents/{iid}/tasks").status_code == 403
    assert c.post(f"{P}/agents/{iid}/tasks/abc/chat", json={"content": "hi"}).status_code == 403
    assert client("b@x.com").get(f"{P}/agents/{iid}").status_code == 404      # 남의 미등록 항목은 안 보인다


def test_detail_refreshes_a_stale_creating_status(env, monkeypatch):
    monkeypatch.setattr(aws, "get_harness", lambda hid: {"status": "READY"})
    iid = agent(HN, "h", t="harness")
    db.update_item(iid, data={**db.get_item(iid)["data"], "status": "CREATING"})
    assert client().get(f"{P}/agents/{iid}").json()["status"] == "READY"


# ── task ──
def test_task_lifecycle_pin_rename_delete(env):
    iid = agent(RT, "a"); c = client()
    t1 = c.post(f"{P}/agents/{iid}/tasks").json(); t2 = c.post(f"{P}/agents/{iid}/tasks").json()
    assert t1["title"] == "New task" and t1["pinned"] is False
    assert c.patch(f"{P}/agents/{iid}/tasks/{t1['id']}", json={"pinned": True}).json()["pinned"] is True
    assert [t["id"] for t in c.get(f"{P}/agents/{iid}/tasks").json()["tasks"]] == [t1["id"], t2["id"]]       # 고정이 먼저
    assert c.patch(f"{P}/agents/{iid}/tasks/{t2['id']}", json={"title": "내 제목"}).json()["title"] == "내 제목"
    assert c.delete(f"{P}/agents/{iid}/tasks/{t2['id']}").json() == {"ok": True}
    assert [t["id"] for t in c.get(f"{P}/agents/{iid}/tasks").json()["tasks"]] == [t1["id"]]
    assert c.get(f"{P}/agents/{iid}/tasks/{t2['id']}").status_code == 404


def test_tasks_are_per_user_and_per_agent(env):
    a1 = agent(RT, "a1"); a2 = agent(RT.replace("rt_a", "rt_b"), "a2")
    for who in ("b@x.com",):
        db.put_usage_request(a1, who, "PENDING"); db.put_usage_request(a1, who, "APPROVED", decided_by="t")
    db.update_item(a1, registry_status="APPROVED")
    mine = client().post(f"{P}/agents/{a1}/tasks").json()
    assert client().get(f"{P}/agents/{a2}/tasks").json()["tasks"] == []                  # 다른 agent 의 목록에는 없다
    assert client().get(f"{P}/agents/{a2}/tasks/{mine['id']}").status_code == 404         # 다른 agent 로는 못 연다
    b = client("b@x.com")
    assert b.get(f"{P}/agents/{a1}/tasks").json()["tasks"] == []                         # 다른 사용자의 task 는 안 보인다
    assert b.get(f"{P}/agents/{a1}/tasks/{mine['id']}").status_code == 404
    assert b.delete(f"{P}/agents/{a1}/tasks/{mine['id']}").status_code == 404
    assert b.patch(f"{P}/agents/{a1}/tasks/{mine['id']}", json={"title": "x"}).status_code == 404
    assert b.post(f"{P}/agents/{a1}/tasks/{mine['id']}/chat", json={"content": "hi"}).status_code == 404
    assert len(client().get(f"{P}/agents/{a1}/tasks").json()["tasks"]) == 1                # 지워지지 않았다


# ── 대화 ──
def test_chat_streams_saves_the_turn_and_titles_the_task(env, monkeypatch):
    seen = {}
    monkeypatch.setattr(catalog, "harness_system_prompt", lambda *a, **k: seen.setdefault("called", True) and [{"text": "PORTAL"}])
    # tool_start → tool_call 은 같은 블록(i)으로 upsert 되어 카드가 하나다(harness-work 와 동일).
    fake_harness(monkeypatch, [
        {"t": "안녕"}, {"t": "하세요"},
        {"tool_start": "search", "i": 0},
        {"tool_call": "search", "input": '{"q":"x"}', "i": 0},
        {"tool_result": "R", "i": 0},
        {"t": "끝"},
    ], seen)
    iid = agent(HN, "h", t="harness"); c = client()
    t = c.post(f"{P}/agents/{iid}/tasks").json()
    r = c.post(f"{P}/agents/{iid}/tasks/{t['id']}/chat", json={"content": "서울 날씨는?"})
    evs = events(r)
    assert evs[-1] == {"done": True} and r.headers["content-type"].startswith("text/event-stream")
    assert {"tool_start": "search", "i": 0} in evs and any(e.get("tool_call") == "search" for e in evs)
    assert seen["system"] is None and "called" not in seen and seen["arn"] == HN          # agent 자체 prompt 를 그대로 쓴다
    got = c.get(f"{P}/agents/{iid}/tasks/{t['id']}").json()
    assert got["title"] == "서울 날씨는?"                                                  # 첫 질문이 제목
    u, a = got["messages"]
    assert (u["role"], u["content"]) == ("user", "서울 날씨는?") and a["role"] == "assistant" and a["content"] == "안녕하세요끝"
    assert [e["type"] for e in a["tool_events"]] == ["text", "tool", "tool_result", "text"]      # 시간순 timeline(start+call = tool 1개)
    assert a["tool_events"][0]["data"] == "안녕하세요" and a["tool_events"][1]["input"] == {"q": "x"}
    assert a["tool_events"][1]["toolUseId"] == "0"


def test_tool_start_card_appears_before_later_text_and_empty_input_tools_persist(env, monkeypatch):
    """답변 text 뒤에 오는 shell 도 tool_start 시점에 타임라인에 들어가고, 인자 없는 도구도 저장된다."""
    fake_harness(monkeypatch, [
        {"t": "날씨입니다"},
        {"tool_start": "shell", "i": 3},
        {"tool_call": "shell", "input": "", "i": 3},
        {"tool_result": "ok", "i": 3},
    ])
    iid = agent(HN, "h", t="harness"); c = client()
    t = c.post(f"{P}/agents/{iid}/tasks").json()
    c.post(f"{P}/agents/{iid}/tasks/{t['id']}/chat", json={"content": "날씨?"})
    te = c.get(f"{P}/agents/{iid}/tasks/{t['id']}").json()["messages"][-1]["tool_events"]
    assert [e["type"] for e in te] == ["text", "tool", "tool_result"]
    assert te[0]["data"] == "날씨입니다" and te[1]["tool"] == "shell" and te[1]["input"] == ""


def test_chat_continues_with_saved_history_and_a_stable_session(env, monkeypatch):
    seen = {}
    fake_harness(monkeypatch, [{"t": "ok"}], seen)
    iid = agent(HN, "h", t="harness"); c = client()
    t = c.post(f"{P}/agents/{iid}/tasks").json()
    c.post(f"{P}/agents/{iid}/tasks/{t['id']}/chat", json={"content": "첫째"}); first = seen["session"]
    c.post(f"{P}/agents/{iid}/tasks/{t['id']}/chat", json={"content": "둘째"})
    assert [(m["role"], m["content"]) for m in seen["history"]] == [("user", "첫째"), ("assistant", "ok"), ("user", "둘째")]
    assert seen["session"] == first                                                       # 같은 task 는 같은 agent 세션
    t2 = c.post(f"{P}/agents/{iid}/tasks").json()
    c.post(f"{P}/agents/{iid}/tasks/{t2['id']}/chat", json={"content": "셋째"})
    assert seen["session"] != first and [m["content"] for m in seen["history"]] == ["셋째"]  # 다른 task 는 이력도 세션도 분리


def test_failed_turn_keeps_the_question_and_empty_replies_do_not_poison_history(env, monkeypatch):
    fake_harness(monkeypatch, [{"error": "boom"}])
    iid = agent(HN, "h", t="harness"); c = client()
    t = c.post(f"{P}/agents/{iid}/tasks").json()
    r = c.post(f"{P}/agents/{iid}/tasks/{t['id']}/chat", json={"content": "q"})
    assert {"error": "boom"} in events(r)
    msgs = c.get(f"{P}/agents/{iid}/tasks/{t['id']}").json()["messages"]
    assert [m["role"] for m in msgs] == []                                               # 응답 없이 실패하면 아무것도 저장하지 않는다(다시 보낼 수 있다)
    fake_harness(monkeypatch, [{"t": "ok"}])
    c.post(f"{P}/agents/{iid}/tasks/{t['id']}/chat", json={"content": "q"})
    assert [m["content"] for m in c.get(f"{P}/agents/{iid}/tasks/{t['id']}").json()["messages"]] == ["q", "ok"]


def test_runtime_agent_chat_works_too(env, monkeypatch):
    class RTc:
        class B:
            def iter_lines(self): yield b'data: {"data": "hi"}'
        def invoke_agent_runtime(self, **kw): return {"contentType": "text/event-stream", "response": RTc.B()}
    monkeypatch.setattr(aws.boto3, "client", lambda *a, **k: RTc())
    iid = agent(RT, "r"); c = client()
    t = c.post(f"{P}/agents/{iid}/tasks").json()
    c.post(f"{P}/agents/{iid}/tasks/{t['id']}/chat", json={"content": "go"})
    assert c.get(f"{P}/agents/{iid}/tasks/{t['id']}").json()["messages"][-1]["content"] == "hi"


def test_validation(env, monkeypatch):
    fake_harness(monkeypatch, [{"t": "x"}])
    iid = agent(HN, "h", t="harness"); c = client()
    t = c.post(f"{P}/agents/{iid}/tasks").json()
    assert c.post(f"{P}/agents/{iid}/tasks/{t['id']}/chat", json={"content": ""}).status_code == 422
    assert c.post(f"{P}/agents/{iid}/tasks/{t['id']}/chat", json={"content": "   "}).status_code == 422
    assert c.post(f"{P}/agents/{iid}/tasks/{t['id']}/chat", json={"content": "x" * 8001}).status_code == 422


# ── 페이지 ──
def test_playground_page_is_served_without_the_portal_spa(env, tmp_path, monkeypatch):
    dist = tmp_path / "dist"; dist.mkdir(); (dist / "index.html").write_text("<title>PG</title>"); (dist / "favicon.svg").write_text("<svg/>")
    monkeypatch.setattr(server, "PG_DIST", dist)
    c = TestClient(server.app)
    for path in ("/playground", "/playground/12"):
        r = c.get(path); assert r.status_code == 200 and "PG" in r.text
    assert c.get("/playground/favicon.svg").text == "<svg/>"
    (tmp_path / "secret.txt").write_text("TOP-SECRET")
    r = c.get("/playground/%2e%2e/secret.txt")                                            # dist 밖 파일은 내주지 않는다(index 로 대체)
    assert "TOP-SECRET" not in r.text and "PG" in r.text


def test_a_turn_stopped_midway_is_still_saved_and_the_agent_stream_is_closed(env, monkeypatch):
    """«중지» 는 클라이언트가 연결을 끊는 것이다. 이때도 받은 만큼 저장되어야 하고(응답 제너레이터의 finally 에 의존하면 사라진다), agent 스트림도 닫혀야 한다."""
    import threading, time
    closed = threading.Event()
    def slow(arn, history, user, session, system=None, bearer=None):
        try:
            for w in "abcdef":
                yield {"t": w}; time.sleep(0.15)
        finally:
            closed.set()
    monkeypatch.setattr(aws, "get_harness", lambda hid: {"status": "READY"})
    monkeypatch.setattr(agent_test, "_harness_events", slow)
    iid = agent(HN, "h", t="harness"); c = client()
    t = c.post(f"{P}/agents/{iid}/tasks").json()
    with c.stream("POST", f"{P}/agents/{iid}/tasks/{t['id']}/chat", json={"content": "중단"}) as r:
        it = r.iter_lines(); next(it); next(it)                  # 첫 조각만 받고
    # with 를 빠져나오면 연결이 닫힌다 (= 사용자가 중지)
    assert closed.wait(5), "agent stream was not closed after the client left"
    deadline = time.time() + 5
    while time.time() < deadline:
        msgs = c.get(f"{P}/agents/{iid}/tasks/{t['id']}").json()["messages"]
        if len(msgs) == 2: break
        time.sleep(0.1)
    assert [m["role"] for m in msgs] == ["user", "assistant"] and msgs[0]["content"] == "중단"
    assert msgs[1]["content"] and "abcdef" != msgs[1]["content"]       # 일부만 받고 끊겼다
