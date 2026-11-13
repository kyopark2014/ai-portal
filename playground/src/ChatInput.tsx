import { useEffect, useLayoutEffect, useRef, useState, type CompositionEvent, type FormEvent, type KeyboardEvent } from "react";

const MIN_H = 24, MAX_H = 160;

export interface QueuedMessage { id: string; text: string }

interface Props {
  disabled?: boolean; waiting?: boolean; onSend: (text: string) => void; onStop?: () => void; focusKey?: string;
  queuedMessages?: QueuedMessage[]; queuePaused?: boolean;
  onRemoveQueued?: (id: string) => void; onSteerQueued?: (id: string) => void; onResumeQueue?: () => void;
}

const STEER_ICON = (w: number) => (
  <svg width={w} height={w} viewBox="0 0 16 16" aria-hidden="true">
    <path d="M5 3.5 2.5 6 5 8.5M2.5 6H10a3.5 3.5 0 0 1 0 7H8" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
  </svg>);
const TRASH_ICON = (
  <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">
    <path d="M5.5 3.5h5M6.5 3.5V2.75A.75.75 0 0 1 7.25 2h1.5a.75.75 0 0 1 .75.75V3.5m2 0V13a1 1 0 0 1-1 1H5.5a1 1 0 0 1-1-1V3.5h8ZM7 6.5v5M9 6.5v5" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
  </svg>);

/**
 * cde-pilot 의 ChatInput 과 같은 입력창: 둥근 카드 안의 자동 높이 textarea + 오른쪽 아래 원형 전송/중지 버튼. 파일·이미지 첨부는 없다.
 * 응답 중에 보낸 메시지는 입력창 위의 대기열(queue)에 쌓인다. 항목마다 «이 메시지로 전환»(진행 중인 응답을 멈추고 바로 보냄)과 삭제가 있고,
 * 응답을 직접 중지하면 대기열은 멈추고 Resume 으로 이어서 보낸다.
 */
export function ChatInput({ disabled, waiting = false, onSend, onStop, focusKey, queuedMessages = [], queuePaused = false,
  onRemoveQueued, onSteerQueued, onResumeQueue }: Props) {
  const [value, setValue] = useState("");
  const ta = useRef<HTMLTextAreaElement>(null);
  const composing = useRef(false);
  const submitAfterComposition = useRef(false);

  useLayoutEffect(() => {
    const el = ta.current; if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(Math.max(el.scrollHeight, MIN_H), MAX_H)}px`;
  }, [value]);
  useEffect(() => { ta.current?.focus(); }, [focusKey]);

  function submit(override?: string) {
    const text = (override ?? value).trim();
    if (!text || disabled) return;
    onSend(text); setValue("");
  }
  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key !== "Enter" || e.shiftKey) return;
    // 한글/CJK IME: Enter 는 조합 확정이므로 조합 중에는 전송하지 않는다(keyCode 229 는 isComposing 을 주지 않는 브라우저용).
    if (e.nativeEvent.isComposing || e.keyCode === 229 || composing.current) { submitAfterComposition.current = true; return; }
    if (submitAfterComposition.current) { e.preventDefault(); return; }
    e.preventDefault(); submit();
  }
  function onCompositionEnd(e: CompositionEvent<HTMLTextAreaElement>) {
    composing.current = false;
    if (!submitAfterComposition.current) return;
    submitAfterComposition.current = false;
    submit(e.currentTarget.value);
  }
  const onSubmit = (e: FormEvent) => { e.preventDefault(); if (!composing.current && !submitAfterComposition.current) submit(); };

  // 응답 중에 쓴 내용이 있으면 왼쪽 버튼이 «대기열에 추가» 가 된다(cde-pilot 의 steer 버튼). 눌러도 진행 중인 응답은 멈추지 않는다.
  const canQueue = waiting && !disabled && value.trim().length > 0;
  return (
    <div className="chat-input-area">
      {queuedMessages.length > 0 && (
        <div className={`chat-queue-panel${queuePaused ? " is-paused" : ""}`} aria-label="대기 중인 메시지">
          {queuePaused && (
            <div className="chat-queue-header">
              <span className="chat-queue-paused-label">Queue paused because you interrupted</span>
              <button type="button" className="chat-queue-resume" onClick={() => onResumeQueue?.()}>Resume</button>
            </div>)}
          <ul className="chat-queue">
            {queuedMessages.map((item) => {
              const label = item.text.trim() || "메시지";
              return (
                <li key={item.id} className="chat-queue-item">
                  <span className="chat-queue-text" title={label}>{label}</span>
                  <div className="chat-queue-actions">
                    <button type="button" className="chat-queue-steer" title="진행 중인 응답을 멈추고 이 메시지로 전환"
                      aria-label={`이 메시지로 전환: ${label}`} onClick={() => onSteerQueued?.(item.id)}>{STEER_ICON(14)}</button>
                    <button type="button" className="chat-queue-remove" aria-label={`대기 메시지 삭제: ${label}`}
                      onClick={() => onRemoveQueued?.(item.id)}>{TRASH_ICON}</button>
                  </div>
                </li>);
            })}
          </ul>
        </div>)}
      <form className="chat-input-wrap" onSubmit={onSubmit}>
        <textarea ref={ta} className="chat-input" rows={1} placeholder="메시지를 입력하세요..." value={value} disabled={disabled}
          onChange={(e) => setValue(e.target.value)} onKeyDown={onKeyDown}
          onCompositionStart={() => { composing.current = true; }} onCompositionEnd={onCompositionEnd} />
        <div className="chat-input-toolbar">
          {canQueue && (
            <button type="button" className="chat-steer-btn" aria-label="진행 중인 응답을 멈추지 않고 대기열에 추가"
              title="진행 중인 응답을 멈추지 않고 대기열에 추가" onClick={() => submit()}>{STEER_ICON(14)}</button>)}
          {waiting ? (
            <button className="chat-send-btn is-waiting" type="button" aria-label="응답 중지" aria-busy="true" onClick={() => onStop?.()}>
              <span className="chat-send-progress" aria-hidden="true" /><span className="chat-send-stop" aria-hidden="true" />
            </button>
          ) : (
            <button className="chat-send-btn" type="submit" aria-label="전송" disabled={disabled || !value.trim()}>
              <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
                <path d="M8 12.5V3.5M4.5 7 8 3.5 11.5 7" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>)}
        </div>
      </form>
    </div>);
}
