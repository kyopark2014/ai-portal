import pytest
from fastapi.testclient import TestClient

from application import admins, auth, aws, catalog, db, policy, server

ADMIN, OWNER, USER, REV, OTHER = "e2npark@gmail.com", "owner@x.com", "user@x.com", "rev@x.com", "other@x.com"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_SIGNING_KEY", "k"); monkeypatch.setenv("PORTAL_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN); monkeypatch.setenv("REGISTRY_ID", "REG"); monkeypatch.setenv("HARNESS_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h")
    monkeypatch.setattr(auth, "_key", None); monkeypatch.setattr(catalog, "start_sync", lambda force=False: False)
    admins.reset_cache(); policy.reset_cache()


def client(email):
    c = TestClient(server.app); c.cookies.set(auth.COOKIE_NAME, auth.make_session(email, "N", "")); return c


def mcp(status="APPROVED", rec="rec123456", owner=OWNER, **data):
    i = db.insert_item("mcp", "m", "", owner, {"endpoint": "https://e/mcp", "headers": [], **data})
    db.update_item(i, registry_id="REG", registry_record_id=rec, registry_status=status); return i


def test_request_only_after_registration_is_complete(env):
    for st in ("DRAFT", "PENDING_APPROVAL", "REJECTED"):
        i = mcp(st, rec=f"rec{st[:4]}12345")
        assert client(USER).post(f"/api/mcp/{i}/usage").status_code == 404                      # 등록이 끝나지 않은 MCP 는 남에게 보이지도 않는다
        s = client(OWNER).get(f"/api/mcp/{i}/usage").json()                                      # 소유자에게는 '아직 등록 전' 으로 보인다
        assert s["registered"] is False and s["can_request"] is False and s["can_use"] is True
    ok = mcp("APPROVED")
    s = client(USER).get(f"/api/mcp/{ok}/usage").json()
    assert s["registered"] is True and s["status"] == "NONE" and s["can_request"] is True and s["can_use"] is False


def test_request_flow_pending_then_approved_by_a_reviewer(env):
    i = mcp()
    r = client(USER).post(f"/api/mcp/{i}/usage").json()
    assert r["status"] == "PENDING" and r["can_use"] is False and r["can_request"] is False
    assert client(USER).post(f"/api/mcp/{i}/usage").status_code == 409                          # 중복 신청 불가
    assert client(ADMIN).get("/api/usage-requests").json()["requests"][0]["email"] == USER
    d = client(ADMIN).post(f"/api/usage-requests/{i}/{USER}/decision", json={"decision": "APPROVED", "reason": "ok"}).json()
    assert d["status"] == "APPROVED" and "[by e2npark@gmail.com]" in d["reason"]
    s = client(USER).get(f"/api/mcp/{i}/usage").json()
    assert s["status"] == "APPROVED" and s["can_use"] is True and s["can_request"] is False
    assert client(USER).post(f"/api/mcp/{i}/usage").status_code == 409                          # 이미 승인됨


def test_using_the_mcp_is_blocked_until_approved(env, monkeypatch):
    i = mcp()
    body = {"session_id": "s" * 36, "messages": [{"role": "user", "content": "hi"}]}
    assert client(USER).post(f"/api/mcp/{i}/test").status_code == 403                           # 연결 테스트
    assert client(USER).post(f"/api/mcp/{i}/chat", json=body).status_code == 403                # harness 테스트
    assert client(USER).get("/api/catalog/mcp").json()["items"][0]["id"] == i                   # 목록·상세는 보인다
    assert client(USER).get(f"/api/mcp/{i}/usage").status_code == 200
    db.put_usage_request(i, USER, "PENDING"); db.put_usage_request(i, USER, "APPROVED", decided_by="a")
    assert client(USER).post(f"/api/mcp/{i}/test").status_code != 403                           # 승인 후에는 통과(여기서는 네트워크 오류 등 다른 결과)


def test_owner_and_admin_never_need_to_apply_and_cannot(env):
    i = mcp()
    for who in (OWNER, ADMIN):
        s = client(who).get(f"/api/mcp/{i}/usage").json()
        assert s["can_use"] is True and s["can_request"] is False and s["status"] == "OWNER"
        assert client(who).post(f"/api/mcp/{i}/usage").status_code == 409


def test_non_mcp_items_have_no_usage_request_and_stay_open(env):
    p = db.insert_item("prompt", "p", "", OWNER, {}); db.update_item(p, registry_status="APPROVED")
    assert client(USER).get(f"/api/mcp/{p}/usage").status_code == 404                           # prompt 는 사용 신청 대상이 아니다
    from application import usage
    assert usage.can_use(db.get_item(p), auth.User(USER, "N", "", "user")) is True


