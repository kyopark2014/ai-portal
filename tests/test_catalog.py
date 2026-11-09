import pytest
from fastapi.testclient import TestClient

from application import auth, aws, catalog, db, server


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_SIGNING_KEY", "k")
    monkeypatch.setenv("PORTAL_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(auth, "_key", None)
    monkeypatch.setattr(catalog, "start_sync", lambda force=False: False)
    monkeypatch.delenv("REGISTRY_ID", raising=False)
    monkeypatch.setattr(catalog, "_config_registry", lambda: "")   # config.json 의 실제 Registry 를 쓰지 않는다
    def _no_aws(*a, **k):
        raise AssertionError("테스트가 실제 AWS 를 호출했습니다")
    monkeypatch.setattr(aws, "client", _no_aws)


def client(email):
    c = TestClient(server.app)
    c.cookies.set(auth.COOKIE_NAME, auth.make_session(email, "N", ""))
    return c


def test_requires_login(env):
    assert TestClient(server.app).get("/api/catalog/mcp").status_code == 401


def test_mcp_headers_masked_and_ownership(env):
    a, b, admin = client("a@x.com"), client("b@x.com"), client("e2npark@gmail.com")
    r = a.post("/api/mcp", json={"name": "m", "endpoint": "https://e.example/mcp",
                                 "headers": [{"key": "Authorization", "value": "Bearer SECRET"}]})
    assert r.status_code == 200 and "SECRET" not in r.text
    iid = r.json()["id"]
    assert "SECRET" not in b.get("/api/catalog/mcp").text
    assert b.delete(f"/api/catalog/mcp/{iid}").status_code == 404          # 승인 전 남의 항목: 존재 자체를 숨긴다
    db.update_item(iid, registry_status="APPROVED")
    assert b.delete(f"/api/catalog/mcp/{iid}").status_code == 403          # 승인된 남의 항목: 보이지만 수정/삭제는 불가
    assert admin.delete(f"/api/catalog/mcp/{iid}").status_code == 200


def test_mcp_rejects_bad_url(env):
    assert client("a@x.com").post("/api/mcp", json={"name": "m", "endpoint": "file:///etc/passwd"}).status_code == 422


def test_skill_default_description_and_edit(env):
    a = client("a@x.com")
    r = a.post("/api/skills", json={"name": "s", "source_type": "git", "git_url": "https://github.com/o/r"}).json()
    assert "github.com/o/r" in r["description"] and not r["desc_edited"]
    r2 = a.patch(f"/api/catalog/skill/{r['id']}/description", json={"description": "내 설명"}).json()
    assert r2["description"] == "내 설명" and r2["desc_edited"]
    assert a.post("/api/skills", json={"name": "s", "source_type": "s3", "s3_uri": "bad"}).status_code == 422


def test_sync_preserves_edited_description(env):
    db.upsert_synced("agent", "runtime:1", "r", "orig", {"agent_type": "runtime"})
    it = db.list_items("agent")[0]
    client("e2npark@gmail.com").patch(f"/api/catalog/agent/{it['id']}/description", json={"description": "mine"})
    db.upsert_synced("agent", "runtime:1", "r2", "aws-new", {"status": "READY"})
    it = db.list_items("agent")[0]
    assert it["description"] == "mine" and it["name"] == "r2"
    assert db.prune_synced("agent", set()) == 1


def test_prompt_create_uses_bedrock(env, monkeypatch):
    seen = {}
    def fake(name, desc, text, model):
        seen.update(name=name, text=text)
        return {"id": "P1", "arn": "arn:p", "version": "DRAFT", "defaultVariant": "default",
                "variants": [{"name": "default", "modelId": model,
                              "templateConfiguration": {"text": {"text": text}}}]}
    monkeypatch.setattr(aws, "create_prompt", fake)
    a = client("a@x.com")
    r = a.post("/api/prompts", json={"name": "p", "text": "Hi {{who}}", "model_id": "m"}).json()
    assert r["data"]["variables"] == ["who"] and r["ext_id"] == "P1"
    assert aws.render_prompt("Hi {{who}}", {"who": "Bob"}) == "Hi Bob"


def test_prompt_from_get_chat_and_record_params():
    p = {"id": "i", "arn": "a", "defaultVariant": "v", "variants": [{"name": "v", "templateConfiguration": {
        "chat": {"system": [{"text": "sys"}], "messages": [{"content": [{"text": "hello {{x}}"}]}]}}}]}
    assert aws.prompt_from_get(p)["variables"] == ["x"]
    for kind, data in (("mcp", {"endpoint": "https://e/mcp"}), ("agent", {"agent_type": "runtime"}),
                       ("skill", {"source_type": "git"}), ("prompt", {"text": "t", "prompt_id": "P", "arn": "a", "version": "1"})):
        out = aws.record_params({"kind": kind, "name": "n", "description": "d", "data": data})
        assert out["recordType"] in ("MCP", "AGENT", "SKILL", "CUSTOM")


def test_prompt_registry_record_is_pointer_only():
    import json
    out = aws.record_params({"kind": "prompt", "name": "n", "description": "d", "data": {
        "prompt_id": "P1", "arn": "arn:aws:bedrock:r:1:prompt/P1", "version": "3", "text": "SECRET BODY {{x}}",
        "model_id": "m", "variables": ["x"]}})
    body = json.loads(out["descriptors"]["custom"]["data"])
    assert out["recordType"] == "CUSTOM" and body["version"] == "3"
    assert body["versionArn"] == "arn:aws:bedrock:r:1:prompt/P1:3" and "SECRET BODY" not in json.dumps(body)


def test_versioned_arn_is_normalized():
    p = {"id": "P1", "arn": "arn:aws:bedrock:r:1:prompt/P1:2", "version": "2", "defaultVariant": "v",
         "variants": [{"name": "v", "templateConfiguration": {"text": {"text": "x"}}}]}
    d = aws.prompt_from_get(p)
    assert d["arn"] == "arn:aws:bedrock:r:1:prompt/P1"
    import json
    b = json.loads(aws.record_params({"kind": "prompt", "name": "n", "description": "", "data": d})
                   ["descriptors"]["custom"]["data"])
    assert b["versionArn"] == "arn:aws:bedrock:r:1:prompt/P1:2"


def _fake_prompt(monkeypatch, calls):
    monkeypatch.setattr(aws, "create_prompt", lambda n, d, t, m: calls.append("create_prompt") or {
        "id": "P1", "arn": "arn:p/P1", "version": "DRAFT", "defaultVariant": "default",
        "variants": [{"name": "default", "templateConfiguration": {"text": {"text": t}}}]})
    monkeypatch.setattr(aws, "create_prompt_version", lambda pid, d="": calls.append("version") or {
        "id": "P1", "arn": "arn:p/P1", "version": "1", "defaultVariant": "default",
        "variants": [{"name": "default", "templateConfiguration": {"text": {"text": "hi"}}}]})
    monkeypatch.setattr(aws, "create_record", lambda rid, it: calls.append("record") or
                        {"recordArn": "arn:x/rec1", "status": "CREATING"})


def test_create_prompt_registers_everywhere_in_one_call(env, monkeypatch):
    monkeypatch.setenv("REGISTRY_ID", "REG")
    calls = []
    _fake_prompt(monkeypatch, calls)
    r = client("a@x.com").post("/api/prompts", json={"name": "p", "text": "hi"}).json()
    assert calls == ["create_prompt", "version", "record"]
    assert r["registry_record_id"] == "rec1" and r["data"]["version"] == "1" and "registry_error" not in r


def test_create_mcp_registers_in_one_call(env, monkeypatch):
    monkeypatch.setenv("REGISTRY_ID", "REG")
    monkeypatch.setattr(aws, "create_record", lambda rid, it: {"recordArn": "arn:x/recM", "status": "CREATING"})
    r = client("a@x.com").post("/api/mcp", json={"name": "m", "endpoint": "https://e.example/mcp"}).json()
    assert r["registry_record_id"] == "recM"


def test_registry_failure_keeps_item_and_reports(env, monkeypatch):
    from botocore.exceptions import ClientError
    monkeypatch.setenv("REGISTRY_ID", "REG")
    def boom(rid, it):
        raise ClientError({"Error": {"Code": "ValidationException", "Message": "bad"}}, "CreateRegistryRecord")
    monkeypatch.setattr(aws, "create_record", boom)
    a = client("a@x.com")
    r = a.post("/api/skills", json={"name": "s", "source_type": "git", "git_url": "https://github.com/o/r"}).json()
    assert "ValidationException" in r["registry_error"] and not r["registry_record_id"]
    assert len(a.get("/api/catalog/skill").json()["items"]) == 1     # 항목은 보존
    monkeypatch.setattr(aws, "create_record", lambda rid, it: {"recordArn": "arn:x/ok", "status": "CREATING"})
    assert a.post(f"/api/catalog/skill/{r['id']}/register").json()["registry_record_id"] == "ok"  # 재시도


def test_no_registry_configured_saves_locally(env, monkeypatch):
    monkeypatch.delenv("REGISTRY_ID", raising=False)
    monkeypatch.setattr(catalog, "_config_registry", lambda: "")
    r = client("a@x.com").post("/api/agents", json={"name": "a"}).json()
    assert r["registry_error"] and not r["registry_record_id"]


class _Reg:
    def __init__(self, exc=None):
        self.exc, self.deleted = exc, []

    def delete_registry_record(self, registryId, recordId):
        if self.exc:
            raise self.exc
        self.deleted.append(recordId)


def _ce(code):
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": code, "Message": "m"}}, "DeleteRegistryRecord")


def _registered_prompt(env, monkeypatch, order):
    monkeypatch.setenv("REGISTRY_ID", "REG")
    _fake_prompt(monkeypatch, [])
    monkeypatch.setattr(aws, "delete_prompt", lambda pid: order.append("prompt"))
    a = client("a@x.com")
    return a, a.post("/api/prompts", json={"name": "p", "text": "hi"}).json()["id"]


def test_delete_removes_record_then_prompt(env, monkeypatch):
    order, reg = [], _Reg()
    a, pid = _registered_prompt(env, monkeypatch, order)
    monkeypatch.setattr(aws, "registry_client", lambda: reg)
    r = a.delete(f"/api/catalog/prompt/{pid}").json()
    assert reg.deleted == ["rec1"] and order == ["prompt"] and r["deleted"] == ["registry_record", "prompt"]
    assert a.get("/api/catalog/prompt").json()["items"] == []


def test_delete_aborts_when_record_busy(env, monkeypatch):
    order = []
    a, pid = _registered_prompt(env, monkeypatch, order)
    monkeypatch.setattr(aws, "registry_client", lambda: _Reg(_ce("ConflictException")))
    resp = a.delete(f"/api/catalog/prompt/{pid}")
    assert resp.status_code == 409 and "아무것도 삭제되지 않았습니다" in resp.json()["detail"]
    assert order == [] and len(a.get("/api/catalog/prompt").json()["items"]) == 1   # prompt 도 항목도 보존


def test_delete_skips_record_already_gone(env, monkeypatch):
    order = []
    a, pid = _registered_prompt(env, monkeypatch, order)
    monkeypatch.setattr(aws, "registry_client", lambda: _Reg(_ce("ResourceNotFoundException")))
    assert a.delete(f"/api/catalog/prompt/{pid}").status_code == 200 and order == ["prompt"]


def test_delete_unregistered_item_skips_registry(env, monkeypatch):
    monkeypatch.setattr(aws, "registry_client", lambda: (_ for _ in ()).throw(AssertionError("no registry call")))
    a = client("a@x.com")
    iid = a.post("/api/mcp", json={"name": "m", "endpoint": "https://e.example/mcp"}).json()["id"]
    assert a.delete(f"/api/catalog/mcp/{iid}").json()["deleted"] == []


class _RegStatus(_Reg):
    def __init__(self, status):
        super().__init__()
        self.status = status

    def get_registry_record(self, registryId, recordId):
        return {"status": self.status}


def _pending_prompt(env, monkeypatch, status):
    order = []
    a, pid = _registered_prompt(env, monkeypatch, order)
    reg = _RegStatus(status)
    monkeypatch.setattr(aws, "registry_client", lambda: reg)
    return a, pid, reg, order


def test_cancel_pending_deletes_record_and_allows_reregister(env, monkeypatch):
    a, pid, reg, order = _pending_prompt(env, monkeypatch, "PENDING_APPROVAL")
    r = a.post(f"/api/catalog/prompt/{pid}/registry/cancel").json()
    assert reg.deleted == ["rec1"] and r["registry_record_id"] is None and r["registry_status"] is None
    assert order == []                                   # prompt 는 지우지 않는다
    assert a.post(f"/api/catalog/prompt/{pid}/register").json()["registry_record_id"] == "rec1"   # 다시 등록 가능


@pytest.mark.parametrize("status", ["APPROVED", "REJECTED", "DRAFT"])
def test_cancel_refused_when_not_pending(env, monkeypatch, status):
    a, pid, reg, _ = _pending_prompt(env, monkeypatch, status)
    resp = a.post(f"/api/catalog/prompt/{pid}/registry/cancel")
    assert resp.status_code == 409 and status in resp.json()["detail"] and reg.deleted == []
    item = a.get("/api/catalog/prompt").json()["items"][0]
    assert item["registry_status"] == status and item["registry_record_id"] == "rec1"   # 낡은 캐시 보정


def test_cancel_permission_and_missing_record(env, monkeypatch):
    a, pid, reg, _ = _pending_prompt(env, monkeypatch, "PENDING_APPROVAL")
    assert client("b@x.com").post(f"/api/catalog/prompt/{pid}/registry/cancel").status_code == 404
    db.update_item(pid, registry_status="APPROVED")
    assert client("b@x.com").post(f"/api/catalog/prompt/{pid}/registry/cancel").status_code == 403
    db.update_item(pid, registry_status="PENDING_APPROVAL")
    assert reg.deleted == []
    monkeypatch.setattr(aws, "registry_client", lambda: _Reg(_ce("ResourceNotFoundException")) and type(
        "R", (), {"get_registry_record": lambda self, **k: (_ for _ in ()).throw(_ce("ResourceNotFoundException"))})())
    assert a.post(f"/api/catalog/prompt/{pid}/registry/cancel").json()["registry_record_id"] is None   # 이미 없으면 정리만


def test_gateway_sync_creates_mcp_items_and_preserves_edits(env, monkeypatch):
    monkeypatch.setenv("GATEWAY_REGIONS", "us-west-2")
    monkeypatch.setattr(aws, "list_gateways", lambda reg=None: [
        {"gatewayId": "gw-1", "name": "gw", "status": "READY", "description": "d", "authorizerType": "AWS_IAM", "protocolType": "MCP"}])
    monkeypatch.setattr(aws, "get_gateway", lambda gid, reg=None: {"gatewayUrl": "https://gw-1.example/mcp"})
    assert catalog._sync_gateways() == 1
    it = db.list_items("mcp")[0]
    assert it["ext_id"] == "gateway:us-west-2:gw-1" and it["data"]["endpoint"] == "https://gw-1.example/mcp" and it["source"] == "aws"
    assert it["data"]["authorizer"] == "AWS_IAM" and it["data"]["headers"] == []
    client("e2npark@gmail.com").patch(f"/api/catalog/mcp/{it['id']}/description", json={"description": "내 설명"})
    monkeypatch.setattr(aws, "list_gateways", lambda reg=None: [
        {"gatewayId": "gw-1", "name": "gw2", "status": "READY", "description": "new", "authorizerType": "AWS_IAM", "protocolType": "MCP"}])
    assert catalog._sync_gateways() == 0
    it = db.list_items("mcp")[0]
    assert it["description"] == "내 설명" and it["name"] == "gw2"             # 사용자 설명 보존
    monkeypatch.setattr(aws, "list_gateways", lambda reg=None: [])
    catalog._sync_gateways()
    assert db.list_items("mcp") == []                                          # AWS 에서 사라지면 제거


def test_gateway_listing_failure_keeps_existing_items(env, monkeypatch):
    from botocore.exceptions import ClientError
    monkeypatch.setenv("GATEWAY_REGIONS", "us-west-2")
    db.upsert_synced("mcp", "gateway:us-west-2:gw-1", "gw", "", {"kind": "gateway"})
    def boom(reg=None):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "ListGateways")
    monkeypatch.setattr(aws, "list_gateways", boom)
    assert catalog._sync_gateways() == 0 and len(db.list_items("mcp")) == 1


