import json

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from application import admins, approvals, auth, aws, catalog, db, server

ADMIN, ADMIN2, USER = "e2npark@gmail.com", "second@x.com", "user@x.com"


class FakeSSM:
    """SSM Parameter Store 흉내: put/get, ParameterNotFound, 장애 주입."""
    def __init__(self): self.store, self.fail = {}, None
    def get_parameter(self, Name):
        if self.fail: raise ClientError({"Error": {"Code": self.fail, "Message": "x"}}, "GetParameter")
        if Name not in self.store: raise ClientError({"Error": {"Code": "ParameterNotFound", "Message": "x"}}, "GetParameter")
        return {"Parameter": {"Value": self.store[Name]}}
    def put_parameter(self, Name, Value, **k): self.store[Name] = Value


class Reg:
    def __init__(self, status="PENDING_APPROVAL"): self.status, self.updates = status, []
    def get_registry_record(self, registryId, recordId):
        return {"recordId": recordId, "name": "n", "description": "", "descriptorType": "CUSTOM", "status": self.status,
                "recordVersion": "1", "statusReason": "", "descriptors": {}}
    def list_registry_records(self, registryId, status, **k):
        return {"registryRecords": [{"recordId": "rec123456", "name": "n", "descriptorType": "CUSTOM", "status": status}]}
    def update_registry_record_status(self, registryId, recordId, status, statusReason):
        self.updates.append((recordId, status)); self.status = status
    def delete_registry_record(self, registryId, recordId): self.deleted = recordId
    def submit_registry_record_for_approval(self, registryId, recordId):
        self.status = "PENDING_APPROVAL"; return {"status": "PENDING_APPROVAL"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_SIGNING_KEY", "k"); monkeypatch.setenv("PORTAL_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN); monkeypatch.setenv("REGISTRY_ID", "REG")
    monkeypatch.setattr(auth, "_key", None); monkeypatch.setattr(catalog, "start_sync", lambda force=False: False)
    ssm, reg = FakeSSM(), Reg()
    monkeypatch.setattr(admins, "_ssm", lambda: ssm); monkeypatch.setattr(aws, "registry_client", lambda: reg)
    admins.reset_cache()
    yield ssm, reg
    admins.reset_cache()


def client(email):
    c = TestClient(server.app); c.cookies.set(auth.COOKIE_NAME, auth.make_session(email, "N", "")); return c


def test_only_admins_manage_the_admin_list(env):
    assert client(USER).get("/api/admins").status_code == 403
    assert client(USER).post("/api/admins", json={"email": "x@y.com"}).status_code == 403
    assert TestClient(server.app).get("/api/admins").status_code == 401


def test_added_admin_becomes_admin_immediately_and_seed_cannot_be_removed(env):
    a = client(ADMIN)
    assert a.post("/api/admins", json={"email": " Second@X.com "}).status_code == 200            # 대소문자·공백 정규화
    assert client(ADMIN2).get("/api/approvals").status_code == 200                                # 다음 요청부터 관리자
    lst = a.get("/api/admins").json()["admins"]
    assert {"email": ADMIN, "removable": False} in lst and {"email": ADMIN2, "removable": True} in lst
    assert a.delete(f"/api/admins?email={ADMIN}").status_code == 422                              # 최초 관리자 보호
    assert client(ADMIN2).delete(f"/api/admins?email={ADMIN2}").status_code == 422               # 자기 자신 삭제 불가
    assert a.delete(f"/api/admins?email={ADMIN2}").status_code == 200
    assert client(ADMIN2).get("/api/approvals").status_code == 403                                # 제거 즉시 권한 상실


@pytest.mark.parametrize("bad", ["", "no-at-sign", "a@b", "a b@c.com", "a@b.com,c@d.com", "<x>@y.com", "a@b.com;c@d.com"])
def test_invalid_emails_rejected(env, bad):
    assert client(ADMIN).post("/api/admins", json={"email": bad or " "}).status_code in (422,)


def test_duplicate_and_unknown_removal(env):
    a = client(ADMIN)
    assert a.post("/api/admins", json={"email": ADMIN}).status_code == 422                        # 이미 관리자
    a.post("/api/admins", json={"email": ADMIN2})
    assert a.post("/api/admins", json={"email": ADMIN2}).status_code == 422
    assert a.delete("/api/admins?email=nobody@x.com").status_code == 422


