"""MCP Apps weather UI (ui:// HTML) shared by korea-weather and ai-portal host."""

VIEW_URI = "ui://kma-short-term/weather.html"

WEATHER_HTML = r"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>기상청 단기예보 종합</title>
  <style>
    :root {
      --bg: #0b1f33;
      --panel: rgba(255,255,255,0.08);
      --text: #f4f8fc;
      --muted: #9db4c8;
      --accent: #3db8ff;
      --cool: #7ed0ff;
      --warm: #ffb454;
      --line: rgba(255,255,255,0.12);
      --chart: #4aa3ff;
    }
    * { box-sizing: border-box; }
    html, body {
      margin: 0; padding: 0;
      background: radial-gradient(1200px 500px at 10% -10%, #1a4d7a 0%, transparent 55%),
                  radial-gradient(900px 420px at 100% 0%, #0f3a5c 0%, transparent 50%),
                  var(--bg);
      color: var(--text);
      font-family: "Pretendard", "Apple SD Gothic Neo", "Noto Sans KR", sans-serif;
      min-height: 100%;
    }
    .wrap { padding: 16px; max-width: 960px; margin: 0 auto; }
    header {
      display: flex; justify-content: space-between; gap: 12px; align-items: flex-start;
      margin-bottom: 12px;
    }
    h1 { margin: 0; font-size: 1.15rem; letter-spacing: -0.02em; }
    .sub { color: var(--muted); font-size: 0.8rem; margin-top: 4px; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; }
    button, select {
      appearance: none; border: 1px solid var(--line); background: var(--panel);
      color: var(--text); border-radius: 10px; padding: 8px 12px; font-size: 0.85rem;
      cursor: pointer;
    }
    button:hover, select:hover { border-color: var(--accent); }
    button.primary { background: linear-gradient(135deg, #1f6fad, #2aa0df); border-color: transparent; }
    .status { color: var(--muted); font-size: 0.78rem; margin-bottom: 10px; min-height: 1.1em; }
    .panel {
      background: linear-gradient(160deg, rgba(255,255,255,0.10), rgba(255,255,255,0.04));
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px;
      margin-bottom: 12px;
    }
    .period { font-size: 1rem; font-weight: 700; margin-bottom: 6px; }
    .announce { color: var(--muted); font-size: 0.82rem; margin-bottom: 10px; }
    .summary { display: flex; flex-direction: column; gap: 6px; }
    .line {
      font-size: 0.88rem; line-height: 1.45;
      padding: 8px 10px; border-radius: 10px; background: rgba(0,0,0,0.18);
    }
    .line.depth_1 { border-left: 3px solid var(--accent); font-weight: 650; }
    .line.depth_2 { border-left: 3px solid var(--cool); }
    .line.depth_3, .line.depth_4 { border-left: 3px solid var(--warm); color: #dce8f3; font-size: 0.82rem; }
    .section-title {
      margin: 4px 0 8px; font-size: 0.92rem; color: var(--cool); font-weight: 650;
      display: flex; justify-content: space-between; align-items: baseline; gap: 8px;
    }
    .section-title .hint { color: var(--muted); font-size: 0.75rem; font-weight: 400; }
    .table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
    table {
      width: 100%; border-collapse: collapse; min-width: 560px;
      font-size: 0.8rem;
    }
    th, td {
      border: 1px solid var(--line); padding: 8px 6px; text-align: center;
      white-space: nowrap;
    }
    th { background: rgba(61,184,255,0.12); color: var(--cool); font-weight: 650; }
    td.label, th.label { text-align: left; color: var(--text); background: rgba(0,0,0,0.16); }
    .links { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 8px; }
    .links a { color: var(--accent); font-size: 0.8rem; text-decoration: none; }
    .links a:hover { text-decoration: underline; }
    .empty { color: var(--muted); padding: 24px 8px; text-align: center; }

    /* Hourly forecast chart (날씨누리 시간별 예보 스타일) */
    .hourly-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
    .hourly {
      display: grid;
      grid-template-columns: 72px 1fr;
      min-width: 100%;
      font-size: 0.72rem;
    }
    .hourly-labels {
      display: flex; flex-direction: column;
      color: var(--muted); font-weight: 650;
      border-right: 1px solid var(--line);
      background: rgba(0,0,0,0.12);
    }
    .hourly-labels > div {
      height: 36px; display: flex; align-items: center; padding: 0 8px;
      border-bottom: 1px solid var(--line);
    }
    .hourly-labels > .chart-label { height: 88px; }
    .hourly-scroll { overflow: visible; }
    .hourly-cols {
      position: relative;
      display: grid;
      grid-auto-flow: column;
      grid-auto-columns: 56px;
    }
    .hcol {
      border-right: 1px solid rgba(255,255,255,0.06);
      text-align: center;
    }
    .hcol > div {
      height: 36px; display: flex; align-items: center; justify-content: center;
      border-bottom: 1px solid var(--line); padding: 0 2px;
    }
    .hcol .date-cell {
      color: #fff; font-weight: 700; font-size: 0.68rem;
      background: linear-gradient(180deg, #2f6dad, #1d4f80);
    }
    .hcol .date-cell.empty-date { background: transparent; color: transparent; }
    .hcol img.wic { width: 28px; height: 28px; }
    .hcol .chart-cell {
      height: 88px; position: relative; overflow: visible;
    }
    .hcol .temp-point {
      position: absolute; left: 50%; transform: translate(-50%, -50%);
      color: var(--chart); font-weight: 700; font-size: 0.78rem;
      white-space: nowrap; z-index: 2;
    }
    .hourly-svg {
      position: absolute;
      left: 0;
      top: 108px; /* 날짜+시각+날씨 = 36*3 */
      height: 88px;
      overflow: visible;
      pointer-events: none;
      z-index: 1;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div>
        <h1>기상청 단기예보 종합</h1>
        <div class="sub">weather.go.kr · 예보 종합 / 단기예보 · 시간별 예보</div>
      </div>
      <div class="actions">
        <select id="region-filter" title="지역"></select>
        <button class="primary" id="refresh-btn">새로고침</button>
      </div>
    </header>
    <div class="status" id="status">불러오는 중…</div>
    <div id="main"></div>
    <div class="links">
      <a id="source-link" href="https://www.weather.go.kr/w/forecast/overall/short-term.do" target="_blank" rel="noreferrer">단기예보 종합</a>
      <a id="pdf-link" href="#" target="_blank" rel="noreferrer" style="display:none;">PDF 통보문</a>
      <a href="https://www.weather.go.kr/w/index.do" target="_blank" rel="noreferrer">날씨누리</a>
    </div>
  </div>
  <script>
    const statusEl = document.getElementById("status");
    const mainEl = document.getElementById("main");
    const regionFilter = document.getElementById("region-filter");
    const refreshBtn = document.getElementById("refresh-btn");
    const sourceLink = document.getElementById("source-link");
    const pdfLink = document.getElementById("pdf-link");

    let payload = null;
    let nextId = 1;
    const pending = new Map();

    function post(msg) {
      parent.postMessage(msg, "*");
    }

    function request(method, params) {
      const id = nextId++;
      return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        post({ jsonrpc: "2.0", id, method, params });
        setTimeout(() => {
          if (pending.has(id)) {
            pending.delete(id);
            reject(new Error("timeout"));
          }
        }, 30000);
      });
    }

    function dash(v) {
      if (v === null || v === undefined || v === "" || v === "-") return "-";
      return String(v);
    }

    function escapeHtml(s) {
      return String(s)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }

    function parsePayload(result) {
      const text = result?.content?.find((c) => c.type === "text")?.text;
      if (!text) return null;
      try {
        const parsed = JSON.parse(text);
        if (parsed && typeof parsed === "object") return parsed;
        return { summary: text };
      } catch {
        return { summary: text };
      }
    }

    function fillRegionOptions(regions, currentId) {
      regionFilter.innerHTML = "";
      for (const r of regions || []) {
        const opt = document.createElement("option");
        opt.value = r.stn_id;
        opt.textContent = r.name;
        regionFilter.appendChild(opt);
      }
      if (currentId) regionFilter.value = String(currentId);
    }

    function reportSize() {
      const height = Math.ceil(document.documentElement.getBoundingClientRect().height);
      post({
        jsonrpc: "2.0",
        method: "ui/notifications/size-changed",
        params: { height: height || 480, width: window.innerWidth },
      });
    }

    function weekdayKo(dateStr) {
      try {
        const d = new Date(dateStr + "T00:00:00");
        return ["일","월","화","수","목","금","토"][d.getDay()];
      } catch { return ""; }
    }

    function renderTable(days, rows) {
      if (!days || !days.length || !rows || !rows.length) return "";
      let head = "<tr><th class='label'>예보요소</th>";
      for (const d of days) head += "<th>" + escapeHtml(d) + "</th>";
      head += "</tr>";
      let body = "";
      for (const row of rows) {
        body += "<tr><td class='label'>" + escapeHtml(row.label) + "</td>";
        const vals = row.values || [];
        for (let i = 0; i < days.length; i++) {
          body += "<td>" + escapeHtml(dash(vals[i])) + "</td>";
        }
        body += "</tr>";
      }
      return (
        '<div class="section-title">기온·해상</div>' +
        '<div class="panel table-wrap"><table><thead>' + head +
        "</thead><tbody>" + body + "</tbody></table></div>"
      );
    }

    function renderHourly(hourly, site) {
      if (!hourly || !hourly.length) return "";
      const temps = hourly.map((h) => {
        const n = parseFloat(h.temp);
        return Number.isFinite(n) ? n : null;
      });
      const valid = temps.filter((t) => t !== null);
      const tMin = valid.length ? Math.min(...valid) - 1 : 0;
      const tMax = valid.length ? Math.max(...valid) + 1 : 30;
      const span = Math.max(tMax - tMin, 1);
      const chartH = 88;
      const padY = 14;
      const ys = temps.map((t) =>
        t === null ? null : padY + ((tMax - t) / span) * (chartH - padY * 2)
      );

      const colW = 56;
      let poly = [];
      for (let i = 0; i < ys.length; i++) {
        if (ys[i] === null) continue;
        poly.push((i * colW + colW / 2) + "," + ys[i]);
      }
      const svgW = hourly.length * colW;
      const svg =
        '<svg class="hourly-svg" width="' + svgW + '" height="' + chartH +
        '" viewBox="0 0 ' + svgW + " " + chartH +
        '" preserveAspectRatio="none" aria-hidden="true">' +
        '<polyline fill="none" stroke="#4aa3ff" stroke-width="2" points="' +
        poly.join(" ") + '" />' +
        poly.map((p) => {
          const [x, y] = p.split(",");
          return '<circle cx="' + x + '" cy="' + y + '" r="2.5" fill="#4aa3ff" />';
        }).join("") +
        "</svg>";

      let cols = "";
      let prevDate = "";
      for (let i = 0; i < hourly.length; i++) {
        const h = hourly[i];
        const date = h.date || "";
        const showDate = date && date !== prevDate;
        prevDate = date || prevDate;
        const dayNum = date ? Number(date.split("-")[2]) : "";
        const dateLabel = showDate
          ? (dayNum + "일(" + weekdayKo(date) + ")")
          : "";
        const y = ys[i];
        const tempLabel = h.temp != null && h.temp !== "" ? (dash(h.temp) + "℃") : "-";
        cols +=
          '<div class="hcol">' +
            '<div class="date-cell' + (showDate ? "" : " empty-date") + '">' +
              escapeHtml(dateLabel || ".") +
            "</div>" +
            "<div>" + escapeHtml(dash(h.hour_label || h.time)) + "</div>" +
            "<div>" +
              (h.icon_url
                ? '<img class="wic" src="' + escapeHtml(h.icon_url) + '" alt="' +
                  escapeHtml(dash(h.condition)) + '" title="' +
                  escapeHtml(dash(h.condition)) + '" />'
                : escapeHtml(dash(h.condition))) +
            "</div>" +
            '<div class="chart-cell">' +
              (y !== null
                ? '<span class="temp-point" style="top:' + y + 'px">' +
                  escapeHtml(tempLabel) + "</span>"
                : '<span class="temp-point" style="top:50%">-</span>') +
            "</div>" +
            "<div>" + escapeHtml(dash(h.feels_like) + (h.feels_like && h.feels_like !== "-" ? "℃" : "")) + "</div>" +
            "<div>" + escapeHtml(dash(h.precip_intensity)) + "</div>" +
            "<div>" + escapeHtml(dash(h.precip_prob)) + "</div>" +
            "<div title=\"" + escapeHtml(dash(h.wind)) + "\">" +
              escapeHtml(dash(h.wind_short || h.wind)) +
            "</div>" +
            "<div>" + escapeHtml(dash(h.humidity)) + "</div>" +
          "</div>";
      }

      const siteHint = site && site.label
        ? ('대표지점 ' + site.label + ' · 3시간 간격')
        : "3시간 간격";

      return (
        '<div class="section-title">시간별 예보' +
          '<span class="hint">' + escapeHtml(siteHint) + "</span></div>" +
        '<div class="panel hourly-wrap"><div class="hourly">' +
          '<div class="hourly-labels">' +
            "<div>날짜</div><div>시각</div><div>날씨</div>" +
            '<div class="chart-label">기온</div>' +
            "<div>체감온도</div><div>강수강도</div><div>강수확률</div>" +
            "<div>바람</div><div>습도</div>" +
          "</div>" +
          '<div class="hourly-scroll"><div class="hourly-cols">' +
            svg + cols +
          "</div></div></div></div>"
      );
    }

    function render() {
      if (!payload || !payload.region) {
        statusEl.textContent = "단기예보 데이터가 없습니다.";
        mainEl.innerHTML = '<div class="empty">도구를 다시 호출해 주세요.</div>';
        reportSize();
        return;
      }

      fillRegionOptions(payload.regions, payload.region.stn_id);
      statusEl.textContent = [
        payload.region.name ? ("지역 " + payload.region.name) : null,
        payload.announce || null,
        payload.fetched_at ? ("갱신 " + payload.fetched_at) : null,
      ].filter(Boolean).join(" · ");

      const lines = (payload.summary_lines || []).map((item) => {
        const depth = item.depth || "depth_2";
        const text = typeof item === "string" ? item : item.text;
        return '<div class="line ' + escapeHtml(depth) + '">' + escapeHtml(text) + "</div>";
      }).join("");

      mainEl.innerHTML =
        '<div class="panel">' +
          '<div class="period">' + escapeHtml(dash(payload.period)) + "</div>" +
          '<div class="announce">' + escapeHtml(dash(payload.announce)) + "</div>" +
          '<div class="summary">' + (lines || '<div class="empty">요약 없음</div>') + "</div>" +
        "</div>" +
        renderHourly(payload.hourly, payload.hourly_site) +
        renderTable(payload.days, payload.rows);

      if (payload.source_url) sourceLink.href = payload.source_url;
      if (payload.pdf_url) {
        pdfLink.href = payload.pdf_url;
        pdfLink.style.display = "";
      } else {
        pdfLink.style.display = "none";
      }
      reportSize();
    }

    function applyResult(result) {
      payload = parsePayload(result);
      render();
    }

    window.addEventListener("message", (event) => {
      const data = event.data;
      if (!data || data.jsonrpc !== "2.0") return;
      if (data.id !== undefined && (data.result !== undefined || data.error)) {
        const waiter = pending.get(data.id);
        if (!waiter) return;
        pending.delete(data.id);
        if (data.error) waiter.reject(new Error(data.error.message || "error"));
        else waiter.resolve(data.result);
        return;
      }
      if (data.method === "ui/notifications/tool-result") {
        applyResult(data.params || {});
      }
    });

    async function reload(region) {
      statusEl.textContent = "새로고침 중…";
      try {
        const result = await request("tools/call", {
          name: "get_weather",
          arguments: { region: region || regionFilter.value || "전국" },
        });
        applyResult(result);
      } catch (err) {
        statusEl.textContent = "새로고침 실패: " + (err && err.message ? err.message : err);
      }
    }

    refreshBtn.addEventListener("click", () => reload());
    regionFilter.addEventListener("change", () => reload(regionFilter.value));

    (async function connect() {
      const boot = window.__MCP_HOST_BOOTSTRAP__;
      if (boot && boot.toolResult) {
        applyResult(boot.toolResult);
      }
      try {
        await request("ui/initialize", {
          appInfo: { name: "KMA Short-term Forecast", version: "2.1.0" },
          appCapabilities: {},
          protocolVersion: "2026-01-26",
        });
        post({ jsonrpc: "2.0", method: "ui/notifications/initialized" });
        reportSize();
      } catch (err) {
        // Keep bootstrapped forecast visible when host handshake fails.
        if (!payload) {
          statusEl.textContent = "호스트 연결 실패: " + (err && err.message ? err.message : err);
        }
      }
    })();
  </script>
</body>
</html>
"""
