import importlib.util
import json
import boto3
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def lam():
    spec = importlib.util.spec_from_file_location("interceptor_fn", ROOT / "lambda-interceptor" / "lambda_function.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def req(body=None, **extra):
    return {"interceptorInputVersion": "1.0", "mcp": {"gatewayResponse": None, "gatewayRequest": {
        "context": {"identity": {"awsPrincipalArn": "arn:aws:iam::111122223333:user/alice"}},
        "path": "/mcp", "httpMethod": "POST", "headers": {"Authorization": "AWS4-HMAC-SHA256 SECRET-SIG"},
        "body": body or {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "target-ai-portal___search", "arguments": {"q": "TOP-SECRET-VALUE"}}}, **extra}}}


def resp():
    return {"interceptorInputVersion": "1.0", "mcp": {
        "gatewayRequest": {"path": "/mcp", "body": {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}},
        "gatewayResponse": {"statusCode": 200, "body": {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"text": "SECRET-RESULT"}]}}}}}


def test_request_passes_through_unchanged(lam):
    ev = req()
    out = lam.handler(ev, None)
    assert out == {"interceptorOutputVersion": "1.0",
                   "mcp": {"transformedGatewayRequest": {"body": ev["mcp"]["gatewayRequest"]["body"]}}}


def test_response_passes_through_unchanged(lam):
    ev = resp()
    out = lam.handler(ev, None)["mcp"]["transformedGatewayResponse"]
    assert out["statusCode"] == 200 and out["body"] == ev["mcp"]["gatewayResponse"]["body"]


def test_logs_tool_and_argument_names_but_never_values_or_headers(lam, caplog):
    caplog.set_level(logging.INFO)
    lam.handler(req(), None); lam.handler(resp(), None)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "target-ai-portal___search" in text and '"argument_keys": ["q"]' in text and '"point": "RESPONSE"' in text
    assert "TOP-SECRET-VALUE" not in text and "SECRET-SIG" not in text and "SECRET-RESULT" not in text


def test_block_answers_immediately_with_a_jsonrpc_error(lam, monkeypatch):
    monkeypatch.setattr(lam, "decide", lambda point, rpc, mcp=None: {"action": "block", "message": "denied"})
    out = lam.handler(req(), None)["mcp"]
    assert "transformedGatewayRequest" not in out                       # target 으로 보내지 않는다
    body = out["transformedGatewayResponse"]["body"]
    assert body["id"] == 1 and body["error"]["message"] == "denied"


def test_string_body_and_garbage_do_not_crash(lam):
    assert lam.handler(req(body='{"jsonrpc":"2.0","id":2,"method":"tools/list"}'), None)["interceptorOutputVersion"] == "1.0"
    assert lam.handler({}, None)["interceptorOutputVersion"] == "1.0"
    assert lam.handler(req(body="not json"), None)["interceptorOutputVersion"] == "1.0"


