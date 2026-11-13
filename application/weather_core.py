"""
기상청 단기예보 종합 + 시간별 예보 fetch/parse helpers.

Sources:
- https://www.weather.go.kr/w/forecast/overall/short-term.do?stnId={region}
- https://www.weather.go.kr/w/wnuri-fct2021/main/digital-forecast.do (hourly chart)
"""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(filename)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("mcp-server-weather")

KST = ZoneInfo("Asia/Seoul")
WEATHER_DOMAIN = "https://www.weather.go.kr"
SOURCE_PAGE = f"{WEATHER_DOMAIN}/w/forecast/overall/short-term.do"
DIGITAL_FORECAST_URL = f"{WEATHER_DOMAIN}/w/wnuri-fct2021/main/digital-forecast.do"
ICON_BASE = f"{WEATHER_DOMAIN}/w/resources/icon"

# Regional forecast-office tabs on short-term.do + representative dong for hourly
REGIONS: dict[str, dict[str, Any]] = {
    "108": {
        "ko": "전국",
        "aliases": ["전국", "korea", "national", "전체"],
        "site": {
            "label": "서울 종로구",
            "code": "1111053000",
            "lat": 37.5691469686793,
            "lon": 126.978647068151,
        },
    },
    "109": {
        "ko": "서울·인천·경기",
        "aliases": [
            "서울", "인천", "경기", "수도권", "서울특별시", "인천광역시",
            "경기도", "수원", "파주", "동두천", "강화", "양평", "이천",
            "seoul", "incheon", "gyeonggi", "metropolitan",
        ],
        "site": {
            "label": "서울 종로구",
            "code": "1111053000",
            "lat": 37.5691469686793,
            "lon": 126.978647068151,
        },
    },
    "159": {
        "ko": "부산·울산·경남",
        "aliases": [
            "부산", "울산", "경남", "경상남도", "부울경", "창원",
            "busan", "ulsan", "gyeongnam",
        ],
        "site": {
            "label": "부산 중구",
            "code": "2611056000",
            "lat": 35.0988678741728,
            "lon": 129.029045595229,
        },
    },
    "143": {
        "ko": "대구·경북",
        "aliases": ["대구", "경북", "경상북도", "포항", "daegu", "gyeongbuk"],
        "site": {
            "label": "대구 중구",
            "code": "2711068000",
            "lat": 35.8602851249823,
            "lon": 128.6069533801216,
        },
    },
    "156": {
        "ko": "전남광주통합특별시",
        "aliases": [
            "광주", "전남", "전라남도", "광주광역시", "목포", "여수",
            "gwangju", "jeonnam",
        ],
        "site": {
            "label": "광주 동구",
            "code": "1221052500",
            "lat": 35.1474913380556,
            "lon": 126.919801652795,
        },
    },
    "146": {
        "ko": "전북특별자치도",
        "aliases": ["전북", "전라북도", "전주", "군산", "jeonbuk"],
        "site": {
            "label": "전주",
            "code": "5211153000",
            "lat": 35.81477744329797,
            "lon": 127.15255700142177,
        },
    },
    "133": {
        "ko": "대전·세종·충남",
        "aliases": [
            "대전", "세종", "충남", "충청남도", "천안",
            "daejeon", "sejong", "chungnam",
        ],
        "site": {
            "label": "대전 서구",
            "code": "3017065000",
            "lat": 36.36994889192411,
            "lon": 127.38801706985069,
        },
    },
    "131": {
        "ko": "충북",
        "aliases": ["충북", "충청북도", "청주", "chungbuk"],
        "site": {
            "label": "청주",
            "code": "4311453000",
            "lat": 36.6623007044479,
            "lon": 127.5413954468505,
        },
    },
    "105": {
        "ko": "강원특별자치도",
        "aliases": ["강원", "강원도", "춘천", "강릉", "gangwon"],
        "site": {
            "label": "춘천",
            "code": "5111057000",
            "lat": 37.86433423002352,
            "lon": 127.69129278762853,
        },
    },
    "184": {
        "ko": "제주특별자치도",
        "aliases": ["제주", "제주도", "서귀포", "jeju"],
        "site": {
            "label": "제주시",
            "code": "5011054000",
            "lat": 33.4966553222693,
            "lon": 126.53592579247683,
        },
    },
}

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Referer": SOURCE_PAGE,
}

