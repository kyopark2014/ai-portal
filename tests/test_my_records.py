import pytest
from fastapi.testclient import TestClient

from application import auth, catalog, db, server

ME, OTHER = "kyopark2014@gmail.com", "other@x.com"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_SIGNING_KEY", "k")
    monkeypatch.setenv("PORTAL_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(auth, "_key", None)
    monkeypatch.setattr(catalog, "start_sync", lambda force=False: False)


def client(email):
    c = TestClient(server.app)
    c.cookies.set(auth.COOKIE_NAME, auth.make_session(email, "N", ""))
    return c


def mk(kind, name, owner, status):
    i = db.insert_item(kind, name, f"desc {name}", owner, {})
    if status:
        db.update_item(i, registry_status=status)
    return i


def test_requires_login(env):
    assert TestClient(server.app).get("/api/my/records").status_code == 401


def test_only_my_items_across_all_kinds_with_every_status(env):
    mk("prompt", "p-ok", ME, "APPROVED"); mk("skill", "s-ok", ME, "APPROVED"); mk("mcp", "m-pend", ME, "PENDING_APPROVAL")
    mk("agent", "a-rej", ME, "REJECTED"); mk("prompt", "p-draft", ME, "DRAFT"); mk("skill", "s-none", ME, None)
    mk("prompt", "theirs", OTHER, "APPROVED"); mk("mcp", "synced", "aws", "APPROVED"); mk("skill", "restored", "registry", "APPROVED")
    r = client(ME).get("/api/my/records").json()
    assert {i["name"] for i in r["items"]} == {"p-ok", "s-ok", "m-pend", "a-rej", "p-draft", "s-none"}      # 남의 것·aws·registry 소유 제외
    assert r["total"] == 6
    assert r["counts"] == {"APPROVED": 2, "PENDING_APPROVAL": 1, "REJECTED": 1, "DRAFT": 1, "NOT_REGISTERED": 1}


def test_other_users_never_see_my_unapproved_items(env):
    mk("prompt", "secret-pending", ME, "PENDING_APPROVAL")
    other = client(OTHER).get("/api/my/records").json()
    assert other["items"] == [] and other["total"] == 0 and other["counts"] == {}


def test_admin_my_records_is_still_only_their_own(env):
    mk("prompt", "mine", "e2npark@gmail.com", "APPROVED"); mk("prompt", "yours", ME, "APPROVED")
    assert [i["name"] for i in client("e2npark@gmail.com").get("/api/my/records").json()["items"]] == ["mine"]


def test_headers_stay_masked_and_can_edit_flag_is_set(env):
    i = db.insert_item("mcp", "m", "", ME, {"endpoint": "https://e/mcp", "headers": [{"key": "Authorization", "value": "SECRET"}]})
    r = client(ME).get("/api/my/records")
    assert "SECRET" not in r.text and r.json()["items"][0]["can_edit"] is True


def test_newest_first(env):
    a = mk("prompt", "old", ME, "APPROVED"); b = mk("prompt", "new", ME, "APPROVED")
    db.update_item(a, description="touched")                       # 최근에 수정한 것이 앞으로
    assert [i["name"] for i in client(ME).get("/api/my/records").json()["items"]][0] == "old"