def test_ssm_outage_falls_back_to_seed_admins_only(env):
    ssm, _ = env
    client(ADMIN).post("/api/admins", json={"email": ADMIN2}); admins.reset_cache()
    ssm.fail = "AccessDeniedException"
    assert client(ADMIN).get("/api/approvals").status_code == 200                                 # 최초 관리자는 계속 동작
    assert client(ADMIN2).get("/api/approvals").status_code == 403                                # 추가 관리자는 확인 불가 → 막는다(fail closed)


def _pending_item(owner=USER, rec="rec123456"):
    i = db.insert_item("prompt", "p", "", owner, {"prompt_id": "P"})
    db.update_item(i, registry_id="REG", registry_record_id=rec, registry_status="PENDING_APPROVAL"); return i


REV = "reviewer@x.com"


def test_admin_adds_a_reviewer_to_one_record_and_only_that_record(env):
    _pending_item(rec="rec123456"); _pending_item(rec="rec777777")
    r = client(ADMIN).post("/api/reviewers", json={"record_id": "rec123456", "email": " Reviewer@X.com "})
    assert r.status_code == 200 and r.json()["reviewers"] == [REV]                                   # 정규화
    body = {"decision": "APPROVED", "reason": "ok"}
    assert client(REV).post("/api/approvals/rec777777/decision", json=body).status_code == 404       # 지정되지 않은 record 는 존재도 안 보인다
    assert env[1].updates == []
    assert client(REV).post("/api/approvals/rec123456/decision", json=body).status_code == 200
    assert env[1].updates == [("rec123456", "APPROVED")]


def test_admins_can_always_decide_regardless_of_reviewers(env):
    _pending_item(); client(ADMIN).post("/api/reviewers", json={"record_id": "rec123456", "email": REV})
    assert client(ADMIN).post("/api/approvals/rec123456/decision", json={"decision": "APPROVED"}).status_code == 200


def test_reviewer_sees_only_their_records_and_non_reviewers_nothing(env):
    _pending_item(rec="rec123456"); _pending_item(rec="rec777777")
    client(ADMIN).post("/api/reviewers", json={"record_id": "rec123456", "email": REV})
    assert client(USER).get("/api/approvals").status_code == 403                                      # 어느 record 의 reviewer 도 아님
    ids = {r["recordId"] for r in client(REV).get("/api/approvals").json()["records"]}
    assert ids == {"rec123456"} and client(REV).get("/api/approvals/rec777777").status_code == 404
    rec = client(REV).get("/api/approvals").json()["records"][0]
    assert rec["can_decide"] is True and rec["reviewers"] == [REV]
    assert {r["recordId"] for r in client(ADMIN).get("/api/approvals").json()["records"]} == {"rec123456"}   # 관리자는 전부(여기선 조회 목록 1개)


def test_reviewer_cannot_deprecate_or_manage_reviewers_or_admins(env):
    _pending_item(); env[1].status = "APPROVED"
    client(ADMIN).post("/api/reviewers", json={"record_id": "rec123456", "email": REV})
    r = client(REV)
    assert r.post("/api/approvals/rec123456/decision", json={"decision": "DEPRECATED"}).status_code == 403
    assert r.post("/api/reviewers", json={"record_id": "rec123456", "email": "x@y.com"}).status_code == 403   # 권한 상승 불가
    assert r.post("/api/admins", json={"email": "x@y.com"}).status_code == 403
    assert r.get("/api/admin/dashboard").status_code == 403 and r.get("/api/reviewers/users").status_code == 403


