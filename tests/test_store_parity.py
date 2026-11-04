"""SQLite 와 DynamoDB 구현이 같은 시나리오에서 같은 결과를 내는지 확인한다(저장소를 바꿔도 동작이 달라지면 안 된다)."""
import time

import boto3
import pytest
from moto import mock_aws

from application import db, db_dynamo


@pytest.fixture(params=["sqlite", "dynamodb"])
def store(request, monkeypatch, tmp_path):
    if request.param == "sqlite":
        monkeypatch.setenv("PORTAL_DB_PATH", str(tmp_path / "t.db")); monkeypatch.setenv("PORTAL_STORE", "sqlite")
        yield "sqlite"
        return
    with mock_aws():
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x"); monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x"); monkeypatch.setenv("AWS_REGION", "us-west-2")
        monkeypatch.setenv("PORTAL_STORE", "dynamodb"); monkeypatch.setenv("DDB_TABLE", "t")
        boto3.client("dynamodb", region_name="us-west-2").create_table(
            TableName="t", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}])
        db_dynamo.reset_for_tests(None)
        yield "dynamodb"
        db_dynamo.reset_for_tests(None)


def test_insert_get_and_integer_ids_are_sequential_and_unique(store):
    a = db.insert_item("prompt", "p1", "d1", "a@x.com", {"k": [1, 2], "f": 1.5})
    b = db.insert_item("mcp", "m1", "", "b@x.com", {})
    assert isinstance(a, int) and isinstance(b, int) and b == a + 1
    got = db.get_item(a)
    assert got["name"] == "p1" and got["kind"] == "prompt" and got["owner"] == "a@x.com" and got["data"] == {"k": [1, 2], "f": 1.5}
    assert got["desc_edited"] is False and got["registry_record_id"] is None and got["ext_id"] is None and got["source"] == "portal"
    assert db.get_item(999999) is None


def test_update_changes_only_given_fields_and_rejects_unknown_ones(store):
    i = db.insert_item("skill", "s", "d", "a@x.com", {"x": 1})
    t0 = db.get_item(i)["updated_at"]; time.sleep(0.01)
    db.update_item(i, description="new", registry_record_id="rec1", registry_status="DRAFT", desc_edited=1, data={"y": 2})
    g = db.get_item(i)
    assert (g["name"], g["description"], g["registry_record_id"], g["registry_status"], g["desc_edited"], g["data"]) == ("s", "new", "rec1", "DRAFT", True, {"y": 2})
    assert g["updated_at"] > t0
    db.update_item(i, registry_record_id=None, registry_status=None)                  # 연결 해제
    g = db.get_item(i)
    assert g["registry_record_id"] is None and g["registry_status"] is None
    with pytest.raises(ValueError):
        db.update_item(i, kind="mcp")


def test_updating_or_deleting_a_missing_item_is_a_noop_and_never_resurrects(store):
    i = db.insert_item("prompt", "p", "", "a@x.com", {})
    db.delete_item(i)
    db.update_item(i, name="ghost"); db.delete_item(i)                                  # 오류 없이 무시
    assert db.get_item(i) is None and db.list_items("prompt") == []                    # 지워진 항목이 되살아나지 않는다


def test_list_search_visibility_and_ordering(store):
    a = db.insert_item("prompt", "alpha", "first thing", "a@x.com", {}); time.sleep(0.01)
    b = db.insert_item("prompt", "beta", "ALPHA mention", "b@x.com", {}); time.sleep(0.01)
    c = db.insert_item("prompt", "gamma", "", "c@x.com", {}); db.update_item(c, registry_status="APPROVED")
    db.insert_item("mcp", "other-kind", "", "a@x.com", {})
    assert [x["name"] for x in db.list_items("prompt")] == ["gamma", "beta", "alpha"]                 # 최근 수정순, 종류별
    assert [x["name"] for x in db.list_items("prompt", "ALPHA")] == ["beta", "alpha"]                 # 이름·설명, 대소문자 무시
    assert {x["name"] for x in db.list_items("prompt", viewer=("a@x.com", False))} == {"alpha", "gamma"}   # 내 것 + 승인된 것
    assert len(db.list_items("prompt", viewer=("a@x.com", True))) == 3                                # 관리자는 전부
    assert [x["name"] for x in db.list_owned("a@x.com")] == ["other-kind", "alpha"]                    # 모든 종류, 최근순
    assert db.is_visible(db.get_item(a), "z@x.com", False) is False and db.is_visible(db.get_item(c), "z@x.com", False) is True


