"""AWS 연동: Bedrock Prompt Management, AgentCore(harness/runtime/registry), Bedrock Runtime."""
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

import boto3
from botocore.config import Config

_cfg = Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=5, read_timeout=30)


def account_id() -> str:
    return boto3.client("sts", region_name=region(), config=_cfg).get_caller_identity()["Account"]


def region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"


def client(service: str):
    return boto3.client(service, region_name=region(), config=_cfg)


_VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def prompt_variables(text: str) -> list[str]:
    return list(dict.fromkeys(_VAR.findall(text or "")))


def render_prompt(text: str, values: dict[str, str]) -> str:
    return _VAR.sub(lambda m: str(values.get(m.group(1), m.group(0))), text)


# ── Prompt management ───────────────────────────────────────────────
def _variant_of(p: dict) -> dict:
    variants = p.get("variants") or []
    default = p.get("defaultVariant")
    for v in variants:
        if v.get("name") == default:
            return v
    return variants[0] if variants else {}


def prompt_from_get(p: dict) -> dict:
    v = _variant_of(p)
    tc = v.get("templateConfiguration", {})
    text = (tc.get("text") or {}).get("text", "")
    if not text and tc.get("chat"):
        sys_ = " ".join(b.get("text", "") for b in tc["chat"].get("system", []))
        msgs = " ".join(b.get("text", "") for m in tc["chat"].get("messages", []) for b in m.get("content", []))
        text = (sys_ + "\n" + msgs).strip()
    return {
        "prompt_id": p["id"], "arn": p["arn"].rsplit(":", 1)[0] if re.search(r":\d+$", p["arn"]) else p["arn"],
        "version": p.get("version", "DRAFT"),
        "text": text, "model_id": v.get("modelId", ""), "variant": v.get("name", "default"),
        "variables": prompt_variables(text),
    }


def get_prompt(prompt_id: str) -> dict:
    return client("bedrock-agent").get_prompt(promptIdentifier=prompt_id)


def create_prompt(name: str, description: str, text: str, model_id: str) -> dict:
    variant: dict[str, Any] = {
        "name": "default", "templateType": "TEXT",
        "templateConfiguration": {"text": {"text": text, "inputVariables": [{"name": v} for v in prompt_variables(text)]}},
    }
    if model_id:
        variant["modelId"] = model_id
    kw: dict[str, Any] = {"name": name, "defaultVariant": "default", "variants": [variant]}
    if description:
        kw["description"] = description
    return client("bedrock-agent").create_prompt(**kw)


def update_prompt(prompt_id: str, name: str, description: str, text: str, model_id: str) -> dict:
    variant: dict[str, Any] = {
        "name": "default", "templateType": "TEXT",
        "templateConfiguration": {"text": {"text": text, "inputVariables": [{"name": v} for v in prompt_variables(text)]}},
    }
    if model_id:
        variant["modelId"] = model_id
    kw: dict[str, Any] = {"promptIdentifier": prompt_id, "name": name, "defaultVariant": "default", "variants": [variant]}
    if description:
        kw["description"] = description
    return client("bedrock-agent").update_prompt(**kw)


def create_prompt_version(prompt_id: str, description: str = "") -> dict:
    """현재 DRAFT 를 불변 버전("1","2",…)으로 게시한다."""
    kw = {"promptIdentifier": prompt_id}
    if description:
        kw["description"] = description[:200]
    return client("bedrock-agent").create_prompt_version(**kw)


def update_record(registry_id: str, record_id: str, item: dict) -> dict:
    p = record_params(item)
    return client("bedrock-agentcore-control").update_registry_record(
        registryId=registry_id, recordId=record_id, name=slug(item["name"]),
        description=(item["description"] or item["name"])[:4096], **{
            "descriptors": {"custom": {"inlineContent": p["descriptors"]["custom"]["inlineContent"]}}
            if item["kind"] == "prompt" else p["descriptors"]})


def delete_prompt(prompt_id: str) -> None:
    client("bedrock-agent").delete_prompt(promptIdentifier=prompt_id)


def list_all_prompts() -> list[dict]:
    c = client("bedrock-agent")
    out, token = [], None
    while True:
        r = c.list_prompts(maxResults=100, **({"nextToken": token} if token else {}))
        out += r.get("promptSummaries", [])
        token = r.get("nextToken")
        if not token:
            return out


# ── AgentCore: harness / runtime ───────────────────────────────────
def _paginate(fn, key: str, **kw) -> list[dict]:
    out, token = [], None
    while True:
        r = fn(**kw, **({"nextToken": token} if token else {}))
        out += r.get(key, [])
        token = r.get("nextToken")
        if not token:
            return out


def list_harnesses() -> list[dict]:
    return _paginate(client("bedrock-agentcore-control").list_harnesses, "harnesses")


def get_harness(harness_id: str) -> dict:
    return client("bedrock-agentcore-control").get_harness(harnessId=harness_id)["harness"]


def delete_harness(harness_id: str) -> None:
    client("bedrock-agentcore-control").delete_harness(harnessId=harness_id)


