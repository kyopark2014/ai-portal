import json

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from application import auth, aws, catalog, db, mcp_test, server

HN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:harness/ai_portal-X"      # 계정 ID 는 12자리여야 agent ARN 검증을 통과한다


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_SIGNING_KEY", "k")
    monkeypatch.setenv("PORTAL_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("HARNESS_ARN", HN)
    monkeypatch.setattr(auth, "_key", None)
    monkeypatch.setattr(catalog, "start_sync", lambda force=False: False)
    mcp_test._node_ready.clear()
    texts = {"P-DEFAULT": "기본 시스템 프롬프트", "P-OTHER": "다른 프롬프트 {{x}}"}
    monkeypatch.setattr(aws, "get_prompt", lambda pid: {
        "id": pid, "arn": f"arn:p/{pid}", "version": "DRAFT", "defaultVariant": "v",
        "variants": [{"name": "v", "templateConfiguration": {"text": {"text": texts[pid]}}}]})
    return texts


def client(email="a@x.com"):
    c = TestClient(server.app)
    c.cookies.set(auth.COOKIE_NAME, auth.make_session(email, "N", ""))
    return c


def prompt(name, pid, owner="a@x.com", status="APPROVED"):
    i = db.insert_item("prompt", name, "", owner, {"prompt_id": pid, "text": "stale copy"})
    db.update_item(i, registry_status=status)
    return i


class RT:
    def __init__(self): self.calls = []
    def invoke_agent_runtime_command(self, **kw): return {"stream": iter([{"chunk": {"contentStop": {"exitCode": 0}}}])}
    def invoke_harness(self, **kw):
        self.calls.append(kw)
        return {"stream": iter([{"metadata": {"usage": {}}}])}


def mcp_chat(c, rt, monkeypatch, **extra):
    monkeypatch.setattr(aws.boto3, "client", lambda *a, **k: rt)
    iid = db.insert_item("mcp", "m", "", "a@x.com", {"endpoint": "https://e/mcp", "headers": []})
    body = {"session_id": "s" * 36, "messages": [{"role": "user", "content": "hi"}], **extra}
    return c.post(f"/api/mcp/{iid}/chat", json=body)


def test_default_harness_prompt_is_used_when_none_given(env, monkeypatch):
    prompt("harness_prompt", "P-DEFAULT", owner="someone@x.com")
    rt = RT()
    assert mcp_chat(client(), rt, monkeypatch).status_code == 200
    assert rt.calls[0]["systemPrompt"] == [{"text": "기본 시스템 프롬프트"}]


def test_body_is_read_from_prompt_management_not_the_stale_db_copy(env, monkeypatch):
    prompt("harness_prompt", "P-DEFAULT")
    rt = RT()
    mcp_chat(client(), rt, monkeypatch)
    assert "stale copy" not in json.dumps(rt.calls[0]["systemPrompt"])


def test_explicit_prompt_overrides_default(env, monkeypatch):
    prompt("harness_prompt", "P-DEFAULT")
    other = prompt("other", "P-OTHER")
    rt = RT()
    mcp_chat(client(), rt, monkeypatch, prompt_id=other)
    assert rt.calls[0]["systemPrompt"] == [{"text": "다른 프롬프트 {{x}}"}]


def test_no_default_prompt_leaves_harness_default(env, monkeypatch):
    rt = RT()
    mcp_chat(client(), rt, monkeypatch)
    assert "systemPrompt" not in rt.calls[0]


@pytest.mark.parametrize("status", ["PENDING_APPROVAL", "REJECTED", "DEPRECATED", None])
def test_unapproved_default_prompt_is_not_applied_to_everyone(env, monkeypatch, status):
    prompt("harness_prompt", "P-DEFAULT", status=status)
    rt = RT()
    mcp_chat(client(), rt, monkeypatch)
    assert "systemPrompt" not in rt.calls[0]


def test_cannot_use_someone_elses_hidden_prompt(env, monkeypatch):
    hidden = prompt("secret", "P-OTHER", owner="bob@x.com", status="PENDING_APPROVAL")
    rt = RT()
    r = mcp_chat(client("a@x.com"), rt, monkeypatch, prompt_id=hidden)
    assert r.status_code == 404 and rt.calls == []


def test_unreadable_default_falls_back_but_unreadable_explicit_is_an_error(env, monkeypatch):
    def boom(pid):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "GetPrompt")
    monkeypatch.setattr(aws, "get_prompt", boom)
    prompt("harness_prompt", "P-DEFAULT")
    other = prompt("other", "P-OTHER")
    rt = RT()
    assert mcp_chat(client(), rt, monkeypatch).status_code == 200 and "systemPrompt" not in rt.calls[0]   # 기본값: 조용히 폴백
    rt2 = RT()
    assert mcp_chat(client(), rt2, monkeypatch, prompt_id=other).status_code == 403 and rt2.calls == []    # 직접 고른 것: 오류


def test_skill_and_harness_agent_also_use_the_default(env, monkeypatch):
    prompt("harness_prompt", "P-DEFAULT")
    rt = RT()
    monkeypatch.setattr(aws.boto3, "client", lambda *a, **k: rt)
    sk = db.insert_item("skill", "docx", "", "a@x.com", {"source_type": "git", "git_url": "https://github.com/o/r"})
    ag = db.insert_item("agent", "ai_portal", "", "a@x.com", {"arn": HN, "agent_type": "harness"})
    body = {"session_id": "s" * 36, "messages": [{"role": "user", "content": "hi"}]}
    assert client().post(f"/api/skills/{sk}/chat", json=body).status_code == 200
    assert client().post(f"/api/agents/{ag}/chat", json=body).status_code == 200
    assert [c["systemPrompt"] for c in rt.calls] == [[{"text": "기본 시스템 프롬프트"}]] * 2


def test_runtime_agents_do_not_get_a_system_prompt(env, monkeypatch):
    prompt("harness_prompt", "P-DEFAULT")
    seen = {}
    class R:
        def invoke_agent_runtime(self, **kw):
            seen.update(kw); return {"contentType": "application/json", "response": type("B", (), {"read": lambda s: b"{}"})()}
    monkeypatch.setattr(aws.boto3, "client", lambda *a, **k: R())
    ag = db.insert_item("agent", "agentic_work", "", "a@x.com", {"arn": "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/agentic_work-A1", "agent_type": "runtime"})
    client().post(f"/api/agents/{ag}/chat", json={"session_id": "s" * 36, "messages": [{"role": "user", "content": "hi"}]})
    assert "systemPrompt" not in json.dumps(seen, default=str) and "기본 시스템 프롬프트" not in json.dumps(seen, default=str)
