import { useEffect, useRef } from "react";

export interface ConfirmSpec {
  title: string;
  lines: string[];                 // 본문 (한 줄씩 문단)
  confirmLabel: string;
  cancelLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
}

/** 브라우저 기본 confirm() 대신 쓰는 앱 내 확인창. Esc/바깥 클릭=취소, 기본 포커스는 안전한 쪽(취소). */
export default function ConfirmDialog({ spec, onClose }: { spec: ConfirmSpec; onClose: () => void }) {
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    cancel.current?.focus();
    const k = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); onClose(); } };
    window.addEventListener("keydown", k, true);        // 아래 모달의 Esc 보다 먼저 받는다
    return () => { window.removeEventListener("keydown", k, true); prev?.focus(); };
  }, [onClose]);
  return (
    <div className="overlay top" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal confirm" role="alertdialog" aria-modal="true" aria-labelledby="cf-t" aria-describedby="cf-d">
        <h2 id="cf-t">{spec.title}</h2>
        <div id="cf-d">{spec.lines.map((l, i) => <p key={i}>{l}</p>)}</div>
        <div className="actions">
          <button type="button" ref={cancel} className="btn ghost" onClick={onClose}>{spec.cancelLabel ?? "취소"}</button>
          <button type="button" className={`btn ${spec.danger ? "danger" : ""}`}
            onClick={() => { onClose(); spec.onConfirm(); }}>{spec.confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}
