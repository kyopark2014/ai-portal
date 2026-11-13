"""AI Portal Gateway interceptor (lambda-ai-portal-intercepter).

AgentCore Gateway `gw-ai-portal` 로 들어오는 MCP 요청(REQUEST)과 나가는 응답(RESPONSE)을 관찰해 CloudWatch Logs 에 남긴다.
지금은 모두 그대로 통과시킨다. 추후 특정 MCP(target/tool)로 가는 흐름을 accept / block 하려면 decide() 만 바꾸면 된다.

로그에는 호출자(IAM principal ARN)·context 전체(자격 증명으로 보이는 키의 값은 가림)·메서드·tool 이름·인자 이름(키)·응답 상태만 남기고, 헤더(SigV4 서명)·인자 값·응답 본문은 남기지 않는다.
실측: 이벤트는 {"interceptorInputVersion","mcp":{"gatewayRequest":{body,headers,path,httpMethod,context},"gatewayResponse":null|{...},"rawGatewayRequest"}}.
이벤트/출력 형식: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors-types.html
"""
import base64
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

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
# map: gateway_id -> sk / targets: "gateway_id|target-name" -> sk (승인 필요 runtime)
_ids = {"at": 0.0, "map": {}, "targets": {}}
ID_CACHE_SECONDS = 60
# ── 요청 로그(Firehose → S3 → Athena). 값이 없으면 기록하지 않는다 ──
LOG_STREAM = os.environ.get("LOG_STREAM", "")                # Firehose delivery stream 이름
LOG_REGION = os.environ.get("LOG_REGION", "") or "us-west-2"  # stream 이 있는 리전(gateway 리전과 다를 수 있다)
IAM_GATEWAY_ID = os.environ.get("IAM_GATEWAY_ID", "")        # 이 리전의 IAM gateway. IAM 호출은 Host 헤더가 없어 gateway 를 이것으로 안다
MAX_ARG_CHARS = 4000
MISS_REFRESH_SECONDS = 10                                   # 모르는 gateway 때문에 매 요청 목록을 훑지 않도록


def _client(name: str, region: str):
    if name not in _clients:
        import boto3
        _clients[name] = boto3.client(name, region_name=region)
    return _clients[name]


def _target_slug(name: str) -> str:
    """포털 MCP 이름 → Gateway target 이름(영문·숫자·하이픈). aws.ensure_runtime_gateway_target 과 같다."""
    return re.sub(r"[^A-Za-z0-9]+", "-", name or "").strip("-")[:100]


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
    ddb, gateways, targets, start = _client("dynamodb", TABLE_REGION), {}, {}, None
    while True:
        r = ddb.query(TableName=TABLE, KeyConditionExpression="pk = :p", ExpressionAttributeValues={":p": {"S": "ITEM#mcp"}},
                      ProjectionExpression="sk, #n, #d", ExpressionAttributeNames={"#d": "data", "#n": "name"},
                      **({"ExclusiveStartKey": start} if start else {}))
        for it in r.get("Items", []):
            try:
                d = json.loads(it.get("data", {}).get("S") or "{}")
            except ValueError:
                continue
            sk = it["sk"]["S"]
            if d.get("kind") == "gateway" and d.get("gateway_id"):
                gateways[d["gateway_id"]] = sk
            elif d.get("kind") == "runtime" and d.get("approval_required") and d.get("approval_gateway_id"):
                slug = _target_slug(it.get("name", {}).get("S") or "")
                if slug:
                    targets[f"{d['approval_gateway_id']}|{slug}"] = sk
        start = r.get("LastEvaluatedKey")
        if not start:
            break
    _ids["map"], _ids["targets"], _ids["at"] = gateways, targets, time.time()


def _load_item(sk: str) -> dict | None:
    """MCP 항목 sk 로 승인자 목록을 지금 읽는다. 승인 취소가 바로 반영되도록 내용은 캐시하지 않는다."""
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


def _maybe_refresh(gw: str, target: str = "") -> None:
    age = time.time() - _ids["at"]
    need = age > ID_CACHE_SECONDS
    if not need and gw and gw not in _ids["map"] and age > MISS_REFRESH_SECONDS:
        need = True
    if not need and target and f"{gw}|{target}" not in _ids.get("targets", {}) and age > MISS_REFRESH_SECONDS:
        need = True
    if need:
        _refresh_ids()


def _gateway_item(gw: str) -> dict | None:
    _maybe_refresh(gw)
    sk = _ids["map"].get(gw)
    return _load_item(sk) if sk else None


def _target_item(gw: str, target: str) -> dict | None:
    """승인 필요 runtime 이 이 gateway target 으로 묶여 있으면 그 MCP 항목. 없으면 None(gateway 기본 target)."""
    if not target:
        return None
    _maybe_refresh(gw, target)
    sk = (_ids.get("targets") or {}).get(f"{gw}|{target}")
    return _load_item(sk) if sk else None