def test_gateway_items_cannot_be_probed_with_header_test(env):
    iid = db.upsert_synced("mcp", "gateway:us-west-2:gw-1", "gw", "", {"kind": "gateway", "authorizer": "AWS_IAM", "endpoint": "https://x/mcp"})
    item = db.list_items("mcp")[0]
    db.put_usage_request(item["id"], "e2npark@gmail.com", "PENDING"); db.put_usage_request(item["id"], "e2npark@gmail.com", "APPROVED", decided_by="t")     # 관리자도 사용 승인이 있어야 테스트할 수 있다
    r = client("e2npark@gmail.com").post(f"/api/mcp/{item['id']}/test")
    assert r.status_code == 422 and "SigV4" in r.json()["detail"]


def test_gateways_from_multiple_regions_and_partial_failure(env, monkeypatch):
    from botocore.exceptions import ClientError
    monkeypatch.setenv("GATEWAY_REGIONS", "us-west-2,us-east-1")
    gw = lambda i, n: {"gatewayId": i, "name": n, "status": "READY", "description": "", "authorizerType": "AWS_IAM", "protocolType": "MCP"}
    by_region = {"us-west-2": [gw("same-id", "west")], "us-east-1": [gw("same-id", "east")]}   # 리전 간 ID 충돌
    monkeypatch.setattr(aws, "list_gateways", lambda reg=None: by_region[reg])
    monkeypatch.setattr(aws, "get_gateway", lambda gid, reg=None: {"gatewayUrl": f"https://{reg}.example/mcp"})
    assert catalog._sync_gateways() == 2
    got = {i["name"]: i["data"]["region"] for i in db.list_items("mcp")}
    assert got == {"west": "us-west-2", "east": "us-east-1"}
    # us-east-1 조회만 실패 → east 항목은 보존, west 는 정상 갱신
    def flaky(reg=None):
        if reg == "us-east-1":
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "ListGateways")
        return [gw("same-id", "west2")]
    monkeypatch.setattr(aws, "list_gateways", flaky)
    catalog._sync_gateways()
    got = {i["name"] for i in db.list_items("mcp")}
    assert got == {"west2", "east"}
    # 리전 설정: 기본값은 현재 리전 + us-east-1, 중복 제거
    monkeypatch.delenv("GATEWAY_REGIONS"); monkeypatch.setattr(aws, "region", lambda: "us-east-1")
    assert aws.gateway_regions() == ["us-east-1"]


