"""AI Portal Gateway interceptor (lambda-ai-portal-intercepter).

AgentCore Gateway `gw-ai-portal` 로 들어오는 MCP 요청(REQUEST)과 나가는 응답(RESPONSE)을 관찰해 CloudWatch Logs 에 남긴다.
지금은 모두 그대로 통과시킨다. 추후 특정 MCP(target/tool)로 가는 흐름을 accept / block 하려면 decide() 만 바꾸면 된다.

로그에는 호출자(IAM principal ARN)·context 전체(자격 증명으로 보이는 키의 값은 가림)·메서드·tool 이름·인자 이름(키)·응답 상태만 남기고, 헤더(SigV4 서명)·인자 값·응답 본문은 남기지 않는다.
실측: 이벤트는 {"interceptorInputVersion","mcp":{"gatewayRequest":{body,headers,path,httpMethod,context},"gatewayResponse":null|{...},"rawGatewayRequest"}}.
이벤트/출력 형식: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors-types.html
"""
import base64
import json
import logging
import os
import re
import time

log = logging.getLogger()
log.setLevel(logging.INFO)


ENFORCE = os.environ.get("ENFORCE_GROUPS", "1") != "0"      # 0 이면 검사하지 않고 로그만(롤백용)
# 연결 정보(initialize·ping·알림)는 어떤 MCP 를 쓰는지가 아직 정해지지 않았으므로 승인 없이 통과시킨다. 실제 목록·호출은 검사한다.
OPEN_METHODS = {"initialize", "ping", "notifications/initialized", "notifications/cancelled"}


def _bearer_claims(headers) -> dict | None:
    """Authorization: Bearer <JWT> 의 페이로드. 서명은 Gateway 가 이미 검증했으므로(통과한 요청만 여기에 온다) 디코딩만 한다.
    토큰 자체는 로그·반환값에 남기지 않는다. 읽을 수 없으면 None."""
    if not isinstance(headers, dict):
        return None
    raw = next((v for k, v in headers.items() if str(k).lower() == "authorization"), "")
    if not isinstance(raw, str) or not raw.lower().startswith("bearer "):
        return None
    try:
        seg = raw.split(" ", 1)[1].strip().split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
        return claims if isinstance(claims, dict) else None
    except (IndexError, ValueError):
        return None


TABLE = os.environ.get("DDB_TABLE", "")                     # 포털의 DynamoDB(사용 신청 기록이 있는 곳)
TABLE_REGION = os.environ.get("DDB_REGION", "") or "us-west-2"
POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")        # 토큰의 sub 로 사용자 이메일을 찾는다
POOL_REGION = os.environ.get("COGNITO_REGION", "") or "us-east-1"
_clients: dict = {}
_emails: dict = {}                                          # sub -> email (바뀌지 않으므로 캐시)
_ids = {"at": 0.0, "map": {}}                               # gateway id -> 포털 MCP 항목 번호 (번호는 바뀌지 않아 잠깐 캐시)
ID_CACHE_SECONDS = 60
MISS_REFRESH_SECONDS = 10                                   # 모르는 gateway 때문에 매 요청 목록을 훑지 않도록


def _client(name: str, region: str):
    if name not in _clients:
        import boto3
        _clients[name] = boto3.client(name, region_name=region)
    return _clients[name]


def _email(claims: dict) -> str | None:
    """토큰의 사용자 이메일. 액세스 토큰에는 이메일이 없어 sub 로 Cognito 에서 찾는다."""
    sub = claims.get("username") or claims.get("sub")
    if not isinstance(sub, str) or not sub:
        return None
    if sub not in _emails:
        r = _client("cognito-idp", POOL_REGION).admin_get_user(UserPoolId=POOL_ID, Username=sub)
        mail = next((a["Value"] for a in r.get("UserAttributes", []) if a["Name"] == "email"), "")
        if not mail:
            return None
        _emails[sub] = mail.lower()
    return _emails[sub]


