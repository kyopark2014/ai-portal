import json

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from application import auth, aws, catalog, db, server

ARN_H = "arn:aws:bedrock-agentcore:us-west-2:1:harness/ai_portal-X"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_SIGNING_KEY", "k")
    monkeypatch.setenv("PORTAL_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("REGISTRY_ID", "REG")
    monkeypatch.setattr(auth, "_key", None)
    monkeypatch.setattr(catalog, "start_sync", lambda force=False: False)


def rec(rid, typ, name, status, **descriptors):
    return {"recordId": rid, "name": name, "displayName": name, "description": f"desc-{name}",
            "recordType": typ, "status": status, "descriptors": descriptors}


def j(o): return json.dumps(o)


PROMPT = rec("r1", "CUSTOM", "basic_prompt", "APPROVED", custom={"data": j(
    {"type": "bedrock-prompt", "promptId": "P1", "promptArn": "arn:p/P1", "version": "3", "modelId": "m", "variables": ["x"]})})
SKILL = rec("r2", "SKILL", "docx", "APPROVED", agentSkillsDefinition={"data": j(
    {"name": "docx", "source": {"source_type": "git", "git_url": "https://github.com/o/r/tree/main/s", "subdirectory": "", "s3_uri": ""}})})
AGENT = rec("r3", "AGENT", "ai_portal", "APPROVED", a2aAgentCard={"data": j({"name": "ai_portal", "url": ARN_H})})
MCP = rec("r4", "MCP", "gw", "APPROVED", mcpServer={"data": j({"remotes": [{"type": "streamable-http", "url": "https://gw.example/mcp"}]})})


class Reg:
    def __init__(self, records): self.records = records
    def list_registry_records(self, registryId, **k):
        return {"registryRecords": [{"recordId": r["recordId"], "status": r["status"]} for r in self.records]}
    def get_registry_record(self, registryId, recordId):
        return next(r for r in self.records if r["recordId"] == recordId)


def use(monkeypatch, *records):
    reg = Reg(list(records))
    monkeypatch.setattr(aws, "registry_client", lambda: reg)
    return reg


def test_empty_db_is_restored_from_registry(env, monkeypatch):
    use(monkeypatch, PROMPT, SKILL, AGENT, MCP)
    assert catalog.sync_registry() == {"linked": 0, "created": 4, "updated": 0}
    p, s, a, m = (db.list_items(k)[0] for k in ("prompt", "skill", "agent", "mcp"))
    assert p["data"]["prompt_id"] == "P1" and p["data"]["version"] == "3" and p["registry_status"] == "APPROVED"
    assert s["data"]["git_url"] == "https://github.com/o/r/tree/main/s" and s["data"]["source_type"] == "git"
    assert a["data"]["arn"] == ARN_H and a["data"]["agent_type"] == "harness"
    assert m["data"]["endpoint"] == "https://gw.example/mcp" and m["data"]["headers"] == []        # 헤더는 복원 불가
    assert all(i["registry_record_id"] and i["owner"] == "registry" and i["registry_id"] == "REG" for i in (p, s, a, m))
    assert p["description"] == "desc-basic_prompt" and p["desc_edited"]                            # 동기화가 덮어쓰지 않는다


def test_links_to_existing_synced_item_instead_of_duplicating(env, monkeypatch):
    db.upsert_synced("prompt", "P1", "basic_prompt", "from-aws", {"prompt_id": "P1", "text": "body", "variables": []})
    db.upsert_synced("agent", "harness:ai_portal-X", "ai_portal", "", {"agent_type": "harness", "arn": ARN_H})
    db.upsert_synced("mcp", "gateway:us-east-1:g1", "gw", "", {"kind": "gateway", "endpoint": "https://gw.example/mcp"})
    use(monkeypatch, PROMPT, AGENT, MCP)
    assert catalog.sync_registry() == {"linked": 3, "created": 0, "updated": 0}
    for k in ("prompt", "agent", "mcp"):
        items = db.list_items(k)
        assert len(items) == 1 and items[0]["registry_status"] == "APPROVED" and items[0]["registry_record_id"]
    assert db.list_items("prompt")[0]["data"]["text"] == "body"                                    # 기존 데이터 보존


def test_is_idempotent_and_follows_status_changes(env, monkeypatch):
    reg = use(monkeypatch, PROMPT)
    catalog.sync_registry()
    assert catalog.sync_registry() == {"linked": 0, "created": 0, "updated": 0}                    # 두 번째는 변화 없음
    reg.records[0] = {**PROMPT, "status": "DEPRECATED"}                                           # 콘솔에서 deprecate
    assert catalog.sync_registry()["updated"] == 1 and db.list_items("prompt")[0]["registry_status"] == "DEPRECATED"
    assert len(db.list_items("prompt")) == 1


def test_owner_edits_survive_and_in_flight_records_are_skipped(env, monkeypatch):
    i = db.insert_item("prompt", "mine", "내 설명", "a@x.com", {"prompt_id": "P9"})
    db.update_item(i, registry_id="REG", registry_record_id="r1", registry_status="PENDING_APPROVAL")
    creating = rec("r5", "CUSTOM", "new", "CREATING", custom={"data": j({"type": "bedrock-prompt", "promptId": "P5"})})
    use(monkeypatch, PROMPT, creating)
    catalog.sync_registry()
    mine = db.get_item(i)
    assert mine["owner"] == "a@x.com" and mine["description"] == "내 설명" and mine["registry_status"] == "APPROVED"
    assert len(db.list_items("prompt")) == 1                                                       # CREATING 은 만들지 않는다


def test_unrestorable_records_are_ignored(env, monkeypatch):
    junk = [rec("x1", "CUSTOM", "foreign", "APPROVED", custom={"data": "not json"}),
            rec("x2", "CUSTOM", "other", "APPROVED", custom={"data": j({"type": "something-else"})}),
            rec("x3", "AGENT", "ext", "APPROVED", a2aAgentCard={"data": j({"url": "https://example.com/agent"})}),
            rec("x4", "SKILL", "nosrc", "APPROVED", agentSkillsDefinition={"data": "{}"})]
    use(monkeypatch, *junk)
    assert catalog.sync_registry() == {"linked": 0, "created": 0, "updated": 0}
    assert all(db.list_items(k) == [] for k in ("prompt", "agent", "skill", "mcp"))


def test_registry_failure_changes_nothing_and_does_not_break_full_sync(env, monkeypatch):
    db.upsert_synced("prompt", "P1", "p", "", {"prompt_id": "P1"})
    class Boom:
        def list_registry_records(self, **k):
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "ListRegistryRecords")
    monkeypatch.setattr(aws, "registry_client", lambda: Boom())
    monkeypatch.setattr(catalog, "sync_all", lambda: {"prompt": 1})
    monkeypatch.setattr(catalog, "_sync_state", {"running": True, "last": None, "error": "", "added": {}})
    catalog._run_sync()
    assert catalog._sync_state["error"] == "" and catalog._sync_state["added"] == {"prompt": 1}    # 나머지 동기화는 유효
    assert len(db.list_items("prompt")) == 1


def test_restored_items_obey_visibility_rules(env, monkeypatch):
    use(monkeypatch, PROMPT, rec("r6", "CUSTOM", "draft_p", "DRAFT", custom={"data": j({"type": "bedrock-prompt", "promptId": "P6"})}))
    catalog.sync_registry()
    c = TestClient(server.app)
    c.cookies.set(auth.COOKIE_NAME, auth.make_session("user@x.com", "U", ""))
    assert [i["name"] for i in c.get("/api/catalog/prompt").json()["items"]] == ["basic_prompt"]   # 승인된 것만
    c.cookies.set(auth.COOKIE_NAME, auth.make_session("e2npark@gmail.com", "A", ""))
    assert len(c.get("/api/catalog/prompt").json()["items"]) == 2                                  # 관리자는 전부


def test_without_registry_nothing_happens(env, monkeypatch):
    monkeypatch.delenv("REGISTRY_ID"); monkeypatch.setattr(catalog, "_config_registry", lambda: "")
    monkeypatch.setattr(aws, "registry_client", lambda: (_ for _ in ()).throw(AssertionError("no call")))
    assert catalog.sync_registry() == {"linked": 0, "created": 0, "updated": 0}
