import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from application import auth, aws, catalog, db, mcp_test, server


def _grant(item_id, email="a@x.com"):          # 소유자도 사용 신청이 승인되어야 쓸 수 있다
    db.put_usage_request(item_id, email, "PENDING"); db.put_usage_request(item_id, email, "APPROVED", decided_by="t")


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_SIGNING_KEY", "k")
    monkeypatch.setenv("PORTAL_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("HARNESS_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h")
    monkeypatch.setattr(auth, "_key", None)
    monkeypatch.setattr(catalog, "start_sync", lambda force=False: False)


def client(email="a@x.com"):
    c = TestClient(server.app)
    c.cookies.set(auth.COOKIE_NAME, auth.make_session(email, "N", ""))
    return c


def src(**d):
    return mcp_test.skill_source({"data": d})


@pytest.mark.parametrize("data,expect", [
    # 사용자가 그대로 붙여 넣는 GitHub 웹 주소 → repo + 하위 경로 (harness 는 git clone 을 하므로 웹 주소는 실패한다)
    ({"source_type": "git", "git_url": "https://github.com/anthropics/skills/tree/main/skills/docx"},
     {"git": {"url": "https://github.com/anthropics/skills", "path": "skills/docx"}}),
    ({"source_type": "git", "git_url": "https://github.com/anthropics/skills/tree/main/skills/docx/"},
     {"git": {"url": "https://github.com/anthropics/skills", "path": "skills/docx"}}),
    ({"source_type": "git", "git_url": "https://github.com/anthropics/skills", "subdirectory": "skills/docx"},
     {"git": {"url": "https://github.com/anthropics/skills", "path": "skills/docx"}}),
    ({"source_type": "git", "git_url": "https://github.com/anthropics/skills.git", "subdirectory": "/skills/docx/"},
     {"git": {"url": "https://github.com/anthropics/skills", "path": "skills/docx"}}),
    ({"source_type": "git", "git_url": "https://github.com/o/r/tree/main/a/b", "subdirectory": "override"},      # 명시한 subdirectory 가 우선
     {"git": {"url": "https://github.com/o/r", "path": "override"}}),
    ({"source_type": "git", "git_url": "https://github.com/o/r"}, {"git": {"url": "https://github.com/o/r"}}),
    ({"source_type": "s3", "s3_uri": "s3://bucket/skills/my-skill"}, {"s3": {"uri": "s3://bucket/skills/my-skill/"}}),
    ({"source_type": "git", "git_url": "https://github.com/o/private", "credential_provider_arn": "arn:aws:bedrock-agentcore:us-west-2:1:token-vault/x", "git_username": "bob"},
     {"git": {"url": "https://github.com/o/private", "auth": {"credentialArn": "arn:aws:bedrock-agentcore:us-west-2:1:token-vault/x", "username": "bob"}}}),
])
def test_skill_source_mapping(data, expect):
    assert src(**data) == expect


@pytest.mark.parametrize("data", [
    {"source_type": "git", "git_url": "http://github.com/o/r"},             # https 만
    {"source_type": "git", "git_url": "git@github.com:o/r.git"},
    {"source_type": "git", "git_url": "file:///etc/passwd"},                 # 로컬 파일 접근 금지
    {"source_type": "s3", "s3_uri": "https://not-s3/x"},
])
def test_skill_source_rejects_unsafe_or_invalid(data):
    with pytest.raises(HTTPException) as e:
        src(**data)
    assert e.value.status_code == 422


def test_skill_chat_passes_skills_not_tools_and_scopes_session(env, monkeypatch):
    seen = []
    class RT:
        def invoke_agent_runtime_command(self, **kw):               # Node 설치(세션당 1회)
            return {"stream": iter([{"chunk": {"contentStop": {"exitCode": 0}}}])}
        def invoke_harness(self, **kw):
            seen.append(kw)
            return {"stream": iter([{"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "ok"}}},
                                    {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1}}}])}
    monkeypatch.setattr(aws.boto3, "client", lambda *a, **k: RT())
    iid = db.insert_item("skill", "docx", "", "a@x.com", {"source_type": "git", "git_url": "https://github.com/anthropics/skills/tree/main/skills/docx"})
    db.update_item(iid, registry_status="APPROVED")
    body = {"session_id": "s" * 36, "messages": [{"role": "user", "content": "hi"}]}
    r = client("a@x.com").post(f"/api/skills/{iid}/chat", json=body)
    evs = [json.loads(l[5:]) for l in r.text.split("\n\n") if l.startswith("data:")]
    assert "status" in evs[0] and {"t": "ok"} in evs and evs[-1]["done"]       # 첫 호출은 Node 준비 안내가 먼저 나온다
    assert seen[0]["skills"] == [{"git": {"url": "https://github.com/anthropics/skills", "path": "skills/docx"}}] and "tools" not in seen[0]
    assert "@" not in seen[0]["actorId"]
    client("b@x.com").post(f"/api/skills/{iid}/chat", json=body)
    assert seen[0]["runtimeSessionId"] != seen[1]["runtimeSessionId"]          # 사용자별 세션 분리


