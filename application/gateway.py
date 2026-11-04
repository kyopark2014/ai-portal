"""AWSome AI Gateway(LiteLLM, OpenAI 호환) 호출. 키는 서버에만 두고 브라우저에는 절대 내보내지 않는다.

모델의 provider/api_format 으로 엔드포인트를 고른다(awsome-ai-gateway 포털과 동일한 규칙):
  /v1/chat/completions ← OPENMODEL, BEDROCK_RUNTIME_OPENAI
  /v1/messages         ← Claude (BEDROCK + BEDROCK_NATIVE / ANTHROPIC_MESSAGES)
"""
from __future__ import annotations

import os
import time
from typing import Any, Iterator, Optional

import httpx

DEFAULT_URL = "https://llm.my-agentic-ai.click"
CHAT_PROVIDERS = ("OPENMODEL", "BEDROCK_RUNTIME_OPENAI")
MAX_TOKENS_CAP = 4096
_CACHE_TTL = 60.0
_models_cache: tuple[float, list[dict]] | None = None


class GatewayError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def base_url() -> str:
    u = (os.environ.get("LLM_GATEWAY_URL") or DEFAULT_URL).strip().rstrip("/")
    if not u.startswith(("https://", "http://")):
        raise GatewayError(503, f"LLM_GATEWAY_URL 이 올바르지 않습니다 (http(s):// 로 시작해야 함): {u[:60]}")
    return u


def api_key() -> str:
    return (os.environ.get("LLM_GATEWAY_KEY") or "").strip()


def configured() -> bool:
    return bool(api_key())


def _headers() -> dict[str, str]:
    k = api_key()
    if not k:
        raise GatewayError(503, "LLM_GATEWAY_KEY 가 설정되지 않았습니다 (.env 또는 ECS secret)")
    return {"Authorization": f"Bearer {k}", "Content-Type": "application/json"}


def endpoint_for(m: dict) -> Optional[str]:
    provider = str(m.get("provider") or "").upper()
    fmt = str(m.get("api_format") or "").upper()
    if provider in CHAT_PROVIDERS:
        return "chat"
    if provider == "BEDROCK" and fmt in ("BEDROCK_NATIVE", "ANTHROPIC_MESSAGES"):
        return "messages"
    return None  # Responses 전용(Mantle) 등은 지원하지 않는다


def _err(r: httpx.Response) -> GatewayError:
    try:
        b = r.json()
        e = b.get("error", b) if isinstance(b, dict) else {}
        msg = e.get("message") if isinstance(e, dict) else None
    except ValueError:
        msg = None
    return GatewayError(502 if r.status_code >= 500 else r.status_code, msg or f"gateway HTTP {r.status_code}")


def list_models(force: bool = False) -> list[dict]:
    global _models_cache
    if not force and _models_cache and time.monotonic() - _models_cache[0] < _CACHE_TTL:
        return _models_cache[1]
    try:
        r = httpx.get(f"{base_url()}/v1/models", headers=_headers(), timeout=10)
    except httpx.HTTPError as e:
        raise GatewayError(502, f"Gateway 에 연결할 수 없습니다 ({type(e).__name__})")
    if r.status_code != 200:
        raise _err(r)
    out = []
    for m in r.json().get("data", []):
        ep = endpoint_for(m)
        out.append({"id": m["id"], "provider": m.get("provider", ""), "endpoint": ep, "usable": ep is not None})
    out.sort(key=lambda m: (not m["usable"], m["id"]))
    _models_cache = (time.monotonic(), out)
    return out


def build_chat(endpoint: str, model: str, max_tokens: int):
    """LangChain 챗 모델. Claude(messages)는 ChatAnthropic, 그 외(chat completions)는 ChatOpenAI.

    Gateway 인증은 Authorization: Bearer(VK) 이다. Anthropic SDK 는 x-api-key 만 보내므로
    default_headers 로 Bearer 를 함께 보낸다(agentic-work 와 동일). Claude 최신 모델은
    temperature 를 거부하므로 넘기지 않는다.
    """
    max_tokens = max(1, min(int(max_tokens), MAX_TOKENS_CAP))
    key = api_key()
    if endpoint == "messages":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model, api_key=key, base_url=base_url(), max_tokens=max_tokens,
                             default_headers={"Authorization": f"Bearer {key}"}, streaming=True, max_retries=1)
    from langchain_openai import ChatOpenAI
    # GPT 계열은 max_tokens 를 거부하고 max_completion_tokens 를 요구한다.
    return ChatOpenAI(model=model, api_key=key, base_url=f"{base_url()}/v1", streaming=True, max_retries=1,
                      stream_usage=True, max_completion_tokens=max_tokens)


def _to_lc(system: str, messages: list[dict]) -> list:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    out: list = [SystemMessage(system)] if system else []
    out += [HumanMessage(m["content"]) if m["role"] == "user" else AIMessage(m["content"]) for m in messages]
    return out


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def _norm_usage(u: Optional[dict]) -> dict:
    return {k: u[k] for k in ("input_tokens", "output_tokens") if u and u.get(k) is not None}


def _status_of(e: Exception) -> tuple[int, str]:
    status = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None) or 502
    body = getattr(e, "body", None)
    msg = None
    if isinstance(body, dict):
        err = body.get("error", body)
        msg = err.get("message") if isinstance(err, dict) else None
    return (int(status) if 400 <= int(status) < 600 else 502), (msg or str(e) or type(e).__name__)[:500]


def stream_chat(model: str, system: str, messages: list[dict], max_tokens: int = 1024) -> Iterator[dict]:
    """정규화된 이벤트를 낸다: {"t": 텍스트} … {"done": True, "usage": {...}} / {"error": 메시지}."""
    meta = next((m for m in list_models() if m["id"] == model), None)
    if meta is None:
        raise GatewayError(404, "이 키로 사용할 수 없는 모델입니다")
    if not meta["endpoint"]:
        raise GatewayError(422, "이 모델은 포털에서 지원하지 않는 API 형식입니다")
    llm = build_chat(meta["endpoint"], model, max_tokens)
    started, total, first = time.monotonic(), None, True
    try:
        for chunk in llm.stream(_to_lc(system, messages)):
            total = chunk if total is None else total + chunk
            text = _text(chunk.content)
            if text:
                first = False
                yield {"t": text}
    except Exception as e:  # noqa: BLE001 — SDK 별 예외(anthropic/openai/httpx)를 한 곳에서 정규화
        status, msg = _status_of(e)
        if first:  # 아직 아무것도 내보내지 않았으면 HTTP 상태로 돌려줄 수 있다
            raise GatewayError(status, msg)
        yield {"error": msg}
        return
    yield {"done": True, "usage": _norm_usage(getattr(total, "usage_metadata", None)),
           "ms": int((time.monotonic() - started) * 1000), "endpoint": meta["endpoint"]}
