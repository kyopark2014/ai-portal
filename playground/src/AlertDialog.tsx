import { useEffect, useRef } from "react";

interface Props {
  title?: string;
  message: string;
  confirmLabel?: string;
  onClose: () => void;
}

/** 브라우저 alert() 대신 쓰는 앱 내 알림창. Esc/바깥 클릭/확인으로 닫는다. */
export function AlertDialog({ title = "알림", message, confirmLabel = "확인", onClose }: Props) {
  const ok = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    ok.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => {
      window.removeEventListener("keydown", onKey, true);
      prev?.focus();
    };
  }, [onClose]);

  return (
    <div className="pg-overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="pg-dialog" role="alertdialog" aria-modal="true" aria-labelledby="pg-alert-title" aria-describedby="pg-alert-body">
        <h2 id="pg-alert-title">{title}</h2>
        <p id="pg-alert-body">{message}</p>
        <div className="pg-dialog-actions">
          <button type="button" ref={ok} className="pg-dialog-ok" onClick={onClose}>{confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}