def test_skill_chat_access_and_validation(env):
    body = {"session_id": "s" * 36, "messages": [{"role": "user", "content": "hi"}]}
    assert TestClient(server.app).post("/api/skills/1/chat", json=body).status_code == 401
    iid = db.insert_item("skill", "s", "", "a@x.com", {"source_type": "git", "git_url": "https://github.com/o/r"})
    assert client("b@x.com").post(f"/api/skills/{iid}/chat", json=body).status_code == 404       # 남의 승인 전 skill
    mcp = db.insert_item("mcp", "m", "", "a@x.com", {})
    _grant(mcp)
    assert client("a@x.com").post(f"/api/skills/{mcp}/chat", json=body).status_code == 404       # skill 이 아닌 항목
    bad = {**body, "messages": [{"role": "assistant", "content": "x"}]}
    assert client("a@x.com").post(f"/api/skills/{iid}/chat", json=bad).status_code == 422


def test_node_install_runs_once_per_session_before_skill_invoke_with_harness_arn(env, monkeypatch):
    order = []
    class RT:
        def invoke_agent_runtime_command(self, **kw):
            order.append(("cmd", kw["agentRuntimeArn"], kw["body"]["command"]))
            return {"stream": iter([{"chunk": {"contentStop": {"exitCode": 0}}}])}
        def invoke_harness(self, **kw):
            order.append(("harness",))
            return {"stream": iter([{"metadata": {"usage": {}}}])}
    mcp_test._node_ready.clear()
    monkeypatch.setattr(aws.boto3, "client", lambda *a, **k: RT())
    iid = db.insert_item("skill", "docx", "", "a@x.com", {"source_type": "git", "git_url": "https://github.com/o/r"})
    body = {"session_id": "s" * 36, "messages": [{"role": "user", "content": "hi"}]}
    c = client("a@x.com")
    r1 = c.post(f"/api/skills/{iid}/chat", json=body)
    c.post(f"/api/skills/{iid}/chat", json=body)
    kinds = [o[0] for o in order]
    assert kinds == ["cmd", "harness", "harness"]                       # 설치는 세션당 한 번, harness 호출보다 먼저
    assert order[0][1] == "arn:aws:bedrock-agentcore:us-west-2:1:harness/h"   # runtime ARN 이 아니라 harness ARN
    assert "dnf install -y nodejs npm" in order[0][2] and order[0][2].index("dnf") < order[0][2].index("apt-get")
    assert "Node.js" in r1.text                                          # 준비 중 상태 안내


