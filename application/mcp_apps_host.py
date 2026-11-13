"""MCP Apps host helpers: weather UI fallback + iframe tool proxy."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger("mcp_apps_host")

_APP_BY_TOOL: dict[str, dict[str, Any]] = {}

_WEATHER_TOOLS = {
    "get_weather",
    "get_short_term_forecast",
    "get_metropolitan_weather",
}
_WEATHER_SERVERS = {
    "weather",
    "korea-weather",
    "korea_weather",
}


def clear() -> None:
    _APP_BY_TOOL.clear()


def register_tool_app(
    tool_name: str,
    *,
    resource_uri: str,
    html: str,
    mcp_server: str | None = None,
    csp: dict[str, Any] | None = None,
    mime_type: str | None = None,
) -> None:
    if not tool_name or not resource_uri or not html:
        return
    _APP_BY_TOOL[tool_name] = {
        "resourceUri": resource_uri,
        "html": html,
        "mcpServer": mcp_server,
        "csp": csp or {},
        "mimeType": mime_type or "text/html;profile=mcp-app",
        "tool": tool_name,
    }


def get_tool_app(tool_name: str | None) -> dict[str, Any] | None:
    if not tool_name:
        return None
    bare = _bare_tool_name(tool_name)
    return _APP_BY_TOOL.get(tool_name) or _APP_BY_TOOL.get(bare)


def _bare_tool_name(tool_name: str) -> str:
    """Gateway 접두사(`server___tool`)를 제거한다."""
    if "___" in tool_name:
        return tool_name.split("___")[-1]
    return tool_name


def _fallback_weather_app(tool_name: str) -> dict[str, Any] | None:
    bare = _bare_tool_name(tool_name)
    if bare not in _WEATHER_TOOLS:
        return None
    try:
        from .weather_ui import VIEW_URI, WEATHER_HTML
    except Exception as exc:
        logger.warning("weather UI fallback import failed: %s", exc)
        return None
    register_tool_app(
        bare,
        resource_uri=VIEW_URI,
        html=WEATHER_HTML,
        mcp_server="korea-weather",
        csp={"resourceDomains": ["https://www.weather.go.kr"]},
    )
    return get_tool_app(bare)


def build_app_payload(tool_name: str | None, tool_result_text: str) -> dict[str, Any] | None:
    if not tool_name:
        return None
    app = get_tool_app(tool_name) or _fallback_weather_app(tool_name)
    if not app:
        return None
    return {
        **app,
        "tool": _bare_tool_name(tool_name),
        "toolResult": {
            "content": [{"type": "text", "text": tool_result_text}],
        },
    }


def build_app_payload_if_ready(tool_name: str | None, tool_result_text: str) -> dict[str, Any] | None:
    """스트리밍 중 잘린 JSON 으로는 iframe 을 만들지 않는다(agentic-work 는 완전본만 전달).

    누적 결과가 완전한 JSON object 일 때만 mcpApp 을 붙인다. 불완전 조각으로 빈 예보 UI 가
    먼저 마운트되는 것을 막는다.
    """
    if not tool_name or not tool_result_text:
        return None
    try:
        parsed = json.loads(tool_result_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    bare = _bare_tool_name(tool_name)
    # weather UI 는 region 이 있어야 예보를 그린다. region 없는 조각은 아직 미완성으로 본다.
    if bare in _WEATHER_TOOLS and "region" not in parsed:
        return None
    return build_app_payload(tool_name, tool_result_text)


def refill_weather_tool_result(tool_name: str | None, tool_input: str = "") -> str | None:
    """스트림이 잘려 JSON 이 깨졌을 때 korea-weather 를 로컬에서 다시 조회한다."""
    if not tool_name:
        return None
    bare = _bare_tool_name(tool_name)
    if bare not in _WEATHER_TOOLS:
        return None
    args: dict[str, Any] = {}
    if tool_input and tool_input.strip()[:1] in "{[":
        try:
            raw = json.loads(tool_input)
            if isinstance(raw, dict):
                args = raw
        except json.JSONDecodeError:
            args = {}
    try:
        from . import weather_core

        if bare in {"get_weather", "get_short_term_forecast"}:
            region = str(args.get("region") or args.get("city") or "전국")
            return weather_core._tool_result(region)
        city = str(args.get("city") or "").strip()
        return weather_core._tool_result(city or "서울·인천·경기")
    except Exception as exc:  # noqa: BLE001
        logger.warning("weather toolResult refill failed: %s", exc)
        return None


def normalize_tool_result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text" and isinstance(content.get("text"), str):
            return content["text"]
        import json

        try:
            return json.dumps(content, ensure_ascii=False)
        except Exception:
            return str(content)
    if isinstance(content, list):
        texts: list[str] = []
        for block in content:
            if isinstance(block, str):
                texts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                texts.append(str(block.get("text", "")))
            elif hasattr(block, "text"):
                texts.append(str(getattr(block, "text") or ""))
            else:
                texts.append(str(block))
        return "\n".join(t for t in texts if t)
    return str(content)


def _normalize_server(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")


async def call_mcp_tool(
    mcp_server: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call korea-weather tools in-process for MCP Apps iframe refresh."""
    arguments = arguments or {}
    server = _normalize_server(mcp_server)
    name = _bare_tool_name((tool_name or "").strip())

    if server not in {_normalize_server(s) for s in _WEATHER_SERVERS}:
        raise ValueError(
            f"mcp-apps proxy only supports korea-weather/weather (got mcpServer={mcp_server!r})"
        )
    if name not in _WEATHER_TOOLS:
        raise ValueError(f"Unsupported weather tool: {tool_name}")

    from . import weather_core

    if name in {"get_weather", "get_short_term_forecast"}:
        region = str(arguments.get("region") or arguments.get("city") or "전국")
        text = weather_core._tool_result(region)
    else:
        city = str(arguments.get("city") or "").strip()
        text = weather_core._tool_result(city or "서울·인천·경기")

    return {
        "content": [{"type": "text", "text": text}],
        "isError": False,
    }