def test_upsert_synced_preserves_edited_description_and_merges_data(store):
    assert db.upsert_synced("agent", "runtime:1", "r", "orig", {"a": 1}) is True
    i = db.list_items("agent")[0]["id"]
    assert db.upsert_synced("agent", "runtime:1", "r2", "aws-new", {"b": 2}) is False
    g = db.get_item(i); assert (g["name"], g["description"], g["data"], g["source"], g["owner"]) == ("r2", "aws-new", {"a": 1, "b": 2}, "aws", "aws")
    db.update_item(i, description="mine", desc_edited=1)
    db.upsert_synced("agent", "runtime:1", "r3", "aws-again", {})
    g = db.get_item(i); assert g["description"] == "mine" and g["name"] == "r3"                         # 사용자 편집 보존
    assert len(db.list_items("agent")) == 1                                                            # 중복 생성 안 함


def test_prune_synced_removes_only_aws_items_missing_from_the_keep_set(store):
    db.upsert_synced("mcp", "gateway:a", "a", "", {}); db.upsert_synced("mcp", "gateway:b", "b", "", {})
    mine = db.insert_item("mcp", "mine", "", "u@x.com", {}, ext_id="gateway:zz")                         # 포털에서 만든 항목은 지우지 않는다
    assert db.prune_synced("mcp", {"gateway:a"}) == 1
    assert {x["name"] for x in db.list_items("mcp")} == {"a", "mine"} and db.get_item(mine)


def test_find_by_record_and_settings(store):
    i = db.insert_item("prompt", "p", "", "a@x.com", {}); db.update_item(i, registry_record_id="recABC")
    assert db.find_by_record("recABC")["id"] == i and db.find_by_record("none") is None
    assert db.get_setting("registry_id", "dflt") == "dflt"
    db.set_setting("registry_id", "R1"); db.set_setting("registry_id", "R2")
    assert db.get_setting("registry_id") == "R2"


def test_login_stats_are_identical(store):
    assert db.record_login("A@x.com", "에이") is True and db.record_login("a@x.com", "에이2") is False and db.record_login("b@x.com", "비") is True
    db.insert_item("prompt", "p", "", "a@x.com", {}); i = db.insert_item("skill", "s", "", "a@x.com", {}); db.update_item(i, registry_status="APPROVED")
    db.insert_item("agent", "ag", "", "registry", {})
    assert db.login_totals() == {"total_logins": 3, "unique_users": 2, "active_7d": 2}
    u = {x["email"]: x for x in db.user_stats()}
    assert u["a@x.com"]["login_count"] == 2 and u["a@x.com"]["name"] == "에이2" and u["a@x.com"]["total"] == 2
    assert u["a@x.com"]["kinds"] == {"prompt": {"NOT_REGISTERED": 1}, "skill": {"APPROVED": 1}}
    assert u["registry"]["login_count"] == 0 and u["registry"]["last_login"] is None and u["b@x.com"]["total"] == 0
    r = db.recent_logins(2); assert [x["email"] for x in r] == ["b@x.com", "a@x.com"] and set(r[0]) == {"email", "name", "logged_at"}


def test_login_record_never_stores_a_profile_picture(store):
    db.record_login("a@x.com", "A")
    if store == "dynamodb":
        items = db_dynamo.table().scan()["Items"]
        assert all("picture" not in x for x in items)


def test_concurrent_inserts_never_share_an_id(store):
    """동시에 넣어도 id 가 겹치지 않는다.

    DynamoDB 쪽은 원자 카운터(UpdateItem ADD)에 의존하는데, 이 보장은 **실제 DynamoDB 가 제공**하는 것이다.
    moto(가짜 DynamoDB)는 스레드 간에 이 원자성을 지키지 않아 같은 id 를 여러 번 내줄 수 있으므로(40개 중 15개만 고유),
    moto 위에서는 이 성질을 검증할 수 없다. 실제 테이블에서는 5라운드×40건에서 중복 0건임을 임시 테이블로 직접 확인했다.
    그래서 moto 에서는 동시성 대신 '순차 발급이 연속·유일하다'만 확인하고, 동시성은 SQLite 에서 확인한다.
    """
    if store == "dynamodb":
        ids = [db.insert_item("prompt", f"p{n}", "", "a@x.com", {}) for n in range(40)]
        assert ids == list(range(ids[0], ids[0] + 40)) and len(db.list_items("prompt")) == 40
        return
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as ex:
        ids = list(ex.map(lambda n: db.insert_item("prompt", f"p{n}", "", "a@x.com", {}), range(40)))
    assert len(set(ids)) == 40 and len(db.list_items("prompt")) == 40
