"""playground_store: SQLite 와 DynamoDB(moto)에서 같은 결과를 내야 한다. 사용자 · agent 별로 격리된다."""
import boto3
import pytest
from moto import mock_aws

from application import playground_store as s


@pytest.fixture(params=["sqlite", "dynamodb"])
def store(request, monkeypatch, tmp_path):
    monkeypatch.setenv("PORTAL_STORE", request.param)
    if request.param == "sqlite":
        monkeypatch.setenv("PLAYGROUND_DB_PATH", str(tmp_path / "pg.db"))
        yield s
        return
    with mock_aws():
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x"); monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
        monkeypatch.setenv("AWS_REGION", "us-west-2"); monkeypatch.setenv("PLAYGROUND_DDB_TABLE", "pg-test")
        monkeypatch.delenv("DDB_REGION", raising=False)
        boto3.client("dynamodb", region_name="us-west-2").create_table(
            TableName="pg-test", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}])
        s.reset_for_tests(None)
        yield s
        s.reset_for_tests(None)


def test_isolation_by_user_and_agent(store):
    a1, a2 = store.create_task("A@x.com", 7), store.create_task("a@x.com", 7)
    other_agent, other_user = store.create_task("a@x.com", 8), store.create_task("b@x.com", 7)
    assert {t["id"] for t in store.list_tasks("a@x.com", 7)} == {a1["id"], a2["id"]}               # 이메일 대소문자는 구분하지 않는다
    assert [t["id"] for t in store.list_tasks("a@x.com", 8)] == [other_agent["id"]]
    assert [t["id"] for t in store.list_tasks("b@x.com", 7)] == [other_user["id"]]
    assert store.get_task("b@x.com", a1["id"]) is None and store.list_messages("b@x.com", a1["id"]) is None


def test_messages_order_title_and_foreign_writes_are_rejected(store):
    t = store.create_task("a@x.com", 7)
    assert store.add_messages("a@x.com", t["id"], [{"role": "user", "content": "안녕 반가워"},
                                                   {"role": "assistant", "content": "네", "tool_events": [{"type": "text", "data": "네"}]}])
    assert store.add_messages("a@x.com", t["id"], [{"role": "user", "content": "둘째"}])
    msgs = store.list_messages("a@x.com", t["id"])
    assert [m["content"] for m in msgs] == ["안녕 반가워", "네", "둘째"] and [m["seq"] for m in msgs] == [0, 1, 2]
    assert msgs[1]["tool_events"] == [{"type": "text", "data": "네"}]
    assert store.get_task("a@x.com", t["id"])["title"] == "안녕 반가워"                              # 첫 질문이 제목(이미 제목이 있으면 유지)
    assert store.add_messages("b@x.com", t["id"], [{"role": "user", "content": "침입"}]) is False
    assert store.update_task("b@x.com", t["id"], title="해킹") is None and store.delete_task("b@x.com", t["id"]) is False
    assert len(store.list_messages("a@x.com", t["id"])) == 3


def test_pin_sort_and_delete_removes_messages(store):
    t1, t2 = store.create_task("a@x.com", 7), store.create_task("a@x.com", 7)
    store.add_messages("a@x.com", t2["id"], [{"role": "user", "content": "q"}])
    assert store.list_tasks("a@x.com", 7)[0]["id"] == t2["id"]                                      # 최근 갱신이 먼저
    assert store.update_task("a@x.com", t1["id"], pinned=True)["pinned"] is True
    assert store.list_tasks("a@x.com", 7)[0]["id"] == t1["id"]                                      # 고정이 먼저
    assert store.update_task("a@x.com", t2["id"], title="x" * 80)["title"] == "x" * 50              # 제목 길이 제한
    assert store.delete_task("a@x.com", t2["id"]) is True and store.get_task("a@x.com", t2["id"]) is None
    assert store.list_messages("a@x.com", t2["id"]) is None