@pytest.mark.parametrize("desc", [
    "GIT skill: https://github.com/anthropics/skills (skills/docx)",     # 실제 실패했던 기본 설명
    "첫 줄\n---\n둘째 줄: x", "yes", "12345", "[a] {b}: c", "문서(DOCX) 생성: 한글 😊 #태그", "C:\\path\\to", '따옴표 " 와 \' 섞임',
])
def test_skill_md_frontmatter_is_valid_yaml_for_any_description(desc):
    import yaml
    md = aws.record_params({"kind": "skill", "name": "My Skill", "description": desc,
                           "data": {"source_type": "git", "git_url": "https://github.com/o/r"}}
                           )["descriptors"]["agentSkillsDefinition"]["additionalData"]["skillMd"]["data"]
    assert md.startswith("---\n")
    fm = yaml.safe_load(md[4:md.index("\n---\n", 4)])      # 첫 번째 닫는 --- 까지가 frontmatter
    assert fm == {"name": "my-skill", "description": desc}                # 값이 그대로 보존된다


def test_skill_description_over_registry_limit_is_rejected_early(env):
    c = client("a@x.com")
    r = c.post("/api/skills", json={"name": "s", "source_type": "git", "git_url": "https://github.com/o/r", "description": "가" * 1025})
    assert r.status_code == 422
