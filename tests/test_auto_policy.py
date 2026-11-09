import json
import time

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from application import admins, auth, aws, catalog, db, policy, server

ADMIN, USER = "e2npark@gmail.com", "user@x.com"


class SSM:
    def __init__(self): self.store, self.fail_put = {}, False
    def get_parameter(self, Name):
        if Name not in self.store: raise ClientError({"Error": {"Code": "ParameterNotFound", "Message": "x"}}, "GetParameter")
        return {"Parameter": {"Value": self.store[Name]}}
    def put_parameter(self, Name, Value, **k):
        if self.fail_put: raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "PutParameter")
        self.store[Name] = Value


class Reg:
    def __init__(self): self.status, self.updates, self.submits, self.fail_update = "DRAFT", [], 0, False
    def list_registry_records(self, registryId, **kw): return {"registryRecords": []}
    def get_registry_record(self, registryId, recordId):
        return {"recordId": recordId, "name": "n", "displayName": "n", "description": "", "recordType": "CUSTOM",
                "status": self.status, "recordVersion": "1", "statusReason": "", "descriptors": {}}
    def submit_registry_record_for_approval(self, registryId, recordId):
        self.submits += 1; self.status = "PENDING_APPROVAL"; return {"status": "PENDING_APPROVAL"}
    def update_registry_record_status(self, registryId, recordId, status, statusReason):
        if self.fail_update: raise ClientError({"Error": {"Code": "InternalServerException", "Message": "x"}}, "UpdateRegistryRecordStatus")
        self.updates.append((status, statusReason)); self.status = status
    def create_registry_record(self, **kw): return {"recordArn": "arn:x/rec1", "status": "CREATING"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_SIGNING_KEY", "k"); monkeypatch.setenv("PORTAL_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN); monkeypatch.setenv("REGISTRY_ID", "REG")
    monkeypatch.setattr(auth, "_key", None); monkeypatch.setattr(catalog, "start_sync", lambda force=False: False)
    ssm, reg = SSM(), Reg()
    monkeypatch.setattr(policy, "_ssm", lambda: ssm); monkeypatch.setattr(admins, "_ssm", lambda: ssm)
    monkeypatch.setattr(aws, "registry_client", lambda: reg)
    monkeypatch.setattr(aws, "create_record", lambda rid, it: {"recordArn": "arn:x/rec1", "status": "CREATING"})
    monkeypatch.setattr(aws, "create_prompt_version", lambda *a, **k: {})
    monkeypatch.setattr(catalog.time, "sleep", lambda s: None)
    policy.reset_cache(); admins.reset_cache()
    yield ssm, reg
    policy.reset_cache()


def client(email):
    c = TestClient(server.app); c.cookies.set(auth.COOKIE_NAME, auth.make_session(email, "N", "")); return c


def wait_until(cond, timeout=5.0):
    """백그라운드 작업(Auto-approval)의 최종 결과를 기다린다. AWS 호출이 끝난 시점이 아니라 포털에 반영된 시점이 기준이다."""
    end = time.time() + timeout
    while time.time() < end:
        if cond(): return True
        time.sleep(0.01)
    return cond()


def draft_item():
    i = db.insert_item("mcp", "m", "", USER, {"endpoint": "https://e/mcp", "headers": []})
    db.update_item(i, registry_id="REG", registry_record_id="rec1", registry_status="DRAFT"); return i


# ── 스위치 자체 ──
def test_both_switches_default_to_off_and_only_admins_can_change_them(env):
    assert client(USER).get("/api/policy").json() == {"auto_approval": False, "auto_review": False}      # 일반 사용자에겐 변경자 정보를 숨긴다
    assert client(USER).put("/api/policy", json={"auto_review": True}).status_code == 403
    assert TestClient(server.app).put("/api/policy", json={"auto_review": True}).status_code == 401
    assert client(ADMIN).put("/api/policy", json={}).status_code == 422                                  # 바꿀 값이 없음
    r = client(ADMIN).put("/api/policy", json={"auto_review": True}).json()
    assert r["auto_review"] is True and r["auto_approval"] is False and r["changed_by"] == ADMIN and r["changed_at"] > 0


def test_switches_are_independent_and_persist_in_ssm(env):
    client(ADMIN).put("/api/policy", json={"auto_approval": True})
    client(ADMIN).put("/api/policy", json={"auto_review": True})
    assert json.loads(env[0].store["/ai-portal/policy"])["auto_approval"] is True          # 두 번째 변경이 첫 번째를 지우지 않는다
    policy.reset_cache()
    assert policy.get()["auto_approval"] is True and policy.get()["auto_review"] is True      # 캐시를 비워도 SSM 에서 복원
    assert client(ADMIN).get("/api/policy").json()["changed_by"] == ADMIN


def test_storage_failure_on_change_is_reported_and_leaves_the_switch_off(env):
    env[0].fail_put = True
    assert client(ADMIN).put("/api/policy", json={"auto_review": True}).status_code in (400, 403)
    assert policy.get()["auto_review"] is False


