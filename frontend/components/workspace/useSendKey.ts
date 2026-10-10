"use client";
import { useRef, type KeyboardEvent } from "react";

/** One physical Enter press is one intent; composition Enter only accepts the IME candidate. */
export function useSendKey(send: () => void) {
  const composing = useRef(false); const held = useRef(false);
  return {
    onCompositionStart: () => { composing.current = true; },
    onCompositionEnd: () => { composing.current = false; },
    onBlur: () => { held.current = false; composing.current = false; },
    onKeyUp: (e: KeyboardEvent<HTMLTextAreaElement>) => { if (e.key === "Enter") held.current = false; },
    onKeyDown: (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key !== "Enter" || e.shiftKey) return;
      if (composing.current || e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229 || e.keyCode === 229) return;
      e.preventDefault();
      if (e.repeat || held.current) return;
      held.current = true; send();
    },
  };
}
