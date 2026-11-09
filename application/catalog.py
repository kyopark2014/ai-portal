"""레지스트리 탐색 REST: prompt / mcp / agent / skill 카탈로그 + 동기화 + Registry 등록."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Literal, Optional
from urllib.parse import urlparse

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import admins, auth, aws, db, policy

log = logging.getLogger("catalog")
router = APIRouter(prefix="/api")
Kind = Literal["prompt", "mcp", "agent", "skill"]


def aws_err(e: Exception) -> HTTPException:
    if isinstance(e, ClientError):
        code = e.response.get("Error", {}).get("Code", "")
        msg = e.response.get("Error", {}).get("Message", str(e))
        status = 404 if "NotFound" in code else 403 if "AccessDenied" in code else 400
        return HTTPException(status, f"{code}: {msg}")
    return HTTPException(502, f"AWS 호출 실패: {type(e).__name__}")


def visible_item(item_id: int, user: auth.User, kind: str | None = None) -> dict:
    """사용자에게 보이는 항목만 돌려준다. 없거나 볼 권한이 없으면 똑같이 404 (존재 여부를 알리지 않는다)."""
    it = db.get_item(item_id)
    if not it or (kind and it["kind"] != kind) or not db.is_visible(it, user.email, user.role == "admin"):
        raise HTTPException(404, "not found")
    return it


DEFAULT_HARNESS_PROMPT = "harness_prompt"
HARNESS_DESCRIPTIONS = {"ai_portal": "AI Portal 운영을 위한 Agent"}


def harness_system_prompt(user: auth.User, prompt_id: int | None = None) -> list[dict] | None:
    """harness 호출에 쓸 systemPrompt. 지정한 prompt > 기본 prompt(`harness_prompt`) > None(harness 자체 기본값).

    지정한 prompt 는 사용자가 볼 수 있는 것이어야 한다(없거나 못 보면 404). 기본 prompt 는 시스템이 고른 것이라
    사용자의 가시성과 무관하게 쓰되, 승인(APPROVED)된 것만 쓴다 — 아직 승인되지 않은 내용이 모두에게 적용되면 안 된다.
    본문은 포털 DB 의 사본이 아니라 Prompt Management 에서 읽는다(수정이 바로 반영되고, 사본이 낡을 일이 없다).
    """
    if prompt_id is not None:
        it = visible_item(prompt_id, user, "prompt")
    else:
        it = next((i for i in db.list_items("prompt") if i["name"] == DEFAULT_HARNESS_PROMPT
                   and i["registry_status"] == "APPROVED"), None)
        if it is None:
            return None
    try:
        text = aws.prompt_from_get(aws.get_prompt(it["data"]["prompt_id"]))["text"]
    except (ClientError, BotoCoreError) as e:
        if prompt_id is not None:
            raise aws_err(e)                          # 사용자가 직접 고른 것이 읽히지 않으면 알려준다
        log.warning("default harness prompt unreadable, using harness default: %s", type(e).__name__)
        return None
    text = aws.render_prompt(text, {})
    return [{"text": text}] if text.strip() else None


def _can_edit(user: auth.User, item: dict) -> bool:
    return user.role == "admin" or item["owner"] == user.email


def _view(item: dict, user: auth.User) -> dict:
    out = {**item, "can_edit": _can_edit(user, item)}
    # status_at: 카드에 보여줄 '상태가 바뀐 시각'. 동기화·새로고침으로 갱신되는 updated_at 은 쓰지 않는다.
    out["status_at"] = item.get("status_changed_at") or item.get("created_at")
    if item["kind"] in ("mcp", "agent") and item.get("registry_status") == "APPROVED":
        from . import usage                      # 순환 import 회피: usage 가 catalog 를 쓴다
        ok, since = usage.usable_since(item, user)
        out["usable"] = ok                       # 카드 배지 '사용 가능' 용
        if ok and since:
            out["status_at"] = since             # 사용 신청이 승인된 시각
    return out


# ── 요청 모델 ──
class PromptIn(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    description: str = Field("", max_length=1000)
    text: str = Field(min_length=1, max_length=20000)
    model_id: str = Field("", max_length=200)


class McpHeader(BaseModel):
    key: str = Field(min_length=1, max_length=200)
    value: str = Field(max_length=4000)


class McpIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field("", max_length=2000)
    endpoint: str = Field(min_length=1, max_length=16383)
    headers: list[McpHeader] = []


class SkillIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field("", max_length=1024)          # Registry skill description 한도
    source_type: Literal["git", "s3"]
    git_url: str = ""
    subdirectory: str = ""
    credential_provider_arn: str = ""
    git_username: str = ""
    s3_uri: str = ""
    object_version: str = ""


class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field("", max_length=2000)
    agent_type: Literal["harness", "runtime", "custom"] = "custom"
    arn: str = ""
    invoke_url: str = ""


class DescIn(BaseModel):
    description: str = Field(max_length=2000)


def _check_url(u: str, field: str) -> None:
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise HTTPException(422, f"{field}: http(s) URL 이어야 합니다")


def _mask_headers(d: dict) -> dict:
    """헤더 값(인증 정보)은 목록/상세 응답에 내보내지 않는다."""
    if "headers" in d:
        d = {**d, "headers": [{"key": h["key"], "value": "••••"} for h in d["headers"]]}
    return d


def _out(item: dict, user: auth.User) -> dict:
    d = _mask_headers(item["data"])
    if user.role != "admin" and "approved_users" in d:
        d = {k: v for k, v in d.items() if k != "approved_users"}        # 승인된 사용자 목록은 관리자에게만 보인다
    return {**_view(item, user), "data": d}


# ── 공용 목록 ──
@router.get("/my/records")
def my_records(scope: Literal["mine", "all"] = "mine", user: auth.User = Depends(auth.current_user)):
    """항목 목록과 상태별 개수. 기본은 내가 만든 것(승인 여부와 무관하게 거절·초안 포함).
    관리자는 scope=all 로 모든 사용자의 항목을 볼 수 있다. 일반 사용자가 all 을 요청하면 거부한다."""
    if scope == "all":
        if user.role != "admin":
            raise HTTPException(403, "관리자만 전체 레코드를 볼 수 있습니다")
        items = [i for k in ("prompt", "mcp", "skill", "agent") for i in db.list_items(k)]
        items.sort(key=lambda i: i["updated_at"], reverse=True)
    else:
        items = db.list_owned(user.email)
    counts: dict[str, int] = {}
    for i in items:
        counts[i["registry_status"] or "NOT_REGISTERED"] = counts.get(i["registry_status"] or "NOT_REGISTERED", 0) + 1
    return {"items": [_out(i, user) for i in items], "counts": counts, "total": len(items)}


@router.get("/catalog/{kind}")
def list_catalog(kind: Kind, q: str = "", user: auth.User = Depends(auth.current_user)):
    return {"items": [_out(i, user) for i in db.list_items(kind, q.strip(), (user.email, user.role == "admin"))]}


def _own(item_id: int, kind: str, user: auth.User) -> dict:
    it = visible_item(item_id, user, kind)
    if not _can_edit(user, it):
        raise HTTPException(403, "수정 권한이 없습니다")
    return it


@router.patch("/catalog/{kind}/{item_id}/description")
def set_description(kind: Kind, item_id: int, body: DescIn, user: auth.User = Depends(auth.current_user)):
    _own(item_id, kind, user)
    db.update_item(item_id, description=body.description, desc_edited=1)
    return _out(db.get_item(item_id), user)


def _missing(e: Exception) -> bool:
    return isinstance(e, ClientError) and e.response["Error"]["Code"] in (
        "ResourceNotFoundException", "NotFoundException", "ResourceNotFound")


@router.delete("/catalog/{kind}/{item_id}")
def delete_item(kind: Kind, item_id: int, user: auth.User = Depends(auth.current_user)):
    """Registry record → Prompt Management → 포털 DB 순으로 지운다.

    앞 단계가 실패하면 중단한다(포털 항목을 남겨 재시도할 수 있게). 이미 없는 리소스는 건너뛴다.
    """
    it = _own(item_id, kind, user)
    done: list[str] = []
    if it["registry_record_id"]:
        try:
            aws.registry_client().delete_registry_record(
                registryId=it["registry_id"], recordId=it["registry_record_id"])
            done.append("registry_record")
            _forget_assignment(it["registry_record_id"])
        except (ClientError, BotoCoreError) as e:
            if not _missing(e):
                if isinstance(e, ClientError) and e.response["Error"]["Code"] == "ConflictException":
                    raise HTTPException(409, "Registry record 가 처리 중(CREATING 등)이라 아직 삭제할 수 없습니다. "
                                             "잠시 후 다시 시도하세요. 아무것도 삭제되지 않았습니다.")
                raise aws_err(e)
    # 포털에서 만든 harness 는 AWS 에서도 지운다. 남겨 두면 다음 동기화가 소유자 'aws' 항목으로 되살려 생성자가 지울 수 없게 된다.
    # 동기화로 가져온 harness(source != portal)는 포털 소유가 아니므로 AWS 리소스는 건드리지 않는다.
    ext = it.get("ext_id") or ""
    if kind == "agent" and it["source"] == "portal" and ext.startswith("harness:"):
        try:
            aws.delete_harness(ext.split(":", 1)[1])
            done.append("harness")
        except (ClientError, BotoCoreError) as e:
            if not _missing(e):
                if "registry_record" in done:
                    db.update_item(item_id, registry_record_id=None, registry_status=None)
                if isinstance(e, ClientError) and e.response["Error"]["Code"] == "ConflictException":
                    raise HTTPException(409, "에이전트가 생성 중이라 아직 삭제할 수 없습니다. 잠시 후 다시 시도하세요.")
                raise aws_err(e)
    if kind == "prompt" and it["data"].get("prompt_id") and it["source"] == "portal":
        try:
            aws.delete_prompt(it["data"]["prompt_id"])
            done.append("prompt")
        except (ClientError, BotoCoreError) as e:
            if not _missing(e) and not (isinstance(e, ClientError) and "NotFound" in e.response["Error"]["Code"]):
                # record 는 이미 지워졌으므로 연결 정보를 비워 재시도 시 record 삭제를 다시 시도하지 않게 한다
                if "registry_record" in done:
                    db.update_item(item_id, registry_record_id=None, registry_status=None)
                raise aws_err(e)
    db.delete_usage_requests(item_id)
    db.delete_item(item_id)
    return {"ok": True, "deleted": done}


# ── Prompt (Bedrock Prompt Management) ──
def _prompt_data(p: dict) -> dict:
    return aws.prompt_from_get(p)


@router.post("/prompts")
def create_prompt(body: PromptIn, user: auth.User = Depends(auth.current_user)):
    try:
        p = aws.create_prompt(body.name, body.description, body.text, body.model_id)
    except (ClientError, BotoCoreError) as e:
        raise aws_err(e)
    d = _prompt_data(p)
    iid = db.insert_item("prompt", body.name, body.description, user.email, d, "portal", p["id"])
    return _created(iid, user)


@router.put("/prompts/{item_id}")
def update_prompt(item_id: int, body: PromptIn, user: auth.User = Depends(auth.current_user)):
    it = _own(item_id, "prompt", user)
    try:
        p = aws.update_prompt(it["data"]["prompt_id"], body.name, body.description, body.text, body.model_id)
    except (ClientError, BotoCoreError) as e:
        raise aws_err(e)
    data = _prompt_data(p)
    if it["registry_record_id"]:
        try:
            v = aws.create_prompt_version(it["data"]["prompt_id"], body.description)
            data = _prompt_data(v)
            aws.update_record(it["registry_id"], it["registry_record_id"],
                              {**it, "name": body.name, "description": body.description, "data": data})
        except (ClientError, BotoCoreError) as e:
            raise aws_err(e)
    db.update_item(item_id, name=body.name, description=body.description, desc_edited=1, data=data)
    return _out(db.get_item(item_id), user)


@router.post("/prompts/{item_id}/refresh")
def refresh_prompt(item_id: int, user: auth.User = Depends(auth.current_user)):
    """get_prompt 로 최신 내용을 다시 읽어 SQLite 를 갱신한다."""
    it = visible_item(item_id, user, "prompt")
    try:
        p = aws.get_prompt(it["data"]["prompt_id"])
    except (ClientError, BotoCoreError) as e:
        raise aws_err(e)
    db.update_item(item_id, name=p["name"], data=_prompt_data(p),
                   **({} if it["desc_edited"] else {"description": p.get("description", "")}))
    return _out(db.get_item(item_id), user)


# ── MCP ──
@router.post("/mcp")
def create_mcp(body: McpIn, user: auth.User = Depends(auth.current_user)):
    _check_url(body.endpoint, "endpoint")
    data = {"endpoint": body.endpoint, "headers": [h.model_dump() for h in body.headers],
            "approval": "not_required_yet"}  # 승인 흐름은 추후 구현
    iid = db.insert_item("mcp", body.name, body.description, user.email, data)
    return _created(iid, user)


@router.post("/mcp/{item_id}/test")
def test_mcp(item_id: int, user: auth.User = Depends(auth.current_user)):
    """MCP initialize + tools/list 를 호출해 연결을 확인한다."""
    import httpx
    it = visible_item(item_id, user, "mcp")
    from . import usage
    usage.require_use(it, user)                                        # 사용 신청이 승인된 사람만 실제로 연결해 볼 수 있다
    d = it["data"]
    if d.get("kind") == "gateway" and d.get("authorizer") != "NONE":
        raise HTTPException(422, f"AgentCore Gateway({d.get('authorizer')} 인증)는 포털에서 직접 호출할 수 없습니다. "
                                 "SigV4 서명 또는 토큰이 필요합니다")
    if not d.get("endpoint"):
        raise HTTPException(422, "endpoint 가 없습니다")
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               **{h["key"]: h["value"] for h in d.get("headers", [])}}
    try:
        with httpx.Client(timeout=10, follow_redirects=False) as c:
            r = c.post(d["endpoint"], headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "ai-portal", "version": "1.0"}}})
            sid = r.headers.get("mcp-session-id")
            if sid:
                headers["mcp-session-id"] = sid
            c.post(d["endpoint"], headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            t = c.post(d["endpoint"], headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        return {"status": r.status_code, "tools_status": t.status_code, "body": t.text[:4000]}
    except httpx.HTTPError as e:
        raise HTTPException(502, f"연결 실패: {type(e).__name__}")


# ── Skill ──
@router.post("/skills")
def create_skill(body: SkillIn, user: auth.User = Depends(auth.current_user)):
    if body.source_type == "git":
        _check_url(body.git_url, "git_url")
        label = body.git_url + (f" ({body.subdirectory})" if body.subdirectory else "")
    else:
        if not body.s3_uri.startswith("s3://") or len(body.s3_uri) <= 5:
            raise HTTPException(422, "s3_uri 는 s3://bucket/key 형식이어야 합니다")
        label = body.s3_uri
    desc = body.description.strip() or f"{body.source_type.upper()} skill: {label}"  # 기본값: git/s3 정보
    data = body.model_dump(exclude={"name", "description"}) | {"source_label": label}
    iid = db.insert_item("skill", body.name, desc, user.email, data)
    if body.description.strip():
        db.update_item(iid, desc_edited=1)
    return _created(iid, user)


# ── Agent ──
@router.post("/agents")
def create_agent(body: AgentIn, user: auth.User = Depends(auth.current_user)):
    if body.invoke_url:
        _check_url(body.invoke_url, "invoke_url")
    iid = db.insert_item("agent", body.name, body.description, user.email, body.model_dump(exclude={"name", "description"}))
    if body.description.strip():
        db.update_item(iid, desc_edited=1)
    return _created(iid, user)


# ── 동기화 (로그인 시 백그라운드) ──
_sync_lock = threading.Lock()
_sync_state: dict[str, Any] = {"running": False, "last": None, "error": "", "added": {}}
SYNC_MIN_INTERVAL = 300


def sync_all() -> dict[str, int]:
    added = {"prompt": 0, "agent": 0}
    seen_p: set[str] = set()
    for s in aws.list_all_prompts():
        pid = s["id"]
        seen_p.add(pid)
        try:
            p = aws.get_prompt(pid)
        except (ClientError, BotoCoreError) as e:
            log.warning("get_prompt %s failed: %s", pid, e)
            continue
        if db.upsert_synced("prompt", pid, p["name"], p.get("description", ""), _prompt_data(p)):
            added["prompt"] += 1
    db.prune_synced("prompt", seen_p)

    seen_a: set[str] = set()
    try:
        for h in aws.list_harnesses():
            hid = f"harness:{h['harnessId']}"
            seen_a.add(hid)
            try:
                full = aws.get_harness(h["harnessId"])
            except (ClientError, BotoCoreError):
                full = h
            # harness 에는 description 필드가 없다(systemPrompt 는 list 라 설명으로 쓸 수 없다). 알려진 harness 는 정해 둔 설명을 쓰고,
            # 그 외에는 비워 둔다(사용자가 화면에서 직접 쓴다). 사용자가 고친 설명은 upsert_synced 가 보존한다.
            desc = HARNESS_DESCRIPTIONS.get(h.get("harnessName", ""), "")
            data = {"agent_type": "harness", "arn": h.get("arn", ""), "status": h.get("status", ""),
                    "model": str(full.get("model", ""))[:300],
                    "auth": "JWT" if full.get("authorizerConfiguration") else "IAM"}     # JWT harness 는 Bearer 토큰으로만 호출된다
            if db.upsert_synced("agent", hid, h.get("harnessName", h["harnessId"]), desc[:300], data):
                added["agent"] += 1
    except (ClientError, BotoCoreError) as e:
        log.warning("list_harnesses failed: %s", e)
        seen_a = {i["ext_id"] for i in db.list_items("agent") if i["ext_id"] and i["ext_id"].startswith("harness:")}
    try:
        for r in aws.list_runtimes():
            # harness 가 만든 내부 runtime("harness_<harnessName>-…")은 harness 항목과 중복이고 직접 호출할 수 없다
            if r.get("agentRuntimeName", "").startswith("harness_"):
                continue
            rid = f"runtime:{r['agentRuntimeId']}"
            seen_a.add(rid)
            data = {"agent_type": "runtime", "arn": r.get("agentRuntimeArn", ""), "status": r.get("status", ""),
                    "version": r.get("agentRuntimeVersion", ""), **aws.runtime_info(r["agentRuntimeId"])}
            if db.upsert_synced("agent", rid, r.get("agentRuntimeName", r["agentRuntimeId"]),
                                r.get("description", ""), data):
                added["agent"] += 1
        db.prune_synced("agent", seen_a)
    except (ClientError, BotoCoreError) as e:
        log.warning("list_agent_runtimes failed: %s", e)
    added["mcp"] = _sync_gateways()
    return added


def _sync_gateways() -> int:
    """AgentCore Gateway(MCP) 를 여러 리전에서 가져와 mcp 항목으로 반영한다.

    리전별로 독립 처리한다: 한 리전의 조회가 실패하면 그 리전의 기존 항목은 그대로 두고(지우지 않음),
    다른 리전은 정상 반영한다. 같은 gatewayId 가 리전마다 있을 수 있어 ext_id 에 리전을 넣는다.
    """
    n, kept, failed = 0, set(), []
    for reg in aws.gateway_regions():
        try:
            gws = aws.list_gateways(reg)
        except (ClientError, BotoCoreError) as e:
            log.warning("list_gateways(%s) failed: %s", reg, e)
            failed.append(reg)
            continue
        for g in gws:
            gid = f"gateway:{reg}:{g['gatewayId']}"
            kept.add(gid)
            url = ""
            try:
                url = aws.get_gateway(g["gatewayId"], reg).get("gatewayUrl", "")
            except (ClientError, BotoCoreError) as e:
                log.warning("get_gateway %s/%s failed: %s", reg, g["gatewayId"], e)
            data = {"kind": "gateway", "endpoint": url, "gateway_id": g["gatewayId"], "status": g.get("status", ""),
                    "authorizer": g.get("authorizerType", ""), "protocol": g.get("protocolType", ""),
                    "region": reg, "headers": []}
            if db.upsert_synced("mcp", gid, g["name"], g.get("description", ""), data):
                n += 1
    # 조회에 실패한 리전의 기존 항목은 보존한다
    for it in db.list_items("mcp"):
        if it["ext_id"] and it["ext_id"].startswith("gateway:") and it["ext_id"].split(":")[1] in failed:
            kept.add(it["ext_id"])
    db.prune_synced("mcp", kept)
    return n


_KIND_OF = {"MCP": "mcp", "A2A": "agent", "AGENT_SKILLS": "skill", "CUSTOM": "prompt"}


def _loads(v: str | None) -> dict:
    try:
        d = json.loads(v or "{}")
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def _item_from_record(rec: dict) -> tuple[str, dict] | None:
    """Registry record → (kind, 항목 data). 복원할 수 없는 record(형식을 모르는 CUSTOM 등)는 None.

    복원 가능한 것만 복원한다: prompt(Prompt Management 포인터), skill(Git/S3 출처), agent(ARN), MCP(URL).
    MCP 의 HTTP 헤더(인증 정보)는 Registry 에 저장하지 않으므로 복원되지 않는다.
    """
    kind = _KIND_OF.get(rec.get("descriptorType", ""))
    d = rec.get("descriptors") or {}
    if kind == "prompt":
        c = _loads((d.get("custom") or {}).get("inlineContent"))
        if c.get("type") != "bedrock-prompt" or not c.get("promptId"):
            return None
        return "prompt", {"prompt_id": c["promptId"], "arn": c.get("promptArn", ""), "version": c.get("version", "DRAFT"),
                          "model_id": c.get("modelId", ""), "variables": c.get("variables", []), "text": ""}
    if kind == "skill":
        sd = _loads(((d.get("agentSkills") or {}).get("skillDefinition") or {}).get("inlineContent"))
        src = sd.get("source") or {}
        if not src:
            return None
        label = src.get("git_url") or src.get("s3_uri") or ""
        return "skill", {**{k: v for k, v in src.items() if v != ""}, "source_label": label}
    if kind == "agent":
        card = _loads(((d.get("a2a") or {}).get("agentCard") or {}).get("inlineContent"))
        arn = card.get("url", "")
        if not arn.startswith("arn:aws:bedrock-agentcore:"):
            return None                                   # 포털이 만든 card 는 url 에 ARN 을 넣는다
        return "agent", {"agent_type": "harness" if ":harness/" in arn else "runtime", "arn": arn}
    if kind == "mcp":
        sv = _loads(((d.get("mcp") or {}).get("server") or {}).get("inlineContent"))
        url = ((sv.get("remotes") or [{}])[0]).get("url", "")
        return "mcp", {"endpoint": url, "headers": [], "kind": "registry"}
    return None


def _ext_key(kind: str, data: dict) -> str | None:
    """같은 대상을 가리키는 동기화 항목을 찾는 키(AWS 에서 가져온 항목의 ext_id 규칙과 같다)."""
    if kind == "prompt":
        return data.get("prompt_id")
    if kind == "agent":
        arn = data.get("arn", "")
        rid = arn.rsplit("/", 1)[-1]
        return (f"harness:{rid}" if ":harness/" in arn else f"runtime:{rid}") if rid else None
    return None


def sync_registry() -> dict[str, int]:
    """Registry 의 record 를 기준으로 포털 항목과 연결(registry_*)한다. DB 가 비어도(재배포·신규 서버) 복구된다.

    - 이미 연결된 항목: 상태만 갱신한다(콘솔에서 승인/거절해도 따라간다).
    - 같은 대상의 동기화 항목(prompt·agent·Gateway)이 있으면 그 항목에 연결한다 — 중복을 만들지 않는다.
    - 없으면 record 내용으로 새 항목을 만든다. 소유자는 알 수 없어 'registry' 로 둔다.
    - Registry 조회가 실패하면 아무것도 지우지 않는다. record 가 사라진 경우의 연결 해제는 하지 않는다(삭제는 사용자 동작).
    """
    rid = selected_registry()
    if not rid:
        return {"linked": 0, "created": 0, "updated": 0}
    client = aws.registry_client()
    recs, token = [], None
    while True:
        r = client.list_registry_records(registryId=rid, **({"nextToken": token} if token else {}))
        recs += r.get("registryRecords", [])
        token = r.get("nextToken")
        if not token:
            break
    linked = created = updated = 0
    by_rec = {i["registry_record_id"]: i for k in ("prompt", "mcp", "skill", "agent")
              for i in db.list_items(k) if i["registry_record_id"]}
    for r in recs:
        if r["status"] in ("CREATING", "UPDATING", "CREATE_FAILED", "UPDATE_FAILED"):
            continue                                       # 전이 중이거나 실패한 record 는 상태를 믿을 수 없다
        existing = by_rec.get(r["recordId"])
        if existing:
            if existing["registry_status"] != r["status"]:
                db.update_item(existing["id"], registry_status=r["status"])
                updated += 1
            continue
        full = client.get_registry_record(registryId=rid, recordId=r["recordId"])
        parsed = _item_from_record(full)
        if parsed is None:
            continue
        kind, data = parsed
        key = _ext_key(kind, data)
        target = next((i for i in db.list_items(kind) if key and i["ext_id"] in (key, f"prompt:{key}")
                       and not i["registry_record_id"]), None)
        if target is None and kind == "mcp":              # Gateway 항목은 endpoint 로 찾는다
            target = next((i for i in db.list_items("mcp") if i["data"].get("endpoint") == data["endpoint"]
                           and not i["registry_record_id"]), None)
        if target:
            db.update_item(target["id"], registry_id=rid, registry_record_id=r["recordId"], registry_status=r["status"])
            linked += 1
            continue
        iid = db.insert_item(kind, full["name"], full.get("description", ""), "registry", data, "registry",
                             f"registry:{r['recordId']}")
        db.update_item(iid, registry_id=rid, registry_record_id=r["recordId"], registry_status=r["status"], desc_edited=1)
        created += 1
    return {"linked": linked, "created": created, "updated": updated}


def _run_sync() -> None:
    try:
        added = sync_all()
        try:
            added["registry"] = sum(sync_registry().values())
        except (ClientError, BotoCoreError) as e:
            log.warning("sync_registry failed: %s", e)         # Registry 실패가 나머지 동기화를 무효로 만들지 않는다
        _sync_state.update(error="", added=added)
    except Exception as e:  # noqa: BLE001
        log.exception("sync failed")
        _sync_state["error"] = type(e).__name__
    finally:
        _sync_state.update(running=False, last=time.time())


def start_sync(force: bool = False) -> bool:
    with _sync_lock:
        if _sync_state["running"]:
            return False
        last = _sync_state["last"]
        if not force and last and time.time() - last < SYNC_MIN_INTERVAL:
            return False
        _sync_state["running"] = True
    threading.Thread(target=_run_sync, daemon=True, name="catalog-sync").start()
    return True


@router.get("/sync")
def sync_status(user: auth.User = Depends(auth.current_user)):
    return dict(_sync_state)


@router.post("/sync")
def sync_now(user: auth.User = Depends(auth.current_user)):
    return {"started": start_sync(force=True)}


# ── Registry (CreateRegistryRecord 로 등록정보 관리) ──
def selected_registry() -> str:
    """이 포털이 쓰는 Registry. 설치 때 만든 `ai-portal` 하나로 고정이며(환경 변수 REGISTRY_ID, 없으면 config.json),
    화면·API 로 바꿀 수 없다 — 승인 대상이 엉뚱한 Registry 로 바뀌는 것을 막는다."""
    return os.environ.get("REGISTRY_ID", "") or _config_registry()


def config_value(key: str) -> str:
    try:
        return str(json.loads((Path(__file__).parent / "config.json").read_text()).get(key, ""))
    except (OSError, ValueError):
        return ""


def _config_registry() -> str:
    return config_value("registry_id")


class PolicyIn(BaseModel):
    auto_approval: bool | None = None
    auto_review: bool | None = None


@router.get("/policy")
def get_policy(user: auth.User = Depends(auth.current_user)):
    p = policy.get()
    return {"auto_approval": p["auto_approval"], "auto_review": p["auto_review"],
            **({"changed_by": p["changed_by"], "changed_at": p["changed_at"]} if user.role == "admin" else {})}


@router.put("/policy")
def put_policy(body: PolicyIn, user: auth.User = Depends(auth.current_user)):
    """승인 자동화 스위치 변경(관리자만). 승인 절차를 줄이는 설정이라 누가 바꿨는지 기록한다."""
    if user.role != "admin":
        raise HTTPException(403, "관리자만 변경할 수 있습니다")
    if body.auto_approval is None and body.auto_review is None:
        raise HTTPException(422, "변경할 값이 없습니다")
    try:
        p = policy.set_(auto_approval=body.auto_approval, auto_review=body.auto_review, by=user.email)
    except (ClientError, BotoCoreError) as e:
        raise aws_err(e)
    log.warning("approval policy changed by %s: auto_approval=%s auto_review=%s", user.email, p["auto_approval"], p["auto_review"])
    return {"auto_approval": p["auto_approval"], "auto_review": p["auto_review"], "changed_by": p["changed_by"], "changed_at": p["changed_at"]}


@router.get("/registry")
def registry_info(user: auth.User = Depends(auth.current_user)):
    """이 포털이 쓰는 Registry(읽기 전용). 생성·변경 API 는 없다 — 설치기(installer.py)가 만든 `ai-portal` 하나로 고정이다."""
    rid = selected_registry()
    if not rid:
        return {"registry_id": "", "name": "", "status": "", "authorizer": "", "auto_approval": None}
    try:
        g = aws.registry_client().get_registry(registryId=rid)
    except (ClientError, BotoCoreError) as e:
        raise aws_err(e)
    return {"registry_id": rid, "name": g.get("name", ""), "status": g.get("status", ""),
            "authorizer": g.get("authorizerType", ""),
            "auto_approval": (g.get("approvalConfiguration") or {}).get("autoApproval")}


def _register_core(item_id: int) -> None:
    """항목을 Registry record 로 만든다. prompt 는 먼저 불변 버전을 게시해 그 버전을 가리킨다."""
    it = db.get_item(item_id)
    rid = selected_registry()
    if not rid:
        raise HTTPException(409, "사용할 Registry 가 선택되지 않았습니다 (관리자가 선택해야 합니다)")
    if it["registry_record_id"]:
        raise HTTPException(409, "이미 Registry 에 등록되었습니다")
    try:
        if it["kind"] == "prompt":
            v = aws.create_prompt_version(it["data"]["prompt_id"], it["description"])
            it["data"] = {**it["data"], **_prompt_data(v)}
            db.update_item(item_id, data=it["data"])
        r = aws.create_record(rid, it)
    except (ClientError, BotoCoreError) as e:
        raise aws_err(e)
    # 자동화 스레드는 이 함수가 끝난 뒤에 시작되므로(_created) 여기서의 기록과 겹치지 않는다.
    db.update_item(item_id, registry_id=rid, registry_record_id=aws.record_id_from_arn(r["recordArn"]),
                   registry_status=r.get("status", "CREATING"))


def _sync_status(item_id: int) -> None:
    """포털에 표시되는 상태를 AWS 의 **현재** 상태로 맞춘다.

    자동화(Auto-approval 의 백그라운드 요청, Auto-review 의 승인)와 사람의 요청이 거의 동시에 상태를 쓰면,
    먼저 읽은 옛 값이 나중에 기록되어 새 상태를 덮어쓸 수 있다(예: AWS 는 APPROVED 인데 화면은 PENDING).
    그래서 값을 넘겨 쓰지 않고 쓰는 순간 AWS 에서 다시 읽는다.
    """
    it = db.get_item(item_id)
    if not it or not it["registry_record_id"]:
        return
    try:
        st = aws.registry_client().get_registry_record(
            registryId=it["registry_id"], recordId=it["registry_record_id"])["status"]
    except (ClientError, BotoCoreError):
        return                                                   # 못 읽으면 건드리지 않는다(다음 동기화·상태 확인이 맞춘다)
    if st != it["registry_status"]:
        db.update_item(item_id, registry_status=st)


def _auto_review(item_id: int) -> bool:
    """Auto-review: 승인 요청이 접수된 record 를 reviewer/관리자의 허락 없이 바로 APPROVED 로 바꾼다.

    사람이 한 승인과 감사 기록에서 구분되도록 사유에 `[auto-review]` 와 이 기능을 켠 관리자를 남긴다.
    정책을 읽지 못하면 꺼진 것으로 본다. 처리에 실패하면 PENDING_APPROVAL 로 남겨 두어 사람이 승인할 수 있다. 성공하면 True.
    """
    pol = policy.get()
    if not pol["auto_review"]:
        return False
    it = db.get_item(item_id)
    if not it or not it["registry_record_id"]:
        return False
    try:
        client = aws.registry_client()
        cur = client.get_registry_record(registryId=it["registry_id"], recordId=it["registry_record_id"])["status"]
        if cur != "PENDING_APPROVAL":                            # 다른 경로로 이미 처리됐거나 아직 요청 전
            return False
        reason = f"[auto-review] 자동 검토 (설정: {pol['changed_by'] or 'unknown'})"[:255]
        client.update_registry_record_status(registryId=it["registry_id"], recordId=it["registry_record_id"],
                                             status="APPROVED", statusReason=reason)
    except (ClientError, BotoCoreError) as e:
        log.warning("auto-review failed for item %s (left pending): %s", item_id, type(e).__name__)
        return False
    _forget_assignment(it["registry_record_id"])
    _sync_status(item_id)
    return True


def _auto_submit(item_id: int) -> None:
    """Auto-approval: 등록한 record 가 DRAFT 가 되기를 기다렸다가 승인 요청을 보낸다(CREATING 중에는 요청할 수 없다).
    백그라운드 스레드에서 돌고, 실패해도 등록 자체에는 영향이 없다 — 사람이 상세 화면에서 직접 요청하면 된다."""
    try:
        for _ in range(30):
            it = db.get_item(item_id)
            if not it or not it["registry_record_id"]:
                return                                           # 그 사이 삭제·취소됨
            st = aws.registry_client().get_registry_record(
                registryId=it["registry_id"], recordId=it["registry_record_id"])["status"]
            if st == "DRAFT":
                r = aws.registry_client().submit_registry_record_for_approval(
                    registryId=it["registry_id"], recordId=it["registry_record_id"])
                log.info("auto-approval: submitted %s", it["registry_record_id"])
                if not _auto_review(item_id):                    # 이 스위치도 켜져 있으면 바로 승인까지(상태 기록 포함)
                    _sync_status(item_id)
                return
            if st != "CREATING":
                return                                           # 이미 요청됐거나 승인·거절 등 다른 상태
            time.sleep(2)
    except Exception as e:  # noqa: BLE001
        log.warning("auto-approval submit failed for item %s: %s", item_id, type(e).__name__)


def _created(item_id: int, user: auth.User) -> dict:
    """생성 직후 Registry 에도 한 번에 등록한다. 실패해도 항목은 남기고 사유를 돌려준다."""
    out: dict[str, Any] = {}
    if selected_registry():
        try:
            _register_core(item_id)
            if policy.get()["auto_approval"]:                    # 정책이 켜져 있을 때만 (읽지 못하면 꺼짐으로 본다)
                threading.Thread(target=_auto_submit, args=(item_id,), daemon=True, name="auto-approval").start()
                out["auto_approval"] = True
        except HTTPException as e:
            out["registry_error"] = str(e.detail)
    else:
        out["registry_error"] = "선택된 Registry 가 없어 포털에만 저장했습니다"
    return {**_out(db.get_item(item_id), user), **out}


@router.post("/catalog/{kind}/{item_id}/register")
def register_item(kind: Kind, item_id: int, user: auth.User = Depends(auth.current_user)):
    _own(item_id, kind, user)
    _register_core(item_id)
    return _out(db.get_item(item_id), user)


@router.post("/catalog/{kind}/{item_id}/registry/refresh")
def refresh_registry(kind: Kind, item_id: int, user: auth.User = Depends(auth.current_user)):
    it = visible_item(item_id, user, kind)
    if not it["registry_record_id"]:
        raise HTTPException(404, "registry record 없음")
    try:
        r = aws.registry_client().get_registry_record(registryId=it["registry_id"], recordId=it["registry_record_id"])
    except (ClientError, BotoCoreError) as e:
        raise aws_err(e)
    db.update_item(item_id, registry_status=r["status"])
    return {**_out(db.get_item(item_id), user), "registry_reason": r.get("statusReason", "")}


def _forget_assignment(record_id: str) -> None:
    """record 가 사라지면 그 record 의 reviewer 정보도 지운다(best effort — 남아도 해가 없지만 목록이 쌓이지 않게)."""
    try:
        admins.clear_reviewers(record_id)
    except Exception:  # noqa: BLE001
        log.warning("could not clear reviewers for %s", record_id)


@router.post("/catalog/{kind}/{item_id}/registry/submit")
def submit_registry(kind: Kind, item_id: int, user: auth.User = Depends(auth.current_user)):
    """승인 요청. 승인은 관리자가 하고, 관리자가 이 record 에 추가해 둔 reviewer 도 할 수 있다(요청자는 승인자를 고르지 않는다)."""
    it = _own(item_id, kind, user)
    if not it["registry_record_id"]:
        raise HTTPException(409, "먼저 Registry 에 등록하세요")
    try:
        r = aws.registry_client().submit_registry_record_for_approval(
            registryId=it["registry_id"], recordId=it["registry_record_id"])
    except (ClientError, BotoCoreError) as e:
        raise aws_err(e)
    reviewed = _auto_review(item_id)
    if not reviewed:
        _sync_status(item_id)
    return {**_out(db.get_item(item_id), user), "auto_reviewed": reviewed}


@router.post("/catalog/{kind}/{item_id}/registry/cancel")
def cancel_approval(kind: Kind, item_id: int, user: auth.User = Depends(auth.current_user)):
    """승인 요청 취소.

    AWS 는 PENDING_APPROVAL → DRAFT 로의 되돌림을 지원하지 않는다(UpdateRegistryRecordStatus 가
    'Invalid target status: DRAFT' 로 거부). REJECTED 로 바꾸면 재제출이 영구히 막힌다.
    그래서 record 를 삭제하고 포털 항목의 연결을 비워, 다시 등록 → 승인 요청할 수 있게 한다.
    prompt 는 본문이 Prompt Management 에 그대로 남는다(불변 버전은 유지).
    """
    it = _own(item_id, kind, user)
    if not it["registry_record_id"]:
        raise HTTPException(409, "Registry 에 등록된 record 가 없습니다")
    # 포털 캐시가 낡았을 수 있으니 AWS 의 실제 상태로 판단한다.
    try:
        cur = aws.registry_client().get_registry_record(
            registryId=it["registry_id"], recordId=it["registry_record_id"])["status"]
    except (ClientError, BotoCoreError) as e:
        if not _missing(e):
            raise aws_err(e)
        cur = None
    if cur is not None and cur != "PENDING_APPROVAL":
        db.update_item(item_id, registry_status=cur)
        raise HTTPException(409, f"승인 대기 상태가 아니라 취소할 수 없습니다 (현재: {cur}). 이미 처리되었을 수 있습니다")
    if cur is not None:
        try:
            aws.registry_client().delete_registry_record(
                registryId=it["registry_id"], recordId=it["registry_record_id"])
        except (ClientError, BotoCoreError) as e:
            if not _missing(e):
                raise aws_err(e)
    _forget_assignment(it["registry_record_id"])
    db.update_item(item_id, registry_id=None, registry_record_id=None, registry_status=None)
    return _out(db.get_item(item_id), user)
