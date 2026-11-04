import os
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
    rows = db.recent_logins(10)                                                          # 저장소와 무관하게 공개 함수로 확인
    assert [r["email"] for r in rows] == ["foo@x.com", "foo@x.com"] and rows[0]["name"] == "Foo"
    assert set(rows[0]) == {"email", "name", "logged_at"} and "p/x.png" not in str(rows)   # 사진 URL 은 어디에도 없다
    if os.environ.get("PORTAL_STORE") != "dynamodb":                                     # SQLite 는 테이블 스키마까지 확인
        with db.conn() as k:
            assert [r[1] for r in k.execute("PRAGMA table_info(login_events)")] == ["id", "email", "name", "logged_at"]


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


# ── Registry 는 설치 때 만든 하나로 고정: 생성·선택·목록 API 가 없다 ──
def test_registry_cannot_be_created_listed_or_switched_from_the_api(env):
    for who in (ADMIN, USER):                                                                                   # 관리자도 불가
        c = client(who)
        assert c.post("/api/registries", json={"name": "x"}).status_code in (404, 405)
        assert c.put("/api/registries/selected", json={"registry_id": "other"}).status_code in (404, 405)
        assert c.get("/api/registries").status_code == 404 and c.get("/api/registries/defaults").status_code == 404


def test_registry_is_pinned_to_the_installed_one_and_ignores_stored_settings(env, monkeypatch):
    db.set_setting("registry_id", "SOMETHING_ELSE")                                                             # 예전에 화면에서 골랐던 값이 남아 있어도
    assert catalog.selected_registry() == "REG"                                                                 # 환경 변수(설치 값)를 쓴다
    monkeypatch.delenv("REGISTRY_ID"); monkeypatch.setattr(catalog, "_config_registry", lambda: "FROM_CONFIG")
    assert catalog.selected_registry() == "FROM_CONFIG"


def test_registry_info_is_read_only_and_reports_the_fixed_registry(env, monkeypatch):
    class C:
        def get_registry(self, registryId):
            assert registryId == "REG"
            return {"name": "ai-portal", "status": "READY", "authorizerType": "AWS_IAM", "approvalConfiguration": {"autoApproval": False}}
    monkeypatch.setattr(aws, "registry_client", lambda: C())
    r = client(USER).get("/api/registry").json()
    assert r == {"registry_id": "REG", "name": "ai-portal", "status": "READY", "authorizer": "AWS_IAM", "auto_approval": False}
    assert client(ADMIN).post("/api/registry", json={}).status_code == 405 and client(ADMIN).put("/api/registry", json={}).status_code == 405
    assert TestClient(server.app).get("/api/registry").status_code == 401


def test_registry_info_without_a_registry_is_empty_not_an_error(env, monkeypatch):
    monkeypatch.delenv("REGISTRY_ID"); monkeypatch.setattr(catalog, "_config_registry", lambda: "")
    assert client(USER).get("/api/registry").json()["registry_id"] == ""


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
