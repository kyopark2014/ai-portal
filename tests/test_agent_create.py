import pytest
from fastapi.testclient import TestClient

from application import agent_create, auth, aws, catalog, db, gateway, mcp_token, server


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_SIGNING_KEY", "k")
    monkeypatch.setenv("PORTAL_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("LLM_GATEWAY_KEY", "gk")
    monkeypatch.setattr(auth, "_key", None)
    monkeypatch.setattr(catalog, "start_sync", lambda force=False: False)
    monkeypatch.setattr(catalog, "selected_registry", lambda: None)
    monkeypatch.setattr(gateway, "list_models", lambda force=False: [
        {"id": "global.anthropic.m1", "usable": True, "provider": "BEDROCK", "endpoint": "messages"},
        {"id": "m2", "usable": False, "provider": "BEDROCK", "endpoint": None},
        {"id": "gpt-x", "usable": True, "provider": "BEDROCK_RUNTIME_OPENAI", "endpoint": "chat"}])
    monkeypatch.setattr(agent_create, "role_arn", lambda: "arn:aws:iam::123456789012:role/r")
    calls = []

    class C:
        def create_harness(self, **kw):
            calls.append(kw)
            return {"harness": {"harnessId": "h-1", "arn": "arn:aws:bedrock-agentcore:us-west-2:123456789012:harness/h-1", "status": "CREATING"}}
    monkeypatch.setattr(aws, "client", lambda s: C())
    return calls


def client(email="a@x.com"):
    c = TestClient(server.app)
    c.cookies.set(auth.COOKIE_NAME, auth.make_session(email, "N", ""))
    return c


def post(c, **over):
    return c.post("/api/agents/create", json={"name": "my_agent", "model": "global.anthropic.m1", **over})


@pytest.mark.parametrize("bad", ["my-agent", "1abc", "a b", "한글", "x" * 41])
def test_bad_names_rejected(env, bad):
    assert post(client(), name=bad).status_code == 422 and not env


def test_create_with_text_prompt_and_iam(env):
    r = post(client(), system_prompt="be nice")
    assert r.status_code == 200, r.text
    kw = env[0]
    assert kw["harnessName"] == "my_agent" and kw["systemPrompt"] == [{"text": "be nice"}]
    assert kw["model"] == {"bedrockModelConfig": {"modelId": "global.anthropic.m1", "maxTokens": 4096}} and "authorizerConfiguration" not in kw
    it = db.list_items("agent")[0]
    assert it["ext_id"] == "harness:h-1" and it["data"]["agent_type"] == "harness"


def test_unusable_model_and_duplicate(env):
    assert post(client(), model="m2").status_code == 422
    assert post(client(), model="gpt-x").status_code == 422                       # Bedrock Claude 만
    assert post(client()).status_code == 200 and post(client()).status_code == 409


def test_mcp_and_skill_selected(env):
    mid = db.insert_item("mcp", "srv", "", "a@x.com", {"endpoint": "https://e.example/mcp"})
    sid = db.insert_item("skill", "sk", "", "a@x.com", {"source_type": "s3", "s3_uri": "s3://b/p"})
    assert post(client(), mcp_ids=[mid], skill_ids=[sid]).status_code == 200
    kw = env[0]
    assert kw["tools"][0]["type"] == "remote_mcp" and kw["skills"] == [{"s3": {"uri": "s3://b/p/"}}]


def test_mcp_without_usage_approval_is_refused(env):
    mid = db.insert_item("mcp", "srv", "", "owner@x.com", {"endpoint": "https://e.example/mcp"})
    db.update_item(mid, registry_status="APPROVED")
    assert post(client("b@x.com"), mcp_ids=[mid]).status_code == 403 and not env


def test_jwt_gateway_mcp_refused_and_jwt_auth(env, monkeypatch):
    mid = db.insert_item("mcp", "gw", "", "a@x.com", {"kind": "gateway", "authorizer": "CUSTOM_JWT", "gateway_id": "g1", "endpoint": "https://g"})
    assert post(client(), mcp_ids=[mid]).status_code == 422
    assert post(client(), auth="JWT").status_code == 503              # Cognito 미설치
    monkeypatch.setattr(mcp_token, "settings", lambda: {"pool": "p1", "client": "c1", "region": "us-east-1"})
    assert post(client(), auth="JWT").status_code == 200
    a = env[0]["authorizerConfiguration"]["customJWTAuthorizer"]
    assert a["allowedClients"] == ["c1"] and "p1/.well-known" in a["discoveryUrl"]


def test_registry_prompt_used(env, monkeypatch):
    pid = db.insert_item("prompt", "pp", "", "a@x.com", {"prompt_id": "P1"})
    monkeypatch.setattr(catalog, "harness_system_prompt", lambda u, i: [{"text": "from registry"}])
    assert post(client(), prompt_id=pid).status_code == 200 and env[0]["systemPrompt"] == [{"text": "from registry"}]