def _refresh_ids() -> None:
    ddb, found, start = _client("dynamodb", TABLE_REGION), {}, None
    while True:
        r = ddb.query(TableName=TABLE, KeyConditionExpression="pk = :p", ExpressionAttributeValues={":p": {"S": "ITEM#mcp"}},
                      ProjectionExpression="sk, #d", ExpressionAttributeNames={"#d": "data"}, **({"ExclusiveStartKey": start} if start else {}))
        for it in r.get("Items", []):
            try:
                d = json.loads(it.get("data", {}).get("S") or "{}")
            except ValueError:
                continue
            if d.get("kind") == "gateway" and d.get("gateway_id"):
                found[d["gateway_id"]] = it["sk"]["S"]
        start = r.get("LastEvaluatedKey")
        if not start:
            break
    _ids["map"], _ids["at"] = found, time.time()


def _gateway_item(gw: str) -> dict | None:
    """이 gateway 의 포털 MCP 항목(승인자 목록 포함)을 지금 읽는다. 승인 취소가 바로 반영되도록 항목 내용은 캐시하지 않는다."""
    age = time.time() - _ids["at"]
    if gw not in _ids["map"] and age > MISS_REFRESH_SECONDS or age > ID_CACHE_SECONDS:
        _refresh_ids()
    sk = _ids["map"].get(gw)
    if not sk:
        return None
    r = _client("dynamodb", TABLE_REGION).get_item(TableName=TABLE, ConsistentRead=True, Key={"pk": {"S": "ITEM#mcp"}, "sk": {"S": sk}})
    it = r.get("Item")
    if not it:
        return None
    try:
        d = json.loads(it.get("data", {}).get("S") or "{}")
    except ValueError:
        return None
    users = d.get("approved_users")
    return {"owner": (it.get("owner", {}).get("S") or "").lower(),
            "approved": {u.lower() for u in users if isinstance(u, str)} if isinstance(users, list) else set()}


def _is_approved(claims: dict, gw: str) -> bool:
    """토큰의 사용자가 이 gateway(MCP)의 승인자 목록(MCP 항목의 data.approved_users)에 있는가. MCP 소유자도 통과(포털과 같은 기준).
    무엇이든 확인하지 못하면 False(fail-closed)."""
    try:
        item = _gateway_item(gw)
        email = _email(claims)
        if not item or not email:
            return False
        return email == item["owner"] or email in item["approved"]
    except Exception as e:  # noqa: BLE001  (조회 실패·권한·네트워크 모두 거절)
        log.warning(json.dumps({"interceptor": "ai-portal", "approval_lookup_failed": type(e).__name__}))
        return False


def _gateway_id(mcp: dict) -> str:
    """요청이 들어온 gateway. Host 헤더(<gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com)에서 읽는다."""
    h = (mcp.get("gatewayRequest") or {}).get("headers") or {}
    host = next((v for k, v in h.items() if str(k).lower() == "host"), "")
    return host.split(".")[0] if isinstance(host, str) else ""


def decide(point: str, rpc: dict, mcp: dict | None = None) -> dict:
    """흐름을 허용할지 판단한다. 반환: {"action": "accept"} 또는 {"action": "block", "message": "..."}.

    JWT 인증 gateway: 토큰으로 사용자를 알아낸 뒤, 포털이 MCP 항목에 기록한 승인자 목록(data.approved_users)에 들어 있어야 한다(소유자도 통과).
    없거나 확인할 수 없으면 거절(fail-closed). 포털에서 승인을 취소하면 이미 발급된 토큰도 바로 거절된다. Cognito 그룹은 보지 않는다.
    IAM 인증 gateway(토큰이 없고 context.identity 가 있음)는 이 검사 대상이 아니다.
    """
    mcp = mcp or {}
    ctx = (mcp.get("gatewayRequest") or {}).get("context") or {}
    claims = _bearer_claims((mcp.get("gatewayRequest") or {}).get("headers"))
    if claims is None:
        if isinstance(ctx, dict) and ctx.get("identity"):
            return {"action": "accept"}                             # IAM 호출자: 이 검사의 대상이 아니다
        if (rpc.get("method") or "") in OPEN_METHODS:
            return {"action": "accept"}
        return {"action": "block", "message": "인증 정보를 확인할 수 없어 요청을 거절했습니다"}
    if (rpc.get("method") or "") in OPEN_METHODS:
        return {"action": "accept"}
    gw = _gateway_id(mcp)
    if gw and _is_approved(claims, gw):
        return {"action": "accept"}
    return {"action": "block", "message": "이 MCP 서버의 사용이 승인되지 않았습니다. 포털에서 사용 신청 후 승인을 받으세요"}


