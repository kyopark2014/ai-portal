"""MCP Apps host: weather fallback payload + call-tool API."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from application import auth, catalog, mcp_apps_host, mcp_test, server


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_SIGNING_KEY", "k")
    monkeypatch.setenv("PORTAL_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(auth, "_key", None)
    monkeypatch.setattr(catalog, "start_sync", lambda force=False: False)


def client(email="a@x.com"):
    c = TestClient(server.app)
    c.cookies.set(auth.COOKIE_NAME, auth.make_session(email, "N", ""))
    return c


def test_build_app_payload_for_weather_tools():
    mcp_apps_host.clear()
    p = mcp_apps_host.build_app_payload("get_weather", '{"summary":"맑음"}')
    assert p["resourceUri"].startswith("ui://")
    assert "html" in p and len(p["html"]) > 100
    assert p["mcpServer"] == "korea-weather"
    assert p["toolResult"]["content"][0]["text"] == '{"summary":"맑음"}'


def test_build_app_payload_if_ready_requires_region_for_weather():
    mcp_apps_host.clear()
    assert mcp_apps_host.build_app_payload_if_ready("get_weather", '{"summary":"맑음"}') is None
    assert mcp_apps_host.build_app_payload_if_ready("get_weather", '{"region":') is None
    ready = '{"region":{"stn_id":"109","name":"서울"},"summary":"맑음"}'
    p = mcp_apps_host.build_app_payload_if_ready("get_weather", ready)
    assert p and p["toolResult"]["content"][0]["text"] == ready


def test_build_app_payload_strips_gateway_prefix():
    mcp_apps_host.clear()
    p = mcp_apps_host.build_app_payload("korea-weather___get_metropolitan_weather", "{}")
    assert p and p["tool"] == "get_metropolitan_weather"


def test_build_app_payload_ignores_unrelated_tools():
    assert mcp_apps_host.build_app_payload("search_docs", "x") is None


def test_relay_attaches_mcp_app_to_weather_tool_result():
    mcp_apps_host.clear()
    ready = '{"region":{"stn_id":"109","name":"서울"},"summary":"맑음"}'
    stream = [
        {"contentBlockStart": {"contentBlockIndex": 0, "start": {"toolUse": {"name": "get_weather"}}}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {"input": '{"region":"서울"}'}}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"toolResult": [{"text": ready}]}}},
    ]
    evs = list(mcp_test.relay_harness_stream(iter(stream)))
    res = next(e for e in evs if e.get("mcpApp"))
    assert res["tool"] == "get_weather"
    assert res["mcpApp"]["html"]
    assert res["mcpApp"]["toolResult"]["content"][0]["text"] == ready


def test_relay_waits_for_complete_json_before_mcp_app():
    """잘린 JSON 조각에는 mcpApp 을 붙이지 않고, 완전본이 모이면 한 번 붙인다(agentic-work 와 동일)."""
    mcp_apps_host.clear()
    full = '{"region":{"stn_id":"109","name":"서울"},"pad":"' + ("x" * 12000) + '"}'
    chunks = [full[i : i + 4000] for i in range(0, len(full), 4000)]
    assert len(full) > 8000 and len(chunks) >= 3

    stream = [
        {"contentBlockStart": {"contentBlockIndex": 0, "start": {"toolUse": {"name": "get_weather"}}}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {"input": '{"region":"서울"}'}}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
    ]
    for c in chunks:
        stream.append(
            {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"toolResult": [{"text": c}]}}}
        )
    stream.append({"contentBlockStop": {"contentBlockIndex": 1}})

    evs = list(mcp_test.relay_harness_stream(iter(stream)))
    apps = [e for e in evs if e.get("mcpApp")]
    assert len(apps) == 1
    assert apps[0]["mcpApp"]["toolResult"]["content"][0]["text"] == full
    shown = "".join(e.get("tool_result", "") for e in evs if "tool_result" in e)
    assert len(shown) == 8000


def test_relay_refills_weather_mcp_app_when_stream_truncated(monkeypatch):
    """스트림 JSON 이 깨진 채 끝나면 로컬 weather 조회로 mcpApp 을 채운다."""
    mcp_apps_host.clear()
    refilled = '{"region":{"stn_id":"109","name":"서울"},"summary":"재조회"}'
    monkeypatch.setattr(mcp_apps_host, "refill_weather_tool_result", lambda *a, **k: refilled)

    stream = [
        {"contentBlockStart": {"contentBlockIndex": 0, "start": {"toolUse": {"name": "get_weather"}}}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {"input": '{"region":"서울"}'}}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"toolResult": [{"text": '{"region":'}]}}},
        {"contentBlockStop": {"contentBlockIndex": 1}},
    ]
    evs = list(mcp_test.relay_harness_stream(iter(stream)))
    apps = [e for e in evs if e.get("mcpApp")]
    assert len(apps) == 1
    assert apps[0]["mcpApp"]["toolResult"]["content"][0]["text"] == refilled


def test_call_tool_requires_login(env):
    assert TestClient(server.app).post(
        "/api/mcp-apps/call-tool",
        json={"mcpServer": "korea-weather", "tool": "get_weather", "arguments": {}},
    ).status_code == 401


def test_call_tool_rejects_unknown_server(env):
    r = client().post(
        "/api/mcp-apps/call-tool",
        json={"mcpServer": "other", "tool": "get_weather", "arguments": {}},
    )
    assert r.status_code == 400
