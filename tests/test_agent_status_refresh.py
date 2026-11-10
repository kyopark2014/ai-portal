"""CREATING/UPDATING 으로 남은 agent 상태를 열 때·대화할 때 AWS 에서 다시 읽어 갱신한다(폴링 없음)."""
import pytest
from fastapi.testclient import TestClient

from application import auth, aws, catalog, db, server

HN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:harness/hr_agent-MopJObRjow"
RT = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/rt_a-AbC123"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_SIGNING_KEY", "k")
    monkeypatch.setenv("PORTAL_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(auth, "_key", None)
    monkeypatch.setattr(catalog, "start_sync", lambda force=False: False)


def client():
    c = TestClient(server.app)
    c.cookies.set(auth.COOKIE_NAME, auth.make_session("a@x.com", "N", ""))
    return c


def item(arn, status, agent_type="harness"):
    return db.insert_item("agent", "ag", "", "a@x.com", {"agent_type": agent_type, "arn": arn, "status": status})


def calls(monkeypatch, harness="READY", runtime="READY"):
    seen = []
    monkeypatch.setattr(aws, "get_harness", lambda hid: seen.append(("h", hid)) or {"status": harness})
    monkeypatch.setattr(aws, "get_runtime_status", lambda rid: seen.append(("r", rid)) or runtime)
    return seen


def test_creating_harness_becomes_ready_and_is_saved(env, monkeypatch):
    seen = calls(monkeypatch, harness="READY")
    iid = item(HN, "CREATING")
    r = client().post(f"/api/catalog/agent/{iid}/status")
    assert r.status_code == 200 and r.json()["data"]["status"] == "READY"
    assert db.get_item(iid)["data"]["status"] == "READY" and seen == [("h", "hr_agent-MopJObRjow")]


def test_updating_runtime_is_refreshed_too(env, monkeypatch):
    seen = calls(monkeypatch, runtime="READY")
    iid = item(RT, "UPDATING", "runtime")
    assert client().post(f"/api/catalog/agent/{iid}/status").json()["data"]["status"] == "READY"
    assert seen == [("r", "rt_a-AbC123")]


@pytest.mark.parametrize("st", ["READY", "FAILED", "CREATE_FAILED"])
def test_stable_states_do_not_call_aws(env, monkeypatch, st):
    seen = calls(monkeypatch)
    iid = item(HN, st)
    assert client().post(f"/api/catalog/agent/{iid}/status").json()["data"]["status"] == st
    assert seen == []


def test_still_creating_stays_and_aws_error_keeps_stored_value(env, monkeypatch):
    calls(monkeypatch, harness="CREATING")
    iid = item(HN, "CREATING")
    assert client().post(f"/api/catalog/agent/{iid}/status").json()["data"]["status"] == "CREATING"
    from botocore.exceptions import ClientError
    def boom(_): raise ClientError({"Error": {"Code": "Throttling"}}, "GetHarness")
    monkeypatch.setattr(aws, "get_harness", boom)
    assert client().post(f"/api/catalog/agent/{iid}/status").json()["data"]["status"] == "CREATING"


def test_chat_saves_the_status_it_just_read(env, monkeypatch):
    calls(monkeypatch, harness="READY")
    iid = item(HN, "CREATING")
    db.put_usage_request(iid, "a@x.com", "PENDING"); db.put_usage_request(iid, "a@x.com", "APPROVED", decided_by="t")
    monkeypatch.setattr(aws, "client", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop")))   # 상태 저장 뒤의 호출은 막는다
    client().post(f"/api/agents/{iid}/chat", json={"session_id": "s" * 36, "messages": [{"role": "user", "content": "hi"}]})
    assert db.get_item(iid)["data"]["status"] == "READY"


def test_hidden_agent_is_404(env, monkeypatch):
    calls(monkeypatch)
    iid = db.insert_item("agent", "x", "", "other@x.com", {"agent_type": "harness", "arn": HN, "status": "CREATING"})
    assert client().post(f"/api/catalog/agent/{iid}/status").status_code == 404