def _any_target_approved(gw: str, email: str) -> bool:
    """이 gateway 에 묶인 승인 필요 runtime 중 하나라도 이 사용자가 승인됐는가(tools/list 용)."""
    _maybe_refresh(gw)
    prefix = f"{gw}|"
    for key, sk in list((_ids.get("targets") or {}).items()):
        if not key.startswith(prefix):
            continue
        item = _load_item(sk)
        if item and email in item["approved"]:
            return True
    return False


def _is_approved(claims: dict, gw: str, target: str = "") -> bool:
    """토큰 사용자가 이 요청을 쓸 수 있는가.
    tools/call 의 target 이 승인 필요 runtime 이면 그 runtime 의 approved_users 만 본다.
    그 외(knowledge-mcp·tools/list 등)는 gateway 의 approved_users(또는 묶인 runtime 승인)를 본다.
    소유자도 예외 없이 승인 필요. 확인 못하면 False(fail-closed)."""
    try:
        email = _email(claims)
        if not email:
            return False
        if target:
            bound = _target_item(gw, target)
            if bound is not None:
                return email in bound["approved"]
        item = _gateway_item(gw)
        if item and email in item["approved"]:
            return True
        if not target:                                         # tools/list 등: runtime 만 승인된 사용자도 목록은 볼 수 있다
            return _any_target_approved(gw, email)
        return False
    except Exception as e:  # noqa: BLE001  (조회 실패·권한·네트워크 모두 거절)
        log.warning(json.dumps({"interceptor": "ai-portal", "approval_lookup_failed": type(e).__name__}))
        return False


def _gateway_id(mcp: dict) -> str:
    """요청이 들어온 gateway. Host 헤더(<gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com)에서 읽는다."""
    h = (mcp.get("gatewayRequest") or {}).get("headers") or {}
    host = next((v for k, v in h.items() if str(k).lower() == "host"), "")
    return host.split(".")[0] if isinstance(host, str) else ""


def _rpc_target(rpc: dict) -> str:
    """tools/call 의 Gateway tool 이름(`<target>___<tool>`)에서 target. 없으면 ''."""
    if (rpc.get("method") or "") != "tools/call":
        return ""
    params = rpc.get("params") if isinstance(rpc.get("params"), dict) else {}
    name = params.get("name") if isinstance(params.get("name"), str) else ""
    return name.partition("___")[0] if "___" in name else ""


def decide(point: str, rpc: dict, mcp: dict | None = None) -> dict:
    """흐름을 허용할지 판단한다. 반환: {"action": "accept"} 또는 {"action": "block", "message": "..."}.

    JWT 인증 gateway: 토큰으로 사용자를 알아낸 뒤, 호출 target 별 승인자 목록(data.approved_users)을 본다.
    승인 필요 runtime(target)이면 그 MCP 승인만, 아니면 gateway(또는 묶인 runtime) 승인.
    없거나 확인할 수 없으면 거절(fail-closed). Cognito 그룹은 보지 않는다.
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
    if gw and _is_approved(claims, gw, _rpc_target(rpc)):
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


# ── 요청 로그 ────────────────────────────────────────────────────────
# REQUEST 와 RESPONSE 를 각각 한 행으로 Firehose 에 보낸다. 포털(Athena)이 (target, rpc_id, req_hash) 로 두 행을 묶는다.
#   rpc_id 는 클라이언트마다 1,2,3… 으로 시작해 겹치므로 요청 본문의 해시(req_hash)를 함께 쓴다.
# CloudWatch 로그와 달리 여기에는 인자 값이 들어간다(자격 증명 키·토큰·AWS 키 형태는 가림). 응답 본문은 저장하지 않는다.
# 기록 실패는 요청을 막지 않는다(짧은 타임아웃, 예외는 삼킨다).
_VALUE_SECRET = re.compile(r"(eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|Bearer\s+[A-Za-z0-9._~+/=-]{12,})")


def _scrub(o):
    """인자 값에서 자격 증명으로 보이는 키의 값과, 값 자체가 JWT·AWS 키·Bearer 토큰 모양인 문자열을 가린다."""
    if isinstance(o, dict):
        return {k: ("<redacted>" if _SENSITIVE.search(str(k)) and not isinstance(v, (dict, list)) else _scrub(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [_scrub(x) for x in o]
    return _VALUE_SECRET.sub("<redacted>", o) if isinstance(o, str) else o


def _safe_part(v) -> str:
    """S3 경로(Firehose 파티션 값)에 쓸 수 있는 문자만 남긴다."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(v or ""))[:64] or "unknown"


def _gateway_for_log(mcp: dict) -> str:
    gw = _gateway_id(mcp)
    if gw:
        return gw
    ctx = (mcp.get("gatewayRequest") or {}).get("context")
    return IAM_GATEWAY_ID if IAM_GATEWAY_ID and isinstance(ctx, dict) and ctx.get("identity") else ""


