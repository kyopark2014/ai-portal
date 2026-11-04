"""MCP Gateway 접속 토큰: 사용 승인 ↔ Cognito 그룹, 승인된 사람에게만 토큰. 실제 Cognito 는 부르지 않는다(moto)."""
import base64
import json

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from application import admins, auth, catalog, db, mcp_token, policy, server

ADMIN, OWNER, USER, OTHER = "e2npark@gmail.com", "owner@x.com", "user@x.com", "other@x.com"
GW = "gw-ai-portal-us-east-1-abc123"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_SIGNING_KEY", "k"); monkeypatch.setenv("PORTAL_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN); monkeypatch.setenv("REGISTRY_ID", "REG")
    monkeypatch.setattr(auth, "_key", None); monkeypatch.setattr(catalog, "start_sync", lambda force=False: False)
    admins.reset_cache(); policy.reset_cache()
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x"); monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    with mock_aws():
        c = boto3.client("cognito-idp", region_name="us-east-1")
        pool = c.create_user_pool(PoolName="p", UsernameAttributes=["email"], AdminCreateUserConfig={"AllowAdminCreateUserOnly": True})["UserPool"]["Id"]
        cl = c.create_user_pool_client(UserPoolId=pool, ClientName="c", ExplicitAuthFlows=["ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"])["UserPoolClient"]["ClientId"]
        monkeypatch.setattr(mcp_token, "settings", lambda: {"pool": pool, "client": cl, "region": "us-east-1"})
        yield c, pool


def client(email):
    c = TestClient(server.app); c.cookies.set(auth.COOKIE_NAME, auth.make_session(email, "N", "")); return c


def gateway_item(owner=OWNER, status="APPROVED"):
    i = db.insert_item("mcp", "gw-x", "", owner, {"kind": "gateway", "gateway_id": GW, "endpoint": "https://e/mcp"})
    db.update_item(i, registry_id="REG", registry_record_id="rec123456", registry_status=status); return i


def groups(c, pool, email):
    try:
        return [g["GroupName"] for g in c.admin_list_groups_for_user(UserPoolId=pool, Username=email)["Groups"]]
    except c.exceptions.UserNotFoundException:       # 승인 전에는 Cognito 사용자가 없을 수 있다 = 어느 그룹에도 없음
        return []


def test_login_creates_the_cognito_user_once(env):
    c, pool = env
    u1 = mcp_token.ensure_user(USER); u2 = mcp_token.ensure_user(USER.upper())
    assert u1 == u2 and len(c.list_users(UserPoolId=pool)["Users"]) == 1


def test_token_is_refused_until_the_usage_is_approved(env):
    i = gateway_item()
    assert client(USER).get(f"/api/mcp/{i}/token").status_code == 403           # 신청 전
    client(USER).post(f"/api/mcp/{i}/usage")
    assert client(USER).get(f"/api/mcp/{i}/token").status_code == 403           # 신청 중


def test_approval_adds_the_group_and_the_token_is_issued(env):
    c, pool = env; i = gateway_item()
    client(USER).post(f"/api/mcp/{i}/usage")
    r = client(ADMIN).post(f"/api/usage-requests/{i}/{USER}/decision", json={"decision": "APPROVED"})
    assert r.status_code == 200 and "token_sync_error" not in r.json()
    assert groups(c, pool, USER) == [f"mcp:{GW}"]                                 # 서버(gateway)별 승인 = 그룹
    t = client(USER).get(f"/api/mcp/{i}/token").json()
    assert t["token_type"] == "Bearer" and t["expires_in"] > 0 and t["refresh_token"] and t["group"] == f"mcp:{GW}"
    payload = json.loads(base64.urlsafe_b64decode(t["access_token"].split(".")[1] + "=="))
    assert payload.get("cognito:groups") == [f"mcp:{GW}"]                         # 인터셉터가 읽을 클레임


def test_cancel_removes_the_group_and_blocks_the_token(env):
    c, pool = env; i = gateway_item()
    client(USER).post(f"/api/mcp/{i}/usage"); client(ADMIN).post(f"/api/usage-requests/{i}/{USER}/decision", json={"decision": "APPROVED"})
    assert client(USER).delete(f"/api/mcp/{i}/usage").status_code == 200
    assert groups(c, pool, USER) == []
    assert client(USER).get(f"/api/mcp/{i}/token").status_code == 403


