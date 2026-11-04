import { useEffect, useState } from "react";
import * as api from "./api";
import type { Item, Kind } from "./api";
import { ago } from "./Explore";

const KIND: Record<Kind, string> = { prompt: "Prompt", mcp: "MCP", skill: "Skill", agent: "Agent" };
const ORDER = ["APPROVED", "PENDING_APPROVAL", "DRAFT", "REJECTED", "DEPRECATED", "NOT_REGISTERED"] as const;
const LABEL: Record<string, string> = {
  APPROVED: "등록", PENDING_APPROVAL: "승인 대기", DRAFT: "초안", REJECTED: "거절됨", DEPRECATED: "Deprecated",
  NOT_REGISTERED: "Registry 미등록", CREATING: "등록 중", UPDATING: "갱신 중",
};

/** 내 정보 화면: 내가 만든 항목을 Registry 상태별로 보여준다. 항목을 누르면 탐색 화면의 상세로 이동한다. */
export default function MyRecords({ onOpen, isAdmin = false }: { onOpen: (it: Item) => void; isAdmin?: boolean }) {
  const [data, setData] = useState<Awaited<ReturnType<typeof api.myRecords>> | null>(null);
  const [filter, setFilter] = useState<string>("ALL");
  const [scope, setScope] = useState<"mine" | "all">("mine");           // 관리자만 "전체" 를 고를 수 있다
  const [err, setErr] = useState("");

  useEffect(() => {
    setData(null); setErr(""); setFilter("ALL");
    (scope === "all" ? api.myRecordsAll() : api.myRecords()).then(setData).catch((e) => setErr(e.message));
  }, [scope]);

  if (err) return <p className="note error" role="alert">{err}</p>;
  if (!data) return <p className="sub">불러오는 중…</p>;

  const statuses = [...ORDER, ...Object.keys(data.counts).filter((s) => !(ORDER as readonly string[]).includes(s))]
    .filter((s) => data.counts[s]);
  const rows = data.items.filter((i) => filter === "ALL" || (i.registry_status ?? "NOT_REGISTERED") === filter);

  return (
    <section className="card" aria-labelledby="my-h">
      <div className="head"><h2 id="my-h">{scope === "all" ? "전체 레코드" : "내 레코드"} <span className="hint">{data.total}개</span></h2>
        {isAdmin && (
          <div className="seg" role="group" aria-label="보기 범위">
            <button type="button" aria-pressed={scope === "mine"} className={scope === "mine" ? "on" : ""} onClick={() => setScope("mine")}>내 것</button>
            <button type="button" aria-pressed={scope === "all"} className={scope === "all" ? "on" : ""} onClick={() => setScope("all")}>전체</button>
          </div>)}</div>
      {data.total === 0 ? (
        <p className="sub">{scope === "all" ? "등록된 항목이 없습니다." : "아직 만든 항목이 없습니다. \"레지스트리 탐색\"에서 Prompt·MCP·Skill·Agent 를 등록해 보세요."}</p>
      ) : (<>
        <div className="tabs" role="tablist" aria-label="상태 필터">
          <button role="tab" aria-selected={filter === "ALL"} className={`tab ${filter === "ALL" ? "on" : ""}`} onClick={() => setFilter("ALL")}>전체 {data.total}</button>
          {statuses.map((s) => (
            <button key={s} role="tab" aria-selected={filter === s} className={`tab ${filter === s ? "on" : ""}`} onClick={() => setFilter(s)}>
              {LABEL[s] ?? s} {data.counts[s]}</button>))}
        </div>
        <div className="tablewrap">
          <table>
            <thead><tr><th>종류</th><th>이름</th>{scope === "all" && <th>소유자</th>}<th>상태</th><th className="n">수정</th><th /></tr></thead>
            <tbody>{rows.map((i) => (
              <tr key={i.id}>
                <td>{KIND[i.kind]}</td>
                <td><strong>{i.name}</strong>{i.description && <div className="hint clip">{i.description}</div>}</td>
                {scope === "all" && <td className="mono sm">{i.owner}</td>}
                <td><span className={`badge st-${i.registry_status ?? ""}`}>{LABEL[i.registry_status ?? "NOT_REGISTERED"] ?? i.registry_status}</span></td>
                <td className="n">{ago(i.updated_at)}</td>
                <td><button type="button" className="link" onClick={() => onOpen(i)}>Open</button></td>
              </tr>))}</tbody>
          </table>
        </div>
      </>)}
    </section>
  );
}