def runtime_info(runtime_id: str) -> dict:
    """runtime 의 serverProtocol(HTTP/MCP/A2A) 과 인증 방식(JWT/IAM). 조회 실패 시 빈 값."""
    try:
        g = client("bedrock-agentcore-control").get_agent_runtime(agentRuntimeId=runtime_id)
        return {"protocol": (g.get("protocolConfiguration") or {}).get("serverProtocol", ""),
                "auth": "JWT" if g.get("authorizerConfiguration") else "IAM"}
    except Exception:  # noqa: BLE001
        return {"protocol": "", "auth": ""}


def runtime_protocol(runtime_id: str) -> str:
    return runtime_info(runtime_id)["protocol"]


def list_runtimes() -> list[dict]:
    return _paginate(client("bedrock-agentcore-control").list_agent_runtimes, "agentRuntimes")


def gateway_regions() -> list[str]:
    """Gateway 를 조회할 리전들. GATEWAY_REGIONS(쉼표 구분)로 바꿀 수 있고, 기본은 현재 리전 + us-east-1."""
    raw = os.environ.get("GATEWAY_REGIONS", "").strip()
    regs = [r.strip() for r in raw.split(",") if r.strip()] if raw else [region(), "us-east-1"]
    return list(dict.fromkeys(regs))


def list_gateways(reg: str | None = None) -> list[dict]:
    return _paginate(boto3.client("bedrock-agentcore-control", region_name=reg or region(), config=_cfg).list_gateways, "items")


def get_gateway(gateway_id: str, reg: str | None = None) -> dict:
    return boto3.client("bedrock-agentcore-control", region_name=reg or region(), config=_cfg).get_gateway(
        gatewayIdentifier=gateway_id)


# ── Registry ────────────────────────────────────────────────────────
def slug(name: str, maxlen: int = 64) -> str:
    s = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_") or "item"
    if not s[0].isalpha():
        s = "r_" + s
    return s[:maxlen]


def skill_slug(name: str) -> str:
    """AGENT_SKILLS 의 name 규칙: 소문자/숫자/하이픈 1-64자, 하이픈으로 시작·끝 금지, 연속 하이픈 금지."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (s[:64].strip("-")) or "skill"


def record_params(item: dict) -> dict:
    """포털 항목 → CreateRegistryRecord 의 (descriptorType, descriptors)."""
    k, d = item["kind"], item["data"]
    name, desc = item["name"], item["description"] or item["name"]
    if k == "mcp":
        server = {"name": f"io.portal/{re.sub(r'[^A-Za-z0-9._-]+', '-', name).strip('-') or 'mcp'}",
                  "description": desc, "version": "1.0.0",
                  "remotes": [{"type": "streamable-http", "url": d["endpoint"]}]}
        return {"descriptorType": "MCP", "descriptors": {"mcp": {"server": {"inlineContent": json.dumps(server)}}}}
    if k == "agent":
        card = {"protocolVersion": "0.3.0", "name": name, "description": desc,
                "url": d.get("invoke_url") or d.get("arn") or "https://example.invalid/",
                "version": "1.0.0", "capabilities": {},
                "defaultInputModes": ["text"], "defaultOutputModes": ["text"],
                "skills": [{"id": slug(name), "name": name, "description": desc, "tags": [d.get("agent_type", "agent")]}]}
        return {"descriptorType": "A2A", "descriptors": {"a2a": {"agentCard": {"inlineContent": json.dumps(card)}}}}
    if k == "skill":
        # SKILL.md frontmatter 는 YAML 이다. 설명에 ':' '#' 줄바꿈 등이 있으면 따옴표 없이는 깨지므로(예: "GIT skill: https://…")
        # JSON 문자열(= 유효한 YAML 큰따옴표 문자열)로 감싼다. AWS 한도: description 1024자.
        fm_desc = json.dumps(desc[:1024], ensure_ascii=False)
        md = f"---\nname: {json.dumps(skill_slug(name))}\ndescription: {fm_desc}\n---\n\n# {name}\n\n{desc}\n\nSource: {d.get('source_label', '')}\n"
        definition = {"name": name, "description": desc[:1024], "source": {k2: v for k2, v in d.items()
                      if k2 in ("source_type", "git_url", "subdirectory", "s3_uri", "object_version")}}
        return {"descriptorType": "AGENT_SKILLS", "descriptors": {"agentSkills": {
            "skillMd": {"inlineContent": md}, "skillDefinition": {"inlineContent": json.dumps(definition)}}}}
    if k == "prompt":
        # 본문은 Prompt Management 에만 있다. Registry 는 불변 버전을 가리키는 포인터만 가진다.
        body = {"type": "bedrock-prompt", "promptId": d["prompt_id"], "promptArn": d["arn"],
                "version": d["version"], "versionArn": f"{d['arn']}:{d['version']}",
                "modelId": d.get("model_id", ""), "variables": d.get("variables", [])}
        return {"descriptorType": "CUSTOM", "descriptors": {"custom": {"inlineContent": json.dumps(body)}}}
    raise ValueError(k)


def create_record(registry_id: str, item: dict) -> dict:
    p = record_params(item)
    return client("bedrock-agentcore-control").create_registry_record(
        registryId=registry_id, name=slug(item["name"]), description=(item["description"] or item["name"])[:4096],
        recordVersion="1.0", **p)


def registry_client():
    return client("bedrock-agentcore-control")


def record_id_from_arn(arn: str) -> Optional[str]:
    return arn.rsplit("/", 1)[-1] if arn else None