def test_groups_are_per_gateway_and_per_user(env):
    c, pool = env; a = gateway_item(); b = db.insert_item("mcp", "gw-y", "", OWNER, {"kind": "gateway", "gateway_id": "gw-other-xyz789", "endpoint": "https://e/mcp"})
    db.update_item(b, registry_id="REG", registry_record_id="rec777777", registry_status="APPROVED")
    for u in (USER, OTHER): client(u).post(f"/api/mcp/{a}/usage")
    client(ADMIN).post(f"/api/usage-requests/{a}/{USER}/decision", json={"decision": "APPROVED"})
    assert groups(c, pool, USER) == [f"mcp:{GW}"] and groups(c, pool, OTHER) == []
    assert client(USER).get(f"/api/mcp/{b}/token").status_code == 403             # 다른 서버는 따로 승인이 필요하다


def test_owner_needs_no_request_admin_does_and_non_gateway_mcp_has_no_token(env):
    i = gateway_item(); plain = db.insert_item("mcp", "m", "", OWNER, {"endpoint": "https://e/mcp"})
    db.update_item(plain, registry_id="REG", registry_record_id="rec999999", registry_status="APPROVED")
    assert client(OWNER).get(f"/api/mcp/{i}/token").status_code == 200
    assert client(ADMIN).get(f"/api/mcp/{i}/token").status_code == 403           # 관리자도 사용 승인이 필요하다
    db.put_usage_request(i, ADMIN, "PENDING"); db.put_usage_request(i, ADMIN, "APPROVED", decided_by="t")
    assert client(ADMIN).get(f"/api/mcp/{i}/token").status_code == 200
    assert client(OWNER).get(f"/api/mcp/{plain}/token").status_code == 409


def test_unregistered_gateway_is_invisible_to_others(env):
    i = gateway_item(status="DRAFT")
    assert client(USER).get(f"/api/mcp/{i}/token").status_code == 404


def test_cognito_down_does_not_break_the_approval_record(env, monkeypatch):
    i = gateway_item(); client(USER).post(f"/api/mcp/{i}/usage")
    monkeypatch.setattr(mcp_token, "grant", lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    r = client(ADMIN).post(f"/api/usage-requests/{i}/{USER}/decision", json={"decision": "APPROVED"})
    assert r.status_code == 200 and r.json()["token_sync_error"] == "RuntimeError"
    assert db.get_usage_request(i, USER)["status"] == "APPROVED"


def test_without_cognito_everything_is_blocked_with_a_reason(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_SIGNING_KEY", "k"); monkeypatch.setenv("PORTAL_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN); monkeypatch.setattr(auth, "_key", None)
    i = gateway_item()
    assert client(OWNER).get(f"/api/mcp/{i}/token").status_code == 503


def test_jwt_gateway_chat_issues_a_fresh_token_for_the_caller_only_when_allowed(env, monkeypatch):
    from application import mcp_test
    i = db.insert_item("mcp", "gw-x", "", OWNER, {"kind": "gateway", "authorizer": "CUSTOM_JWT", "gateway_id": GW, "endpoint": "https://e/mcp"})
    db.update_item(i, registry_id="REG", registry_record_id="rec123456", registry_status="APPROVED")
    monkeypatch.setenv("HARNESS_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h")
    seen = {}
    monkeypatch.setattr(mcp_test, "_stream_harness", lambda arn, user, sid, hist, system=None, tools=None: seen.update(tools=tools, who=user.email) or {"ok": 1})
    body = {"session_id": "s" * 36, "messages": [{"role": "user", "content": "hi"}]}
    assert client(USER).post(f"/api/mcp/{i}/chat", json=body).status_code == 403            # 승인 전: 토큰도 만들지 않는다
    assert not seen
    client(USER).post(f"/api/mcp/{i}/usage"); client(ADMIN).post(f"/api/usage-requests/{i}/{USER}/decision", json={"decision": "APPROVED"})
    assert client(USER).post(f"/api/mcp/{i}/chat", json=body).status_code == 200
    cfg = seen["tools"][0]["config"]["remoteMcp"]
    assert cfg["url"] == "https://e/mcp" and cfg["headers"]["Authorization"].startswith("Bearer ey")   # 이 사용자의 액세스 토큰
    payload = json.loads(base64.urlsafe_b64decode(cfg["headers"]["Authorization"].split(".")[1] + "=="))
    assert payload["cognito:groups"] == [f"mcp:{GW}"]                                                  # 인터셉터가 통과시킬 그룹
