import importlib.util
import json
import sys
import types

import pytest

SENDER = "AI Portal <ai-portal@my-agentic-ai.click>"


class NotFound(Exception): pass


class FakeSSM:
    class exceptions: ParameterNotFound = NotFound
    def __init__(self): self.params, self.fail = {}, False
    def get_parameter(self, Name):
        if self.fail: raise RuntimeError("ssm down")
        if Name not in self.params: raise NotFound()
        return {"Parameter": {"Value": json.dumps(self.params[Name])}}


class FakeSES:
    def __init__(self): self.sent, self.fail_for = [], set()
    def send_email(self, FromEmailAddress, Destination, Content):
        to = Destination["ToAddresses"][0]
        if to in self.fail_for: raise RuntimeError("rejected")
        self.sent.append((to, Content["Simple"]["Body"]["Text"]["Data"]))


@pytest.fixture
def lam(monkeypatch):
    ssm, ses = FakeSSM(), FakeSES()
    fake = types.SimpleNamespace(client=lambda svc, **k: ssm if svc == "ssm" else ses)
    monkeypatch.setitem(sys.modules, "boto3", fake)
    for k, v in (("SENDER", SENDER), ("PORTAL_ORIGIN", "https://portal.example.com/"), ("ADMIN_EMAILS", "root@x.com")):
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("notify_lambda", "lambda-notify/lambda_function.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m, ssm, ses


def ev(rec="rec123456"): return {"detail": {"registryRecordId": rec, "registryId": "R"}, "time": "2026-11-04T08:00:00Z"}


def test_assigned_approver_is_the_only_recipient(lam):
    m, ssm, ses = lam
    ssm.params = {"/ai-portal/admins": ["second@x.com", "third@x.com"], "/ai-portal/approvers": {"rec123456": "Second@X.com"}}
    assert m.handler(ev(), None) == {"sent": 1}
    assert [t for t, _ in ses.sent] == ["second@x.com"]                              # 대소문자 정규화, 다른 관리자에게는 가지 않는다
    assert "지정된 승인자" in ses.sent[0][1]


def test_unassigned_goes_to_every_admin(lam):
    m, ssm, ses = lam
    ssm.params = {"/ai-portal/admins": ["second@x.com"]}
    assert m.handler(ev(), None) == {"sent": 2}
    assert sorted(t for t, _ in ses.sent) == ["root@x.com", "second@x.com"] and "모든 관리자" in ses.sent[0][1]


def test_assignment_for_other_record_does_not_leak(lam):
    m, ssm, ses = lam
    ssm.params = {"/ai-portal/admins": ["second@x.com"], "/ai-portal/approvers": {"OTHER9999": "second@x.com"}}
    m.handler(ev(), None)
    assert sorted(t for t, _ in ses.sent) == ["root@x.com", "second@x.com"]


def test_assignee_no_longer_admin_falls_back_to_all_admins(lam):
    m, ssm, ses = lam
    ssm.params = {"/ai-portal/admins": [], "/ai-portal/approvers": {"rec123456": "removed@x.com"}}
    m.handler(ev(), None)
    assert [t for t, _ in ses.sent] == ["root@x.com"]                                # 요청이 아무에게도 안 가는 일이 없다


def test_ssm_outage_notifies_seed_admins(lam):
    m, ssm, ses = lam
    ssm.fail = True
    assert m.handler(ev(), None) == {"sent": 1} and [t for t, _ in ses.sent] == ["root@x.com"]


def test_one_failed_recipient_does_not_block_the_others(lam):
    m, ssm, ses = lam
    ssm.params = {"/ai-portal/admins": ["bad@x.com", "ok@x.com"]}; ses.fail_for = {"bad@x.com"}
    assert m.handler(ev(), None) == {"sent": 2}                                       # root + ok
    assert "bad@x.com" not in [t for t, _ in ses.sent]


def test_link_points_to_the_review_page_and_bad_ids_are_ignored(lam):
    m, ssm, ses = lam
    m.handler(ev("rec123456"), None)
    assert "https://portal.example.com/approvals/rec123456" in ses.sent[0][1]         # 끝의 / 정리
    n = len(ses.sent)
    for bad in ("../../etc", "a b", "x", "", "<script>"):
        assert m.handler(ev(bad), None) == {"sent": 0}
    assert len(ses.sent) == n
