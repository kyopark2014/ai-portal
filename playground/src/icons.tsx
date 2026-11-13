// cde-pilot SidebarIcons 와 같은 16px 선 아이콘(이 화면에 쓰는 것만).
type P = { className?: string };
const base = { viewBox: "0 0 16 16", "aria-hidden": true, fill: "none", stroke: "currentColor", strokeWidth: 1.15, strokeLinecap: "round", strokeLinejoin: "round" } as const;

export const NewTaskIcon = ({ className }: P) => (
  <svg className={className} {...base}><path d="M3 2.5h5.5L12.5 6.5V12.5A1 1 0 0 1 11.5 13.5H3A1 1 0 0 1 2 12.5V3.5A1 1 0 0 1 3 2.5Z" /><path d="M8.5 2.5V6h3.5" /><path d="M12 2.5 13.5 4" /></svg>);
export const MenuIcon = ({ className }: P) => (
  <svg className={className} {...base}><path d="M2.5 4h11" /><path d="M2.5 8h11" /><path d="M2.5 12h11" /></svg>);
export const CloseIcon = ({ className }: P) => (
  <svg className={className} {...base}><path d="M4 4l8 8" /><path d="M12 4l-8 8" /></svg>);
export const LogoutIcon = ({ className }: P) => (
  <svg className={className} {...base}><path d="M6 2.5H3A1 1 0 0 0 2 3.5v9a1 1 0 0 0 1 1h3" /><path d="M10.5 11.5 14 8l-3.5-3.5" /><path d="M14 8H6" /></svg>);
export const AgentIcon = ({ className }: P) => (
  <svg className={className} {...base}><rect x="3" y="5" width="10" height="8" rx="1.5" /><path d="M8 2.5V5" /><circle cx="6" cy="9" r=".7" fill="currentColor" /><circle cx="10" cy="9" r=".7" fill="currentColor" /></svg>);
export const ThemeIcon = ({ className }: P) => (
  <svg className={className} {...base}><circle cx="8" cy="8" r="2.6" /><path d="M8 1.8v1.6M8 12.6v1.6M1.8 8h1.6M12.6 8h1.6M3.6 3.6l1.1 1.1M11.3 11.3l1.1 1.1M3.6 12.4l1.1-1.1M11.3 4.7l1.1-1.1" /></svg>);