def _norm(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"[\s·⋅•./]", "", text.strip().lower())


def _region_catalog() -> list[dict[str, str]]:
    return [{"stn_id": sid, "name": meta["ko"]} for sid, meta in REGIONS.items()]


def resolve_region(region: str | None) -> tuple[str, str]:
    """Return (stn_id, region_name). Empty => 전국(108)."""
    if not region or not str(region).strip():
        return "108", REGIONS["108"]["ko"]

    raw = str(region).strip()
    if raw in REGIONS:
        return raw, REGIONS[raw]["ko"]

    needle = _norm(raw)
    for stn_id, meta in REGIONS.items():
        names = [_norm(meta["ko"]), *[_norm(a) for a in meta["aliases"]]]
        if needle in names:
            return stn_id, meta["ko"]

    for stn_id, meta in REGIONS.items():
        names = [_norm(meta["ko"]), *[_norm(a) for a in meta["aliases"]]]
        if any(needle in n or n in needle for n in names if n):
            return stn_id, meta["ko"]

    options = ", ".join(f"{sid}:{meta['ko']}" for sid, meta in REGIONS.items())
    raise ValueError(f"지원하지 않는 지역입니다: {region}. 가능 지역: {options}")


def _parse_summary_lines(summary) -> list[dict[str, str]]:
    lines: list[dict[str, str]] = []
    if not summary:
        return lines
    spans = summary.select("span")
    if spans:
        for sp in spans:
            text = " ".join(sp.stripped_strings)
            if not text:
                continue
            classes = sp.get("class") or []
            depth = next((c for c in classes if str(c).startswith("depth_")), "depth_2")
            lines.append({"text": text, "depth": depth})
        return lines
    for ln in summary.get_text("\n").splitlines():
        text = ln.strip()
        if text:
            lines.append({"text": text, "depth": "depth_2"})
    return lines


def _parse_table(section) -> tuple[list[str], list[dict[str, Any]]]:
    table = section.select_one("table.table-col")
    if not table:
        return [], []

    heads = [th.get_text(" ", strip=True) for th in table.select("thead th")]
    days = heads[1:] if len(heads) > 1 else heads
    rows: list[dict[str, Any]] = []
    pending_group: str | None = None

    for tr in table.select("tbody tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"], recursive=False)]
        if not cells:
            continue
        if cells[0].startswith("파고"):
            pending_group = cells[0]
            if len(cells) >= 2:
                label = f"{pending_group} {cells[1]}" if cells[1] else pending_group
                values = cells[2:]
                rows.append({"label": label, "values": _align_values(days, values)})
            continue
        if pending_group and not any(x in cells[0] for x in ("최저기온", "최고기온")):
            label = f"{pending_group} {cells[0]}"
            values = cells[1:]
            rows.append({"label": label, "values": _align_values(days, values)})
            continue
        pending_group = None
        label = cells[0]
        values = cells[1:]
        rows.append({"label": label, "values": _align_values(days, values)})
    return days, rows


def _align_values(days: list[str], values: list[str]) -> list[str]:
    if len(values) == len(days):
        return values
    if len(values) < len(days):
        pad = len(days) - len(values)
        return (["-"] * pad) + values
    return values[: len(days)]


def _weather_icon_code(el) -> str | None:
    if not el:
        return None
    for cls in el.get("class") or []:
        text = str(cls)
        if text.startswith(("DB", "NB")):
            return text
    return None


def _weather_icon_url(code: str | None) -> str | None:
    if not code or code in {"DB00", "NB00"}:
        return None
    if code.startswith("NB"):
        folder = "NY@64/Light"
    else:
        folder = "DY@64/Light"
    return f"{ICON_BASE}/{folder}/{code}.png"


