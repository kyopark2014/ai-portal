import json

import pytest
from fastapi.testclient import TestClient

from application import auth, aws, catalog, chat, db, gateway, server


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_SIGNING_KEY", "k")
    monkeypatch.setenv("PORTAL_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("LLM_GATEWAY_KEY", "vk-test")
    monkeypatch.setattr(auth, "_key", None)
    monkeypatch.setattr(gateway, "_models_cache", None)
    monkeypatch.setattr(catalog, "start_sync", lambda force=False: False)


def client():
    c = TestClient(server.app)
    c.cookies.set(auth.COOKIE_NAME, auth.make_session("a@x.com", "N", ""))
    return c


def test_endpoint_selection():
    assert gateway.endpoint_for({"provider": "BEDROCK", "api_format": "BEDROCK_NATIVE"}) == "messages"
    assert gateway.endpoint_for({"provider": "OPENMODEL"}) == "chat"
    assert gateway.endpoint_for({"provider": "BEDROCK_RUNTIME_OPENAI", "api_format": "OPENAI_RESPONSES"}) == "chat"
    assert gateway.endpoint_for({"provider": "BEDROCK_MANTLE_OPENAI", "api_format": "OPENAI_RESPONSES"}) is None


def test_requires_login(env):
    assert TestClient(server.app).get("/api/gateway/models").status_code == 401
    assert TestClient(server.app).post("/api/chat", json={}).status_code == 401


def test_models_never_expose_key(env, monkeypatch):
    monkeypatch.setattr(gateway, "list_models", lambda force=False: [
        {"id": "m", "provider": "BEDROCK", "endpoint": "messages", "usable": True}])
    r = client().get("/api/gateway/models")
    assert r.status_code == 200 and "vk-test" not in r.text


def test_chat_streams_and_applies_prompt(env, monkeypatch):
    iid = db.insert_item("prompt", "p", "", "a@x.com", {"text": "Be {{tone}}.", "variables": ["tone"]})
    seen = {}
    def fake(model, system, messages, max_tokens=1024):
        seen.update(model=model, system=system, messages=messages)
        yield {"t": "he"}; yield {"t": "llo"}; yield {"done": True, "usage": {"input_tokens": 3}, "ms": 5}
    monkeypatch.setattr(gateway, "stream_chat", fake)
    r = client().post("/api/chat", json={"model": "m", "prompt_id": iid, "values": {"tone": "brief"},
                                         "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    evs = [json.loads(l[5:]) for l in r.text.split("\n\n") if l.startswith("data:")]
    assert "".join(e.get("t", "") for e in evs) == "hello" and evs[-1]["done"]
    assert seen["system"] == "Be brief." and "vk-test" not in r.text


def test_chat_validation_errors_are_http_status(env, monkeypatch):
    def bad(model, system, messages, max_tokens=1024):
        raise gateway.GatewayError(404, "이 키로 사용할 수 없는 모델입니다")
        yield
    monkeypatch.setattr(gateway, "stream_chat", bad)
    c = client()
    assert c.post("/api/chat", json={"model": "x", "messages": [{"role": "user", "content": "hi"}]}).status_code == 404
    assert c.post("/api/chat", json={"model": "x", "messages": [{"role": "assistant", "content": "hi"}]}).status_code == 422
    assert c.post("/api/chat", json={"model": "x", "prompt_id": 999, "messages": [{"role": "user", "content": "hi"}]}).status_code == 404


def test_chat_without_key(env, monkeypatch):
    monkeypatch.delenv("LLM_GATEWAY_KEY")
    assert client().post("/api/chat", json={"model": "x", "messages": [{"role": "user", "content": "hi"}]}).status_code == 503


def test_claude_uses_chatanthropic_with_bearer_header(env):
    from langchain_anthropic import ChatAnthropic
    from langchain_openai import ChatOpenAI
    m = gateway.build_chat("messages", "claude-sonnet-5-5", 99999)
    assert isinstance(m, ChatAnthropic) and m.max_tokens == gateway.MAX_TOKENS_CAP
    assert m.default_headers["Authorization"] == "Bearer vk-test"
    assert m.model_kwargs.get("temperature") is None and m.temperature is None   # 최신 Claude 는 temperature 거부
    o = gateway.build_chat("chat", "gpt-6-luna", 10)
    from langchain_core.messages import HumanMessage
    payload = o._get_request_payload([HumanMessage("hi")])      # 실제로 전송될 요청 본문
    assert isinstance(o, ChatOpenAI) and payload["max_completion_tokens"] == 10 and "max_tokens" not in payload


def test_stream_chat_normalizes_langchain_chunks(env, monkeypatch):
    from langchain_core.messages import AIMessageChunk
    class Fake:
        def stream(self, msgs):
            assert [type(m).__name__ for m in msgs] == ["SystemMessage", "HumanMessage", "AIMessage", "HumanMessage"]
            yield AIMessageChunk(content="he")
            yield AIMessageChunk(content=[{"type": "text", "text": "llo"}])
            yield AIMessageChunk(content="", usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5})
    monkeypatch.setattr(gateway, "list_models", lambda force=False: [
        {"id": "m", "provider": "BEDROCK", "endpoint": "messages", "usable": True}])
    monkeypatch.setattr(gateway, "build_chat", lambda ep, model, mt: Fake())
    evs = list(gateway.stream_chat("m", "SYS", [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
                                                {"role": "user", "content": "c"}]))
    assert "".join(e.get("t", "") for e in evs) == "hello"
    assert evs[-1]["done"] and evs[-1]["usage"] == {"input_tokens": 3, "output_tokens": 2}


def test_stream_chat_error_before_first_token_is_http_status(env, monkeypatch):
    class E(Exception):
        status_code = 403
        body = {"error": {"message": "denied"}}
    class Fake:
        def stream(self, msgs):
            raise E("x")
            yield
    monkeypatch.setattr(gateway, "list_models", lambda force=False: [
        {"id": "m", "provider": "BEDROCK", "endpoint": "messages", "usable": True}])
    monkeypatch.setattr(gateway, "build_chat", lambda ep, model, mt: Fake())
    with pytest.raises(gateway.GatewayError) as ei:
        next(gateway.stream_chat("m", "", [{"role": "user", "content": "a"}]))
    assert ei.value.status == 403 and ei.value.message == "denied"


def test_bad_gateway_url_is_reported(env, monkeypatch):
    monkeypatch.setenv("LLM_GATEWAY_URL", "ttps://llm.example.com")
    with pytest.raises(gateway.GatewayError) as ei:
        gateway.base_url()
    assert ei.value.status == 503 and "올바르지 않습니다" in ei.value.message
