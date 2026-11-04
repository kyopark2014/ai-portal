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


def sent_to(ses): return sorted(t for t, _ in ses.sent)


def test_admins_and_the_records_reviewers_are_notified(lam):
    m, ssm, ses = lam
    ssm.params = {"/ai-portal/admins": ["second@x.com"], "/ai-portal/reviewers": {"rec123456": ["Rev@X.com", "rev2@x.com"]}}
    assert m.handler(ev(), None) == {"sent": 4}
    assert sent_to(ses) == ["rev2@x.com", "rev@x.com", "root@x.com", "second@x.com"]               # 정규화, 관리자 + reviewer
    body = dict(ses.sent)
    assert "관리자" in body["root@x.com"] and "reviewer" in body["rev@x.com"]


def test_a_reviewer_is_not_told_about_other_records(lam):
    m, ssm, ses = lam
    ssm.params = {"/ai-portal/reviewers": {"OTHER9999": ["rev@x.com"]}}
    m.handler(ev("rec123456"), None)
    assert sent_to(ses) == ["root@x.com"]                                                           # reviewer 는 자기 record 알림만


def test_no_reviewers_means_admins_only(lam):
    m, ssm, ses = lam
    assert m.handler(ev(), None) == {"sent": 1} and sent_to(ses) == ["root@x.com"]


def test_someone_who_is_both_admin_and_reviewer_gets_one_mail(lam):
    m, ssm, ses = lam
    ssm.params = {"/ai-portal/admins": ["second@x.com"], "/ai-portal/reviewers": {"rec123456": ["second@x.com", "rev@x.com"]}}
    assert m.handler(ev(), None) == {"sent": 3} and sent_to(ses).count("second@x.com") == 1


def test_ssm_outage_notifies_seed_admins_only(lam):
    m, ssm, ses = lam
    ssm.fail = True
    assert m.handler(ev(), None) == {"sent": 1} and sent_to(ses) == ["root@x.com"]                  # reviewer 확인 불가 → 권한을 넓히지 않는다


def test_one_failed_recipient_does_not_block_the_others(lam):
    m, ssm, ses = lam
    ssm.params = {"/ai-portal/reviewers": {"rec123456": ["bad@x.com", "ok@x.com"]}}; ses.fail_for = {"bad@x.com"}
    assert m.handler(ev(), None) == {"sent": 2}
    assert "bad@x.com" not in sent_to(ses) and "ok@x.com" in sent_to(ses)


def test_link_points_to_the_review_page_and_bad_ids_are_ignored(lam):
    m, ssm, ses = lam
    m.handler(ev("rec123456"), None)
    assert "https://portal.example.com/approvals/rec123456" in ses.sent[0][1]
    n = len(ses.sent)
    for bad in ("../../etc", "a b", "x", "", "<script>"):
        assert m.handler(ev(bad), None) == {"sent": 0}
    assert len(ses.sent) == n