def _li_field(item, *labels: str) -> str | None:
    for li in item.select("li"):
        hid = li.select_one("span.hid")
        if not hid:
            continue
        label = hid.get_text(" ", strip=True).rstrip(":： ").replace(" ", "")
        if not any(lab.replace(" ", "") in label for lab in labels):
            continue
        for child in li.find_all("span"):
            classes = child.get("class") or []
            if "hid" in classes:
                continue
            text = child.get_text(" ", strip=True)
            if text:
                return text
        texts = []
        for child in li.children:
            name = getattr(child, "name", None)
            if name is None:
                t = str(child).strip()
                if t:
                    texts.append(t)
                continue
            classes = child.get("class") or []
            if "hid" in classes:
                continue
            texts.append(child.get_text(" ", strip=True))
        val = re.sub(r"\s+", " ", " ".join(texts)).strip()
        return val or None
    return None


def _num_celsius(value: str | None) -> str | None:
    if not value:
        return None
    m = re.search(r"([-\d.]+)", value)
    return m.group(1) if m else None


def parse_hourly_forecast(html: str) -> list[dict[str, Any]]:
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    hourly: list[dict[str, Any]] = []
    for item in soup.select("ul.item[data-time]"):
        date = item.get("data-date") or ""
        time = item.get("data-time") or ""
        if not (date and time):
            continue

        temp = None
        feel_el = item.select_one("span.feel")
        if feel_el:
            temp = _num_celsius(feel_el.get_text(" ", strip=True))
        if temp is None:
            temp = _num_celsius(_li_field(item, "기온"))

        feels = _num_celsius(_li_field(item, "체감온도"))
        sky_el = item.select_one(".wic")
        icon = _weather_icon_code(sky_el)
        condition = None
        if sky_el:
            condition = sky_el.get("title") or sky_el.get_text(strip=True)

        wind = _li_field(item, "바람") or "-"
        wind_short = wind
        wspd = item.select_one(".wspd")
        wdir = item.select_one(".wdic")
        if wdir or wspd:
            parts = []
            if wdir:
                parts.append(wdir.get("title") or wdir.get_text(strip=True))
            if wspd:
                parts.append(wspd.get_text(strip=True))
            wind = " ".join(p for p in parts if p) or wind
            wind_short = wspd.get_text(strip=True) if wspd else wind

        intensity = _li_field(item, "강수강도")
        if intensity is None:
            # KMA chart places intensity as an unlabeled <li> after 강수량.
            lis = item.select("li")
            for idx, li in enumerate(lis):
                hid = li.select_one("span.hid")
                if hid and "강수량" in hid.get_text(" ", strip=True):
                    if idx + 1 < len(lis) and not lis[idx + 1].select_one("span.hid"):
                        intensity = lis[idx + 1].get_text(" ", strip=True) or "-"
                    break
        if not intensity:
            intensity = "-"

        hour_label = time
        if re.match(r"^\d{2}:\d{2}$", time):
            hour_label = f"{int(time[:2])}시"
        elif time == "24:00":
            hour_label = "24시"

        hourly.append(
            {
                "date": date,
                "time": time,
                "hour_label": hour_label,
                "temp": temp,
                "feels_like": feels,
                "condition": condition or "-",
                "icon": icon,
                "icon_url": _weather_icon_url(icon),
                "precip_amount": _li_field(item, "강수량") or "-",
                "precip_intensity": intensity,
                "precip_prob": _li_field(item, "강수확률") or "-",
                "wind": wind,
                "wind_short": wind_short,
                "humidity": _li_field(item, "습도") or "-",
            }
        )
    return hourly


