"""모든 테스트 공통 안전장치: 실수로 실제 AWS(SSM 등)를 호출하지 못하게 한다.

관리자/reviewer 목록은 SSM 에 저장되므로 권한 판정이 들어가는 거의 모든 테스트가 이 경로를 지난다.
각 테스트는 필요하면 fixture 로 가짜 SSM 을 주입한다(예: test_approvers). 주입하지 않은 테스트가 SSM 을 건드리면
실제 계정의 값을 읽고 쓰게 되므로, 기본값은 '빈 저장소(ParameterNotFound)' 이고 쓰기는 거부한다.
"""
import pytest
from botocore.exceptions import ClientError

from application import admins


class _NoAwsSSM:
    def get_parameter(self, Name):
        raise ClientError({"Error": {"Code": "ParameterNotFound", "Message": "test default: empty"}}, "GetParameter")

    def put_parameter(self, **kw):
        raise AssertionError("테스트가 실제 SSM 에 쓰려고 했습니다 — fixture 로 가짜 SSM 을 주입하세요")


@pytest.fixture(autouse=True)
def _isolate_ssm(monkeypatch):
    monkeypatch.setattr(admins, "_ssm", lambda: _NoAwsSSM())
    admins.reset_cache()
    yield
    admins.reset_cache()


# ── 저장소 전환: PORTAL_STORE=dynamodb pytest 로 전체 테스트를 DynamoDB(moto) 위에서 다시 돌릴 수 있다 ──
import os

if os.environ.get("PORTAL_STORE") == "dynamodb":
    import boto3
    from moto import mock_aws

    @pytest.fixture(autouse=True)
    def _fake_dynamodb(monkeypatch):
        from application import db_dynamo
        with mock_aws():
            monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x"); monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
            monkeypatch.setenv("AWS_REGION", "us-west-2"); monkeypatch.setenv("DDB_TABLE", "ai-portal-test")
            boto3.client("dynamodb", region_name="us-west-2").create_table(
                TableName="ai-portal-test", BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
                AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}])
            db_dynamo.reset_for_tests(None)
            yield
            db_dynamo.reset_for_tests(None)
