import { useEffect, useRef, useState } from "react";
import type { Task } from "./api";

interface Props { task: Task; active: boolean; onSelect: () => void; onDelete: () => void; onRename: (title: string) => void; onTogglePin: () => void }

/** cde-pilot 의 TaskListItem: 제목, 마우스를 올리면 휴지통(삭제 확인)과 ⋮ 메뉴(Rename · Pin). */
export function TaskListItem({ task, active, onSelect, onDelete, onRename, onTogglePin }: Props) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [value, setValue] = useState(task.title);
  const menuRef = useRef<HTMLDivElement>(null);
  const delRef = useRef<HTMLDivElement>(null);

  useEffect(() => setValue(task.title), [task.title]);
  useEffect(() => {
    if (!menuOpen && !confirmDelete) return;
    const down = (e: MouseEvent) => {
      if (menuOpen && menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false);
      if (confirmDelete && delRef.current && !delRef.current.contains(e.target as Node)) setConfirmDelete(false);
    };
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") { setMenuOpen(false); setConfirmDelete(false); } };
    document.addEventListener("mousedown", down); addEventListener("keydown", key);
    return () => { document.removeEventListener("mousedown", down); removeEventListener("keydown", key); };
  }, [menuOpen, confirmDelete]);

  function submitRename() {
    const t = value.trim();
    if (t && t !== task.title) onRename(t);
    setRenaming(false);
  }

  if (renaming) {
    return (
      <div className={`task-row ${active ? "active" : ""}`}>
        <input className="task-rename-input" value={value} autoFocus onChange={(e) => setValue(e.target.value)} onBlur={submitRename}
          onKeyDown={(e) => { if (e.key === "Enter") submitRename(); if (e.key === "Escape") { setRenaming(false); setValue(task.title); } }} />
      </div>);
  }
  return (
    <div className={`task-row ${active ? "active" : ""}`}>
      <button type="button" className="task-item" onClick={onSelect} title={task.title}>
        {task.pinned && (
          <svg className="task-pin-icon" viewBox="0 0 16 16" aria-hidden="true">
            <path fill="currentColor" d="M9.5 1.5 8 0 6.5 1.5v3L3 7v1h10V7L9.5 4.5v-3ZM4 9v5l4-2 4 2V9H4Z" /></svg>)}
        <span className="task-item-label">{task.title}</span>
      </button>
      <div className="task-actions">
        <div className="task-delete-wrap" ref={delRef}>
          <button type="button" className="task-action-btn" aria-label="Delete task" aria-expanded={confirmDelete}
            onClick={(e) => { e.stopPropagation(); setConfirmDelete((o) => !o); }}>
            <svg viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M5 2V1h6v1h4v2H1V2h4ZM3 6h1v8H2V6h1Zm3 0h1v8H6V6Zm4 0h1v8h-1V6ZM3 16h10V6H3v10Z" /></svg>
          </button>
          {confirmDelete && (
            <div className="task-delete-popover" role="dialog" aria-label="Delete task">
              <div className="task-delete-popover-actions">
                <button type="button" className="task-delete-cancel" onClick={() => setConfirmDelete(false)}>취소</button>
                <button type="button" className="task-delete-confirm-btn" onClick={() => { setConfirmDelete(false); onDelete(); }}>삭제</button>
              </div>
            </div>)}
        </div>
        <div className="task-menu-wrap" ref={menuRef}>
          <button type="button" className="task-action-btn" aria-label="Task options" onClick={(e) => { e.stopPropagation(); setMenuOpen((o) => !o); }}>
            <svg viewBox="0 0 16 16" aria-hidden="true"><circle fill="currentColor" cx="8" cy="3" r="1.5" /><circle fill="currentColor" cx="8" cy="8" r="1.5" /><circle fill="currentColor" cx="8" cy="13" r="1.5" /></svg>
          </button>
          {menuOpen && (
            <div className="task-menu" role="menu">
              <button type="button" role="menuitem" onClick={() => { setMenuOpen(false); setRenaming(true); setValue(task.title); }}>
                <svg viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="m11.7 2.3 1 1-8.4 8.4-1.3.3.3-1.3 8.4-8.4ZM12 1 15 4 5.5 13.5l-3 .8.8-3L12 1Z" /></svg>Rename</button>
              <button type="button" role="menuitem" onClick={() => { setMenuOpen(false); onTogglePin(); }}>
                <svg viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M9.5 1.5 8 0 6.5 1.5v3L3 7v1h10V7L9.5 4.5v-3ZM4 9v5l4-2 4 2V9H4Z" /></svg>{task.pinned ? "Unpin" : "Pin"}</button>
            </div>)}
        </div>
      </div>
    </div>);
}