def _rpc(body) -> dict:
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return {}
    return body if isinstance(body, dict) else {}


_SENSITIVE = re.compile(r"(secret|token|authorization|signature|password|credential|api[-_]?key|access[-_]?key)", re.I)


def _redact(o):
    """context 전체를 남기되, 자격 증명으로 보이는 키의 값은 가린다(키 이름은 남긴다)."""
    if isinstance(o, dict):
        return {k: ("<redacted>" if _SENSITIVE.search(str(k)) and not isinstance(v, (dict, list)) else _redact(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [_redact(x) for x in o]
    return o


def _caller(mcp: dict) -> str | None:
    """호출자. IAM 인증이면 context.identity.awsPrincipalArn, JWT 인증이면 토큰의 Cognito 사용자(sub). 토큰 자체는 남기지 않는다."""
    ctx = (mcp.get("gatewayRequest") or {}).get("context") or {}
    ident = ctx.get("identity") if isinstance(ctx, dict) else None
    arn = ident.get("awsPrincipalArn") if isinstance(ident, dict) else None
    if isinstance(arn, str):
        return arn
    claims = _bearer_claims((mcp.get("gatewayRequest") or {}).get("headers"))
    sub = claims.get("sub") if claims else None
    return f"cognito:{sub}" if isinstance(sub, str) else None


def _summary(point: str, mcp: dict, rpc: dict) -> dict:
    params = rpc.get("params") if isinstance(rpc.get("params"), dict) else {}
    args = params.get("arguments")
    out = {"point": point, "path": (mcp.get("gatewayRequest") or {}).get("path"), "method": rpc.get("method"),
           "id": rpc.get("id"), "caller": _caller(mcp),
           "context": _redact((mcp.get("gatewayRequest") or {}).get("context")), "tool": params.get("name"), "argument_keys": sorted(args) if isinstance(args, dict) else None}
    if point == "RESPONSE":
        resp = mcp.get("gatewayResponse") or {}
        out["status"] = resp.get("statusCode")
        out["is_error"] = bool(_rpc(resp.get("body")).get("error"))
    return {k: v for k, v in out.items() if v is not None}


def handler(event, _ctx):
    mcp = event.get("mcp") or {}
    is_response = bool(mcp.get("gatewayResponse"))               # REQUEST 이벤트에도 키는 있고 값이 null 이다(실측)
    point = "RESPONSE" if is_response else "REQUEST"
    req = _rpc((mcp.get("gatewayRequest") or {}).get("body"))
    log.info(json.dumps({"interceptor": "ai-portal", **_summary(point, mcp, req)}, ensure_ascii=False))

    if is_response:                                             # 응답은 그대로 돌려보낸다
        resp = mcp.get("gatewayResponse") or {}
        out = {"body": resp.get("body", {})}
        if resp.get("statusCode") is not None:
            out["statusCode"] = resp["statusCode"]
        if resp.get("headers"):
            out["headers"] = resp["headers"]
        return {"interceptorOutputVersion": "1.0", "mcp": {"transformedGatewayResponse": out}}

    verdict = decide(point, req, mcp)
    if verdict["action"] == "block" and not ENFORCE:
        log.info(json.dumps({"interceptor": "ai-portal", "would_block": True, "method": req.get("method"), "reason": verdict.get("message")}))
        verdict = {"action": "accept"}
    if verdict["action"] == "block":                            # transformedGatewayResponse 가 있으면 target 으로 가지 않고 바로 응답한다
        log.info(json.dumps({"interceptor": "ai-portal", "blocked": True, "method": req.get("method"), "caller": _caller(mcp),
                             "gateway": _gateway_id(mcp)}))
        return {"interceptorOutputVersion": "1.0", "mcp": {"transformedGatewayResponse": {
            "statusCode": 200, "headers": {"Content-Type": "application/json"},
            "body": {"jsonrpc": "2.0", "id": req.get("id"), "error": {"code": -32000, "message": verdict.get("message", "blocked")}}}}}
    return {"interceptorOutputVersion": "1.0",
            "mcp": {"transformedGatewayRequest": {"body": (mcp.get("gatewayRequest") or {}).get("body", {})}}}
