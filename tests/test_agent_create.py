import pytest
from fastapi.testclient import TestClient

from application import agent_create, auth, aws, catalog, db, gateway, mcp_token, server


def _grant(item_id, email="a@x.com"):
    """Registry 등록 + 사용 신청 승인. 에이전트에 붙이려면 둘 다 필요하다."""
    db.update_item(item_id, registry_id="REG", registry_record_id=f"rec{item_id:08d}", registry_status="APPROVED")
    db.put_usage_request(item_id, email, "PENDING"); db.put_usage_request(item_id, email, "APPROVED", decided_by="t")


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
    monkeypatch.setattr(aws, "account_id", lambda: "123456789012")
    monkeypatch.setattr(aws, "region", lambda: "us-west-2")
    calls = []
    n = {"i": 0}

    class C:
        def create_harness(self, **kw):
            calls.append(kw)
            n["i"] += 1
            hid = f"h-{n['i']}"
            return {"harness": {"harnessId": hid, "arn": f"arn:aws:bedrock-agentcore:us-west-2:123456789012:harness/{hid}", "status": "CREATING"}}
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
    assert it["ext_id"].startswith("harness:h-") and it["data"]["agent_type"] == "harness"


def test_unusable_model_and_duplicate(env):
    assert post(client(), model="m2").status_code == 422
    assert post(client(), model="gpt-x").status_code == 422                       # Bedrock Claude 만
    assert post(client()).status_code == 200 and post(client()).status_code == 409


def test_mcp_and_skill_selected(env):
    mid = db.insert_item("mcp", "srv", "", "a@x.com", {"endpoint": "https://e.example/mcp"})
    _grant(mid)
    sid = db.insert_item("skill", "sk", "", "a@x.com", {"source_type": "s3", "s3_uri": "s3://b/p"})
    assert post(client(), mcp_ids=[mid], skill_ids=[sid]).status_code == 200
    kw = env[0]
    assert kw["tools"][0]["type"] == "remote_mcp" and kw["skills"] == [{"s3": {"uri": "s3://b/p/"}}]


def test_mcp_without_usage_approval_is_refused(env):
    mid = db.insert_item("mcp", "srv", "", "owner@x.com", {"endpoint": "https://e.example/mcp"})
    db.update_item(mid, registry_status="APPROVED")
    assert post(client("b@x.com"), mcp_ids=[mid]).status_code == 403 and not env


def test_jwt_gateway_mcp_not_baked_and_forces_jwt_auth(env, monkeypatch):
    """JWT Gateway 는 create 때 bake 하지 않고(호출 시 remote_mcp+Bearer), harness 인증은 JWT 로 강제한다."""
    mid = db.insert_item("mcp", "gw", "", "a@x.com",
                         {"kind": "gateway", "authorizer": "CUSTOM_JWT", "gateway_id": "g1",
                          "endpoint": "https://g.example/mcp", "region": "us-west-2"})
    _grant(mid)
    monkeypatch.setattr(mcp_token, "settings", lambda: {"pool": "p1", "client": "c1", "region": "us-east-1"})
    r = post(client(), name="with_jwt_gw", mcp_ids=[mid], auth="IAM")
    assert r.status_code == 200, r.text
    assert "tools" not in env[0]
    a = env[0]["authorizerConfiguration"]["customJWTAuthorizer"]
    assert a["allowedClients"] == ["c1"] and "p1/.well-known" in a["discoveryUrl"]
    it = next(i for i in db.list_items("agent") if i["name"] == "with_jwt_gw")
    assert it["data"]["auth"] == "JWT" and it["data"]["jwt_mcp"] is True and it["data"]["mcp_ids"] == [mid]
    user = auth.User("a@x.com", "N", "", "user")
    tools = agent_create.invoke_tools([mid], user, "TOK")
    assert tools[0]["type"] == "remote_mcp"
    assert tools[0]["config"]["remoteMcp"]["headers"]["Authorization"] == "Bearer TOK"


def test_jwt_auth_requires_cognito(env, monkeypatch):
    assert post(client(), name="jwt_auth_a", auth="JWT").status_code == 503
    monkeypatch.setattr(mcp_token, "settings", lambda: {"pool": "p1", "client": "c1", "region": "us-east-1"})
    assert post(client(), name="jwt_auth_b", auth="JWT").status_code == 200


def test_registry_prompt_used(env, monkeypatch):
    pid = db.insert_item("prompt", "pp", "", "a@x.com", {"prompt_id": "P1"})
    monkeypatch.setattr(catalog, "harness_system_prompt", lambda u, i: [{"text": "from registry"}])
    assert post(client(), prompt_id=pid).status_code == 200 and env[0]["systemPrompt"] == [{"text": "from registry"}]


def test_delete_removes_harness_only_for_creator_or_admin(env, monkeypatch):
    deleted = []
    monkeypatch.setattr(aws, "delete_harness", lambda hid: deleted.append(hid))
    iid = post(client("a@x.com")).json()["id"]
    db.update_item(iid, registry_status="APPROVED")                                  # 남에게도 보이는 상태
    assert client("b@x.com").delete(f"/api/catalog/agent/{iid}").status_code == 403    # 생성자·관리자만 삭제 가능
    assert deleted == []
    r = client("a@x.com").delete(f"/api/catalog/agent/{iid}")
    assert r.status_code == 200 and "harness" in r.json()["deleted"] and deleted == ["h-1"]
    assert db.get_item(iid) is None


def test_admin_can_delete_and_synced_harness_is_left_alone(env, monkeypatch):
    deleted = []
    monkeypatch.setattr(aws, "delete_harness", lambda hid: deleted.append(hid))
    iid = post(client("a@x.com")).json()["id"]
    assert client("e2npark@gmail.com").delete(f"/api/catalog/agent/{iid}").status_code == 200 and deleted == ["h-1"]
    sid = db.insert_item("agent", "synced", "", "a@x.com", {"agent_type": "harness"}, source="aws", ext_id="harness:zzz")
    assert client("a@x.com").delete(f"/api/catalog/agent/{sid}").status_code == 200 and deleted == ["h-1"]  # synced harness 는 AWS 삭제 안 함