def test_node_install_failure_does_not_break_the_chat(env, monkeypatch):
    class RT:
        def invoke_agent_runtime_command(self, **kw):
            return {"stream": iter([{"chunk": {"contentDelta": {"stderr": "boom"}}}, {"chunk": {"contentStop": {"exitCode": 1}}}])}
        def invoke_harness(self, **kw):
            return {"stream": iter([{"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "still works"}}}])}
    mcp_test._node_ready.clear()
    monkeypatch.setattr(aws.boto3, "client", lambda *a, **k: RT())
    iid = db.insert_item("skill", "docx", "", "a@x.com", {"source_type": "git", "git_url": "https://github.com/o/r"})
    r = client("a@x.com").post(f"/api/skills/{iid}/chat", json={"session_id": "t" * 36, "messages": [{"role": "user", "content": "hi"}]})
    assert "still works" in r.text and not mcp_test._node_ready          # 실패는 기록하지 않아 다음에 재시도


def test_mcp_chat_does_not_install_node(env, monkeypatch):
    calls = []
    class RT:
        def invoke_agent_runtime_command(self, **kw): calls.append("cmd"); return {"stream": iter([])}
        def invoke_harness(self, **kw): return {"stream": iter([])}
    monkeypatch.setattr(aws.boto3, "client", lambda *a, **k: RT())
    iid = db.insert_item("mcp", "m", "", "a@x.com", {"endpoint": "https://e/mcp", "headers": []})
    _grant(iid)
    client("a@x.com").post(f"/api/mcp/{iid}/chat", json={"session_id": "u" * 36, "messages": [{"role": "user", "content": "hi"}]})
    assert calls == []


# ── zip 업로드 ──
def _zip(files):
    import io, zipfile
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, c in files.items():
            z.writestr(n, c)
    return b.getvalue()


class _S3:
    def __init__(self): self.put = {}
    def put_object(self, Bucket, Key, Body, ContentType=None): self.put[Key] = Body; return {}


def _up(monkeypatch, files, name="My Skill"):
    s3 = _S3()
    monkeypatch.setenv("STORAGE_BUCKET", "bkt")
    monkeypatch.setattr(aws, "client", lambda svc: s3)
    monkeypatch.setattr(catalog, "_ensure_name_free", lambda *a, **k: None)
    monkeypatch.setattr(catalog, "selected_registry", lambda: "")
    r = client().post("/api/skills/upload", data={"name": name}, files={"file": ("s.zip", files, "application/zip")})
    return r, s3


def test_s3_zip_uri_is_a_directory():
    assert src(source_type="s3", s3_uri="s3://b/skills/u/s/skill.zip") == {"s3": {"uri": "s3://b/skills/u/s/skill.zip/"}}


def test_upload_extracts_to_contents_dir(env, monkeypatch):
    r, s3 = _up(monkeypatch, _zip({"x/SKILL.md": "---\nname: x\n---", "x/scripts/a.py": "1"}))
    assert r.status_code == 200, r.text
    assert set(s3.put) == {"skills/a@x.com/my-skill/skill.zip", "skills/a@x.com/my-skill/contents/SKILL.md",
                           "skills/a@x.com/my-skill/contents/scripts/a.py"}      # 단일 최상위 폴더(x/)는 벗긴다
    assert r.json()["data"]["s3_uri"] == "s3://bkt/skills/a@x.com/my-skill/contents/"


def test_upload_rejects_bad_zip(env, monkeypatch):
    assert _up(monkeypatch, b"nope")[0].status_code == 422
    assert _up(monkeypatch, _zip({"a.txt": "x"}))[0].status_code == 422
    r, s3 = _up(monkeypatch, _zip({"SKILL.md": "x", "../evil": "x"}))
    assert r.status_code == 422 and not s3.put


class _Del:
    def __init__(self, keys): self.keys, self.deleted = keys, []
    def get_paginator(self, _):
        outer = self
        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Versions": [{"Key": k, "VersionId": "1"} for k in outer.keys if k.startswith(Prefix)]}
        return P()
    def delete_objects(self, Bucket, Delete): self.deleted += [o["Key"] for o in Delete["Objects"]]


def test_delete_skill_removes_s3_folder(env, monkeypatch):
    s3 = _Del(["skills/a@x.com/my-skill/skill.zip", "skills/a@x.com/my-skill/contents/SKILL.md", "skills/a@x.com/other/skill.zip"])
    monkeypatch.setenv("STORAGE_BUCKET", "bkt")
    monkeypatch.setattr(aws, "client", lambda svc: s3)
    # Registry 동기화로 복원된 항목처럼 업로드 표시가 없어도 URI 와 소유자로 판단한다
    catalog._delete_skill_object({"owner": "a@x.com", "data": {"source_type": "s3", "s3_uri": "s3://bkt/skills/a@x.com/my-skill/contents/"}})
    assert sorted(s3.deleted) == ["skills/a@x.com/my-skill/contents/SKILL.md", "skills/a@x.com/my-skill/skill.zip"]


@pytest.mark.parametrize("owner,uri", [
    ("b@x.com", "s3://bkt/skills/a@x.com/my-skill/contents/"),      # 남의 폴더
    ("a@x.com", "s3://other/skills/a@x.com/my-skill/contents/"),    # 다른 버킷
    ("a@x.com", "s3://bkt/skills/a@x.com/"),                        # 사용자 폴더 전체
    ("a@x.com", "s3://bkt/docs/x/y/"),
])
def test_delete_skill_never_touches_foreign_paths(env, monkeypatch, owner, uri):
    s3 = _Del(["skills/a@x.com/my-skill/skill.zip"])
    monkeypatch.setenv("STORAGE_BUCKET", "bkt")
    monkeypatch.setattr(aws, "client", lambda svc: s3)
    catalog._delete_skill_object({"owner": owner, "data": {"source_type": "s3", "s3_uri": uri}})
    assert s3.deleted == []