def test_reviewer_rules_and_limits(env):
    a = client(ADMIN)
    assert a.post("/api/reviewers", json={"record_id": "rec123456", "email": ADMIN}).status_code == 422          # 관리자는 이미 전체 권한
    assert a.post("/api/reviewers", json={"record_id": "rec123456", "email": "not-an-email"}).status_code == 422
    assert a.post("/api/reviewers", json={"record_id": "../x", "email": REV}).status_code == 422
    a.post("/api/reviewers", json={"record_id": "rec123456", "email": REV})
    assert a.post("/api/reviewers", json={"record_id": "rec123456", "email": REV}).status_code == 422             # 중복
    for i in range(admins.MAX_REVIEWERS_PER_RECORD - 1):
        assert a.post("/api/reviewers", json={"record_id": "rec123456", "email": f"u{i}@x.com"}).status_code == 200
    assert a.post("/api/reviewers", json={"record_id": "rec123456", "email": "overflow@x.com"}).status_code == 422   # 상한


def test_remove_reviewer_revokes_access_immediately(env):
    _pending_item(); a = client(ADMIN)
    a.post("/api/reviewers", json={"record_id": "rec123456", "email": REV})
    assert client(REV).get("/api/approvals").status_code == 200
    assert a.delete(f"/api/reviewers?record_id=rec123456&email={REV}").json()["reviewers"] == []
    assert client(REV).get("/api/approvals").status_code == 403 and a.delete(f"/api/reviewers?record_id=rec123456&email={REV}").status_code == 422


def test_ssm_outage_means_only_admins_can_decide(env):
    ssm, _ = env
    _pending_item(); client(ADMIN).post("/api/reviewers", json={"record_id": "rec123456", "email": REV}); admins.reset_cache()
    ssm.fail = "AccessDeniedException"
    assert client(REV).get("/api/approvals").status_code == 403                                       # 확인 불가 → 권한 부여하지 않는다
    assert client(ADMIN).get("/api/approvals").status_code == 200


def test_reviewer_search_finds_people_by_email_or_name_and_hides_synthetic_owners(env):
    db.record_login("kim.chulsoo@x.com", "김철수"); db.record_login("lee@x.com", "이영희"); db.record_login("park@x.com", "Park")
    db.insert_item("prompt", "p", "", "registry", {}); db.insert_item("prompt", "q", "", "aws", {})
    a = client(ADMIN)
    emails = lambda q: [u["email"] for u in a.get(f"/api/reviewers/users?q={q}").json()["users"]]
    assert set(emails("")) >= {"kim.chulsoo@x.com", "lee@x.com", "park@x.com"} and "aws" not in emails("") and "registry" not in emails("")
    assert emails("김철") == ["kim.chulsoo@x.com"] and emails("LEE") == ["lee@x.com"] and emails("chulsoo") == ["kim.chulsoo@x.com"]
    assert emails("nobody-here") == []
    assert len(a.get("/api/reviewers/users?limit=2").json()["users"]) == 2
    assert client(USER).get("/api/reviewers/users").status_code == 403


def test_removing_or_cancelling_a_record_clears_its_reviewers(env):
    i = _pending_item(); client(ADMIN).post("/api/reviewers", json={"record_id": "rec123456", "email": REV})
    assert admins.reviewers_of("rec123456") == [REV]
    env[1].status = "PENDING_APPROVAL"
    assert client(USER).post(f"/api/catalog/prompt/{i}/registry/cancel").status_code == 200
    assert admins.reviewers_of("rec123456") == []


def test_request_form_no_longer_picks_an_approver(env):
    i = _pending_item(); env[1].status = "DRAFT"; db.update_item(i, registry_status="DRAFT")
    assert client(USER).post(f"/api/catalog/prompt/{i}/registry/submit").status_code == 200
    assert client(USER).get("/api/approvers").status_code == 404                                      # 요청자가 고르는 승인자 목록은 없다


def test_me_reports_whether_the_approval_menu_should_show(env):
    _pending_item(); client(ADMIN).post("/api/reviewers", json={"record_id": "rec123456", "email": REV})
    me = lambda e: client(e).get("/api/me").json()
    assert me(ADMIN)["can_review"] is True and me(REV)["can_review"] is True and me(REV)["reviewing"] == 1
    assert me(USER)["can_review"] is False and me(USER)["reviewing"] == 0


def test_me_still_works_when_the_reviewer_store_is_down(env):
    env[0].fail = "AccessDeniedException"; admins.reset_cache()
    r = client(USER).get("/api/me")
    assert r.status_code == 200 and r.json()["can_review"] is False                                   # 로그인은 막지 않고 권한도 넓히지 않는다