def test_rejection_requires_a_reason_and_allows_reapplying(env):
    i = mcp(); client(USER).post(f"/api/mcp/{i}/usage")
    assert client(ADMIN).post(f"/api/usage-requests/{i}/{USER}/decision", json={"decision": "REJECTED", "reason": " "}).status_code == 422
    client(ADMIN).post(f"/api/usage-requests/{i}/{USER}/decision", json={"decision": "REJECTED", "reason": "목적 불분명"})
    s = client(USER).get(f"/api/mcp/{i}/usage").json()
    assert s["status"] == "REJECTED" and "목적 불분명" in s["reason"] and s["can_use"] is False and s["can_request"] is True
    assert client(USER).post(f"/api/mcp/{i}/usage").json()["status"] == "PENDING"               # 다시 신청 가능
    assert client(ADMIN).post(f"/api/usage-requests/{i}/{USER}/decision", json={"decision": "APPROVED"}).status_code == 200
    assert client(ADMIN).post(f"/api/usage-requests/{i}/{USER}/decision", json={"decision": "REJECTED", "reason": "x"}).status_code == 409   # 이중 처리 불가


def test_reviewer_handles_only_their_mcp_requests(env):
    a, b = mcp(rec="recAAAAAA1"), mcp(rec="recBBBBBB1")
    ssm_store = {}
    class S:
        def get_parameter(self, Name):
            from botocore.exceptions import ClientError
            if Name not in ssm_store: raise ClientError({"Error": {"Code": "ParameterNotFound", "Message": "x"}}, "GetParameter")
            return {"Parameter": {"Value": ssm_store[Name]}}
        def put_parameter(self, Name, Value, **k): ssm_store[Name] = Value
    admins._ssm = lambda: S(); admins.reset_cache()
    admins.add_reviewer("recAAAAAA1", REV)
    client(USER).post(f"/api/mcp/{a}/usage"); client(USER).post(f"/api/mcp/{b}/usage")
    ids = {r["item_id"] for r in client(REV).get("/api/usage-requests").json()["requests"]}
    assert ids == {a}                                                                            # 자기에게 지정된 MCP 의 신청만 보인다
    assert client(REV).post(f"/api/usage-requests/{b}/{USER}/decision", json={"decision": "APPROVED"}).status_code == 404
    assert client(REV).post(f"/api/usage-requests/{a}/{USER}/decision", json={"decision": "APPROVED"}).status_code == 200
    assert client(OTHER).get("/api/usage-requests").status_code == 403 and client(OTHER).post(f"/api/usage-requests/{a}/{USER}/decision", json={"decision": "APPROVED"}).status_code == 404


def test_auto_review_approves_usage_requests_immediately_with_an_audit_mark(env, monkeypatch):
    class S:
        store = {}
        def get_parameter(self, Name):
            from botocore.exceptions import ClientError
            if Name not in self.store: raise ClientError({"Error": {"Code": "ParameterNotFound", "Message": "x"}}, "GetParameter")
            return {"Parameter": {"Value": self.store[Name]}}
        def put_parameter(self, Name, Value, **k): self.store[Name] = Value
    s = S(); monkeypatch.setattr(policy, "_ssm", lambda: s); policy.reset_cache()
    client(ADMIN).put("/api/policy", json={"auto_review": True})
    i = mcp()
    r = client(USER).post(f"/api/mcp/{i}/usage").json()
    assert r["status"] == "APPROVED" and r["can_use"] is True
    assert "[auto-review]" in db.get_usage_request(i, USER)["reason"]
    assert db.get_usage_request(i, USER)["decided_by"] == "auto-review"


def test_deleting_the_mcp_removes_its_usage_requests(env, monkeypatch):
    class R:
        def delete_registry_record(self, **k): pass
    monkeypatch.setattr(aws, "registry_client", lambda: R())
    i = mcp(); client(USER).post(f"/api/mcp/{i}/usage")
    assert client(ADMIN).delete(f"/api/catalog/mcp/{i}").status_code == 200
    assert db.list_usage_requests(item_id=i) == []


def test_cancel_usage_removes_approval_without_consent(env):
    i = mcp()
    client(USER).post(f"/api/mcp/{i}/usage")
    client(ADMIN).post(f"/api/usage-requests/{i}/{USER}/decision", json={"decision": "APPROVED"})
    assert client(USER).get(f"/api/mcp/{i}/usage").json()["can_use"] is True
    r = client(USER).delete(f"/api/mcp/{i}/usage")                       # 동의 없이 즉시 취소
    assert r.status_code == 200 and r.json()["status"] == "NONE" and r.json()["can_use"] is False
    assert client(USER).delete(f"/api/mcp/{i}/usage").status_code == 404
    assert client(OWNER).delete(f"/api/mcp/{i}/usage").status_code == 409  # 소유자는 대상 아님
    assert client(USER).post(f"/api/mcp/{i}/usage").json()["status"] == "PENDING"  # 다시 신청 가능


def test_card_shows_usable_only_when_the_viewer_may_use_it(env):
    i = mcp()
    def usable(c): return next(x for x in c.get("/api/catalog/mcp").json()["items"] if x["id"] == i).get("usable")
    assert usable(client(USER)) is False and usable(client(OWNER)) is True and usable(client(ADMIN)) is True
    client(USER).post(f"/api/mcp/{i}/usage"); assert usable(client(USER)) is False
    client(ADMIN).post(f"/api/usage-requests/{i}/{USER}/decision", json={"decision": "APPROVED"}); assert usable(client(USER)) is True
    client(USER).delete(f"/api/mcp/{i}/usage"); assert usable(client(USER)) is False
