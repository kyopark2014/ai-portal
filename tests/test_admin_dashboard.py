import pytest
from fastapi.testclient import TestClient

from application import admins, auth, aws, catalog, db, server

ADMIN, USER, OTHER = "e2npark@gmail.com", "user@x.com", "other@x.com"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_SIGNING_KEY", "k"); monkeypatch.setenv("PORTAL_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN); monkeypatch.setenv("REGISTRY_ID", "REG")
    monkeypatch.setattr(auth, "_key", None); monkeypatch.setattr(catalog, "start_sync", lambda force=False: False)
    monkeypatch.setattr(admins, "extra", lambda: set())
    admins.reset_cache()


def client(email):
    c = TestClient(server.app); c.cookies.set(auth.COOKIE_NAME, auth.make_session(email, "N", "")); return c


class Reg:
    def __init__(self, recs): self.recs = recs
    def list_registry_records(self, registryId, **k): return {"registryRecords": self.recs}


def mk(kind, name, owner, status):
    i = db.insert_item(kind, name, "", owner, {})
    if status: db.update_item(i, registry_status=status)
    return i


# ── 접속 기록 ──
def test_login_is_recorded_with_email_and_name_only(env, monkeypatch):
    monkeypatch.setattr(auth, "verify_google_credential", lambda c: {"email": "Foo@X.com", "name": "Foo", "picture": "https://p/x.png"})
    c = TestClient(server.app)
    assert c.post("/api/auth/google", json={"credential": "x"}).status_code == 200
    c.post("/api/auth/google", json={"credential": "x"})
    with db.conn() as k:
        cols = [r[1] for r in k.execute("PRAGMA table_info(login_events)")]
        rows = k.execute("SELECT * FROM login_events").fetchall()
    assert cols == ["id", "email", "name", "logged_at"] and "picture" not in cols          # 사진 URL 은 저장하지 않는다
    assert [r["email"] for r in rows] == ["foo@x.com", "foo@x.com"] and rows[0]["name"] == "Foo"


def test_recording_failure_never_blocks_login(env, monkeypatch):
    monkeypatch.setattr(auth, "verify_google_credential", lambda c: {"email": "a@x.com", "name": "A"})
    monkeypatch.setattr(db, "record_login", lambda *a: (_ for _ in ()).throw(RuntimeError("disk full")))
    assert TestClient(server.app).post("/api/auth/google", json={"credential": "x"}).status_code == 200


def test_first_login_flag_and_counts(env):
    assert db.record_login("a@x.com", "A") is True and db.record_login("A@x.com", "A") is False
    assert db.login_totals() == {"total_logins": 2, "unique_users": 1, "active_7d": 1}


# ── 대시보드 접근 제어 ──
def test_dashboard_is_admin_only(env):
    assert TestClient(server.app).get("/api/admin/dashboard").status_code == 401
    assert client(USER).get("/api/admin/dashboard").status_code == 403


def test_dashboard_shows_users_with_per_type_and_per_status_counts(env, monkeypatch):
    db.record_login(USER, "사용자"); db.record_login(USER, "사용자"); db.record_login(OTHER, "다른")
    mk("prompt", "p1", USER, "APPROVED"); mk("prompt", "p2", USER, "PENDING_APPROVAL"); mk("mcp", "m1", USER, "REJECTED")
    mk("skill", "s1", OTHER, None); mk("agent", "a1", "registry", "APPROVED")
    monkeypatch.setattr(aws, "registry_client", lambda: Reg([
        {"descriptorType": "CUSTOM", "status": "APPROVED"}, {"descriptorType": "CUSTOM", "status": "PENDING_APPROVAL"},
        {"descriptorType": "MCP", "status": "REJECTED"}, {"descriptorType": "AGENT_SKILLS", "status": "APPROVED"},
        {"descriptorType": "A2A", "status": "DRAFT"}]))
    d = client(ADMIN).get("/api/admin/dashboard").json()
    assert d["totals"]["unique_users"] == 2 and d["totals"]["total_logins"] == 3
    u = {x["email"]: x for x in d["users"]}
    assert u[USER]["login_count"] == 2 and u[USER]["total"] == 3 and u[USER]["name"] == "사용자"
    assert u[USER]["kinds"] == {"prompt": {"APPROVED": 1, "PENDING_APPROVAL": 1}, "mcp": {"REJECTED": 1}}          # type 별 · 상태별
    assert u[USER]["statuses"] == {"APPROVED": 1, "PENDING_APPROVAL": 1, "REJECTED": 1}
    assert u[OTHER]["kinds"] == {"skill": {"NOT_REGISTERED": 1}} and u[OTHER]["login_count"] == 1
    assert "registry" in u and u["registry"]["login_count"] == 0                                                  # 로그인 안 한 소유자(복원 항목)도 보인다
    assert d["registry"]["by_type"] == {"prompt": {"APPROVED": 1, "PENDING_APPROVAL": 1}, "mcp": {"REJECTED": 1},
                                        "skill": {"APPROVED": 1}, "agent": {"DRAFT": 1}} and d["registry"]["total"] == 5
    assert u[ADMIN]["is_admin"] if ADMIN in u else True