def test_installer_gateway_params_match_the_api_model():
    """create_gateway / create_gateway_target 인자가 boto3 서비스 모델의 스키마를 만족한다(두 gateway·두 target). AWS 는 호출하지 않는다."""
    import botocore.session
    from botocore.validate import ParamValidator
    src = (ROOT / "installer.py").read_text()
    ns = {}
    # installer 는 import 시 AWS(STS)를 호출하므로, 필요한 순수 함수만 떼어 실행한다
    consts = src[src.index("def gateway_name"):src.index("def _package_lambda_zip")]
    gp = src[src.index("def gateway_params"):src.index("def _wait(")]
    cg = src[src.index("COGNITO_REGION = "):src.index("def ensure_cognito")]
    exec("project_name='ai-portal'; region='us-west-2'; account_id='123456789012'\nfrom typing import *\n" + consts + gp + cg, ns)
    model = botocore.session.get_session().create_client("bedrock-agentcore-control", region_name="us-west-2").meta.service_model
    # IAM websearch gateway
    iam = ns["gateway_params"]("arn:aws:iam::1:role/r", "arn:aws:lambda:us-west-2:1:function:f", ns["WEBSEARCH_GATEWAY_NAME"])
    checks = [("CreateGateway", iam),
              ("CreateGatewayTarget", ns["target_params"]("gw-1", "https://example.com/mcp", ns["TARGET_NAME"])),
              ("CreateGatewayTarget", ns["websearch_target_params"]("gw-1"))]
    for op, params in checks:
        rep = ParamValidator().validate(params, model.operation_model(op).input_shape)
        assert not rep.has_errors(), rep.generate_report()
    assert iam["authorizerType"] == "AWS_IAM" and iam["protocolType"] == "MCP"
    ic = iam["interceptorConfigurations"][0]
    assert ic["interceptionPoints"] == ["REQUEST", "RESPONSE"] and ic["interceptor"]["lambda"]["arn"].endswith(":function:f")
    assert ic["inputConfiguration"] == {"passRequestHeaders": False}  # IAM: 서명은 안 넘긴다
    # JWT(Cognito) 인증 gateway (gw-ai-portal)
    jwt = ns["gateway_params"]("arn:aws:iam::1:role/r", "arn:aws:lambda:us-west-2:1:function:f", ns["PORTAL_JWT_GATEWAY_NAME"],
                               {"discovery_url": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_X/.well-known/openid-configuration", "client_id": "abc123"})
    rep = ParamValidator().validate(jwt, model.operation_model("CreateGateway").input_shape)
    assert not rep.has_errors(), rep.generate_report()
    assert jwt["authorizerType"] == "CUSTOM_JWT"
    assert jwt["authorizerConfiguration"]["customJWTAuthorizer"] == {
        "discoveryUrl": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_X/.well-known/openid-configuration", "allowedClients": ["abc123"]}
    assert jwt["interceptorConfigurations"][0]["interceptionPoints"] == ["REQUEST", "RESPONSE"]
    assert jwt["interceptorConfigurations"][0]["inputConfiguration"] == {"passRequestHeaders": True}  # 토큰의 그룹을 읽으려면 필요
    # Cognito 파라미터
    cog = boto3.client("cognito-idp", region_name="us-east-1").meta.service_model
    pairs = [("CreateUserPool", ns["cognito_pool_params"]()),
             ("CreateUserPoolClient", ns["cognito_client_params"]("us-east-1_X"))]
    for op, params in pairs:
        rep = ParamValidator().validate(params, cog.operation_model(op).input_shape)
        assert not rep.has_errors(), (op, rep.generate_report())
    c = pairs[1][1]
    assert c["AccessTokenValidity"] == 24 and c["TokenValidityUnits"]["AccessToken"] == "hours"          # Cognito 최대(86400초)
    assert c["RefreshTokenValidity"] == 3650 and c["TokenValidityUnits"]["RefreshToken"] == "days"        # Cognito 최대(약 10년)
    assert c["EnableTokenRevocation"] is True and c["GenerateSecret"] is False
    assert c["ExplicitAuthFlows"] == ["ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_ADMIN_USER_PASSWORD_AUTH"] and "AllowedOAuthFlows" not in c and "CallbackURLs" not in c      # 호스티드 UI/OAuth·federation 미사용
    assert pairs[0][1]["AdminCreateUserConfig"] == {"AllowAdminCreateUserOnly": True}                    # 셀프 가입 금지
    ws = checks[2][1]
    assert ws["targetConfiguration"]["mcp"]["connector"]["source"]["connectorId"] == "web-search"
    assert ws["credentialProviderConfigurations"] == [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]
    assert ws["name"] == "websearch"
    assert ns["PORTAL_JWT_GATEWAY_NAME"] == "gw-ai-portal" and ns["WEBSEARCH_GATEWAY_NAME"] == "gw-websearch"
    assert ns["INTERCEPTOR_FUNCTION"] == "lambda-ai-portal-intercepter" and ns["WEBSEARCH_REGION"] == "us-west-2"
    # 기존 us-west-2 역할 이름은 바뀌지 않는다(이미 만든 리소스를 재사용해야 한다)
    assert ns["interceptor_role_name"]("us-west-2") == "role-lambda-interceptor-for-ai-portal-us-west-2"
    assert ns["gateway_role_name"]("us-west-2") == "role-gateway-for-ai-portal-us-west-2"


def test_logs_who_called(lam, caplog):
    caplog.set_level(logging.INFO)
    lam.handler(req(), None)
    assert '"caller": "arn:aws:iam::111122223333:user/alice"' in "\n".join(r.getMessage() for r in caplog.records)
    caplog.clear(); ev = req(); ev["mcp"]["gatewayRequest"].pop("context")          # context 가 없어도 깨지지 않는다
    assert lam.handler(ev, None)["interceptorOutputVersion"] == "1.0"


def test_logs_the_whole_context_but_redacts_credential_like_values(lam, caplog):
    caplog.set_level(logging.INFO)
    ev = req(); ev["mcp"]["gatewayRequest"]["context"] = {"identity": {"awsPrincipalArn": "arn:aws:iam::1:user/a", "accessKeyId": "AKIASECRET"},
                                                            "extra": {"sessionToken": "TOK123", "tags": [{"k": "v"}]}}
    lam.handler(ev, None)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert '"awsPrincipalArn": "arn:aws:iam::1:user/a"' in text and '"tags": [{"k": "v"}]' in text      # 나머지는 전부 남는다
    assert "AKIASECRET" not in text and "TOK123" not in text and text.count("<redacted>") == 2


# ── 승인 검사(JWT 인증 gateway): 토큰의 사용자가 이 MCP 의 사용 승인(포털 기록)을 지금 가지고 있어야 한다 ──
GW_ID = "gw-ai-portal-us-east-1-exhuakgukh"
ITEM = 55
EMAIL = "user@x.com"


def jwt(sub="user-sub-1", **extra):
    def b64(o): return base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()
    return f"{b64({'alg': 'RS256'})}.{b64({'sub': sub, 'client_id': 'c', 'token_use': 'access', **extra})}.SIGNATURE-DO-NOT-LOG"


def jreq(token, method="tools/list", host=f"{GW_ID}.gateway.bedrock-agentcore.us-east-1.amazonaws.com", headers=None, **params):
    h = {"Host": host, "Content-Type": "application/json"}
    if token: h["Authorization"] = "Bearer " + token
    h.update(headers or {})
    body = {"jsonrpc": "2.0", "id": 5, "method": method}
    if params: body["params"] = params
    return {"interceptorInputVersion": "1.0", "mcp": {"gatewayResponse": None, "gatewayRequest": {
        "path": "/mcp", "httpMethod": "POST", "headers": h, "context": None, "body": body}}}


def blocked(out): return "transformedGatewayResponse" in out["mcp"]


import base64


class FakeAws:
    """Cognito(sub→email)와 포털 DynamoDB(MCP 항목 + data.approved_users)를 흉내 낸다.
    runtimes: [{name, approved, sk}] — 승인 필요 runtime target(예: hr_advisor → hr-advisor)."""
    def __init__(self, approved=(EMAIL,), owner="owner@x.com", gateway=GW_ID, email=EMAIL, fail=None, runtimes=None):
        self.approved = list(approved) if approved is not None else None
        self.owner, self.gateway, self.email, self.fail, self.calls = owner, gateway, email, fail, []
        self.runtimes = runtimes or []

    def admin_get_user(self, **kw):
        self.calls.append(("cognito", kw["Username"]))
        if self.fail: raise self.fail
        return {"UserAttributes": [{"Name": "email", "Value": self.email}]} if self.email else {"UserAttributes": []}

    def _gw_data(self, with_users=True):
        d = {"kind": "gateway", "gateway_id": self.gateway}
        if with_users and self.approved is not None: d["approved_users"] = self.approved
        return json.dumps(d)

    def _rt_data(self, rt, with_users=True):
        d = {"kind": "runtime", "approval_required": True, "approval_gateway_id": self.gateway}
        if with_users and rt.get("approved") is not None: d["approved_users"] = rt["approved"]
        return json.dumps(d)

    def query(self, **kw):
        self.calls.append(("query", kw["ExpressionAttributeValues"][":p"]["S"]))
        if self.fail: raise self.fail
        items = [{"sk": {"S": f"{ITEM:010d}"}, "name": {"S": "gw"}, "data": {"S": self._gw_data(False)}}]
        for i, rt in enumerate(self.runtimes):
            sk = rt.get("sk") or f"{ITEM + 1 + i:010d}"
            items.append({"sk": {"S": sk}, "name": {"S": rt["name"]}, "data": {"S": self._rt_data(rt, False)}})
        return {"Items": items}

    def get_item(self, **kw):
        sk = kw["Key"]["sk"]["S"]
        self.calls.append(("get_item", kw["Key"]["pk"]["S"], sk))
        if self.fail: raise self.fail
        if sk == f"{ITEM:010d}":
            return {"Item": {"owner": {"S": self.owner}, "name": {"S": "gw"}, "data": {"S": self._gw_data()}}}
        for i, rt in enumerate(self.runtimes):
            rsk = rt.get("sk") or f"{ITEM + 1 + i:010d}"
            if sk == rsk:
                return {"Item": {"owner": {"S": self.owner}, "name": {"S": rt["name"]}, "data": {"S": self._rt_data(rt)}}}
        return {}


@pytest.fixture
def aws(lam, monkeypatch):
    def make(**kw):
        fake = FakeAws(**kw)
        monkeypatch.setattr(lam, "TABLE", "ai-portal"); monkeypatch.setattr(lam, "POOL_ID", "us-east-1_pool")
        monkeypatch.setattr(lam, "_clients", {"cognito-idp": fake, "dynamodb": fake})
        monkeypatch.setattr(lam, "_emails", {}); monkeypatch.setattr(lam, "_ids", {"at": 0.0, "map": {}, "targets": {}})
        return fake
    return make


def test_user_listed_in_the_mcp_items_approved_users_passes(lam, aws):
    fake = aws(approved=[EMAIL, "b@x.com"])
    assert not blocked(lam.handler(jreq(jwt()), None))
    assert ("get_item", "ITEM#mcp", f"{ITEM:010d}") in fake.calls                  # 토큰의 사용자 → 이메일 → 그 MCP 항목의 승인자 목록


def test_user_not_in_the_approved_list_is_rejected_even_if_others_are(lam, aws):
    aws(approved=["someone-else@x.com"])                                             # 다른 사람만 승인됨(한 사람의 승인이 다른 사람에게 번지지 않는다)
    out = lam.handler(jreq(jwt(), method="tools/call", name="x", arguments={}), None)
    assert blocked(out)
    body = out["mcp"]["transformedGatewayResponse"]["body"]
    assert body["id"] == 5 and body["error"]["message"].startswith("이 MCP 서버의 사용이 승인되지 않았습니다")
    assert out["mcp"]["transformedGatewayResponse"]["statusCode"] == 200


def test_bound_runtime_target_requires_that_runtime_approval_not_just_gateway(lam, aws):
    """gw-ai-portal 사용 승인만으로는 hr-advisor tools/call 이 통과하면 안 된다."""
    aws(approved=[EMAIL], runtimes=[{"name": "hr_advisor", "approved": ["e2npark@gmail.com"]}])
    assert blocked(lam.handler(jreq(jwt(), method="tools/call", name="hr-advisor___sql", arguments={"q": "x"}), None))
    assert not blocked(lam.handler(jreq(jwt(), method="tools/call", name="knowledge-mcp___search", arguments={}), None))
    assert not blocked(lam.handler(jreq(jwt(), method="tools/list"), None))           # gateway 승인이 있으면 목록 OK
    # runtime 승인이 있으면 그 target 호출은 통과(gateway 승인 없어도)
    aws(approved=[], runtimes=[{"name": "hr_advisor", "approved": [EMAIL]}])
    assert not blocked(lam.handler(jreq(jwt(), method="tools/call", name="hr-advisor___sql", arguments={}), None))
    assert blocked(lam.handler(jreq(jwt(), method="tools/call", name="knowledge-mcp___search", arguments={}), None))
    assert not blocked(lam.handler(jreq(jwt(), method="tools/list"), None))           # runtime 만 승인돼도 목록은 허용


def test_missing_or_empty_approved_list_rejects_everyone_but_the_owner(lam, aws):
    for lst in (None, []):
        aws(approved=lst); assert blocked(lam.handler(jreq(jwt()), None)), lst


def test_cognito_group_in_the_token_or_pool_is_irrelevant(lam, aws):
    aws(approved=[])
    assert blocked(lam.handler(jreq(jwt(**{"cognito:groups": [f"mcp:{GW_ID}"]})), None))


def test_mcp_owner_is_blocked_without_an_approval(lam, aws):
    aws(approved=[], owner=EMAIL)                                           # 소유자도 사용 승인이 있어야 통과한다
    assert blocked(lam.handler(jreq(jwt()), None))
    aws(approved=[EMAIL], owner=EMAIL)
    assert not blocked(lam.handler(jreq(jwt()), None))


def test_approval_is_per_server(lam, aws):
    aws(approved=[EMAIL])
    other = "gw-other-111111"
    assert blocked(lam.handler(jreq(jwt(), host=f"{other}.gateway.bedrock-agentcore.us-east-1.amazonaws.com"), None))   # 포털에 없는 gateway


def test_lookup_failure_or_unknown_email_is_rejected_fail_closed(lam, aws):
    aws(fail=RuntimeError("down")); assert blocked(lam.handler(jreq(jwt()), None))
    aws(email=""); assert blocked(lam.handler(jreq(jwt()), None))


def test_connection_handshake_is_not_blocked_and_does_not_query(lam, aws):
    fake = aws(approved=[])
    for m in ("initialize", "notifications/initialized", "ping"):
        assert not blocked(lam.handler(jreq(jwt(), method=m), None)), m
    assert fake.calls == []
    for m in ("tools/list", "tools/call", "resources/list", "prompts/get"):
        assert blocked(lam.handler(jreq(jwt(), method=m), None)), m


def test_missing_or_garbage_token_is_rejected_fail_closed(lam, aws):
    aws()
    assert blocked(lam.handler(jreq(None), None))
    assert blocked(lam.handler(jreq(None, headers={"Authorization": "Bearer not.a.jwt"}), None))
    assert blocked(lam.handler(jreq("a.b.c"), None))


def test_iam_callers_are_not_subject_to_the_check(lam):
    ev = jreq(None); ev["mcp"]["gatewayRequest"]["context"] = {"identity": {"awsPrincipalArn": "arn:aws:iam::1:user/a"}}
    assert not blocked(lam.handler(ev, None))


def test_responses_are_never_blocked(lam):
    ev = resp(); ev["mcp"]["gatewayRequest"]["headers"] = {"Host": f"{GW_ID}.gateway.x.amazonaws.com"}
    assert "transformedGatewayResponse" in lam.handler(ev, None)["mcp"] and lam.handler(ev, None)["mcp"]["transformedGatewayResponse"]["statusCode"] == 200


def test_the_token_never_reaches_the_logs(lam, aws, caplog):
    aws(approved=[EMAIL]); caplog.set_level(logging.INFO)
    lam.handler(jreq(jwt(), method="tools/call", name="x", arguments={"q": "TOP-SECRET-VALUE"}), None)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "SIGNATURE-DO-NOT-LOG" not in text and "TOP-SECRET-VALUE" not in text and "Bearer" not in text


def test_new_approval_is_seen_without_waiting_for_a_cache(lam, aws):
    fake = aws(approved=[])
    assert blocked(lam.handler(jreq(jwt()), None))
    fake.approved = [EMAIL]                                                          # 포털이 승인자 목록에 추가
    assert not blocked(lam.handler(jreq(jwt()), None))
    fake.approved = []                                                               # 취소
    assert blocked(lam.handler(jreq(jwt()), None))


# ── 요청 로그(Firehose) ──
class _FH:
    def __init__(self): self.rows = []
    def put_record(self, DeliveryStreamName, Record): self.rows.append((DeliveryStreamName, json.loads(Record["Data"])))


@pytest.fixture
def fh(lam, monkeypatch):
    f = _FH()
    monkeypatch.setattr(lam, "LOG_STREAM", "stream-x")
    monkeypatch.setattr(lam, "IAM_GATEWAY_ID", "gw-iam-1")
    lam._clients["firehose"] = f
    return f


def test_request_and_response_are_two_rows_with_the_same_join_key(lam, fh):
    lam.handler(req(), None)
    rs = resp(); rs["mcp"]["gatewayRequest"]["body"] = req()["mcp"]["gatewayRequest"]["body"]
    lam.handler(rs, None)
    (n1, q), (n2, r) = fh.rows
    assert n1 == n2 == "stream-x" and q["point"] == "REQUEST" and r["point"] == "RESPONSE"
    assert q["rpc_id"] == r["rpc_id"] == "1" and q["req_hash"] == r["req_hash"] != ""
    assert q["kind"] == "mcp" and q["target"] == "gw-iam-1" and q["dt"] == q["ts"][:10]
    assert q["tool"] == "search" and q["mcp_target"] == "target-ai-portal" and q["blocked"] is False
    assert q["caller"] == "arn:aws:iam::111122223333:user/alice" and q["arguments"] == '{"q": "TOP-SECRET-VALUE"}'
    assert r["status"] == 200 and r["is_error"] is False and r["resp_bytes"] > 0


def test_log_row_never_has_headers_or_response_body_and_scrubs_credentials(lam, fh):
    body = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "t___x", "arguments": {
        "api_key": "K123", "note": "use Bearer abcdefghijklmnop1234 and AKIAABCDEFGHIJKLMNOP", "ok": "hello"}}}
    lam.handler(req(body=body), None); lam.handler(resp(), None)
    text = json.dumps(fh.rows)
    assert "SECRET-SIG" not in text and "SECRET-RESULT" not in text and "K123" not in text
    assert "abcdefghijklmnop1234" not in text and "AKIAABCDEFGHIJKLMNOP" not in text
    assert json.loads(fh.rows[0][1]["arguments"])["ok"] == "hello"        # 나머지 값은 그대로 남는다


def test_blocked_request_is_logged_with_reason_and_email(lam, fh, monkeypatch):
    monkeypatch.setattr(lam, "_email", lambda claims: "bad@x.com")
    out = lam.handler(jreq(jwt(), method="tools/call", name="s___t", arguments={"a": 1}), None)
    assert blocked(out)
    row = fh.rows[0][1]
    assert row["blocked"] is True and row["user_email"] == "bad@x.com" and row["auth"] == "jwt" and row["block_reason"]
    assert row["target"] == GW_ID                                          # Host 헤더에서
    assert "SIGNATURE-DO-NOT-LOG" not in json.dumps(row)


def test_log_failure_never_blocks_the_request(lam, monkeypatch):
    class Boom:
        def put_record(self, **kw): raise RuntimeError("firehose down")
    monkeypatch.setattr(lam, "LOG_STREAM", "s"); lam._clients["firehose"] = Boom()
    ev = req()
    assert lam.handler(ev, None)["mcp"]["transformedGatewayRequest"]["body"] == ev["mcp"]["gatewayRequest"]["body"]
    assert lam.handler(resp(), None)["interceptorOutputVersion"] == "1.0"


def test_no_stream_means_no_logging_and_target_is_path_safe(lam):
    assert lam.LOG_STREAM == "" and lam.handler(req(), None)["interceptorOutputVersion"] == "1.0"
    assert lam._safe_part("../../x/y") == ".._.._x_y" and lam._safe_part("") == "unknown"