def fetch_hourly_forecast(stn_id: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    meta = REGIONS.get(stn_id) or {}
    site = meta.get("site")
    if not site:
        return [], None
    params = {
        "code": site["code"],
        "unit": "m/s",
        "hr1": "N",  # 3-hour interval (matches KMA chart default screenshot)
        "lat": site["lat"],
        "lon": site["lon"],
    }
    headers = {
        **REQUEST_HEADERS,
        "Accept": "text/html, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{WEATHER_DOMAIN}/w/index.do",
    }
    resp = requests.get(DIGITAL_FORECAST_URL, params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return parse_hourly_forecast(resp.text), {
        "label": site["label"],
        "code": site["code"],
        "lat": site["lat"],
        "lon": site["lon"],
        "interval": "3h",
    }


def fetch_short_term_forecast(region: str | None = None) -> dict[str, Any]:
    stn_id, region_name = resolve_region(region)
    url = f"{SOURCE_PAGE}?stnId={stn_id}"
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=20)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    html = resp.text

    soup = BeautifulSoup(html, "html.parser")
    section = soup.select_one("section.cmp-view")
    if not section:
        raise RuntimeError("단기예보 본문을 찾지 못했습니다.")

    announce_el = section.select_one(".cmp-view-announce")
    announce = " ".join(announce_el.stripped_strings) if announce_el else ""
    header_el = section.select_one(".cmp-view-header h4, .cmp-view-header h3")
    period = header_el.get_text(strip=True) if header_el else ""
    summary_lines = _parse_summary_lines(section.select_one("p.summary"))
    days, rows = _parse_table(section)

    pdf_el = section.select_one("a.pdf-download") or soup.select_one('a[href*="rpt_wid_day"]')
    pdf_url = None
    if pdf_el and pdf_el.get("href"):
        pdf_url = urljoin(WEATHER_DOMAIN, pdf_el["href"])

    hourly: list[dict[str, Any]] = []
    hourly_site: dict[str, Any] | None = None
    try:
        hourly, hourly_site = fetch_hourly_forecast(stn_id)
    except Exception as exc:
        logger.warning("hourly forecast fetch failed for %s: %s", stn_id, exc)

    return {
        "source": SOURCE_PAGE,
        "source_url": url,
        "office": "기상청",
        "fetched_at": datetime.now(KST).strftime("%Y.%m.%d %H:%M"),
        "region": {"stn_id": stn_id, "name": region_name},
        "regions": _region_catalog(),
        "announce": announce,
        "period": period,
        "summary_lines": summary_lines,
        "days": days,
        "rows": rows,
        "pdf_url": pdf_url,
        "hourly": hourly,
        "hourly_site": hourly_site,
    }


def format_weather_text(payload: dict[str, Any]) -> str:
    region = payload.get("region") or {}
    lines = [
        f"[기상청] 단기예보 종합 — {region.get('name', '')}",
        f"출처: {payload.get('source_url') or payload.get('source')}",
    ]
    if payload.get("announce"):
        lines.append(payload["announce"])
    if payload.get("period"):
        lines.append(f"기간: {payload['period']}")
    lines.append("")

    for item in payload.get("summary_lines") or []:
        text = item.get("text") if isinstance(item, dict) else str(item)
        if text:
            lines.append(text)

    days = payload.get("days") or []
    temp_rows = [
        r
        for r in (payload.get("rows") or [])
        if "기온" in str(r.get("label", ""))
    ]
    if days and temp_rows:
        lines.append("")
        lines.append("기온: " + " | ".join(days))
        for row in temp_rows:
            vals = " | ".join(dash for dash in (row.get("values") or []))
            lines.append(f"- {row.get('label')}: {vals}")

    hourly = payload.get("hourly") or []
    site = payload.get("hourly_site") or {}
    if hourly:
        lines.append("")
        site_label = site.get("label") or "대표지점"
        lines.append(f"시간별 예보 ({site_label}, 3시간 간격):")
        for h in hourly[:12]:
            lines.append(
                f"- {h.get('date')} {h.get('hour_label')}: "
                f"{h.get('temp', '-')}℃/{h.get('condition', '-')} · "
                f"강수확률 {h.get('precip_prob', '-')} · "
                f"바람 {h.get('wind', '-')} · 습도 {h.get('humidity', '-')}"
            )

    if payload.get("pdf_url"):
        lines.append(f"\nPDF 통보문: {payload['pdf_url']}")
    return "\n".join(lines)


def _tool_result(region: str | None) -> str:
    try:
        payload = fetch_short_term_forecast(region)
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        logger.exception("short-term forecast fetch failed")
        return f"단기예보 조회 중 오류가 발생했습니다: {exc}"

    payload_out = {
        "summary": format_weather_text(payload),
        **payload,
    }
    return json.dumps(payload_out, ensure_ascii=False)