def test_dashboard_survives_registry_outage(env, monkeypatch):
    db.record_login(USER, "U")
    monkeypatch.setattr(aws, "registry_client", lambda: (_ for _ in ()).throw(RuntimeError("down")) if False else Reg(None))
    class Boom:
        def list_registry_records(self, **k):
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "ListRegistryRecords")
    monkeypatch.setattr(aws, "registry_client", lambda: Boom())
    d = client(ADMIN).get("/api/admin/dashboard")
    assert d.status_code == 200 and d.json()["users"] and "AccessDenied" in d.json()["registry"]["error"]          # 접속 통계는 계속 보인다


def test_recent_logins_are_newest_first_and_capped(env):
    for i in range(40): db.record_login(f"u{i}@x.com", f"U{i}")
    r = db.recent_logins(30)
    assert len(r) == 30 and r[0]["email"] == "u39@x.com" and "picture" not in r[0]


# ── 관리자의 내 레코드(전체) ──
def test_admin_can_list_everyones_records_but_users_cannot(env):
    mk("prompt", "mine", ADMIN, "APPROVED"); mk("prompt", "theirs", USER, "PENDING_APPROVAL"); mk("mcp", "synced", "aws", None)
    assert [i["name"] for i in client(ADMIN).get("/api/my/records").json()["items"]] == ["mine"]                 # 기본은 내 것
    all_ = client(ADMIN).get("/api/my/records?scope=all").json()
    assert {i["name"] for i in all_["items"]} == {"mine", "theirs", "synced"} and all_["total"] == 3
    assert all_["counts"] == {"APPROVED": 1, "PENDING_APPROVAL": 1, "NOT_REGISTERED": 1}
    assert client(USER).get("/api/my/records?scope=all").status_code == 403                                       # 일반 사용자는 전체 불가
    assert client(USER).get("/api/my/records").status_code == 200


def test_all_scope_still_masks_headers(env):
    db.insert_item("mcp", "m", "", USER, {"endpoint": "https://e/mcp", "headers": [{"key": "Authorization", "value": "SECRET"}]})
    assert "SECRET" not in client(ADMIN).get("/api/my/records?scope=all").text


# ── 4) Registry 생성 기본값 ──
def test_registry_defaults_are_iam_and_no_auto_approval(env):
    assert client(USER).get("/api/registries/defaults").json() == {"authorizer": "AWS_IAM", "auto_approval": False}
    seen = {}
    class C:
        def create_registry(self, **kw): seen.update(kw); return {"registryArn": "arn:x"}
    import application.aws as a
    a_orig = a.registry_client; a.registry_client = lambda: C()
    try:
        assert client(ADMIN).post("/api/registries", json={"name": "r1"}).status_code == 200
    finally:
        a.registry_client = a_orig
    assert seen["authorizerType"] == "AWS_IAM" and seen["approvalConfiguration"] == {"autoApproval": False} and "authorizerConfiguration" not in seen


def test_jwt_registry_requires_a_discovery_url_and_non_admins_cannot_create(env, monkeypatch):
    seen = {}
    class C:
        def create_registry(self, **kw): seen.update(kw); return {"registryArn": "arn:x"}
    monkeypatch.setattr(aws, "registry_client", lambda: C())
    a = client(ADMIN)
    assert a.post("/api/registries", json={"name": "j", "authorizer": "CUSTOM_JWT"}).status_code == 422          # URL 없음
    assert a.post("/api/registries", json={"name": "j", "authorizer": "CUSTOM_JWT", "jwt_discovery_url": "file:///etc/x"}).status_code == 422
    ok = a.post("/api/registries", json={"name": "j", "authorizer": "CUSTOM_JWT", "auto_approval": True,
                                         "jwt_discovery_url": "https://idp.example/.well-known/openid-configuration", "jwt_allowed_clients": ["c1"]})
    assert ok.status_code == 200 and seen["authorizerConfiguration"]["customJWTAuthorizer"] == {
        "discoveryUrl": "https://idp.example/.well-known/openid-configuration", "allowedClients": ["c1"]}
    assert client(USER).post("/api/registries", json={"name": "x"}).status_code == 403


# ── 1) harness 설명 ──
def test_known_harness_gets_a_description_and_edits_are_kept(env, monkeypatch):
    monkeypatch.setattr(aws, "list_all_prompts", lambda: []); monkeypatch.setattr(aws, "list_gateways", lambda reg=None: [])
    monkeypatch.setattr(aws, "list_runtimes", lambda: [])
    monkeypatch.setattr(aws, "list_harnesses", lambda: [{"harnessId": "ai_portal-X", "harnessName": "ai_portal", "arn": "arn:h", "status": "READY"},
                                                        {"harnessId": "other-Y", "harnessName": "other", "arn": "arn:o", "status": "READY"}])
    monkeypatch.setattr(aws, "get_harness", lambda hid: {"systemPrompt": [{"text": "긴 시스템 프롬프트"}], "model": {}})
    catalog.sync_all()
    by = {i["name"]: i for i in db.list_items("agent")}
    assert by["ai_portal"]["description"] == "AI Portal 운영을 위한 Agent" and by["other"]["description"] == ""
    client(ADMIN).patch(f"/api/catalog/agent/{by['ai_portal']['id']}/description", json={"description": "내가 고친 설명"})
    catalog.sync_all()
    assert {i["name"]: i["description"] for i in db.list_items("agent")}["ai_portal"] == "내가 고친 설명"        # 사용자 편집 보존