def test_unreadable_policy_means_automation_stays_off(env):
    class Broken:
        def get_parameter(self, Name): raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "GetParameter")
    policy._ssm = lambda: Broken(); policy.reset_cache()
    assert policy.get() == {**policy.DEFAULT}                                                       # 장애로 검토 없이 승인되는 일이 없다


# ── Auto-review: 승인 요청 → 바로 승인, Lambda·reviewer 불필요 ──
def test_auto_review_approves_on_request_so_the_requester_can_use_it_immediately(env):
    ssm, reg = env
    client(ADMIN).put("/api/policy", json={"auto_review": True})
    i = draft_item()
    r = client(USER).post(f"/api/catalog/mcp/{i}/registry/submit").json()
    assert r["registry_status"] == "APPROVED" and r["auto_reviewed"] is True
    assert reg.submits == 1 and reg.updates == [("APPROVED", f"[auto-review] 자동 검토 (설정: {ADMIN})")]     # 감사 기록: 사람 승인과 구분, 켠 관리자 표시
    assert db.get_item(i)["registry_status"] == "APPROVED"
    # 다른 사용자도 승인된 항목은 볼 수 있고, 요청자는 계속 소유자
    assert [x["name"] for x in client("other@x.com").get("/api/catalog/mcp").json()["items"]] == ["m"]


def test_without_auto_review_the_request_stays_pending_for_a_reviewer(env):
    ssm, reg = env
    i = draft_item()
    r = client(USER).post(f"/api/catalog/mcp/{i}/registry/submit").json()
    assert r["registry_status"] == "PENDING_APPROVAL" and r["auto_reviewed"] is False and reg.updates == []


def test_auto_review_failure_leaves_the_request_pending_for_a_human(env):
    ssm, reg = env
    client(ADMIN).put("/api/policy", json={"auto_review": True}); reg.fail_update = True
    i = draft_item()
    r = client(USER).post(f"/api/catalog/mcp/{i}/registry/submit")
    assert r.status_code == 200 and r.json()["registry_status"] == "PENDING_APPROVAL" and r.json()["auto_reviewed"] is False


def test_auto_review_replaces_reviewers_with_the_owner_and_never_overrides_a_decided_record(env):
    ssm, reg = env
    client(ADMIN).put("/api/policy", json={"auto_review": True})
    i = draft_item(); admins.add_reviewer("rec1", "rev@x.com")
    client(USER).post(f"/api/catalog/mcp/{i}/registry/submit")
    assert admins.reviewers_of("rec1") == ["user@x.com"]                                            # 이전 지정은 정리되고 소유자만 reviewer
    reg.updates.clear(); reg.status = "REJECTED"                                                    # 이미 거절된 record 는 건드리지 않는다
    assert catalog._auto_review(i) is False and reg.updates == []


def test_turning_auto_review_off_takes_effect_immediately(env):
    ssm, reg = env
    client(ADMIN).put("/api/policy", json={"auto_review": True}); client(ADMIN).put("/api/policy", json={"auto_review": False})
    i = draft_item()
    assert client(USER).post(f"/api/catalog/mcp/{i}/registry/submit").json()["registry_status"] == "PENDING_APPROVAL"


# ── Auto-approval: 등록하면 승인 요청까지 자동 ──
def test_auto_approval_submits_right_after_registration(env):
    ssm, reg = env
    client(ADMIN).put("/api/policy", json={"auto_approval": True})
    reg.status = "DRAFT"
    r = client(USER).post("/api/mcp", json={"name": "m2", "endpoint": "https://e.example/mcp"}).json()
    assert r["auto_approval"] is True
    assert wait_until(lambda: db.get_item(r["id"])["registry_status"] == "PENDING_APPROVAL")           # 포털에도 반영될 때까지
    assert reg.submits == 1 and reg.status == "PENDING_APPROVAL" and reg.updates == []                  # 요청까지만, 승인은 reviewer 몫


def test_both_on_means_register_and_use_immediately(env):
    ssm, reg = env
    client(ADMIN).put("/api/policy", json={"auto_approval": True, "auto_review": True}); reg.status = "DRAFT"
    iid = client(USER).post("/api/mcp", json={"name": "m3", "endpoint": "https://e.example/mcp"}).json()["id"]
    assert wait_until(lambda: db.get_item(iid)["registry_status"] == "APPROVED")                       # 최종 결과(포털 반영)를 기다린다
    assert reg.updates[0][0] == "APPROVED" and "[auto-review]" in reg.updates[0][1]


def test_auto_approval_off_does_not_submit_on_registration(env):
    ssm, reg = env
    iid = client(USER).post("/api/mcp", json={"name": "m4", "endpoint": "https://e.example/mcp"}).json()["id"]
    assert not wait_until(lambda: reg.submits > 0, timeout=0.3)                                         # 일부러 기다려도 요청이 나가지 않는다
    assert db.get_item(iid)["registry_status"] in ("CREATING", "DRAFT")
