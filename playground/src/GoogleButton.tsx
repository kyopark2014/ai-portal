import { useEffect, useRef } from "react";

interface Gsi {
  accounts: { id: {
    initialize(o: { client_id: string; callback: (r: { credential?: string }) => void }): void;
    renderButton(el: HTMLElement, o: Record<string, unknown>): void;
    disableAutoSelect(): void;
  } };
}
declare global { interface Window { google?: Gsi } }

const SRC = "https://accounts.google.com/gsi/client";

function loadGsi(): Promise<Gsi> {
  return new Promise((resolve, reject) => {
    if (window.google) return resolve(window.google);
    let s = document.querySelector<HTMLScriptElement>(`script[src="${SRC}"]`);
    if (!s) { s = document.createElement("script"); s.src = SRC; s.async = true; document.head.appendChild(s); }
    s.addEventListener("load", () => window.google ? resolve(window.google) : reject(new Error("gsi")));
    s.addEventListener("error", () => reject(new Error("gsi load failed")));
  });
}

export function disableAutoSelect() { window.google?.accounts.id.disableAutoSelect(); }

export default function GoogleButton({ clientId, onCredential, onError }: {
  clientId: string; onCredential: (c: string) => void; onError: (m: string) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let alive = true;
    loadGsi().then((g) => {
      if (!alive || !ref.current) return;
      g.accounts.id.initialize({ client_id: clientId, callback: (r) => r.credential && onCredential(r.credential) });
      g.accounts.id.renderButton(ref.current, { theme: "outline", size: "large", text: "signin_with", locale: "ko", width: 260 });
    }).catch(() => onError("Google 로그인 스크립트를 불러오지 못했습니다."));
    return () => { alive = false; };
  }, [clientId]); // eslint-disable-line react-hooks/exhaustive-deps
  return <div ref={ref} className="g-btn" aria-label="Google 로그인" />;
}
