import pytest
from fastapi.testclient import TestClient

from application import approvals, auth, aws, catalog, db, server


class Reg:
    def __init__(self, status="PENDING_APPROVAL"):
        self.status, self.updates = status, []

    def get_registry_record(self, registryId, recordId):
        return {"recordId": recordId, "name": "n", "displayName": "n", "description": "d", "recordType": "CUSTOM",
                "status": self.status, "recordVersion": "1.0", "statusReason": "",
                "descriptors": {"custom": {"data": '{"version":"2"}'}}}

    def list_registry_records(self, registryId, **k):
        status = next((v for f in k.get("filters") or [] if f.get("name") == "status" for v in f.get("values") or []), self.status)
        return {"registryRecords": [{"recordId": "rec123456", "name": "n", "displayName": "n",
                                     "recordType": "CUSTOM", "status": status}]}

    def update_registry_record_status(self, registryId, recordId, status, statusReason):
        self.updates.append((recordId, status, statusReason))
        self.status = status


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_SIGNING_KEY", "k")
    monkeypatch.setenv("PORTAL_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("REGISTRY_ID", "REG")
    monkeypatch.setattr(auth, "_key", None)
    monkeypatch.setattr(catalog, "start_sync", lambda force=False: False)
    reg = Reg()
    monkeypatch.setattr(aws, "registry_client", lambda: reg)
    return reg


def client(email):
    c = TestClient(server.app)
    c.cookies.set(auth.COOKIE_NAME, auth.make_session(email, "N", ""))
    return c


ADMIN, USER = "e2npark@gmail.com", "a@x.com"


def test_login_required_and_admin_only(env):
    assert TestClient(server.app).get("/api/approvals").status_code == 401
    u = client(USER)
    assert u.get("/api/approvals").status_code == 403
    assert u.get("/api/approvals/rec123456").status_code == 403
    assert u.post("/api/approvals/rec123456/decision", json={"decision": "APPROVED"}).status_code == 403
    assert env.updates == []


def test_queue_and_detail(env):
    a = client(ADMIN)
    assert a.get("/api/approvals").json()["records"][0]["recordId"] == "rec123456"
    d = a.get("/api/approvals/rec123456").json()
    assert d["content"] == {"version": "2"} and d["status"] == "PENDING_APPROVAL"


def test_approve_records_reviewer_and_syncs_portal(env):
    iid = db.insert_item("prompt", "p", "", USER, {})
    db.update_item(iid, registry_record_id="rec123456", registry_status="PENDING_APPROVAL")
    r = client(ADMIN).post("/api/approvals/rec123456/decision", json={"decision": "APPROVED", "reason": "ok"})
    assert r.status_code == 200 and env.updates == [("rec123456", "APPROVED", "ok [by e2npark@gmail.com]")]
    assert db.get_item(iid)["registry_status"] == "APPROVED"


def test_reject_requires_reason(env):
    a = client(ADMIN)
    assert a.post("/api/approvals/rec123456/decision", json={"decision": "REJECTED", "reason": "  "}).status_code == 422
    assert a.post("/api/approvals/rec123456/decision", json={"decision": "REJECTED", "reason": "no"}).status_code == 200


def test_double_decision_is_refused(env):
    a = client(ADMIN)
    assert a.post("/api/approvals/rec123456/decision", json={"decision": "APPROVED"}).status_code == 200
    r = a.post("/api/approvals/rec123456/decision", json={"decision": "REJECTED", "reason": "x"})
    assert r.status_code == 409 and "APPROVED" in r.json()["detail"] and len(env.updates) == 1


def test_only_valid_transitions_and_long_reason_and_bad_id(env):
    a = client(ADMIN)
    env.status = "DRAFT"
    assert a.post("/api/approvals/rec123456/decision", json={"decision": "APPROVED"}).status_code == 409
    env.status = "PENDING_APPROVAL"
    assert a.post("/api/approvals/rec123456/decision", json={"decision": "DEPRECATED"}).status_code == 409
    assert a.post("/api/approvals/rec123456/decision", json={"decision": "APPROVED", "reason": "x" * 201}).status_code == 422
    assert a.get("/api/approvals/..%2F..%2Fetc").status_code in (404, 422)
    assert a.get("/api/approvals/a b").status_code == 422


def test_stale_link_to_deleted_record_explains_instead_of_raw_error(env, monkeypatch):
    from botocore.exceptions import ClientError
    class Gone(Reg):
        def get_registry_record(self, registryId, recordId):
            raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}}, "GetRegistryRecord")
    monkeypatch.setattr(aws, "registry_client", lambda: Gone())
    r = client(ADMIN).get("/api/approvals/rec123456")
    assert r.status_code == 404 and "더 이상 존재하지 않습니다" in r.json()["detail"] and "ResourceNotFound" not in r.json()["detail"]