def _req_hash(body) -> str:
    """REQUEST·RESPONSE 행을 묶는 보조 키. 본문 전체가 아니라 바뀔 수 없는 값(method·id·tool 이름)만 해시한다 —
    RESPONSE 이벤트의 gatewayRequest.body 가 요청과 바이트 단위로 같다는 보장이 없기 때문이다."""
    rpc = _rpc(body)
    params = rpc.get("params") if isinstance(rpc.get("params"), dict) else {}
    key = "|".join(str(x) for x in (rpc.get("method"), rpc.get("id"), params.get("name")))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _log_row(point: str, mcp: dict, rpc: dict, verdict: dict | None = None) -> dict:
    greq = mcp.get("gatewayRequest") or {}
    params = rpc.get("params") if isinstance(rpc.get("params"), dict) else {}
    name = params.get("name") if isinstance(params.get("name"), str) else ""
    mcp_target, _, tool = name.partition("___") if "___" in name else ("", "", name)
    claims = _bearer_claims(greq.get("headers"))
    email = None
    if claims is not None and (rpc.get("method") or "") not in OPEN_METHODS:
        try:
            email = _email(claims)                           # decide() 가 이미 조회해 캐시에 있다
        except Exception:  # noqa: BLE001
            email = None
    gw = _gateway_for_log(mcp)
    now = datetime.now(timezone.utc)
    row = {"ts": now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", "dt": now.strftime("%Y-%m-%d"), "kind": "mcp", "target": _safe_part(gw),
           "point": point, "rpc_id": "" if rpc.get("id") is None else str(rpc.get("id")), "req_hash": _req_hash(greq.get("body")),
           "gateway": gw, "method": rpc.get("method") or "", "mcp_target": mcp_target, "tool": tool,
           "caller": _caller(mcp) or "", "user_email": email or "", "auth": "jwt" if claims is not None else "iam"}
    if point == "REQUEST":
        args = params.get("arguments")
        if args is not None:
            text = json.dumps(_scrub(args), ensure_ascii=False)
            row["arguments"] = text if len(text) <= MAX_ARG_CHARS else text[:MAX_ARG_CHARS] + "…(truncated)"
        row["blocked"] = bool(verdict and verdict.get("action") == "block")
        if row["blocked"]:
            row["block_reason"] = (verdict or {}).get("message", "")
    else:
        resp = mcp.get("gatewayResponse") or {}
        body = _rpc(resp.get("body"))
        err = body.get("error") if isinstance(body.get("error"), dict) else None
        row.update({"status": resp.get("statusCode"), "is_error": bool(body.get("error")) or bool((body.get("result") or {}).get("isError")),
                    "error_message": str(err.get("message", ""))[:300] if err else "",
                    "resp_bytes": len(json.dumps(resp.get("body"), ensure_ascii=False)) if resp.get("body") is not None else 0})
    return row


def _emit(row: dict) -> None:
    if not LOG_STREAM:
        return
    try:
        if "firehose" not in _clients:
            import boto3
            from botocore.config import Config
            _clients["firehose"] = boto3.client("firehose", region_name=LOG_REGION,
                                                config=Config(connect_timeout=2, read_timeout=3, retries={"max_attempts": 2}))
        _clients["firehose"].put_record(DeliveryStreamName=LOG_STREAM, Record={"Data": json.dumps(row, ensure_ascii=False).encode()})
    except Exception as e:  # noqa: BLE001 — 기록 실패로 요청을 막지 않는다
        log.warning(json.dumps({"interceptor": "ai-portal", "log_emit_failed": type(e).__name__}))


def _emit_safe(point: str, mcp: dict, rpc: dict, verdict: dict | None = None) -> None:
    if not LOG_STREAM:
        return
    try:
        _emit(_log_row(point, mcp, rpc, verdict))
    except Exception as e:  # noqa: BLE001
        log.warning(json.dumps({"interceptor": "ai-portal", "log_row_failed": type(e).__name__}))


def handler(event, _ctx):
    mcp = event.get("mcp") or {}
    is_response = bool(mcp.get("gatewayResponse"))               # REQUEST 이벤트에도 키는 있고 값이 null 이다(실측)
    point = "RESPONSE" if is_response else "REQUEST"
    req = _rpc((mcp.get("gatewayRequest") or {}).get("body"))
    log.info(json.dumps({"interceptor": "ai-portal", **_summary(point, mcp, req)}, ensure_ascii=False))

    if is_response:                                             # 응답은 그대로 돌려보낸다
        _emit_safe("RESPONSE", mcp, req)
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
    _emit_safe("REQUEST", mcp, req, verdict)
    if verdict["action"] == "block":                            # transformedGatewayResponse 가 있으면 target 으로 가지 않고 바로 응답한다
        log.info(json.dumps({"interceptor": "ai-portal", "blocked": True, "method": req.get("method"), "caller": _caller(mcp),
                             "gateway": _gateway_id(mcp)}))
        return {"interceptorOutputVersion": "1.0", "mcp": {"transformedGatewayResponse": {
            "statusCode": 200, "headers": {"Content-Type": "application/json"},
            "body": {"jsonrpc": "2.0", "id": req.get("id"), "error": {"code": -32000, "message": verdict.get("message", "blocked")}}}}}
    return {"interceptorOutputVersion": "1.0",
            "mcp": {"transformedGatewayRequest": {"body": (mcp.get("gatewayRequest") or {}).get("body", {})}}}
