import { useCallback, useEffect, useState } from "react";

export type Theme = "light" | "dark";
const KEY = "playground-theme";

function initial(): Theme {
  try { const v = localStorage.getItem(KEY); if (v === "light" || v === "dark") return v; } catch { /* */ }
  return matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

/** cde-pilot 처럼 data-theme 로 light/dark 를 바꾼다. 선택은 이 브라우저에만 기억한다. */
export function useTheme() {
  const [theme, setTheme] = useState<Theme>(initial);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem(KEY, theme); } catch { /* */ }
  }, [theme]);
  const toggle = useCallback(() => setTheme((t) => (t === "light" ? "dark" : "light")), []);
  return { theme, toggle };
}
