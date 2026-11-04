import pytest
from fastapi.testclient import TestClient

from application import auth, aws, catalog, db, server

ADMIN, ALICE, BOB = "e2npark@gmail.com", "alice@x.com", "bob@x.com"
KINDS = ("prompt", "mcp", "agent", "skill")


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_SIGNING_KEY", "k")
    monkeypatch.setenv("PORTAL_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("HARNESS_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h")
    monkeypatch.setenv("LLM_GATEWAY_KEY", "vk")
    monkeypatch.setattr(auth, "_key", None)
    monkeypatch.setattr(catalog, "start_sync", lambda force=False: False)


def client(email):
    c = TestClient(server.app)
    c.cookies.set(auth.COOKIE_NAME, auth.make_session(email, "N", ""))
    return c


def seed():
    """종류마다: bob 이 만든 것(승인 전/승인됨), alice 가 만든 것, AWS 동기화 항목(승인 전/승인됨)."""
    out = {}
    for k in KINDS:
        d = {"text": "t", "variables": []} if k == "prompt" else {"endpoint": "https://e/mcp", "headers": []}
        out[k] = {
            "bob_pending": db.insert_item(k, f"{k}-bob-pending", "", BOB, d),
            "bob_approved": db.insert_item(k, f"{k}-bob-approved", "", BOB, d),
            "alice_own": db.insert_item(k, f"{k}-alice-own", "", ALICE, d),
            "aws_pending": db.insert_item(k, f"{k}-aws-pending", "", "aws", d, "aws", f"{k}:1"),
            "aws_approved": db.insert_item(k, f"{k}-aws-approved", "", "aws", d, "aws", f"{k}:2"),
        }
        db.update_item(out[k]["bob_approved"], registry_status="APPROVED")
        db.update_item(out[k]["aws_approved"], registry_status="APPROVED")
        db.update_item(out[k]["bob_pending"], registry_status="PENDING_APPROVAL")
    return out


@pytest.mark.parametrize("kind", KINDS)
def test_user_sees_own_and_approved_only(env, kind):
    seed()
    names = {i["name"] for i in client(ALICE).get(f"/api/catalog/{kind}").json()["items"]}
    assert names == {f"{kind}-alice-own", f"{kind}-bob-approved", f"{kind}-aws-approved"}


@pytest.mark.parametrize("kind", KINDS)
def test_admin_sees_everything(env, kind):
    seed()
    assert len(client(ADMIN).get(f"/api/catalog/{kind}").json()["items"]) == 5


def test_search_does_not_leak_hidden_items(env):
    seed()
    r = client(ALICE).get("/api/catalog/prompt?q=pending").json()["items"]
    assert r == []                                           # 숨겨진 항목은 검색어로도 안 나온다
    assert [i["name"] for i in client(ADMIN).get("/api/catalog/prompt?q=bob-pending").json()["items"]] == ["prompt-bob-pending"]


def test_hidden_item_is_404_on_every_by_id_endpoint(env, monkeypatch):
    ids = seed()
    hidden = lambda k: ids[k]["bob_pending"]
    a = client(ALICE)
    for k in KINDS:                                           # 404 (403 이 아님): 존재 자체를 알리지 않는다
        i = hidden(k)
        assert a.patch(f"/api/catalog/{k}/{i}/description", json={"description": "x"}).status_code == 404
        assert a.delete(f"/api/catalog/{k}/{i}").status_code == 404
        assert a.post(f"/api/catalog/{k}/{i}/register").status_code == 404
        assert a.post(f"/api/catalog/{k}/{i}/registry/refresh").status_code == 404
        assert a.post(f"/api/catalog/{k}/{i}/registry/submit").status_code == 404
        assert a.post(f"/api/catalog/{k}/{i}/registry/cancel").status_code == 404
    assert a.post(f"/api/prompts/{hidden('prompt')}/refresh").status_code == 404
    assert a.put(f"/api/prompts/{hidden('prompt')}", json={"name": "p", "text": "t"}).status_code == 404
    assert a.post(f"/api/mcp/{hidden('mcp')}/test").status_code == 404
    body = {"session_id": "s" * 36, "messages": [{"role": "user", "content": "x"}]}
    assert a.post(f"/api/mcp/{hidden('mcp')}/chat", json=body).status_code == 404
    assert a.post(f"/api/agents/{hidden('agent')}/chat", json=body).status_code == 404
    assert a.post("/api/chat", json={"model": "m", "prompt_id": hidden("prompt"), "messages": body["messages"]}).status_code == 404
    # 존재하지 않는 ID 와 응답이 구분되지 않는다
    assert a.delete("/api/catalog/mcp/99999").json() == a.delete(f"/api/catalog/mcp/{hidden('mcp')}").json()


def test_approved_item_of_others_is_usable_but_read_only(env):
    ids = seed()
    a = client(ALICE)
    i = ids["prompt"]["bob_approved"]
    assert a.delete(f"/api/catalog/prompt/{i}").status_code == 403
    assert a.patch(f"/api/catalog/prompt/{i}/description", json={"description": "x"}).status_code == 403
    assert a.post(f"/api/catalog/prompt/{i}/registry/cancel").status_code == 403


def test_visibility_follows_status_changes(env):
    ids = seed()
    i = ids["mcp"]["bob_pending"]
    a = client(ALICE)
    assert i not in [x["id"] for x in a.get("/api/catalog/mcp").json()["items"]]
    db.update_item(i, registry_status="APPROVED")             # 승인되면 보인다
    assert i in [x["id"] for x in a.get("/api/catalog/mcp").json()["items"]]
    db.update_item(i, registry_status="DEPRECATED")           # deprecate/거절되면 다시 숨겨진다
    assert i not in [x["id"] for x in a.get("/api/catalog/mcp").json()["items"]]
    db.update_item(i, registry_status="REJECTED")
    assert i not in [x["id"] for x in a.get("/api/catalog/mcp").json()["items"]]


def test_owner_still_sees_own_rejected_items(env):
    i = db.insert_item("prompt", "mine", "", ALICE, {})
    db.update_item(i, registry_status="REJECTED")
    assert i in [x["id"] for x in client(ALICE).get("/api/catalog/prompt").json()["items"]]
