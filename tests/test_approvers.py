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


def _pending_item(owner=USER):
    i = db.insert_item("prompt", "p", "", owner, {"prompt_id": "P"})
    db.update_item(i, registry_id="REG", registry_record_id="rec123456", registry_status="DRAFT"); return i


def test_approver_list_for_the_request_form_contains_admins(env):
    client(ADMIN).post("/api/admins", json={"email": ADMIN2})
    assert {a["email"] for a in client(USER).get("/api/approvers").json()["approvers"]} == {ADMIN, ADMIN2}


def test_submit_with_chosen_approver_restricts_decision_to_that_person(env):
    ssm, reg = env
    client(ADMIN).post("/api/admins", json={"email": ADMIN2}); i = _pending_item(); reg.status = "DRAFT"
    r = client(USER).post(f"/api/catalog/prompt/{i}/registry/submit", json={"approver": ADMIN2})
    assert r.status_code == 200 and r.json()["approver"] == ADMIN2
    body = {"decision": "APPROVED", "reason": "ok"}
    denied = client(ADMIN).post("/api/approvals/rec123456/decision", json=body)                   # 지정되지 않은 다른 관리자
    assert denied.status_code == 403 and ADMIN2 in denied.json()["detail"] and reg.updates == []
    assert client(ADMIN2).post("/api/approvals/rec123456/decision", json=body).status_code == 200
    assert reg.updates == [("rec123456", "APPROVED")]
    assert "rec123456" not in admins.assignments()                                                  # 처리 후 지정 해제


def test_approvals_list_shows_assignee_and_who_can_decide(env):
    client(ADMIN).post("/api/admins", json={"email": ADMIN2}); i = _pending_item(); env[1].status = "DRAFT"
    client(USER).post(f"/api/catalog/prompt/{i}/registry/submit", json={"approver": ADMIN2})
    rec = client(ADMIN).get("/api/approvals").json()["records"][0]
    assert rec["approver"] == ADMIN2 and rec["can_decide"] is False
    assert client(ADMIN2).get("/api/approvals").json()["records"][0]["can_decide"] is True


def test_no_approver_means_any_admin_may_decide(env):
    client(ADMIN).post("/api/admins", json={"email": ADMIN2}); i = _pending_item(); env[1].status = "DRAFT"
    assert client(USER).post(f"/api/catalog/prompt/{i}/registry/submit", json={}).status_code == 200
    assert client(ADMIN2).post("/api/approvals/rec123456/decision", json={"decision": "APPROVED"}).status_code == 200


def test_cannot_pick_a_non_admin_as_approver(env):
    i = _pending_item(); env[1].status = "DRAFT"
    r = client(USER).post(f"/api/catalog/prompt/{i}/registry/submit", json={"approver": "friend@x.com"})
    assert r.status_code == 422 and env[1].status == "DRAFT"                                        # 요청 자체가 나가지 않는다


def test_removed_assignee_does_not_block_the_request(env):
    client(ADMIN).post("/api/admins", json={"email": ADMIN2}); i = _pending_item(); env[1].status = "DRAFT"
    client(USER).post(f"/api/catalog/prompt/{i}/registry/submit", json={"approver": ADMIN2})
    client(ADMIN).delete(f"/api/admins?email={ADMIN2}")                                             # 지정된 사람이 관리자에서 빠짐
    assert client(ADMIN).post("/api/approvals/rec123456/decision", json={"decision": "APPROVED"}).status_code == 200


def test_assignment_save_failure_does_not_lose_the_approval_request(env, monkeypatch):
    client(ADMIN).post("/api/admins", json={"email": ADMIN2}); i = _pending_item(); env[1].status = "DRAFT"
    monkeypatch.setattr(admins, "assign", lambda *a: (_ for _ in ()).throw(RuntimeError("ssm down")))
    r = client(USER).post(f"/api/catalog/prompt/{i}/registry/submit", json={"approver": ADMIN2})
    assert r.status_code == 200 and env[1].status == "PENDING_APPROVAL"


def test_cancelling_a_request_clears_its_assignment(env):
    client(ADMIN).post("/api/admins", json={"email": ADMIN2}); i = _pending_item(); env[1].status = "DRAFT"
    client(USER).post(f"/api/catalog/prompt/{i}/registry/submit", json={"approver": ADMIN2})
    assert admins.assignments().get("rec123456") == ADMIN2
    assert client(USER).post(f"/api/catalog/prompt/{i}/registry/cancel").status_code == 200       # 승인 요청 취소
    assert "rec123456" not in admins.assignments()
    env[1].status = "DRAFT"; j = _pending_item(); db.update_item(j, registry_record_id="rec999999")
    admins.assign("rec999999", ADMIN2)
    client(USER).delete(f"/api/catalog/prompt/{j}")                                                 # 항목 삭제 → record 삭제 → 지정도 정리
    assert "rec999999" not in admins.assignments()
