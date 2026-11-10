#!/usr/bin/env python3
"""gw-ai-portal(IAM) 을 통해 HR Advisor MCP 도구를 호출하는 스모크 테스트.

흐름:
  1. application/config.json 에서 gateway_iam_url(gw-ai-portal-iam, SigV4) 읽기
  2. SigV4 서명으로 MCP initialize → tools/list
  3. HR Advisor 도구(query_hr / generate_query / execute_query) 찾기
  4. query_hr 로 샘플 HR 질문 실행

사용:
  python test_hr_advisor.py
  python test_hr_advisor.py "개발팀 인원 수는?"
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "application" / "config.json"
DEFAULT_QUESTION = "부서별 인원 수를 알려줘"
HR_TOOL_HINTS = ("query_hr", "generate_query", "execute_query")
PROTOCOL_VERSION = "2025-03-26"


def load_config() -> dict[str, Any]:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not cfg.get("gateway_iam_url"):
        raise SystemExit("config.json 에 gateway_iam_url 이 없습니다. installer.py --gateway-only 를 먼저 실행하세요.")
    return cfg


def _creds():
    raw = boto3.Session().get_credentials()
    if raw is None:
        raise SystemExit("AWS credentials 가 없습니다.")
    return raw.get_frozen_credentials()


def _sign(url: str, body: bytes, region: str, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if extra:
        headers.update(extra)
    req = AWSRequest(method="POST", url=url, data=body, headers=headers)
    SigV4Auth(_creds(), "bedrock-agentcore", region).add_auth(req)
    return dict(req.headers)


def _parse_rpc(resp: httpx.Response) -> dict[str, Any]:
    """application/json 또는 SSE(data:) 응답에서 JSON-RPC result 추출."""
    text = resp.text or ""
    ct = (resp.headers.get("content-type") or "").lower()
    payload: Optional[dict[str, Any]] = None

    if "text/event-stream" in ct or text.lstrip().startswith("event:") or "\ndata:" in f"\n{text}":
        for line in text.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and ("result" in obj or "error" in obj or "id" in obj):
                    payload = obj
                    break
    if payload is None:
        try:
            payload = resp.json()
        except ValueError as e:
            raise RuntimeError(f"MCP 응답 파싱 실패 HTTP {resp.status_code}: {text[:400]}") from e

    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected MCP payload: {payload!r}")
    if "error" in payload:
        raise RuntimeError(f"MCP error: {json.dumps(payload['error'], ensure_ascii=False)}")
    return payload.get("result") or {}


class GatewayMcpClient:
    """AgentCore Gateway MCP (AWS_IAM / SigV4) 클라이언트."""

    def __init__(self, gateway_url: str, region: str, timeout: float = 120.0):
        self.url = gateway_url.rstrip("/")
        self.region = region
        self.timeout = timeout
        self.session_id: Optional[str] = None
        self._id = 0
        self._http = httpx.Client(timeout=timeout, follow_redirects=False)

    def close(self) -> None:
        self._http.close()

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _post(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        *,
        notification: bool = False,
    ) -> Optional[dict[str, Any]]:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notification:
            payload["id"] = self._next_id()
        body = json.dumps(payload).encode()

        extra: dict[str, str] = {}
        if self.session_id:
            extra["Mcp-Session-Id"] = self.session_id
        headers = _sign(self.url, body, self.region, extra)
        resp = self._http.post(self.url, content=body, headers=headers)
        sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        if notification:
            return None
        if resp.status_code >= 400:
            raise RuntimeError(
                f"HTTP {resp.status_code} on {method}: {resp.text[:500]}"
            )
        return _parse_rpc(resp)

    def initialize(self) -> dict[str, Any]:
        result = self._post(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test_hr_advisor", "version": "1.0"},
            },
        ) or {}
        self._post("notifications/initialized", {}, notification=True)
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._post("tools/list", {}) or {}
        return list(result.get("tools") or [])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = self._post("tools/call", {"name": name, "arguments": arguments}) or {}
        return result


def _tool_text(result: Any) -> str:
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                else:
                    parts.append(json.dumps(block, ensure_ascii=False))
            if parts:
                return "\n".join(parts)
        return json.dumps(result, ensure_ascii=False, indent=2)
    return str(result)


def _pick_hr_tools(tools: list[dict[str, Any]]) -> dict[str, str]:
    """Gateway 는 보통 `<target>___<tool>` 형태로 노출한다. 힌트 → 실제 이름 매핑."""
    found: dict[str, str] = {}
    for t in tools:
        name = str(t.get("name") or "")
        lower = name.lower()
        for hint in HR_TOOL_HINTS:
            if lower == hint or lower.endswith(f"___{hint}") or lower.endswith(f"_{hint}"):
                found[hint] = name
    return found


def main(argv: list[str]) -> int:
    question = " ".join(argv[1:]).strip() or DEFAULT_QUESTION
    cfg = load_config()
    gateway_url = cfg["gateway_iam_url"]
    region = cfg.get("gateway_iam_region") or cfg.get("gateway_region") or cfg.get("region") or "us-west-2"

    print(f"Gateway: {gateway_url}")
    print(f"Region:  {region}")
    print(f"Question: {question}")
    print("-" * 60)

    client = GatewayMcpClient(gateway_url, region)
    try:
        init = client.initialize()
        print(f"initialize OK  protocol={init.get('protocolVersion')}  session={client.session_id}")

        tools = client.list_tools()
        print(f"tools/list → {len(tools)} tool(s)")
        for t in tools:
            print(f"  - {t.get('name')}: {(t.get('description') or '')[:80]}")

        hr = _pick_hr_tools(tools)
        if not hr:
            print(
                "\n✗ HR Advisor 도구를 찾지 못했습니다. "
                "installer.py --hr-only 로 gateway target 이 READY 인지 확인하세요.",
                file=sys.stderr,
            )
            return 1

        print("\nHR Advisor tools:")
        for hint, name in hr.items():
            print(f"  {hint} → {name}")

        query_tool = hr.get("query_hr")
        if not query_tool:
            print("\n✗ query_hr 도구가 없습니다.", file=sys.stderr)
            return 1

        print(f"\n→ tools/call {query_tool}")
        result = client.call_tool(query_tool, {"question": question})
        text = _tool_text(result)
        print("\nResult:")
        print(text)
        if isinstance(result, dict) and result.get("isError"):
            return 1
        print("\n✓ HR Advisor MCP via gateway OK")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"✗ {exc}", file=sys.stderr)
        raise SystemExit(1)
